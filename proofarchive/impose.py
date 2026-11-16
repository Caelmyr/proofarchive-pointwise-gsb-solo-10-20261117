import csv
import io
import json
import zipfile
from PIL import Image, ImageDraw
from . import validation as v
from .images import load, paper_rgb
from .store import now

MM = 25.4
MAX_SHEET_PIXELS = 30000000
MAX_TOTAL_PIXELS = 240000000
WARN_TOTAL_PIXELS = 80000000
MARK_GAP_MM = 2.
MARK_LENGTH_MM = 5.
MARK_WIDTH_MM = .25
BINDINGS = {'left': '左装订', 'right': '右装订'}
FLIPS = {'long': '长边翻转', 'short': '短边翻转'}
SIDES = {'front': '正面', 'back': '背面'}
POSITIONS = {'left': '左', 'right': '右'}


def parse_config(args, page_count):
    if page_count < 1:
        raise v.Invalid('档案暂无页面')
    first = v.integer(args.get('from', 1), '起始页', 1, page_count)
    last = v.integer(args.get('to', page_count), '结束页', 1, page_count)
    if last < first:
        raise v.Invalid('结束页不能小于起始页')
    config = {'from': first, 'to': last,
              'trim_w': v.number(args.get('trim_w'), '成品宽mm', 20, 1000),
              'trim_h': v.number(args.get('trim_h'), '成品高mm', 20, 1000),
              'paper_w': v.number(args.get('paper_w'), '纸张宽mm', 50, 1300),
              'paper_h': v.number(args.get('paper_h'), '纸张高mm', 50, 1300),
              'margin': v.number(args.get('margin', 10), '留白mm', 0, 100),
              'dpi': v.integer(args.get('dpi', 300), '输出分辨率', 72, 600),
              'binding': v.choice(args.get('binding', 'left'), '装订边', list(BINDINGS)),
              'flip': v.choice(args.get('flip', 'short'), '双面翻转', list(FLIPS)),
              'marks': str(args.get('marks', '1')) == '1'}
    need_w = 2 * config['trim_w'] + 2 * config['margin']
    need_h = config['trim_h'] + 2 * config['margin']
    if config['paper_w'] < need_w or config['paper_h'] < need_h:
        raise v.Invalid('纸张放不下：两页成品加留白至少需要 %.1f × %.1f mm，当前纸张 %.1f × %.1f mm'
                        % (need_w, need_h, config['paper_w'], config['paper_h']))
    return config


def slot_entry(number, sequence, upright):
    if number > len(sequence):
        return {'number': number, 'blank': True, 'page_id': None, 'page_name': '空白补页',
                'version_id': None, 'document_index': None, 'upright': upright}
    item = sequence[number - 1]
    return {'number': number, 'blank': False, 'page_id': item['page']['id'],
            'page_name': item['page']['name'], 'version_id': item['version']['id'],
            'document_index': item['index'], 'upright': upright}


def build_plan(store, document, config):
    selected = document['pages'][config['from'] - 1:config['to']]
    sequence = [{'page': page, 'version': store.version(page), 'index': config['from'] + i}
                for i, page in enumerate(selected)]
    count = len(sequence)
    blanks = (-count) % 4
    total = count + blanks
    sheet_count = total // 4
    dpi = config['dpi']
    sheet_w = round(config['paper_w'] / MM * dpi)
    sheet_h = round(config['paper_h'] / MM * dpi)
    if sheet_w * sheet_h > MAX_SHEET_PIXELS:
        raise v.Invalid('单张输出约 %.0f 万像素，超过 %d 万上限；请降低输出分辨率或纸张尺寸'
                        % (sheet_w * sheet_h / 10000, MAX_SHEET_PIXELS // 10000))
    total_pixels = sheet_w * sheet_h * sheet_count * 2
    if total_pixels > MAX_TOTAL_PIXELS:
        raise v.Invalid('全部正反面合计约 %.0f 万像素，资源开销过大；请降低分辨率、纸张或缩小页范围'
                        % (total_pixels / 10000))
    warnings = []
    if total_pixels > WARN_TOTAL_PIXELS:
        warnings.append('输出合计约 %.0f 万像素（原始RGB约 %.0f MiB），生成与下载可能较慢'
                        % (total_pixels / 10000, total_pixels * 3 / 1048576))
    trim_ratio = config['trim_w'] / config['trim_h']
    for item in sequence:
        version = item['version']
        declared = version['trim_mm']
        if abs(declared[0] / declared[1] - trim_ratio) > .02:
            warnings.append('第%d页「%s」声明成品 %.1f×%.1f mm 与拼版成品比例不一致'
                            % (item['index'], item['page']['name'], declared[0], declared[1]))
        pixels = version['pixels']
        if abs(pixels[0] / pixels[1] - trim_ratio) > .02:
            warnings.append('第%d页「%s」像素比例与成品不一致，将居中放置并露出白边'
                            % (item['index'], item['page']['name']))
    if blanks:
        warnings.append('所选 %d 页不是4的倍数，末尾补 %d 个空白页；空白仅存在于拼版输出，不写入档案'
                        % (count, blanks))
    if config['marks']:
        blank_vertical = (config['paper_h'] - config['trim_h']) / 2
        blank_horizontal = (config['paper_w'] - 2 * config['trim_w']) / 2
        if min(blank_vertical, blank_horizontal) < MARK_GAP_MM + MARK_LENGTH_MM:
            warnings.append('成品外留白不足 %.0f mm，部分裁切标记将缩短或省略；标记始终画在成品之外'
                            % (MARK_GAP_MM + MARK_LENGTH_MM))
    axis_vertical = (config['flip'] == 'long') == (config['paper_h'] > config['paper_w'])
    back_rotation = 0 if axis_vertical else 180
    digits = max(2, len(str(sheet_count)))
    sheets = []
    for i in range(sheet_count):
        pairs = {'front': [total - 2 * i, 2 * i + 1], 'back': [2 * i + 2, total - 2 * i - 1]}
        if config['binding'] == 'right':
            pairs = {side: [numbers[1], numbers[0]] for side, numbers in pairs.items()}
        sides = {}
        for side in ('front', 'back'):
            rotation = back_rotation if side == 'back' else 0
            numbers = pairs[side]
            if rotation:
                numbers = [numbers[1], numbers[0]]
            sides[side] = {'file': 'sheet-%0*d-%s.png' % (digits, i + 1, side),
                           'file_rotation': rotation,
                           'slots': [slot_entry(numbers[0], sequence, rotation == 0),
                                     slot_entry(numbers[1], sequence, rotation == 0)]}
            sides[side]['slots'][0]['position'] = 'left'
            sides[side]['slots'][1]['position'] = 'right'
        sheets.append({'index': i + 1, 'sides': sides})
    orientation = '横向' if config['paper_w'] > config['paper_h'] else (
        '纵向' if config['paper_w'] < config['paper_h'] else '方形')
    hint = ('纸张%s，双面打印请选择「%s」，折叠后全部页面正向'
            % (orientation, FLIPS[config['flip']]))
    hint += '；背面文件内容已整体旋转180°' if back_rotation else '；背面文件无需旋转'
    return {'document_id': document['id'], 'revision': document['revision'],
            'config': config | {'binding_label': BINDINGS[config['binding']],
                                'flip_label': FLIPS[config['flip']]},
            'range': {'from': config['from'], 'to': config['to'], 'selected': count},
            'padding_blanks': blanks, 'total_pages': total, 'sheet_count': sheet_count,
            'sheet_pixels': [sheet_w, sheet_h], 'sheet_mm': [config['paper_w'], config['paper_h']],
            'trim_mm': [config['trim_w'], config['trim_h']], 'spread_mm': [2 * config['trim_w'], config['trim_h']],
            'margin_mm': config['margin'], 'dpi': dpi,
            'estimated_pixels': total_pixels,
            'estimated_raw_mb': round(total_pixels * 3 / 1048576, 1),
            'duplex_hint': hint, 'warnings': warnings, 'sheets': sheets}


def draw_marks(output, ox, oy, trim_w, trim_h, dpi):
    draw = ImageDraw.Draw(output)
    gap = round(MARK_GAP_MM / MM * dpi)
    length = round(MARK_LENGTH_MM / MM * dpi)
    width = max(1, round(MARK_WIDTH_MM / MM * dpi))
    sheet_w, sheet_h = output.size
    for x in (ox, ox + trim_w, ox + 2 * trim_w):
        for edge, direction in ((oy, -1), (oy + trim_h, 1)):
            y0 = max(0, min(sheet_h - 1, edge + direction * (gap + length)))
            y1 = max(0, min(sheet_h - 1, edge + direction * gap))
            draw.line([(x, min(y0, y1)), (x, max(y0, y1))], fill='black', width=width)
    for y in (oy, oy + trim_h):
        for edge, direction in ((ox, -1), (ox + 2 * trim_w, 1)):
            x0 = max(0, min(sheet_w - 1, edge + direction * (gap + length)))
            x1 = max(0, min(sheet_w - 1, edge + direction * gap))
            draw.line([(min(x0, x1), y), (max(x0, x1), y)], fill='black', width=width)


def encode_sheet(image, dpi):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', dpi=(dpi, dpi))
    return buffer.getvalue()


def render_sheet(store, document, config, plan, sheet_index, side):
    info = plan['sheets'][sheet_index - 1]['sides'][side]
    dpi = config['dpi']
    output = Image.new('RGB', plan['sheet_pixels'], 'white')
    trim_w = round(config['trim_w'] / MM * dpi)
    trim_h = round(config['trim_h'] / MM * dpi)
    ox = (plan['sheet_pixels'][0] - 2 * trim_w) // 2
    oy = (plan['sheet_pixels'][1] - trim_h) // 2
    for slot in info['slots']:
        if slot['blank']:
            continue
        page = store.page(document, slot['page_id'])
        image = paper_rgb(load(store, store.version(page)))
        ratio = min(trim_w / image.width, trim_h / image.height)
        size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        fitted = image.resize(size, Image.Resampling.LANCZOS)
        if not slot['upright']:
            fitted = fitted.rotate(180)
        x = ox + (0 if slot['position'] == 'left' else trim_w) + (trim_w - size[0]) // 2
        y = oy + (trim_h - size[1]) // 2
        output.paste(fitted, (x, y))
    if config['marks']:
        draw_marks(output, ox, oy, trim_w, trim_h, dpi)
    return output


def checklist_csv(plan):
    output = io.StringIO()
    writer = csv.writer(output)
    config = plan['config']
    writer.writerow(['纸张', '面', '文件', '位置', '书页码', '空白补页', '档案页序', '页面名', '版次',
                     '文件中正向', '纸宽mm', '纸高mm', '图像宽px', '图像高px', '输出DPI',
                     '成品宽mm', '成品高mm', '装订', '双面翻转'])
    for sheet in plan['sheets']:
        for side in ('front', 'back'):
            info = sheet['sides'][side]
            for slot in info['slots']:
                writer.writerow([sheet['index'], SIDES[side], info['file'], POSITIONS[slot['position']],
                                 slot['number'], '是' if slot['blank'] else '否',
                                 slot['document_index'] or '', slot['page_name'], slot['version_id'] or '',
                                 '是' if slot['upright'] else '否(旋转180°)',
                                 plan['sheet_mm'][0], plan['sheet_mm'][1],
                                 plan['sheet_pixels'][0], plan['sheet_pixels'][1], plan['dpi'],
                                 plan['trim_mm'][0], plan['trim_mm'][1],
                                 config['binding_label'], config['flip_label']])
    return output.getvalue().encode('utf-8-sig')


def impose_zip(store, document, config):
    plan = build_plan(store, document, config)
    manifest = {'format': 'proofarchive-impose-1', 'generated_at': now(),
                'document': {'id': document['id'], 'name': document['name'],
                             'revision': document['revision']},
                'plan': plan}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for sheet in plan['sheets']:
            for side in ('front', 'back'):
                image = render_sheet(store, document, config, plan, sheet['index'], side)
                archive.writestr(sheet['sides'][side]['file'], encode_sheet(image, config['dpi']))
        archive.writestr('impose-manifest.json',
                         json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2))
        archive.writestr('impose-checklist.csv', checklist_csv(plan))
    return output.getvalue()

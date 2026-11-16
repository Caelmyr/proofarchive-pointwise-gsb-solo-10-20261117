"""分张校样打印（tiled proof）。

把一个不可变版次按其声明的成品物理尺寸铺到选定纸张的可打印区域，
生成带行列编号、接缝重叠标记和物理坐标标尺的分张图，以及可按行列拼回
原版的清单。本模块只读取版次像素，不创建或修改任何版次/档案数据。
"""
import csv
import hashlib
import io
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import validation as v
from .images import load, paper_rgb, encode

MM_PER_INCH = 25.4
FONT_PATH = Path(__file__).resolve().parents[1] / 'static' / 'archive-font.ttf'
# Droid Sans Fallback 仅含 CJK 字形，拉丁/数字走 DejaVu；系统缺字时退回 PIL 默认字体
FALLBACK_FONT_PATHS = ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                       '/usr/share/fonts/dejavu/DejaVuSans.ttf')

# 常用办公纸张（短边, 长边），单位 mm
PAPERS = {
    'A5': (148.0, 210.0),
    'A4': (210.0, 297.0),
    'A3': (297.0, 420.0),
    'Letter': (215.9, 279.4),
    'Legal': (215.9, 355.6),
    'Tabloid': (279.4, 431.8),
}
ORIENTATIONS = ('auto', 'portrait', 'landscape')

# 容量护栏（同步小型本地工具：CPU 2 核 / 内存 2 GiB）
MAX_SHEETS = 48
MAX_TILE_PIXELS = 24_000_000
MAX_BOARD_PIXELS = 120_000_000
NARROW_FRACTION = 0.5          # 末张窄条阈值：小于有效覆盖宽度的一半
OVERLAP_WARN_FRACTION = 0.5    # 重叠超过有效覆盖一半给出提示
SMALL_MARGIN_MM = 3.0          # 小于此留白，文字标记移入重叠带/空白区
RULER_MARGIN_MM = 5.0          # 至少此留白才绘制物理标尺

# 接缝与标记配色（标记只落在纸张留白、重叠带或末尾空白，不遮挡成品内容）
SEAM_X = (194, 38, 124, 34)     # 竖向接缝（列间）品红
SEAM_Y = (15, 125, 140, 34)     # 横向接缝（行间）青色
LINE_X = (180, 24, 110, 255)
LINE_Y = (11, 106, 120, 255)
TRIM = (16, 46, 67, 255)        # 成品裁切线，深蓝
FRAME = (91, 107, 123, 255)     # 可打印区域框线
PAGE_COLORS = ['#165e83', '#ce6c48', '#468575', '#8a5a9e', '#b07d2b', '#3d6fb4']

class ProofFont:
    """按字形回退的字体链：拉丁/数字用 DejaVu，CJK 用内置 Droid Sans Fallback。

    对外暴露与 ImageFont 兼容的 getbbox/getlength/textlength，draw_text 逐字
    选择首个含该字形的字体，保证分张标记不出现豆腐块。
    """

    def __init__(self, size):
        self.size = size
        self.faces = []
        for path in FALLBACK_FONT_PATHS:
            if Path(path).exists():
                self.faces.append(ImageFont.truetype(path, size))
                break
        if FONT_PATH.exists():
            self.faces.append(ImageFont.truetype(str(FONT_PATH), size))
        if not self.faces:
            self.faces.append(ImageFont.load_default())
        self._missing = {id(face): self._missing_glyphs(face) for face in self.faces}

    @staticmethod
    def _missing_glyphs(face):
        box = face.getbbox('\x00')
        return box if box is not None else None

    def face_for(self, ch):
        missing_box = None
        for face in self.faces:
            box = face.getbbox(ch)
            if box is None:
                continue
            if missing_box is None:
                missing_box = self._missing[id(face)]
            if missing_box is not None and box == missing_box:
                continue
            return face
        return self.faces[0]

    def draw_text(self, draw, xy, text, fill, anchor=None):
        x, y = xy
        for ch in text:
            face = self.face_for(ch)
            if anchor == 'ra':
                width = face.getlength(ch)
                draw.text((x - width, y), ch, font=face, fill=fill)
                x -= width
            else:
                draw.text((x, y), ch, font=face, fill=fill)
                x += face.getlength(ch)
        return x

    def rotated_text(self, text, fill, angle):
        widths = []
        height = self.size + 6
        for ch in text:
            widths.append(max(1, int(self.face_for(ch).getlength(ch))))
        layer = Image.new('RGBA', (sum(widths) + 6, height + 6), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        x = 3
        for ch, width in zip(text, widths):
            draw.text((x, 3), ch, font=self.face_for(ch), fill=fill)
            x += width
        return layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)

    def getbbox(self, text):
        left = min((self.face_for(ch).getbbox(ch) or (0, 0, 0, 0))[0] for ch in text)
        top = min((self.face_for(ch).getbbox(ch) or (0, 0, 0, 0))[1] for ch in text)
        right = sum((self.face_for(ch).getbbox(ch) or (0, 0, 0, 0))[2] for ch in text)
        bottom = max((self.face_for(ch).getbbox(ch) or (0, 0, 0, 0))[3] for ch in text)
        return left, top, right, bottom

    def getlength(self, text):
        return sum(self.face_for(ch).getlength(ch) for ch in text)


_font_cache = {}


def font(size):
    size = max(8, int(size))
    if size not in _font_cache:
        _font_cache[size] = ProofFont(size)
    return _font_cache[size]


def half_up(value):
    return int(math.floor(value + 0.5))


def parse_args(args):
    """从 Flask 查询参数读取并校验分张设置，返回纯数据字典。"""
    paper = v.choice(args.get('paper', 'A4'), '纸张', list(PAPERS) + ['custom'])
    if paper == 'custom':
        short = v.number(args.get('paper_w'), '自定义纸张宽 mm', 40, 1200)
        long = v.number(args.get('paper_h'), '自定义纸张高 mm', 40, 1200)
        paper_w, paper_h = min(short, long), max(short, long)
    else:
        paper_w, paper_h = PAPERS[paper]
    orientation = v.choice(args.get('orientation', 'auto'), '纸张方向', ORIENTATIONS)
    margin = v.number(args.get('margin', 10), '四边留白 mm', 0, 120)
    overlap = v.number(args.get('overlap', 8), '相邻纸张重叠 mm', 0, 80)
    dpi = v.integer(args.get('dpi', 150), '打印分辨率 DPI', 72, 300)
    return {'paper': paper, 'paper_w': paper_w, 'paper_h': paper_h, 'orientation': orientation,
            'margin': margin, 'overlap': overlap, 'dpi': dpi}


def resolve_orientation(settings, board_w, board_h):
    if settings['orientation'] != 'auto':
        return settings['orientation']
    return 'portrait' if board_h >= board_w else 'landscape'


def build_plan(version, image_size, settings):
    """计算分张几何，不做重活渲染。返回含 warnings 的计划字典；硬约束直接抛 Invalid。"""
    board_w, board_h = (v.number(x, '成品尺寸 mm', 1, 5000) for x in version['trim_mm'])
    margin, overlap, dpi = settings['margin'], settings['overlap'], settings['dpi']
    ppm = dpi / MM_PER_INCH
    orientation = resolve_orientation(settings, board_w, board_h)
    if orientation == 'portrait':
        sheet_w, sheet_h = settings['paper_w'], settings['paper_h']
    else:
        sheet_w, sheet_h = settings['paper_h'], settings['paper_w']
    eff_w, eff_h = sheet_w - 2 * margin, sheet_h - 2 * margin
    if eff_w <= 1 or eff_h <= 1:
        raise v.Invalid('纸张放不下有效内容：四边留白已占满纸张，请减小留白或换大纸')
    if overlap >= min(eff_w, eff_h):
        raise v.Invalid('重叠宽度过大：重叠必须小于纸张可打印区域的短边')

    def spans(length, effective):
        if length <= effective:
            return 1
        step = effective - overlap
        if step <= 0:
            raise v.Invalid('重叠宽度过大，无法沿此方向推进分张')
        return 1 + math.ceil((length - effective) / step)

    cols = spans(board_w, eff_w)
    rows = spans(board_h, eff_h)

    sheet_px = (half_up(sheet_w * ppm), half_up(sheet_h * ppm))
    board_px = (half_up(board_w * ppm), half_up(board_h * ppm))
    if sheet_px[0] * sheet_px[1] > MAX_TILE_PIXELS:
        raise v.Invalid('单张像素过多，请降低 DPI 或换更小的纸张')
    if board_px[0] * board_px[1] > MAX_BOARD_PIXELS:
        raise v.Invalid('整张版面像素过多，请降低 DPI 后再生成')
    if cols * rows > MAX_SHEETS:
        raise v.Invalid(f'分张数量 {cols * rows} 超过 {MAX_SHEETS} 张上限，请换大纸、降低 DPI 或减小重叠')

    def bounds(length, effective, count):
        result = []
        for index in range(count):
            start = 0 if index == 0 else half_up(index * (effective - overlap))
            end = min(start + effective, length)
            result.append((start, end))
        return result

    xs = bounds(board_w, eff_w, cols)
    ys = bounds(board_h, eff_h, rows)

    src_w, src_h = image_size
    src_dpi = (src_w / board_w * MM_PER_INCH, src_h / board_h * MM_PER_INCH)

    warnings = []
    if min(src_dpi) < dpi:
        warnings.append({'code': 'upscale', 'message':
            f'版次有效分辨率约 {min(src_dpi):.0f} DPI，低于打印 {dpi} DPI，输出会放大，细节可能发虚'})
    if margin < SMALL_MARGIN_MM:
        warnings.append({'code': 'small_margin', 'message':
            '四边留白小于 3mm，页号文字将移入重叠带或空白区，建议加大留白以便打印裁切'})
    if overlap > min(eff_w, eff_h) * OVERLAP_WARN_FRACTION:
        warnings.append({'code': 'overlap_large', 'message':
            '重叠超过可打印区域的一半，用纸效率偏低，建议减小重叠宽度'})
    last_w = xs[-1][1] - xs[-1][0]
    last_h = ys[-1][1] - ys[-1][0]
    narrow_x = cols > 1 and last_w < eff_w * NARROW_FRACTION
    narrow_y = rows > 1 and last_h < eff_h * NARROW_FRACTION
    if narrow_x or narrow_y:
        warnings.append({'code': 'narrow_strip', 'message':
            f'末列/末行只剩窄条（末列宽 {last_w:.1f}mm、末行高 {last_h:.1f}mm）；窄条保持原比例不拉伸，可调整重叠或纸张改善用纸'})

    tiles = []
    for r in range(rows):
        for c in range(cols):
            x0, x1 = xs[c]
            y0, y1 = ys[r]
            page_no = r * cols + c + 1
            tiles.append({
                'row': r + 1, 'col': c + 1, 'page_no': page_no,
                'board_coverage_mm': [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
                'content_used_mm': [round(x1 - x0, 3), round(y1 - y0, 3)],
                'narrow': (narrow_x and c == cols - 1) or (narrow_y and r == rows - 1),
                'seams_mm': {
                    'left': 0.0 if c > 0 else None,
                    'right': overlap if c < cols - 1 else None,
                    'top': 0.0 if r > 0 else None,
                    'bottom': overlap if r < rows - 1 else None,
                },
                'file': f'tile-R{r + 1:02d}-C{c + 1:02d}.png',
            })

    plan = {
        'format': 'proofarchive-tiling-1',
        'paper': settings['paper'], 'paper_mm': [sheet_w, sheet_h],
        'orientation': orientation, 'margin_mm': margin, 'overlap_mm': overlap,
        'print_dpi': dpi, 'px_per_mm': round(ppm, 6),
        'printable_mm': [round(eff_w, 3), round(eff_h, 3)],
        'board_mm': [round(board_w, 3), round(board_h, 3)],
        'board_pixels': board_px, 'sheet_pixels': sheet_px,
        'cols': cols, 'rows': rows, 'sheet_count': cols * rows,
        'source_effective_dpi': [round(src_dpi[0], 2), round(src_dpi[1], 2)],
        'content_box_px': [half_up(margin * ppm), half_up(margin * ppm),
                           half_up((margin + eff_w) * ppm), half_up((margin + eff_h) * ppm)],
        'tiles': tiles, 'warnings': warnings,
        'note': '分张校样为临时打印输出，不生成新版次，不修改原始像素；标记只位于留白、重叠带或末尾空白。',
    }
    return plan


# ---------------------------------------------------------------- 标记绘制

def _dashed_line(draw, start, end, fill, width, dash=9, gap=6):
    vertical = start[0] == end[0]
    if vertical:
        y = start[1]
        while y < end[1]:
            draw.line([(start[0], y), (end[0], min(y + dash, end[1]))], fill=fill, width=width)
            y += dash + gap
    else:
        x = start[0]
        while x < end[0]:
            draw.line([(x, start[1]), (min(x + dash, end[0]), end[1])], fill=fill, width=width)
            x += dash + gap


def _cross(draw, x, y, arm, fill, width):
    draw.line([(x - arm, y), (x + arm, y)], fill=fill, width=width)
    draw.line([(x, y - arm), (x, y + arm)], fill=fill, width=width)
    draw.rectangle([x - width, y - width, x + width, y + width], fill=fill)


def _rotated_text(overlay, xy, text, fnt, fill, angle):
    overlay.alpha_composite(fnt.rotated_text(text, fill, angle), xy)


def _ruler(draw, origin, length_mm, ppm, vertical, margin_mm):
    """在留白区画物理坐标尺，刻度值等于本张覆盖区起始的版面绝对坐标 mm。"""
    ox, oy = origin
    span_px = half_up(length_mm * ppm)
    width = max(1, round(0.25 * ppm))
    fnt = font(max(8, round(0.9 * ppm)))
    if not vertical:
        y = oy + round((margin_mm - 1.2) * ppm)
        draw.line([(ox, y), (ox + span_px, y)], fill=TRIM, width=width)
        k = 0
        while k <= math.floor(length_mm):
            x = ox + round(k * ppm)
            tick = round(1.6 * ppm) if k % 10 == 0 else (round(1.1 * ppm) if k % 5 == 0 else round(0.6 * ppm))
            draw.line([(x, y), (x, y - tick)], fill=TRIM, width=width)
            if k % 10 == 0:
                fnt.draw_text(draw, (x + 2, y + 2), str(k), TRIM)
            k += 1
    else:
        x = ox - round((margin_mm - 1.2) * ppm)
        draw.line([(x, oy), (x, oy + span_px)], fill=TRIM, width=width)
        k = 0
        while k <= math.floor(length_mm):
            y = oy + round(k * ppm)
            tick = round(1.6 * ppm) if k % 10 == 0 else (round(1.1 * ppm) if k % 5 == 0 else round(0.6 * ppm))
            draw.line([(x, y), (x - tick, y)], fill=TRIM, width=width)
            if k % 10 == 0:
                fnt.draw_text(draw, (x - tick - 2, y + 2), str(k), TRIM, anchor='ra')
            k += 1


def render_tile(store, page, version, plan, tile, document_name=''):
    """渲染单张分张图：同一物理比例，标记不遮挡成品内容。"""
    settings_dpi = plan['print_dpi']
    ppm = plan['px_per_mm']
    sheet_w, sheet_h = plan['sheet_pixels']
    ox, oy, box_r, box_b = plan['content_box_px']
    cw, ch = box_r - ox, box_b - oy
    margin = plan['margin_mm']
    overlap = plan['overlap_mm']

    board = paper_rgb(load(store, version)).resize(plan['board_pixels'], Image.Resampling.LANCZOS)
    x0, y0, x1, y1 = tile['board_coverage_mm']
    tile_image = Image.new('RGB', (sheet_w, sheet_h), 'white')
    crop = board.crop((half_up(x0 * ppm), half_up(y0 * ppm), half_up(x1 * ppm), half_up(y1 * ppm)))
    tile_image.paste(crop, (ox, oy))

    overlay = Image.new('RGBA', (sheet_w, sheet_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line_w = max(1, round(0.3 * ppm))
    r, c = tile['row'], tile['col']
    cols, rows = plan['cols'], plan['rows']

    # 1) 可打印区域细框（贴边界，落在留白一侧）
    draw.rectangle([ox - 1, oy - 1, ox + cw, oy + ch], outline=FRAME, width=1)

    # 2) 成品外边缘的 L 形裁切角线（深蓝），只画在版面真实边界
    tick = max(3, round(2 * ppm))
    at_left, at_right = c == 1, c == cols
    at_top, at_bottom = r == 1, r == rows
    for corner, h_dir, v_dir, x, y in (
        ('tl', at_left, at_top, ox, oy), ('tr', at_right, at_top, ox + cw, oy),
        ('bl', at_left, at_bottom, ox, oy + ch), ('br', at_right, at_bottom, ox + cw, oy + ch)):
        if h_dir:
            direction = 1 if corner in ('tl', 'bl') else -1
            draw.line([(x, y), (x + direction * tick, y)], fill=TRIM, width=line_w)
        if v_dir:
            direction = 1 if corner in ('tl', 'tr') else -1
            draw.line([(x, y), (x, y + direction * tick)], fill=TRIM, width=line_w)

    # 3) 接缝：重叠带浅色铺底、虚线对位线、十字配准标记
    if c < cols:  # 右侧（列间）接缝
        if overlap > 0:
            band_x0 = ox + round((x1 - overlap - x0) * ppm)
            draw.rectangle([band_x0, oy, ox + cw, oy + ch], fill=SEAM_X)
            line_x = band_x0
            center_x = ox + round((x1 - overlap / 2 - x0) * ppm)
        else:
            line_x, center_x = ox + cw, ox + cw - round(2 * ppm)
        _dashed_line(draw, (line_x, oy), (line_x, oy + ch), LINE_X, line_w)
        for frac in (.25, .5, .75):
            _cross(draw, center_x, oy + round(ch * frac), round(1.6 * ppm), LINE_X, line_w)
        if overlap * ppm >= 4 * ppm:
            fnt = font(max(8, round(1.0 * ppm)))
            _rotated_text(overlay, (center_x - 8, oy + round(ch * .55)),
                          f'列接缝 重叠{overlap:g}mm 沿虚线对齐', fnt, LINE_X, 90)
    if c > 1:  # 左侧重叠带
        if overlap > 0:
            band_x1 = ox + round(overlap * ppm)
            draw.rectangle([ox, oy, band_x1, oy + ch], fill=SEAM_X)
            line_x = band_x1
            center_x = ox + round(overlap / 2 * ppm)
        else:
            line_x, center_x = ox, ox + round(2 * ppm)
        _dashed_line(draw, (line_x, oy), (line_x, oy + ch), LINE_X, line_w)
        for frac in (.25, .5, .75):
            _cross(draw, center_x, oy + round(ch * frac), round(1.6 * ppm), LINE_X, line_w)
    if r < rows:  # 下侧（行间）接缝
        if overlap > 0:
            band_y0 = oy + round((y1 - overlap - y0) * ppm)
            draw.rectangle([ox, band_y0, ox + cw, oy + ch], fill=SEAM_Y)
            line_y, center_y = band_y0, oy + round((y1 - overlap / 2 - y0) * ppm)
        else:
            line_y, center_y = oy + ch, oy + ch - round(2 * ppm)
        _dashed_line(draw, (ox, line_y), (ox + cw, line_y), LINE_Y, line_w)
        for frac in (.25, .5, .75):
            _cross(draw, ox + round(cw * frac), center_y, round(1.6 * ppm), LINE_Y, line_w)
        if overlap * ppm >= 4 * ppm:
            fnt = font(max(8, round(1.0 * ppm)))
            fnt.draw_text(draw, (ox + round(cw * .55), center_y - 8),
                          f'行接缝 重叠{overlap:g}mm 沿虚线对齐', LINE_Y)
    if r > 1:  # 上侧重叠带
        if overlap > 0:
            band_y1 = oy + round(overlap * ppm)
            draw.rectangle([ox, oy, ox + cw, band_y1], fill=SEAM_Y)
            line_y, center_y = band_y1, oy + round(overlap / 2 * ppm)
        else:
            line_y, center_y = oy, oy + round(2 * ppm)
        _dashed_line(draw, (ox, line_y), (ox + cw, line_y), LINE_Y, line_w)
        for frac in (.25, .5, .75):
            _cross(draw, ox + round(cw * frac), center_y, round(1.6 * ppm), LINE_Y, line_w)

    # 4) 页号与说明文字：优先放留白；留白不足时移入重叠带/末尾空白
    page_color = PAGE_COLORS[(tile['page_no'] - 1) % len(PAGE_COLORS)]
    head_fs = max(8, min(round(1.15 * ppm), round(margin * ppm * 0.42))) if margin >= 2 else round(0.9 * ppm)
    head = font(head_fs)
    title = f"R{r:02d}C{c:02d} · 第{tile['page_no']}/{plan['sheet_count']}张 · {document_name[:24]}"
    scale = f"1:1 @ {settings_dpi}DPI · 板面 X{x0:g}–{x1:g}mm Y{y0:g}–{y1:g}mm"
    if margin >= SMALL_MARGIN_MM:
        chip = max(4, round(2.2 * ppm))
        draw.rectangle([ox, max(1, oy - round(margin * ppm) + 2), ox + chip,
                        max(1, oy - round(margin * ppm) + 2) + chip], fill=page_color)
        head.draw_text(draw, (ox + chip + 4, max(0, oy - round(margin * ppm))), title, TRIM)
        foot_fs = max(8, min(round(0.95 * ppm), round(margin * ppm * 0.36)))
        font(foot_fs).draw_text(draw, (ox, oy + ch + round(0.8 * ppm)), scale, FRAME)
    else:
        # 留白太窄：写到接缝重叠带或末尾空白，加白底；仍尽量避开成品独有的内容区
        if c > 1 and overlap >= 4:
            anchor_x = ox + round(overlap / 2 * ppm)
        elif r > 1 and overlap >= 4:
            anchor_x = ox + 4
        else:
            anchor_x = ox + 4                      # 首张无重叠带，仅小留白极端情形
        anchor_y = oy + (round(overlap / 2 * ppm) if r > 1 and overlap >= 4 else 4)
        draw.rectangle([anchor_x - 2, anchor_y - 2, anchor_x + half_up(42 * ppm),
                        anchor_y + head_fs * 2 + 4], fill=(255, 255, 255, 210))
        head.draw_text(draw, (anchor_x, anchor_y), f"R{r:02d}C{c:02d} #{tile['page_no']}", TRIM)
        font(max(8, head_fs - 2)).draw_text(draw, (anchor_x, anchor_y + head_fs + 2), scale, FRAME)

    # 5) 留白足够时绘制物理坐标标尺（完全在留白内）
    if margin >= RULER_MARGIN_MM:
        _ruler(draw, (ox, oy), x1 - x0, ppm, False, margin)
        _ruler(draw, (ox, oy), y1 - y0, ppm, True, margin)

    tile_image = Image.alpha_composite(tile_image.convert('RGBA'), overlay).convert('RGB')
    return tile_image


# ---------------------------------------------------------------- 总览预览

def render_overview(plan):
    """整张覆盖关系预览：纸张网格、重叠带、行列编号与窄条提示。"""
    cols, rows = plan['cols'], plan['rows']
    margin = plan['margin_mm']
    eff_w, eff_h = plan['printable_mm']
    sheet_w, sheet_h = plan['paper_mm']
    board_w, board_h = plan['board_mm']
    max_w = 1280.0
    scale = min(max_w / (cols * sheet_w + 60), 3.2)
    pad = 30
    canvas_w = half_up(cols * sheet_w * scale) + pad * 2
    header_h = 78
    canvas_h = half_up(rows * sheet_h * scale) + pad * 2 + header_h
    image = Image.new('RGB', (canvas_w, canvas_h), '#f4f6f8')
    draw = ImageDraw.Draw(image)
    title_fs, label_fs, small_fs = 19, 13, 11
    font(title_fs).draw_text(draw, (pad, 14),
                             f"分张覆盖预览 · {plan['sheet_count']} 张（{rows} 行 × {cols} 列）"
                             f" · 成品 {board_w:g}×{board_h:g} mm · {plan['print_dpi']} DPI", TRIM)
    font(small_fs).draw_text(draw, (pad, 42),
                             f"纸张 {plan['paper']} {sheet_w:g}×{sheet_h:g}mm / {plan['orientation']}"
                             f" · 留白 {margin:g}mm · 重叠 {plan['overlap_mm']:g}mm"
                             f" · 可打印 {eff_w:g}×{eff_h:g}mm", FRAME)
    for tile in plan['tiles']:
        r, c = tile['row'], tile['col']
        x0, y0, x1, y1 = tile['board_coverage_mm']
        sx = pad + (c - 1) * sheet_w * scale
        sy = header_h + pad + (r - 1) * sheet_h * scale
        draw.rectangle([sx, sy, sx + sheet_w * scale, sy + sheet_h * scale],
                       fill='white', outline='#9fb0bf', width=1)
        bx, by = sx + margin * scale, sy + margin * scale
        cover = [bx, by, bx + (x1 - x0) * scale, by + (y1 - y0) * scale]
        draw.rectangle(cover, fill='#dceaf2', outline=FRAME, width=1)
        if tile['seams_mm']['right'] is not None and plan['overlap_mm'] > 0:
            ox0 = bx + (x1 - x0 - plan['overlap_mm']) * scale
            draw.rectangle([ox0, by, cover[2], cover[3]], fill=(194, 38, 124, 50))
        if tile['seams_mm']['bottom'] is not None and plan['overlap_mm'] > 0:
            oy0 = by + (y1 - y0 - plan['overlap_mm']) * scale
            draw.rectangle([bx, oy0, cover[2], cover[3]], fill=(15, 125, 140, 50))
        if tile['narrow']:
            draw.rectangle(cover, outline=(206, 61, 50), width=3)
        font(label_fs).draw_text(draw, (bx + 4, by + 3), f"R{r:02d}C{c:02d}", TRIM)
        font(small_fs).draw_text(draw, (bx + 4, by + 20), f"{x0:g},{y0:g}", FRAME)
    legend_x = pad
    legend_y = canvas_h - 22
    draw.rectangle([legend_x, legend_y, legend_x + 14, legend_y + 10], fill=(194, 38, 124, 90))
    font(small_fs).draw_text(draw, (legend_x + 18, legend_y - 2), '列重叠', FRAME)
    draw.rectangle([legend_x + 90, legend_y, legend_x + 104, legend_y + 10], fill=(15, 125, 140, 90))
    font(small_fs).draw_text(draw, (legend_x + 108, legend_y - 2), '行重叠', FRAME)
    draw.rectangle([legend_x + 180, legend_y, legend_x + 194, legend_y + 10], outline=(206, 61, 50), width=3)
    font(small_fs).draw_text(draw, (legend_x + 198, legend_y - 2), '末张窄条（保持原比例不拉伸）', FRAME)
    return image


# ---------------------------------------------------------------- 清单与打包

def manifest(page, version, plan, document_name):
    """清单 JSON：行列拼回顺序、重叠核对与物理坐标。"""
    result = {key: plan[key] for key in (
        'format', 'paper', 'paper_mm', 'orientation', 'margin_mm', 'overlap_mm', 'print_dpi',
        'px_per_mm', 'printable_mm', 'board_mm', 'board_pixels', 'sheet_pixels', 'cols', 'rows',
        'sheet_count', 'source_effective_dpi', 'content_box_px', 'warnings', 'note')}
    result.update({'document_name': document_name, 'page_id': page['id'], 'page_name': page['name'],
                   'version_id': version['id'], 'version_label': version['label'],
                   'assembly': {
                       'rule': '按 R 行号从上到下、C 列号从左到右拼接；相邻张沿同色虚线与十字标记对齐，重叠带只保留一份。',
                       'row_order': 'top_to_bottom', 'column_order': 'left_to_right',
                       'origin_mm': [0, 0], 'coordinate_unit': 'mm'},
                   'tiles': []})
    for tile in plan['tiles']:
        entry = {k: tile[k] for k in (
            'row', 'col', 'page_no', 'file', 'board_coverage_mm', 'content_used_mm',
            'seams_mm', 'narrow')}
        entry['content_box_px'] = plan['content_box_px']
        result['tiles'].append(entry)
    return result


def manifest_csv(plan):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['page_no', 'row', 'col', 'file', 'board_x0_mm', 'board_y0_mm',
                     'board_x1_mm', 'board_y1_mm', 'used_w_mm', 'used_h_mm',
                     'seam_left_mm', 'seam_right_mm', 'seam_top_mm', 'seam_bottom_mm',
                     'narrow_strip', 'content_box_px'])
    for tile in plan['tiles']:
        x0, y0, x1, y1 = tile['board_coverage_mm']
        seams = tile['seams_mm']
        writer.writerow([tile['page_no'], tile['row'], tile['col'], tile['file'],
                         x0, y0, x1, y1, tile['content_used_mm'][0], tile['content_used_mm'][1],
                         '' if seams['left'] is None else seams['left'],
                         '' if seams['right'] is None else seams['right'],
                         '' if seams['top'] is None else seams['top'],
                         '' if seams['bottom'] is None else seams['bottom'],
                         'yes' if tile['narrow'] else 'no',
                         ';'.join(str(x) for x in plan['content_box_px'])])
    return out.getvalue().encode('utf-8-sig')


def assembly_text(plan, document_name):
    lines = [
        f'分张校样拼回说明 · {document_name}',
        f'成品物理尺寸：{plan["board_mm"][0]:g} × {plan["board_mm"][1]:g} mm',
        f'纸张：{plan["paper"]} {plan["paper_mm"][0]:g} × {plan["paper_mm"][1]:g} mm'
        f'（{plan["orientation"]}），四边留白 {plan["margin_mm"]:g} mm，'
        f'相邻重叠 {plan["overlap_mm"]:g} mm，打印 {plan["print_dpi"]} DPI，比例 1:1',
        f'共 {plan["sheet_count"]} 张：{plan["rows"]} 行 × {plan["cols"]} 列',
        '拼法：按 R 行号从上到下、C 列号从左到右排列；同色虚线与十字标记为对位线，',
        '重叠带内容相同，拼接时只保留一份；深蓝 L 形角线对应成品真实裁切边缘。',
        '每张留白处的标尺刻度即版面绝对物理坐标（mm），可与清单 board_coverage_mm 核对。',
        '',
        '页号\t行列\t板面覆盖 X(mm)\t板面覆盖 Y(mm)\t窄条',
    ]
    for tile in plan['tiles']:
        x0, y0, x1, y1 = tile['board_coverage_mm']
        lines.append(f'{tile["page_no"]}\tR{tile["row"]:02d}C{tile["col"]:02d}\t'
                     f'{x0:g}–{x1:g}\t{y0:g}–{y1:g}\t{"是" if tile["narrow"] else "否"}')
    if plan['warnings']:
        lines.append('')
        lines.append('生成提示：')
        lines.extend(f'- {item["message"]}' for item in plan['warnings'])
    return ('\n'.join(lines) + '\n').encode('utf-8')


def build_bundle(store, page, version, plan, document_name):
    """打包全部分张 PNG、总览、清单（JSON/CSV）与拼回说明。"""
    import zipfile
    manifest_data = manifest(page, version, plan, document_name)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for tile in plan['tiles']:
            raw = encode_at_dpi(render_tile(store, page, version, plan, tile, document_name),
                                plan['print_dpi'])
            manifest_data['tiles'][tile['page_no'] - 1]['sha256'] = hashlib.sha256(raw).hexdigest()
            manifest_data['tiles'][tile['page_no'] - 1]['bytes'] = len(raw)
            archive.writestr(tile['file'], raw)
        archive.writestr('overview.png', encode(render_overview(plan)))
        archive.writestr('manifest.json',
                         json.dumps(manifest_data, ensure_ascii=False, allow_nan=False, indent=2))
        archive.writestr('manifest.csv', manifest_csv(plan))
        archive.writestr('assembly.txt', assembly_text(plan, document_name))
    return output.getvalue()


def encode_at_dpi(image, dpi):
    """PNG 内嵌 pHYs 物理分辨率，保证各张以同一实际比例打印。"""
    out = io.BytesIO()
    image.save(out, format='PNG', dpi=(float(dpi), float(dpi)))
    return out.getvalue()

"""连续批注审阅与审阅包导入导出。

审阅包以批注的稳定身份（档案/页/版次/批注编号）为唯一对应依据；导回时只更新
能明确对应到的原批注，绝不新建批注，也不把结论套到其它位置。
"""
import copy
import csv
import io
import json
import zipfile
from PIL import Image, ImageDraw
from . import validation as v
from .store import uid, now
from .annotations import COLORS
from .images import load, paper_rgb, encode

PACKAGE_FORMAT = 'proofarchive-review-2'
BOUNDS_TOLERANCE = 0.02
MAX_PACKAGE_BYTES = 96 * 1024 * 1024
MAX_ENTRIES = 200 * 200

STATUS_LABELS = {'issue': '问题', 'note': '备注', 'approved': '确认'}
STATE_LABELS = {True: '已解决', False: '待解决'}


def build_queue(document, store, unresolved_only=True):
    """按档案页序、再按版次内批注顺序生成连续审阅队列。"""
    items = []
    for page_index, page in enumerate(document['pages']):
        for version in page['versions']:
            for annotation_index, annotation in enumerate(version['annotations']):
                if unresolved_only and annotation['resolved']:
                    continue
                items.append({
                    'page_index': page_index,
                    'page_number': page_index + 1,
                    'page_id': page['id'],
                    'page_name': page['name'],
                    'version_id': version['id'],
                    'version_label': version['label'],
                    'is_current': version['id'] == page['current'],
                    'annotation_index': annotation_index,
                    'annotation_number': annotation_index + 1,
                    'annotation': copy.deepcopy(annotation),
                    'pixels': list(version['pixels']),
                    'orientation': 'landscape' if version['pixels'][0] > version['pixels'][1] else 'portrait',
                })
    return {
        'document_id': document['id'],
        'document_name': document['name'],
        'revision': document['revision'],
        'scope': 'unresolved' if unresolved_only else 'all',
        'total': len(items),
        'unresolved_count': sum(
            1 for p in document['pages'] for ver in p['versions'] for a in ver['annotations']
            if not a['resolved']),
        'items': items,
    }


def crop_image(store, version, annotation, max_width=960, max_height=760, margin_fraction=0.35):
    """围绕批注框生成带可辨认上下文的局部图。

    先把取景框向四周扩展（短边至少扩展批注短边的 margin_fraction 倍），再按
    目标宽高比补足；靠近边缘的取景在画布外侧补白边，保证边缘批注也有上下文，
    且批注框本身不会被裁掉。横竖图均按各自像素尺寸处理。
    """
    image = paper_rgb(load(store, version))
    width, height = image.size
    x0, y0, x1, y1 = annotation['bounds']
    box = [x0 * width, y0 * height, x1 * width, y1 * height]
    box_w, box_h = box[2] - box[0], box[3] - box[1]

    pad_x = max(box_w * margin_fraction, width * 0.04)
    pad_y = max(box_h * margin_fraction, height * 0.04)
    crop_left = box[0] - pad_x
    crop_top = box[1] - pad_y
    crop_right = box[2] + pad_x
    crop_bottom = box[3] + pad_y

    target_ratio = max_width / max_height
    current_ratio = (crop_right - crop_left) / max(1.0, crop_bottom - crop_top)
    if current_ratio < target_ratio:
        extra = ((crop_bottom - crop_top) * target_ratio - (crop_right - crop_left)) / 2
        crop_left -= extra
        crop_right += extra
    elif current_ratio > target_ratio:
        extra = ((crop_right - crop_left) / target_ratio - (crop_bottom - crop_top)) / 2
        crop_top -= extra
        crop_bottom += extra

    left = max(0, int(round(crop_left)))
    top = max(0, int(round(crop_top)))
    right = min(width, int(round(crop_right)))
    bottom = min(height, int(round(crop_bottom)))
    cropped = image.crop((left, top, right, bottom))

    border_left = max(0, int(round(-crop_left)))
    border_top = max(0, int(round(-crop_top)))
    border_right = max(0, int(round(crop_right - width)))
    border_bottom = max(0, int(round(crop_bottom - height)))
    if border_left or border_top or border_right or border_bottom:
        padded = Image.new('RGB', (cropped.width + border_left + border_right,
                                   cropped.height + border_top + border_bottom), 'white')
        padded.paste(cropped, (border_left, border_top))
        cropped = padded
        draw_left, draw_top = border_left, border_top
    else:
        draw_left, draw_top = 0, 0

    draw = ImageDraw.Draw(cropped)
    highlight = [
        draw_left + (box[0] - left), draw_top + (box[1] - top),
        draw_left + (box[2] - left), draw_top + (box[3] - top),
    ]
    color = COLORS[annotation.get('kind', 'issue')]
    line_width = max(2, round(max(cropped.size) / 260))
    draw.rectangle(highlight, outline=color, width=line_width)

    ratio = min(max_width / cropped.width, max_height / cropped.height, 1.0)
    if ratio < 1.0:
        cropped = cropped.resize(
            (max(1, round(cropped.width * ratio)), max(1, round(cropped.height * ratio))),
            Image.Resampling.LANCZOS)
    return cropped


def review_entries(document):
    """构造审阅包条目（页序与批注编号在包内稳定，便于人工核对）。"""
    entries = []
    for page_index, page in enumerate(document['pages']):
        for version in page['versions']:
            for annotation_index, annotation in enumerate(version['annotations']):
                entries.append({
                    'sequence': len(entries) + 1,
                    'page_number': page_index + 1,
                    'page_name': page['name'],
                    'page_id': page['id'],
                    'version_label': version['label'],
                    'version_id': version['id'],
                    'annotation_number': annotation_index + 1,
                    'annotation_id': annotation['id'],
                    'kind': annotation['kind'],
                    'bounds': list(annotation['bounds']),
                    'text': annotation['text'],
                    'resolved': bool(annotation['resolved']),
                    'review_note': annotation.get('review_note', ''),
                })
    return entries


def export_review_package(store, document):
    export_id = uid()
    exported_at = now()
    entries = review_entries(document)
    manifest = {
        'format': PACKAGE_FORMAT,
        'export_id': export_id,
        'exported_at': exported_at,
        'document_id': document['id'],
        'document_name': document['name'],
        'revision': document['revision'],
        'instruction': '仅可修改每条条目的 resolved（true/false）与 review_note（审校意见）；'
                       '其余字段用于对应原批注，请勿改动。导回时只更新能明确对应的原批注。',
        'entry_count': len(entries),
        'entries': entries,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            page = next(p for p in document['pages'] if p['id'] == entry['page_id'])
            version = next(ver for ver in page['versions'] if ver['id'] == entry['version_id'])
            annotation = next(a for a in version['annotations'] if a['id'] == entry['annotation_id'])
            thumb = crop_image(store, version, annotation)
            archive.writestr('crops/' + entry['annotation_id'] + '.png', encode(thumb))
        archive.writestr('review.json',
                         json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2))
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(['序号', '页序', '页名', '版次', '批注编号', '类型',
                         '原图位置[左,上,右,下]', '批注内容', '处理状态', '审校意见',
                         '批注编号ID', '局部缩略图'])
        for entry in entries:
            writer.writerow([
                entry['sequence'], entry['page_number'], entry['page_name'],
                entry['version_label'], entry['annotation_number'],
                STATUS_LABELS.get(entry['kind'], entry['kind']),
                ' '.join(format(x, '.4f') for x in entry['bounds']),
                entry['text'], STATE_LABELS[entry['resolved']], entry['review_note'],
                entry['annotation_id'], 'crops/' + entry['annotation_id'] + '.png',
            ])
        archive.writestr('index.csv', '﻿' + csv_buffer.getvalue())
    return output.getvalue(), export_id


def _read_package(raw):
    if len(raw) > MAX_PACKAGE_BYTES:
        raise v.Invalid('审阅包不得超过96MiB')
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise v.Invalid('不是有效的ZIP审阅包') from error
    with archive:
        names = archive.namelist()
        if len(names) > MAX_ENTRIES + 50 or len(names) != len(set(names)):
            raise v.Invalid('审阅包文件数量异常')
        if sum(info.file_size for info in archive.infolist()) > 2 * MAX_PACKAGE_BYTES:
            raise v.Invalid('审阅包解压容量过大')
        try:
            manifest = json.loads(archive.read('review.json'))
        except (KeyError, ValueError) as error:
            raise v.Invalid('审阅包清单 review.json 不可读取') from error
    if manifest.get('format') != PACKAGE_FORMAT:
        raise v.Invalid('不支持此审阅包格式，请使用本档案导出的审阅包')
    entries = v.list_value(manifest.get('entries'), MAX_ENTRIES)
    return manifest, entries


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    raise v.Invalid('处理状态 resolved 需要 true 或 false')


def _coerce_note(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise v.Invalid('审校意见 review_note 需要文本')
    return v.text(value, '审校意见', 3000, True)


def _bounds_close(actual, provided):
    return len(provided) == 4 and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and abs(float(x) - y) <= BOUNDS_TOLERANCE
        for x, y in zip(provided, actual))


def analyze_import(document, raw):
    """逐条核对审阅包，返回每条的对应结果；不修改档案。"""
    manifest, entries = _read_package(raw)
    package_doc_id = manifest.get('document_id')
    pages = {page['id']: page for page in document['pages']}
    results = []
    seen = set()
    for raw_entry in entries:
        entry = v.object_value(raw_entry)
        ref = {field: entry.get(field) for field in
               ('page_id', 'version_id', 'annotation_id')}
        result = {'entry': entry, 'status': None, 'reason': None,
                  'annotation_id': entry.get('annotation_id'),
                  'page_name': entry.get('page_name'), 'page_number': entry.get('page_number'),
                  'annotation_number': entry.get('annotation_number')}
        if not all(isinstance(ref[field], str) and ref[field] for field in ref):
            result['status'], result['reason'] = 'skipped', '条目缺少页/版次/批注编号，无法对应'
            results.append(result)
            continue
        key = (ref['page_id'], ref['version_id'], ref['annotation_id'])
        if key in seen:
            result['status'], result['reason'] = 'skipped', '审阅包内条目重复，已跳过重复项'
            results.append(result)
            continue
        seen.add(key)
        if package_doc_id != document['id']:
            result['status'], result['reason'] = 'skipped', '审阅包来自其它档案（档案编号不一致）'
            results.append(result)
            continue
        page = pages.get(ref['page_id'])
        if page is None:
            result['status'], result['reason'] = 'skipped', '对应页面已删除'
            results.append(result)
            continue
        version = next((ver for ver in page['versions'] if ver['id'] == ref['version_id']), None)
        if version is None:
            result['status'], result['reason'] = 'skipped', '对应版次已删除或已改版'
            results.append(result)
            continue
        annotation = next((a for a in version['annotations'] if a['id'] == ref['annotation_id']), None)
        if annotation is None:
            result['status'], result['reason'] = 'skipped', '对应批注已删除，不会新建批注'
            results.append(result)
            continue
        provided_bounds = entry.get('bounds')
        if not isinstance(provided_bounds, list) or not _bounds_close(annotation['bounds'], provided_bounds):
            result['status'], result['reason'] = 'skipped', '原图位置与现档案不一致，拒绝套用结论'
            results.append(result)
            continue
        try:
            resolved = _coerce_bool(entry.get('resolved'))
            note = _coerce_note(entry.get('review_note'))
        except v.Invalid as error:
            result['status'], result['reason'] = 'skipped', str(error)
            results.append(result)
            continue
        result['resolved'] = resolved
        result['review_note'] = note
        same_export = annotation.get('review_export_id') == manifest.get('export_id')
        unchanged = (annotation['resolved'] == resolved and annotation.get('review_note', '') == note)
        if same_export and unchanged:
            result['status'], result['reason'] = 'duplicate', '该审阅包已导回过且内容未变化'
        elif unchanged:
            result['status'], result['reason'] = 'unchanged', '处理状态与意见和现档案一致，无需更新'
        else:
            result['status'], result['reason'] = 'applied', None
        results.append(result)
    summary = {
        'format': manifest.get('format'),
        'export_id': manifest.get('export_id'),
        'exported_at': manifest.get('exported_at'),
        'document_id': package_doc_id,
        'document_match': package_doc_id == document['id'],
        'entry_count': len(entries),
        'applied': sum(1 for r in results if r['status'] == 'applied'),
        'unchanged': sum(1 for r in results if r['status'] == 'unchanged'),
        'duplicate': sum(1 for r in results if r['status'] == 'duplicate'),
        'skipped': sum(1 for r in results if r['status'] == 'skipped'),
        'results': results,
    }
    return summary


def apply_import(store, key, raw, expected=None):
    """按核对结果只更新能明确对应的原批注；没有可写回内容时不改动档案。"""
    document = store.read(key)
    summary = analyze_import(document, raw)
    applied = [r for r in summary['results'] if r['status'] == 'applied']
    if not applied:
        return document, summary

    def change(doc):
        pages = {page['id']: page for page in doc['pages']}
        for result in applied:
            page = pages[result['entry']['page_id']]
            version = next(ver for ver in page['versions'] if ver['id'] == result['entry']['version_id'])
            annotation = next(a for a in version['annotations']
                              if a['id'] == result['entry']['annotation_id'])
            annotation['resolved'] = result['resolved']
            annotation['review_note'] = result['review_note']
            annotation['review_export_id'] = summary['export_id']
            annotation['review_imported_at'] = now()
            annotation['updated'] = now()
    updated = store.mutate(key, expected, '导回批注审阅包', change)
    return updated, summary

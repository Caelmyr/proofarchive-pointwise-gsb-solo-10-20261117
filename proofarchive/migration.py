"""Move selected annotations between two versions of the same page.

Coordinates alone are not enough: the page may have been shifted, scaled or
cropped, and individual blocks may have moved during a reflow.  A page-level
similarity transform is estimated from feature correspondences, then every
selected region is located independently by matching its actual pixels in the
new version.  Only regions with an unambiguous, sufficiently similar match are
offered for writing.  Everything else is reported as manual work with a reason.
"""
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import validation as v
from .store import now, uid
from .images import load, paper_rgb


MAX_SELECTION = 60
WORK_SIDE = 1200.0
MIN_MATCH = 0.62
STRONG_MATCH = 0.78
AMBIGUOUS_SCORE = 0.80
AMBIGUOUS_GAP = 0.012
MIN_TEXTURE = 0.012

REASONS = {
    'no_registration': '页面整体版面变化过大，无法建立可靠的版面对应关系。',
    'outside_target': '原区域映射后超出新版画布，可能已被裁掉或不在新版范围内。',
    'region_too_large': '映射后的区域大于新版画布，无法可靠放置。',
    'ambiguous': '新版中找到多处同样相似的图案，无法确定应该跟随哪一处。',
    'content_changed': '原区域图案在新版中相似度不足，无法确认对应位置。',
    'uncertain_position': '最佳相似位置离版面预计位置过远，置信度不足。',
    'no_region': '原区域过小，无法读取可比较的图案。',
}


def _message(reason):
    return REASONS.get(reason, '无法自动确定对应位置。')


def _selection(value):
    if isinstance(value, str):
        value = [part for part in value.split(',') if part]
    if not isinstance(value, list) or not value:
        raise v.Invalid('请选择要迁移的批注')
    if len(value) > MAX_SELECTION:
        raise v.Invalid(f'一次最多迁移{MAX_SELECTION}条批注')
    result = []
    for item in value:
        identifier = v.identifier(item)
        if identifier in result:
            raise v.Invalid('批注选择重复')
        result.append(identifier)
    return result


def _work(image):
    pixels = np.asarray(paper_rgb(image), dtype=np.float32) / 255.
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    scale = min(1., WORK_SIDE / max(width, height))
    if scale < 1.:
        gray = cv2.resize(gray, (max(1, round(width * scale)), max(1, round(height * scale))),
                          interpolation=cv2.INTER_AREA)
    return gray.astype(np.float32)


def _register(source, target):
    """Estimate a similarity transform from source work pixels to target work pixels."""
    sift = cv2.SIFT_create(nfeatures=5000)
    source_8u = np.uint8(np.clip(source, 0., 1.) * 255)
    target_8u = np.uint8(np.clip(target, 0., 1.) * 255)
    source_points, source_descriptors = sift.detectAndCompute(source_8u, None)
    target_points, target_descriptors = sift.detectAndCompute(target_8u, None)
    report = {'method': 'SIFT + RANSAC similarity', 'features_source': len(source_points or ()),
              'features_target': len(target_points or ()), 'matches': 0, 'inliers': 0,
              'inlier_ratio': 0., 'median_error': None, 'scale': None, 'rotation_degrees': None,
              'translation': None, 'spread': 0., 'confidence': 0., 'reliable': False,
              'message': '无法建立版面对应关系。'}
    if source_descriptors is None or target_descriptors is None or len(source_points) < 8 or len(target_points) < 8:
        return None, report
    pairs = cv2.BFMatcher().knnMatch(source_descriptors, target_descriptors, k=2)
    good = [pair[0] for pair in pairs
            if len(pair) == 2 and pair[0].distance < 0.72 * pair[1].distance]
    report['matches'] = len(good)
    if len(good) < 8:
        return None, report
    source_xy = np.float32([source_points[m.queryIdx].pt for m in good])
    target_xy = np.float32([target_points[m.trainIdx].pt for m in good])
    matrix, mask = cv2.estimateAffinePartial2D(
        source_xy, target_xy, method=cv2.RANSAC, ransacReprojThreshold=2.0,
        maxIters=5000, confidence=0.995)
    if matrix is None or mask is None:
        return None, report
    mask = mask.ravel().astype(bool)
    inliers = int(mask.sum())
    report['inliers'] = inliers
    report['inlier_ratio'] = round(inliers / max(len(good), 1), 4)
    if inliers:
        predicted = (matrix[:, :2] @ source_xy[mask].T).T + matrix[:, 2]
        errors = np.linalg.norm(predicted - target_xy[mask], axis=1)
        report['median_error'] = round(float(np.median(errors)), 3)
        spread_points = source_xy[mask]
        area = ((np.ptp(spread_points[:, 0]) * np.ptp(spread_points[:, 1]))
                / max(source.shape[0] * source.shape[1], 1))
        report['spread'] = round(float(area), 4)
    scale = float(math.hypot(matrix[0, 0], matrix[1, 0]))
    report['scale'] = round(scale, 5)
    report['rotation_degrees'] = round(math.degrees(math.atan2(matrix[1, 0], matrix[0, 0])), 3)
    report['translation'] = [round(float(matrix[0, 2]), 3), round(float(matrix[1, 2]), 3)]
    ratio = report['inlier_ratio']
    spread = report['spread']
    confidence = (min(1., inliers / 45.) * .5 + min(1., ratio / .6) * .3
                  + min(1., spread / .12) * .2)
    report['confidence'] = round(float(confidence), 4)
    error = report['median_error']
    reliable = (inliers >= 12 and ratio >= .2 and spread >= .015
                and .12 <= scale <= 8. and error is not None and error <= 3.5)
    report['reliable'] = bool(reliable)
    if not reliable:
        report['message'] = '页面对应关系证据不足，可能是整版改版或图案重复过多。'
    else:
        report['message'] = '已建立版面对应关系；每条区域仍会单独核对图案。'
    return (matrix, report) if reliable else (None, report)


def _box(bounds, width, height):
    x0 = max(0, min(width - 1, int(math.floor(bounds[0] * width))))
    y0 = max(0, min(height - 1, int(math.floor(bounds[1] * height))))
    x1 = max(x0 + 1, min(width, int(math.ceil(bounds[2] * width))))
    y1 = max(y0 + 1, min(height, int(math.ceil(bounds[3] * height))))
    return x0, y0, x1, y1


def _map_box(matrix, box):
    x0, y0, x1, y1 = box
    corners = np.float64([[x0, y0, 1.], [x1, y0, 1.], [x1, y1, 1.], [x0, y1, 1.]])
    mapped = corners @ matrix.T
    return (float(mapped[:, 0].min()), float(mapped[:, 1].min()),
            float(mapped[:, 0].max()), float(mapped[:, 1].max()))


def _warp_template(source, box, matrix, target_shape):
    x0, y0, x1, y1 = box
    crop = source[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    linear = matrix[:, :2]
    height, width = crop.shape[:2]
    corners = np.float64([[0., 0.], [width, 0.], [width, height], [0., height]])
    mapped = corners @ linear.T
    low = mapped.min(axis=0)
    high = mapped.max(axis=0)
    template_width = max(1, int(math.ceil(high[0] - low[0])))
    template_height = max(1, int(math.ceil(high[1] - low[1])))
    if template_width > target_shape[1] or template_height > target_shape[0]:
        return None
    transform = np.array([[linear[0, 0], linear[0, 1], -low[0]],
                          [linear[1, 0], linear[1, 1], -low[1]]], dtype=np.float64)
    warped = cv2.warpAffine(crop, transform, (template_width, template_height),
                            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return warped.astype(np.float32)


def _peaks(result, radius, threshold):
    kernel = np.ones((radius * 2 + 1, radius * 2 + 1), dtype=np.uint8)
    local_max = cv2.dilate(result, kernel)
    candidates = np.argwhere((result >= local_max - 1e-6) & (result >= threshold))
    return [(float(result[y, x]), int(x), int(y)) for y, x in candidates]


def _locate(source, target, matrix, annotation, registration):
    """Locate one annotation in the target, or explain why it cannot be done."""
    target_height, target_width = target.shape
    source_box = _box(annotation['bounds'], source.shape[1], source.shape[0])
    predicted = _map_box(matrix, source_box)
    tolerance = 2.
    # A crop moves the page origin without moving any pixels relative to the
    # origin, so identical coordinates carry no evidence by themselves.  Treat
    # sub-pixel jitter and rounding from the estimator as identical too.
    linear = matrix[:, :2]
    identity = (np.abs(linear - np.eye(2)).max() <= .01
                and np.abs(matrix[:, 2]).max() <= 1.5)
    if (predicted[0] < -tolerance or predicted[1] < -tolerance
            or predicted[2] > target_width + tolerance or predicted[3] > target_height + tolerance):
        return {'status': 'manual', 'reason': 'outside_target',
                'message': _message('outside_target'), 'confidence': 0.}
    predicted = (max(0., predicted[0]), max(0., predicted[1]),
                 min(float(target_width), predicted[2]), min(float(target_height), predicted[3]))
    if predicted[2] - predicted[0] < 1. or predicted[3] - predicted[1] < 1.:
        return {'status': 'manual', 'reason': 'no_region', 'message': _message('no_region'),
                'confidence': 0.}
    template = _warp_template(source, source_box, matrix, target.shape)
    if template is None:
        return {'status': 'manual', 'reason': 'region_too_large',
                'message': _message('region_too_large'), 'confidence': 0.}
    if template.shape[1] < 4 or template.shape[0] < 4 or float(template.std()) < MIN_TEXTURE:
        # There is no pattern to verify here.  Following the page transform is
        # only defensible when that transform itself is well supported; a weak
        # registration would silently put a featureless box in the wrong place.
        if registration['confidence'] < .65:
            return {'status': 'manual', 'reason': 'uncertain_position',
                    'message': '区域缺少可核对的图案，且版面对应关系证据不足。',
                    'confidence': round(registration['confidence'], 3)}
        return {'status': 'ready', 'method': 'registration',
                'confidence': round(min(.5, .25 + .3 * registration['confidence']), 3),
                'bounds': [predicted[0] / target_width, predicted[1] / target_height,
                           predicted[2] / target_width, predicted[3] / target_height],
                'note': '区域缺少可核对图案，仅按已核实的整体版面变换放置。'}
    result = cv2.matchTemplate(target, template, cv2.TM_CCOEFF_NORMED)
    result = np.nan_to_num(result, nan=-1., posinf=-1., neginf=-1.).astype(np.float32)
    if result.size == 0:
        return {'status': 'manual', 'reason': 'content_changed',
                'message': _message('content_changed'), 'confidence': 0.}
    _, best, _, best_location = cv2.minMaxLoc(result)
    best = float(best)
    if best < MIN_MATCH:
        return {'status': 'manual', 'reason': 'content_changed',
                'message': _message('content_changed'), 'confidence': round(max(0., best), 2)}
    template_width, template_height = template.shape[1], template.shape[0]
    radius = max(6, int(round(min(template_width, template_height) * .45)))
    predicted_location = (int(round(predicted[0])), int(round(predicted[1])))
    distance = math.hypot(best_location[0] - predicted_location[0],
                          best_location[1] - predicted_location[1])
    far_limit = max(30., 1.5 * max(template_width, template_height))
    far = distance > far_limit
    # A second, nearly equal match somewhere else means the same pattern occurs
    # more than once and we cannot know which copy the annotation belongs to.
    if best >= STRONG_MATCH:
        gap = max(AMBIGUOUS_GAP, .3 * (1. - best))
        distinct = [peak for peak in sorted(_peaks(result, radius, max(MIN_MATCH, best - gap)),
                                            key=lambda item: item[0], reverse=True)
                    if math.hypot(peak[1] - best_location[0], peak[2] - best_location[1]) > radius
                    and math.hypot(peak[1] - predicted_location[0],
                                   peak[2] - predicted_location[1]) > radius]
        if distinct and distinct[0][0] >= best - gap:
            return {'status': 'manual', 'reason': 'ambiguous', 'message': _message('ambiguous'),
                    'confidence': round(best, 3)}
    elif far and not identity:
        # The pattern is only moderately similar and does not sit where the
        # layout transform expects it: following it would be a guess.
        return {'status': 'manual', 'reason': 'uncertain_position',
                'message': _message('uncertain_position'), 'confidence': round(best, 3)}
    local_confidence = min(1., max(0., (best - .52) / .42))
    confidence = min(.99, .32 * registration['confidence'] + .68 * local_confidence)
    if identity and best < STRONG_MATCH:
        return {'status': 'manual', 'reason': 'content_changed',
                'message': _message('content_changed'), 'confidence': round(best, 3)}
    if far:
        confidence = max(0., confidence - .08)
    return {'status': 'ready', 'method': 'template',
            'confidence': round(float(confidence), 3),
            'bounds': [best_location[0] / target_width, best_location[1] / target_height,
                       (best_location[0] + template_width) / target_width,
                       (best_location[1] + template_height) / target_height],
            'score': round(best, 4),
            'displacement_work_px': round(float(distance), 2),
            'note': '按实际图案在新版中定位。'}


def existing_by_source(target_version):
    result = {}
    for annotation in target_version.get('annotations', []):
        provenance = annotation.get('provenance')
        if isinstance(provenance, dict) and provenance.get('kind') == 'migration':
            result[(provenance.get('source_version_id'), provenance.get('source_annotation_id'))] = annotation['id']
    return result


def plan_digest(plan):
    """Fingerprint of the reviewed plan, so a confirmation cannot silently apply
    positions that differ from what the reviewer saw."""
    payload = json.dumps({
        'source': plan['source_version_id'], 'target': plan['target_version_id'],
        'items': [{key: item.get(key) for key in
                   ('source_annotation_id', 'status', 'bounds', 'reason')}
                  for item in plan['items']]}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _migration_id(document, page, source, target, annotation_ids):
    payload = '\x1f'.join([document['id'], page['id'], source['id'], target['id'],
                           ','.join(sorted(annotation_ids))])
    return hashlib.sha256(payload.encode()).hexdigest()


def build_plan(store, document, page, source, target, annotation_ids):
    annotation_ids = _selection(annotation_ids)
    annotations = {item['id']: item for item in source.get('annotations', [])}
    missing = [item for item in annotation_ids if item not in annotations]
    if missing:
        raise v.Invalid('来源版次中不存在所选批注')
    source_image = load(store, source)
    target_image = load(store, target)
    existing = existing_by_source(target)
    migration_id = _migration_id(document, page, source, target, annotation_ids)
    target_annotations = {item['id']: item for item in target.get('annotations', [])}
    plan = {'document_id': document['id'], 'page_id': page['id'],
            'source_version_id': source['id'], 'target_version_id': target['id'],
            'source_label': source['label'], 'target_label': target['label'],
            'migration_id': migration_id, 'registration': None, 'items': [],
            'summary': {'selected': len(annotation_ids), 'ready': 0, 'manual': 0, 'existing': 0}}
    items = []
    for annotation_id in annotation_ids:
        annotation = annotations[annotation_id]
        item = {'source_annotation_id': annotation_id, 'text': annotation['text'],
                'kind': annotation['kind'], 'source_bounds': list(annotation['bounds']),
                'status': 'pending'}
        if (source['id'], annotation_id) in existing:
            migrated_id = existing[(source['id'], annotation_id)]
            item.update(status='existing', reason=None,
                        message='这个原批注已经迁移到目标版次，不会重复创建。',
                        confidence=1., method='existing',
                        existing_annotation_id=migrated_id,
                        bounds=list(target_annotations[migrated_id]['bounds']))
            plan['summary']['existing'] += 1
        items.append(item)
    pending = [item for item in items if item['status'] == 'pending']
    if pending:
        source_work = _work(source_image)
        target_work = _work(target_image)
        # A blank page has no local structure at all; a sparse page (one banner
        # or one rule) still carries feature points and is registered normally.
        if max(float(source_work.std()), float(target_work.std())) < MIN_TEXTURE:
            matrix, registration = None, {
                'method': 'SIFT + RANSAC similarity', 'features_source': 0,
                'features_target': 0, 'matches': 0, 'inliers': 0, 'inlier_ratio': 0.,
                'median_error': None, 'scale': None, 'rotation_degrees': None,
                'translation': None, 'spread': 0., 'confidence': 0., 'reliable': False,
                'message': '新版或原版页面几乎没有图案，无法核对区域对应位置。'}
        else:
            matrix, registration = _register(source_work, target_work)
        plan['registration'] = registration
        if matrix is None:
            for item in pending:
                item.update(status='manual', reason='no_registration',
                            message=_message('no_registration'), confidence=0.,
                            method=None)
                plan['summary']['manual'] += 1
        else:
            for item in pending:
                annotation = annotations[item['source_annotation_id']]
                located = _locate(source_work, target_work, matrix, annotation, registration)
                item.update(located)
                plan['summary'][located['status']] += 1
    for index, item in enumerate(items):
        item['index'] = index + 1
    plan['items'] = items
    plan['registration_note'] = ('逐条按来源图案在新版中重新定位；不能定位的批注只列出原因，'
                                 '不会写入看似成功但位置不可靠的框。')
    plan['digest'] = plan_digest(plan)
    return plan, source_image, target_image


FONT_PATH = Path(__file__).resolve().parents[1] / 'static' / 'archive-font.ttf'
_font_cache = {}


def _font(size):
    """Bundled archive font so preview labels keep their Chinese text."""
    if size not in _font_cache:
        try:
            _font_cache[size] = ImageFont.truetype(str(FONT_PATH), size)
        except OSError:
            _font_cache[size] = ImageFont.load_default(size=size)
    return _font_cache[size]


def _label(draw, x, y, text, size, fill):
    """Draw mixed CJK/Latin text; the bundled font covers only CJK glyphs."""
    latin = ImageFont.load_default(size=max(9, size - 1))
    cjk = _font(size)
    for run, is_ascii in _runs(text):
        font = latin if is_ascii else cjk
        draw.text((x, y), run, fill=fill, font=font)
        x += font.getlength(run)
    return x


def _runs(text):
    current, flag = '', None
    for character in text:
        ascii_run = ord(character) < 128
        if flag is None or flag == ascii_run:
            current += character
        else:
            yield current, flag
            current, flag = character, ascii_run
        flag = ascii_run
    if current:
        yield current, flag


def _fit(image, width, height):
    ratio = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _bounds_box(bounds, width, height):
    return [round(bounds[0] * width), round(bounds[1] * height),
            round(bounds[2] * width), round(bounds[3] * height)]


def preview_image(plan, source_image, target_image, width=980, panel_height=620):
    """Side-by-side pages: numbered source regions and confirmed target positions.

    Manual items are numbered and outlined on the source only.  The target page
    intentionally receives no candidate rectangle for them, so the preview can
    never read as a successful placement that was not actually verified.
    """
    margin = 16
    panel_width = (width - margin * 3) // 2
    header = 46
    legend_lines = min(len(plan['items']), 30) // 2 + 1
    detail_items = plan['items'][:10]
    detail_height = 108 * len(detail_items) + (26 if detail_items else 0)
    height = header + panel_height + 18 + 20 * legend_lines + detail_height
    output = Image.new('RGB', (width, height), '#eef1f4')
    draw = ImageDraw.Draw(output)
    _label(draw, margin, 13, 'SOURCE / 原区域', 14, '#31485c')
    _label(draw, margin * 2 + panel_width, 13, 'TARGET / 新版对应位置', 14, '#31485c')
    panels = []
    for image, left in ((source_image, margin), (target_image, margin * 2 + panel_width)):
        panel = _fit(paper_rgb(image), panel_width, panel_height)
        x = left + (panel_width - panel.width) // 2
        y = header + (panel_height - panel.height) // 2
        output.paste(panel, (x, y))
        draw.rectangle([left, header, left + panel_width, header + panel_height], outline='#cbd5de')
        panels.append((panel, x, y))
    scale_x = panels[0][0].width / source_image.width
    scale_y = panels[0][0].height / source_image.height
    target_scale_x = panels[1][0].width / target_image.width
    target_scale_y = panels[1][0].height / target_image.height
    for item in plan['items']:
        color = {'ready': '#d94b3b', 'existing': '#2876a4', 'manual': '#c98a2c'}[item['status']]
        box = _bounds_box(item['source_bounds'], source_image.width, source_image.height)
        draw.rectangle([panels[0][1] + box[0] * scale_x, panels[0][2] + box[1] * scale_y,
                        panels[0][1] + box[2] * scale_x, panels[0][2] + box[3] * scale_y],
                       outline=color, width=2)
        _number(draw, panels[0][1] + box[0] * scale_x, panels[0][2] + box[1] * scale_y,
                item['index'], color)
        if item['status'] in ('ready', 'existing'):
            color = '#23865b' if item['status'] == 'ready' else '#2876a4'
            box = _bounds_box(item['bounds'], target_image.width, target_image.height)
            draw.rectangle([panels[1][1] + box[0] * target_scale_x,
                            panels[1][2] + box[1] * target_scale_y,
                            panels[1][1] + box[2] * target_scale_x,
                            panels[1][2] + box[3] * target_scale_y], outline=color, width=2)
            _number(draw, panels[1][1] + box[0] * target_scale_x,
                    panels[1][2] + box[1] * target_scale_y, item['index'], color)
    legend_top = header + panel_height + 10
    for item in plan['items'][:30]:
        column = 0 if item['index'] <= 15 else 1
        row = (item['index'] - 1) % 15
        label = f"{item['index']:02d}  {item['status'].upper()}"
        if item['status'] == 'manual':
            label += '  ' + reason_label(item)
        color = {'ready': '#23865b', 'existing': '#2876a4', 'manual': '#b06d17'}[item['status']]
        _label(draw, margin + column * (panel_width + margin), legend_top + row * 16,
               label[:52], 11, color)
    if len(plan['items']) > 30:
        _label(draw, margin, legend_top + 15 * 16, f'+ {len(plan["items"]) - 30} more in list',
               11, '#7d8c9a')
    detail_top = legend_top + 20 * legend_lines + 6
    if detail_items:
        _label(draw, margin, detail_top, 'DETAIL / 原区域与新版对应位置', 12, '#31485c')
        _label(draw, 620, detail_top, 'STATUS', 12, '#31485c')
    for index, item in enumerate(detail_items):
        top = detail_top + 24 + index * 108
        draw.line([margin, top - 4, width - margin, top - 4], fill='#dbe3ea')
        source_crop = _crop_with_box(source_image, item['source_bounds'], (284, 92))
        output.paste(source_crop[0], (margin, top))
        if item['status'] in ('ready', 'existing'):
            target_crop = _crop_with_box(target_image, item['bounds'], (296, 92),
                                         '#23865b' if item['status'] == 'ready' else '#2876a4')
            output.paste(target_crop[0], (margin * 2 + 284, top))
        else:
            frame = Image.new('RGB', (296, 92), '#f7f3ea')
            target_frame = ImageDraw.Draw(frame)
            target_frame.rectangle([0, 0, 295, 91], outline='#d08b31')
            _label(target_frame, 14, 30, 'NO BOX WRITTEN', 11, '#b06d17')
            _label(target_frame, 14, 52, '待人工处理', 11, '#b06d17')
            output.paste(frame, (margin * 2 + 284, top))
        color = {'ready': '#23865b', 'existing': '#2876a4', 'manual': '#b06d17'}[item['status']]
        _label(draw, 620, top + 4, f"{item['index']:02d}  {item['status'].upper()}", 12, color)
        note = reason_label(item) if item['status'] == 'manual' else (
            f"confidence {item.get('confidence', 0):.2f} / {item.get('method', '')}")
        _label(draw, 620, top + 26, note[:44], 11, '#52677a')
        _label(draw, 620, top + 46, item['text'][:40], 11, '#7d8c9a')
    if len(plan['items']) > len(detail_items):
        _label(draw, margin, detail_top + 24 + 108 * len(detail_items) + 2,
               f'+ {len(plan["items"]) - len(detail_items)} more in list', 11, '#7d8c9a')
    return output


def reason_label(item):
    reason = item.get('reason')
    if not reason:
        return '待人工处理'
    return REASON_LABELS.get(reason, reason)


REASON_LABELS = {
    'no_registration': '版面变化过大',
    'outside_target': '区域超出新版或被裁掉',
    'region_too_large': '区域大于新版',
    'ambiguous': '图案重复，位置不确定',
    'content_changed': '图案已改变',
    'uncertain_position': '位置置信度不足',
    'no_region': '区域过小',
    'not_confirmed': '确认时未勾选',
}


def _crop_with_box(image, bounds, size, color='#d94b3b'):
    """Crop around one region with padding and mark the region itself."""
    width, height = image.size
    x0, y0 = bounds[0] * width, bounds[1] * height
    x1, y1 = bounds[2] * width, bounds[3] * height
    pad_x = max(10., (x1 - x0) * .45)
    pad_y = max(10., (y1 - y0) * .45)
    left = max(0, int(math.floor(x0 - pad_x)))
    top = max(0, int(math.floor(y0 - pad_y)))
    right = min(width, int(math.ceil(x1 + pad_x)))
    bottom = min(height, int(math.ceil(y1 + pad_y)))
    if right <= left or bottom <= top:
        left, top, right, bottom = 0, 0, width, height
    panel = _fit(paper_rgb(image.crop((left, top, right, bottom))), size[0], size[1])
    canvas = Image.new('RGB', size, 'white')
    offset = ((size[0] - panel.width) // 2, (size[1] - panel.height) // 2)
    canvas.paste(panel, offset)
    draw = ImageDraw.Draw(canvas)
    scale_x = panel.width / (right - left)
    scale_y = panel.height / (bottom - top)
    draw.rectangle([offset[0] + (x0 - left) * scale_x, offset[1] + (y0 - top) * scale_y,
                    offset[0] + (x1 - left) * scale_x, offset[1] + (y1 - top) * scale_y],
                   outline=color, width=2)
    return canvas, offset


def _number(draw, x, y, index, color):
    label = str(index)
    draw.rectangle([x, y, x + 8 + 7 * len(label), y + 16], fill=color)
    _label(draw, x + 3, y + 2, label, 11, 'white')


def preview(store, document, page, source_version_id, target_version_id, annotation_ids):
    source = store.version(page, source_version_id)
    target = store.version(page, target_version_id)
    if source['id'] == target['id']:
        raise v.Invalid('来源与目标版次必须不同')
    return build_plan(store, document, page, source, target, annotation_ids)


def commit(store, key, page_id, source_version_id, target_version_id, annotation_ids,
           include, expected, digest=None):
    annotation_ids = _selection(annotation_ids)
    if include is None:
        include = annotation_ids
    if isinstance(include, str):
        include = [part for part in include.split(',') if part]
    if not isinstance(include, list):
        raise v.Invalid('确认迁移的批注列表无效')
    include = [v.identifier(item) for item in include]
    if len(set(include)) != len(include) or not set(include).issubset(set(annotation_ids)):
        raise v.Invalid('确认迁移的批注必须包含在所选批注中')

    document = store.read(key)
    page = store.page(document, page_id)
    source = store.version(page, source_version_id)
    target = store.version(page, target_version_id)
    if source['id'] == target['id']:
        raise v.Invalid('来源与目标版次必须不同')
    plan, _, _ = build_plan(store, document, page, source, target, annotation_ids)
    migration_id = plan['migration_id']
    ready = {item['source_annotation_id']: item for item in plan['items']
             if item['status'] == 'ready' and item['source_annotation_id'] in include}
    if ready and digest is not None:
        # Bind the written positions to the plan the reviewer actually saw.  A
        # repeat of an already applied migration writes nothing, so it is
        # allowed even though its plan now lists those regions as done.
        if not isinstance(digest, str) or digest != plan['digest']:
            raise v.Conflict('版面或批注已变化，请重新生成迁移预览后再确认')
    records = target.get('migrations', [])
    previous = next((record for record in records if record.get('id') == migration_id), None)

    def outcome(document, record, created, duplicate, skipped):
        return {'document': document, 'migration': record, 'duplicate': duplicate,
                'created': created, 'existing': plan['summary']['existing'],
                'manual': plan['summary']['manual'], 'skipped': skipped}

    skipped_now = [{'source_annotation_id': item['source_annotation_id'],
                    'reason': item.get('reason'), 'message': item.get('message')}
                   for item in plan['items'] if item['status'] == 'manual']
    if not ready:
        # Everything is either already migrated, manual, or deliberately not
        # confirmed.  Do not touch the document: a repeated confirmation must
        # never create another copy or a second migration record.  Nothing is
        # written, so a stale revision is harmless when the record already
        # exists (a double click usually arrives with the previous revision).
        if previous is None and expected is not None and expected != document['revision']:
            raise v.Conflict('档案已更新，请刷新后重试')
        return outcome(document, previous, 0, previous is not None, skipped_now)

    created = {}

    def change(document):
        page = store.page(document, page_id)
        current_source = store.version(page, source_version_id)
        current_target = store.version(page, target_version_id)
        source_annotations = {item['id']: item for item in current_source.get('annotations', [])}
        if any(item not in source_annotations for item in annotation_ids):
            raise v.Invalid('来源版次中不存在所选批注')
        already = existing_by_source(current_target)
        creation = []
        for annotation_id in annotation_ids:
            if annotation_id not in ready or (current_source['id'], annotation_id) in already:
                continue
            origin = source_annotations[annotation_id]
            box = ready[annotation_id]
            creation.append({
                'id': uid(), 'bounds': [float(round(value, 8)) for value in box['bounds']],
                'text': origin['text'], 'kind': origin['kind'], 'resolved': False,
                'created': now(), 'updated': now(),
                'provenance': {
                    'kind': 'migration', 'migration_id': migration_id,
                    'source_version_id': current_source['id'],
                    'source_annotation_id': annotation_id,
                    'source_bounds': list(origin['bounds']),
                    'method': box.get('method'), 'confidence': box.get('confidence'),
                    'migrated_at': now()}})
        if not creation:
            # Every selected region was already migrated by an earlier run; keep
            # the existing record instead of appending a second one.
            created['duplicate'] = True
            return
        if len(current_target.get('annotations', [])) + len(creation) > 200:
            raise v.Invalid('目标版次批注容量不足，请减少迁移数量')
        current_target.setdefault('annotations', []).extend(creation)
        records = current_target.setdefault('migrations', [])
        record = next((item for item in records if item.get('id') == migration_id), None)
        skipped = [{'source_annotation_id': item['source_annotation_id'],
                    'reason': item.get('reason'), 'message': item.get('message')}
                   for item in plan['items'] if item['status'] == 'manual']
        # A later confirmation only mentions the regions it left unchecked this
        # time; a region migrated by an earlier confirmation is not "skipped".
        skipped += [{'source_annotation_id': item['source_annotation_id'],
                     'reason': 'not_confirmed', 'message': '确认时未勾选此条批注。'}
                    for item in plan['items']
                    if item['status'] == 'ready' and item['source_annotation_id'] not in include
                    and (current_source['id'], item['source_annotation_id']) not in already]
        if record is None:
            record = {'id': migration_id, 'source_version_id': current_source['id'],
                      'target_version_id': current_target['id'], 'created': now(),
                      'source_annotation_ids': list(annotation_ids),
                      'annotation_ids': [], 'skipped': [], 'registration': plan['registration']}
            records.append(record)
        record['annotation_ids'].extend(item['id'] for item in creation)
        record['skipped'] = skipped
        record['updated'] = now()
        created['record'] = record
        created['count'] = len(creation)

    try:
        updated = store.mutate(key, expected, '迁移区域批注', change)
    except v.Conflict:
        # A parallel confirmation of the same migration may have won the race.
        # If every confirmed region is now present with its provenance, this
        # request is a duplicate rather than a conflict.
        current = store.read(key)
        current_target = store.version(store.page(current, page_id), target_version_id)
        already = existing_by_source(current_target)
        current_source = store.version(store.page(current, page_id), source_version_id)
        if all((current_source['id'], item) in already for item in ready):
            record = next((entry for entry in current_target.get('migrations', [])
                           if entry.get('id') == migration_id), None)
            return outcome(current, record, 0, True, skipped_now)
        raise
    if created.get('duplicate'):
        target = store.version(store.page(updated, page_id), target_version_id)
        record = next((item for item in target.get('migrations', [])
                       if item.get('id') == migration_id), previous)
        return outcome(updated, record, 0, True, skipped_now)
    return outcome(updated, created.get('record'), created.get('count', 0), False, skipped_now)

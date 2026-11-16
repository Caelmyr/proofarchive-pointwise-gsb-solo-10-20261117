"""版面图像相似度：忽略导出分辨率、白边和透明纸白，保留版面结构。

特征在工作分辨率下提取：先按纸白合成透明像素，再裁掉内容外的白边，
然后归一化为固定网格，比较边缘方向、明暗结构、颜色分布和内容长宽比。
颜色权重刻意较低，避免同配色但结构不同的版面占据结果前列。
像素资源 SHA256 相同的版次由调用方单独标记为完全相同，不依赖相似度。
"""
import numpy as np
from PIL import Image, ImageDraw
from .images import paper_rgb

GRID = 48
WORK = 640
VIEW = 900
WHITE_THRESHOLD = 0.045
LUMA = np.array([.2126, .7152, .0722], np.float32)
WEIGHTS = {'edge': .55, 'luminance': .27, 'color': .10, 'aspect': .08}


def _working(image, limit=WORK):
    rgb = paper_rgb(image)
    if max(rgb.size) > limit:
        factor = int(np.ceil(max(rgb.size) / limit))
        if factor > 1:
            try:
                rgb = rgb.reduce(factor)
            except (ValueError, OSError):
                rgb = rgb.copy()
        rgb.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    return rgb


def content_box(rgb):
    """返回忽略白边和透明区域后的内容框；全白图像保留完整画布。"""
    pixels = np.asarray(rgb, dtype=np.float32) / 255.
    mask = np.max(np.abs(pixels - 1.), axis=2) > WHITE_THRESHOLD
    if not mask.any():
        return (0, 0, rgb.width, rgb.height)
    rows, columns = np.nonzero(mask)
    return (int(columns.min()), int(rows.min()),
            int(columns.max()) + 1, int(rows.max()) + 1)


def features(image):
    rgb = _working(image)
    box = content_box(rgb)
    content = rgb.crop(box)
    small = np.asarray(content.resize((GRID, GRID), Image.Resampling.LANCZOS), dtype=np.float32) / 255.
    gray = small @ LUMA
    centered = gray - gray.mean()
    normalized = centered / (centered.std() + 1e-6)
    gradient_x = np.zeros_like(normalized)
    gradient_y = np.zeros_like(normalized)
    gradient_x[:, 1:-1] = normalized[:, 2:] - normalized[:, :-2]
    gradient_y[1:-1, :] = normalized[2:, :] - normalized[:-2, :]
    magnitude = np.hypot(gradient_x, gradient_y)
    edge = (magnitude / (np.linalg.norm(magnitude) + 1e-9)).astype(np.float32)
    luminance = (normalized / (np.linalg.norm(normalized) + 1e-9)).astype(np.float32)
    quantized = np.clip((small * 4).astype(np.int64), 0, 3)
    histogram = np.bincount((quantized[..., 0] * 16 + quantized[..., 1] * 4 + quantized[..., 2]).ravel(),
                            minlength=64).astype(np.float32)
    histogram /= histogram.sum() + 1e-9
    return {'edge': edge.ravel(), 'luminance': luminance.ravel(), 'color': histogram,
            'aspect': float(content.width / max(1, content.height)),
            'content_box': list(box), 'working_size': [rgb.width, rgb.height]}


def score(first, second):
    edge = float(np.clip(np.dot(first['edge'], second['edge']), 0., 1.))
    luminance = float((np.clip(np.dot(first['luminance'], second['luminance']), -1., 1.) + 1.) / 2.)
    color = float(np.minimum(first['color'], second['color']).sum())
    ratio = min(first['aspect'], second['aspect']) / max(first['aspect'], second['aspect'], 1e-9)
    parts = {'edge': edge, 'luminance': luminance, 'color': color, 'aspect': float(ratio)}
    total = sum(WEIGHTS[key] * value for key, value in parts.items())
    parts['score'] = float(np.clip(total, 0., 1.))
    return parts


def normalized_contents(first, second):
    """按内容框裁切并缩放到同一画布，仅用于并排查看，不改变文件。"""
    rgb_first = _working(first, VIEW)
    rgb_second = _working(second, VIEW)
    box_first = content_box(rgb_first)
    box_second = content_box(rgb_second)
    content_first = rgb_first.crop(box_first)
    content_second = rgb_second.crop(box_second)
    width = max(1, content_first.width)
    height = max(1, content_first.height)
    if max(width, height) > VIEW:
        ratio = VIEW / max(width, height)
        width, height = max(1, round(width * ratio)), max(1, round(height * ratio))
    left = content_first.resize((width, height), Image.Resampling.LANCZOS)
    right = content_second.resize((width, height), Image.Resampling.LANCZOS)
    return left, right, {'first_box': list(box_first), 'second_box': list(box_second),
                         'canvas': [width, height],
                         'first_source': list(rgb_first.size), 'second_source': list(rgb_second.size)}


def pair_view(first, second, same_asset=False):
    left, right, geometry = normalized_contents(first, second)
    pixels_left = np.asarray(left, dtype=np.float32) / 255.
    pixels_right = np.asarray(right, dtype=np.float32) / 255.
    difference = np.max(np.abs(pixels_left - pixels_right), axis=2)
    changed = difference > 1 / 255
    total = int(difference.size)
    heat = np.zeros((*difference.shape, 3), dtype=np.float32)
    heat[..., 0] = np.clip(difference * 5, 0, 1)
    heat[..., 1] = np.clip(1 - difference * 3, 0, 1) * .25
    heat[..., 2] = .1
    heat[~changed] = .96
    overlay = pixels_right * .65 + heat * .35
    side = Image.new('RGB', (left.width * 2 + 16, left.height), 'white')
    side.paste(left, (0, 0))
    side.paste(right, (left.width + 16, 0))
    ImageDraw.Draw(side).rectangle([left.width, 0, left.width + 15, left.height], fill='#dce2e9')
    report = {'mode': 'content', 'canvas': [left.width, left.height],
              'changed_pixels': int(changed.sum()), 'compared_pixels': total,
              'changed_fraction': float(changed.sum() / max(total, 1)),
              'mean_difference': float(difference.mean()),
              'max_difference': float(difference.max()),
              'same_asset': bool(same_asset), 'geometry': geometry,
              'note': '先按内容框裁掉白边并归一化尺寸再比较；用于判断版面是否重复，不代表物理像素尺寸。'}
    return {'report': report,
            'side_by_side': side,
            'overlay': Image.fromarray(np.uint8(np.clip(overlay, 0, 1) * 255)),
            'heat': Image.fromarray(np.uint8(np.clip(heat, 0, 1) * 255))}

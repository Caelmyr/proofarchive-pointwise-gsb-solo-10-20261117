import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from . import validation as v
from .images import array, paper_rgb


def place_canvas(image, canvas, offset):
    source = image.convert('RGBA')
    target = Image.new('RGBA', canvas, (255, 255, 255, 0))
    target.alpha_composite(source, dest=offset)
    valid = np.zeros((canvas[1], canvas[0]), bool)
    x, y = offset
    valid[y:y+source.height, x:x+source.width] = True
    return target, valid


def compare_images(first, second, anchor='center'):
    anchor = v.choice(anchor, '对齐方式', ['center', 'top_left'])
    width = max(first.width, second.width)
    height = max(first.height, second.height)
    if width * height > 16000000:
        raise v.Invalid('比较画布超过1600万像素')
    offsets = [(0, 0), (0, 0)]
    if anchor == 'center':
        offsets = [((width-im.width)//2, (height-im.height)//2) for im in (first, second)]
    a, valid_a = place_canvas(first, (width, height), offsets[0])
    b, valid_b = place_canvas(second, (width, height), offsets[1])
    rgb_a, rgb_b = array(a), array(b)
    valid = valid_a | valid_b
    difference = np.max(np.abs(rgb_a - rgb_b), axis=2)
    difference[~valid] = 0
    changed = (difference > 1/255) & valid
    count = int(valid.sum())
    heat = np.zeros((height, width, 3), dtype=np.float32)
    heat[..., 0] = np.clip(difference * 5, 0, 1)
    heat[..., 1] = np.clip(1 - difference * 3, 0, 1) * .25
    heat[..., 2] = .1
    heat[~changed] = .96
    overlay = rgb_b * .65 + heat * .35
    report = {'canvas': [width, height], 'anchor': anchor, 'offsets': [list(o) for o in offsets],
              'changed_pixels': int(changed.sum()), 'compared_pixels': count,
              'changed_fraction': float(changed.sum()/max(count, 1)),
              'mean_difference': float(difference[valid].mean()) if count else 0,
              'max_difference': float(difference.max()),
              'bounds_a': [offsets[0][0], offsets[0][1], first.width, first.height],
              'bounds_b': [offsets[1][0], offsets[1][1], second.width, second.height],
              'note': '按像素居中或左上角比较；不自动配准，不将大小变化隐藏为缩放。'}
    return {'report': report, 'heat': Image.fromarray(np.uint8(np.clip(heat, 0, 1)*255)),
            'overlay': Image.fromarray(np.uint8(np.clip(overlay, 0, 1)*255)),
            'canvas_a': Image.fromarray(np.uint8(np.clip(rgb_a, 0, 1)*255)),
            'canvas_b': Image.fromarray(np.uint8(np.clip(rgb_b, 0, 1)*255)),
            'side_by_side': side_by_side(a, b), 'difference': difference, 'valid': valid}


def diff_regions(difference, valid, cell=8, merge=None, limit=240):
    """把变化像素聚成可审阅的区域：块网格上按有限半径膨胀合并邻近变化，
    相距很远的块不会相连；区域统计精确到原始变化像素。"""
    changed = (difference > 1/255) & valid
    height, width = changed.shape
    grid_w, grid_h = (width + cell - 1)//cell, (height + cell - 1)//cell
    padded = np.zeros((grid_h*cell, grid_w*cell), bool)
    padded[:height, :width] = changed
    counts = padded.reshape(grid_h, cell, grid_w, cell).sum(axis=(1, 3))
    weighted = np.zeros((grid_h*cell, grid_w*cell), np.float32)
    weighted[:height, :width] = np.where(changed, difference, 0)
    blocks = counts > 0
    if not blocks.any():
        return []
    sums = weighted.reshape(grid_h, cell, grid_w, cell).sum(axis=(1, 3))
    peaks = weighted.reshape(grid_h, cell, grid_w, cell).max(axis=(1, 3))
    if merge is None:
        merge = max(2, min(10, round(min(height, width)/300)))
    structure = np.ones((3, 3), bool)
    grown = ndimage.binary_dilation(blocks, structure=structure, iterations=merge)
    labels, total = ndimage.label(grown, structure=structure)
    block_labels = labels[blocks]
    ys, xs = np.nonzero(blocks)
    pixels = np.bincount(block_labels, weights=counts[blocks], minlength=total+1)
    summed = np.bincount(block_labels, weights=sums[blocks], minlength=total+1)
    peak = np.zeros(total+1)
    np.maximum.at(peak, block_labels, peaks[blocks])
    x0 = np.full(total+1, grid_w)
    y0 = np.full(total+1, grid_h)
    x1 = np.full(total+1, -1)
    y1 = np.full(total+1, -1)
    np.minimum.at(x0, block_labels, xs)
    np.minimum.at(y0, block_labels, ys)
    np.maximum.at(x1, block_labels, xs)
    np.maximum.at(y1, block_labels, ys)
    regions = []
    for component in range(1, total+1):
        if not pixels[component]:
            continue
        left, top = x0[component]*cell, y0[component]*cell
        right = min(width, (x1[component]+1)*cell)
        bottom = min(height, (y1[component]+1)*cell)
        regions.append({'bounds': [int(left), int(top), int(right-left), int(bottom-top)],
                        'changed_pixels': int(pixels[component]),
                        'mean_difference': float(summed[component]/pixels[component]),
                        'max_difference': float(peak[component]), 'aggregated': False})
    regions.sort(key=lambda r: (r['bounds'][1], r['bounds'][0]))
    if len(regions) > limit:
        ranked = sorted(regions, key=lambda r: -r['changed_pixels'])
        keep, rest = ranked[:limit-1], ranked[limit-1:]
        keep.sort(key=lambda r: (r['bounds'][1], r['bounds'][0]))
        left = min(r['bounds'][0] for r in rest)
        top = min(r['bounds'][1] for r in rest)
        right = max(r['bounds'][0]+r['bounds'][2] for r in rest)
        bottom = max(r['bounds'][1]+r['bounds'][3] for r in rest)
        total_pixels = sum(r['changed_pixels'] for r in rest)
        regions = keep + [{'bounds': [left, top, right-left, bottom-top], 'changed_pixels': total_pixels,
                           'mean_difference': float(sum(r['mean_difference']*r['changed_pixels'] for r in rest)/total_pixels),
                           'max_difference': float(max(r['max_difference'] for r in rest)),
                           'aggregated': True, 'parts': len(rest)}]
    for index, region in enumerate(regions, 1):
        region['id'] = index
    return regions


def side_by_side(first, second):
    output = Image.new('RGB', (first.width + second.width + 16, max(first.height, second.height)), 'white')
    output.paste(paper_rgb(first), (0, 0))
    output.paste(paper_rgb(second), (first.width + 16, 0))
    draw = ImageDraw.Draw(output)
    draw.rectangle([first.width, 0, first.width+15, output.height], fill='#dce2e9')
    return output

import numpy as np
from PIL import Image, ImageDraw
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
            'side_by_side': side_by_side(a, b), 'difference': difference, 'valid': valid}


def side_by_side(first, second):
    output = Image.new('RGB', (first.width + second.width + 16, max(first.height, second.height)), 'white')
    output.paste(paper_rgb(first), (0, 0))
    output.paste(paper_rgb(second), (first.width + 16, 0))
    draw = ImageDraw.Draw(output)
    draw.rectangle([first.width, 0, first.width+15, output.height], fill='#dce2e9')
    return output

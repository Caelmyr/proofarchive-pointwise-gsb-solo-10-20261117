import numpy as np
from PIL import Image
from . import validation as v
from .images import array


def separate(image, black_generation=1.):
    amount = v.number(black_generation, '黑版生成', 0, 1)
    rgb = array(image)
    complement = 1 - rgb
    black = complement.min(axis=2) * amount
    denominator = 1 - black
    cmy = np.divide(complement - black[..., None], denominator[..., None],
                    out=np.zeros_like(complement), where=denominator[..., None] > 1e-7)
    channels = np.concatenate([np.clip(cmy, 0, 1), black[..., None]], axis=2)
    return channels.astype(np.float32)


def separation_report(image, black_generation=1., ink_limit=300.):
    limit = v.number(ink_limit, '总墨量限制%', 100, 400)
    channels = separate(image, black_generation)
    total = channels.sum(axis=2) * 100
    summary = {'model': '简化sRGB互补色软分色；不代替印厂ICC分色或真实墨水密度',
               'black_generation': float(black_generation), 'ink_limit': limit,
               'mean_coverage_percent': (channels.mean(axis=(0, 1))*100).tolist(),
               'max_total_ink': float(total.max()), 'mean_total_ink': float(total.mean()),
               'over_limit_pixels': int((total > limit).sum()),
               'over_limit_fraction': float((total > limit).mean())}
    return summary, channels, total


def channel_preview(channels, channel):
    channel = v.choice(channel, '分色通道', ['c', 'm', 'y', 'k'])
    index = ['c', 'm', 'y', 'k'].index(channel)
    coverage = channels[..., index]
    color = np.array({'c': [0., 1., 1.], 'm': [1., 0., 1.], 'y': [1., 1., 0.], 'k': [0., 0., 0.]}[channel])
    rgb = 1 - (1-color) * coverage[..., None]
    return Image.fromarray(np.uint8(np.clip(rgb, 0, 1)*255))


def ink_preview(image, total, limit):
    rgb = array(image)
    over = total > limit
    rgb[over] = rgb[over] * .25 + np.array([1., .05, .05]) * .75
    return Image.fromarray(np.uint8(np.clip(rgb, 0, 1)*255))

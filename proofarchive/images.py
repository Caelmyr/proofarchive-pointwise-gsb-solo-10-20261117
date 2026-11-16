import io
import warnings
import numpy as np
from PIL import Image, ImageOps, ImageCms
from . import validation as v


def encode(image):
    out = io.BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


def decode(raw):
    if len(raw) > 48 * 1024 * 1024:
        raise v.Invalid('图像不得超过48MiB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            source = Image.open(io.BytesIO(raw))
            if getattr(source, 'n_frames', 1) != 1:
                raise v.Invalid('请将多页文件逐页导出为PNG、JPEG或TIFF')
            if min(source.size) < 16 or max(source.size) > 6000 or source.width * source.height > 16000000:
                raise v.Invalid('图像每边16–6000，最多1600万像素')
            dpi = source.info.get('dpi', (300., 300.))
            if not isinstance(dpi, (list, tuple)) or len(dpi) < 2:
                dpi = (300., 300.)
            dpi = [float(d) if 10 <= float(d) <= 9600 else 300. for d in dpi[:2]]
            orientation = source.getexif().get(274, 1)
            image = ImageOps.exif_transpose(source)
            if orientation in (5, 6, 7, 8):
                dpi = dpi[::-1]
            profile = source.info.get('icc_profile')
            conversion = 'sRGB assumed'
            if profile:
                try:
                    alpha = image.convert('RGBA').getchannel('A')
                    image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(profile)), ImageCms.createProfile('sRGB'), outputMode='RGB')
                    image.putalpha(alpha)
                    conversion = 'ICC converted to sRGB'
                except (ValueError, OSError) as error:
                    raise v.Invalid('嵌入色彩配置无法转换') from error
            else:
                image = image.convert('RGBA')
            return image, {'pixels': list(image.size), 'dpi': dpi, 'color': conversion, 'source_mode': source.mode, 'orientation': orientation}
    except v.Invalid:
        raise
    except Exception as error:
        raise v.Invalid('无法读取图像') from error


def load(store, version):
    with Image.open(io.BytesIO(store.asset_get(version['asset']))) as image:
        return image.convert('RGBA').copy()


def paper_rgb(image, paper=(255, 255, 255)):
    background = Image.new('RGBA', image.size, tuple(paper) + (255,))
    return Image.alpha_composite(background, image.convert('RGBA')).convert('RGB')


def thumbnail(image, size=900):
    image = image.copy()
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return image


def array(image, paper=(255, 255, 255)):
    return np.asarray(paper_rgb(image, paper), dtype=np.float32) / 255.


def region_pixels(image, bounds):
    bounds = v.rectangle(bounds)
    w, h = image.size
    x0 = max(0, min(w - 1, int(np.floor(bounds[0] * w))))
    y0 = max(0, min(h - 1, int(np.floor(bounds[1] * h))))
    x1 = max(x0 + 1, min(w, int(np.ceil(bounds[2] * w))))
    y1 = max(y0 + 1, min(h, int(np.ceil(bounds[3] * h))))
    return image.crop((x0, y0, x1, y1)), (x0, y0, x1, y1)


def region_statistics(image, bounds):
    region, box = region_pixels(image, bounds)
    pixels = array(region)
    gray = pixels @ np.array([.2126, .7152, .0722], np.float32)
    alpha = np.asarray(region.getchannel('A'), dtype=np.float32) / 255
    result = {'box_pixels': list(box), 'pixel_count': int(gray.size),
              'mean_rgb': pixels.mean(axis=(0, 1)).tolist(),
              'min_rgb': pixels.min(axis=(0, 1)).tolist(),
              'max_rgb': pixels.max(axis=(0, 1)).tolist(),
              'luminance_mean': float(gray.mean()),
              'luminance_std': float(gray.std()),
              'alpha_mean': float(alpha.mean())}
    histogram, edges = np.histogram(gray, 32, range=(0, 1))
    result['luminance_histogram'] = histogram.tolist()
    result['histogram_edges'] = edges.tolist()
    return result

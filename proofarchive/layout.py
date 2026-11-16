import math
from PIL import Image, ImageDraw, ImageFont
from . import validation as v
from .images import load, paper_rgb


def fit(image, width, height):
    ratio = min(width / image.width, height / image.height)
    new_size = (max(1, round(image.width*ratio)), max(1, round(image.height*ratio)))
    fitted = paper_rgb(image).resize(new_size, Image.Resampling.LANCZOS)
    return fitted, ((width-fitted.width)//2, (height-fitted.height)//2)


def contact_sheet(store, document, columns=3, cell_width=300, show_status=True):
    columns = v.integer(columns, '列数', 1, 8)
    cell_width = v.integer(cell_width, '单元宽', 120, 600)
    pages = document['pages']
    if not pages:
        raise v.Invalid('档案暂无页面')
    cell_height = round(cell_width * 1.15)
    rows = math.ceil(len(pages)/columns)
    width, height = columns * cell_width, rows * cell_height
    if width*height > 24000000:
        raise v.Invalid('联系表过大，请减少列宽或档案页数')
    output = Image.new('RGB', (width, height), '#e9edf1')
    draw = ImageDraw.Draw(output)
    placements = []
    for index, page in enumerate(pages):
        version = store.version(page)
        image = load(store, version)
        cell_x = (index % columns) * cell_width
        cell_y = (index // columns) * cell_height
        margin = 14
        slot_w = cell_width - margin*2
        slot_h = cell_height - 62
        fitted, offset = fit(image, slot_w, slot_h)
        x = cell_x + margin + offset[0]
        y = cell_y + margin + offset[1]
        draw.rectangle([cell_x+7, cell_y+7, cell_x+cell_width-7, cell_y+cell_height-7], fill='white')
        output.paste(fitted, (x, y))
        label = f'{index+1:03d}  {page["name"][:28]}'
        # Pillow default font has limited glyphs; the exact Unicode names remain in the JSON index.
        label = label.encode('ascii', errors='replace').decode()
        draw.text((cell_x+margin, cell_y+cell_height-40), label, fill='#253447')
        if show_status:
            draw.text((cell_x+margin, cell_y+cell_height-23), version['status'], fill='#4d6178')
        placements.append({'page_id': page['id'], 'version_id': version['id'], 'name': page['name'],
                           'cell': [cell_x, cell_y, cell_width, cell_height],
                           'image_box': [x, y, fitted.width, fitted.height], 'source_pixels': list(image.size)})
    return output, {'columns': columns, 'rows': rows, 'canvas': [width, height], 'placements': placements}

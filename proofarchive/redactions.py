import numpy as np
from PIL import Image
from . import validation as v
from .store import uid, now


# Delivery images replace every covered pixel with this fully opaque colour.
FILL = (31, 41, 55, 255)
LIMIT = 100


def items(version):
    """Return the redaction list of a version, tolerating older documents."""
    return version.setdefault('redactions', [])


class Redactions:
    def __init__(self, store):
        self.store = store

    def add(self, key, page_id, version_id, values, expected=None):
        values = v.object_value(values)
        redaction = {'id': uid(), 'bounds': v.rectangle(values.get('bounds')),
                     'purpose': v.text(values.get('purpose', ''), '遮挡用途', 500),
                     'created': now(), 'updated': now()}

        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            redactions = items(version)
            if len(redactions) >= LIMIT:
                raise v.Invalid('每个版次最多 %d 处遮敏区域' % LIMIT)
            redactions.append(redaction)

        return self.store.mutate(key, expected, '添加遮敏区域', change)

    def update(self, key, page_id, version_id, redaction_id, values, expected=None):
        values = v.object_value(values)

        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            redaction = next((item for item in items(version) if item['id'] == redaction_id), None)
            if redaction is None:
                raise v.Invalid('遮敏区域不存在')
            if 'bounds' in values:
                redaction['bounds'] = v.rectangle(values['bounds'])
            if 'purpose' in values:
                redaction['purpose'] = v.text(values['purpose'], '遮挡用途', 500)
            redaction['updated'] = now()

        return self.store.mutate(key, expected, '更新遮敏区域', change)

    def remove(self, key, page_id, version_id, redaction_id, expected=None):
        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            redactions = items(version)
            if not any(item['id'] == redaction_id for item in redactions):
                raise v.Invalid('遮敏区域不存在')
            version['redactions'] = [item for item in redactions if item['id'] != redaction_id]

        return self.store.mutate(key, expected, '移除遮敏区域', change)


def pixel_box(bounds, size):
    """Expand a normalised region to every integer pixel it touches.

    Floor for the leading edge and ceiling for the trailing edge guarantee that
    no partially covered pixel at the border keeps its original content.
    """
    width, height = size
    left = max(0, min(width - 1, int(np.floor(bounds[0] * width))))
    top = max(0, min(height - 1, int(np.floor(bounds[1] * height))))
    right = max(left + 1, min(width, int(np.ceil(bounds[2] * width))))
    bottom = max(top + 1, min(height, int(np.ceil(bounds[3] * height))))
    return left, top, right, bottom


def apply(image, redactions):
    """Return a new RGBA image with the regions replaced by an opaque block."""
    output = np.array(image.convert('RGBA'), dtype=np.uint8).copy()
    height, width = output.shape[:2]
    for redaction in redactions or []:
        left, top, right, bottom = pixel_box(redaction['bounds'], (width, height))
        output[top:bottom, left:right] = FILL
    return Image.fromarray(output, 'RGBA')

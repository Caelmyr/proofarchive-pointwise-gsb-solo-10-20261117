import copy
from PIL import Image
from . import validation as v
from .store import uid, now
from .images import load, encode, region_pixels


def rotate_bounds(bounds, turns):
    x0, y0, x1, y1 = bounds
    for _ in range(turns % 4):
        x0, y0, x1, y1 = 1 - y1, x0, 1 - y0, x1
    return [x0, y0, x1, y1]


def transform_annotations(annotations, crop, turns):
    left, top, right, bottom = crop
    result = []
    for item in annotations:
        bounds = item['bounds']
        x0, y0 = max(bounds[0], left), max(bounds[1], top)
        x1, y1 = min(bounds[2], right), min(bounds[3], bottom)
        if x1 <= x0 or y1 <= y0:
            continue
        updated = copy.deepcopy(item)
        updated['id'] = uid()
        normalized = [(x0-left)/(right-left), (y0-top)/(bottom-top),
                      (x1-left)/(right-left), (y1-top)/(bottom-top)]
        updated['bounds'] = rotate_bounds(normalized, turns)
        linchpin = updated.get('provenance')
        if isinstance(linchpin, dict) and linchpin.get('kind') == 'migration':
            # The derived version has its own annotation identity, but it still
            # came from the same original annotation.
            linchpin['derived_from_annotation_id'] = item['id']
        result.append(updated)
    return result


def reanchor_derived(version):
    """Point copied migration records at this version's own annotation copies."""
    mapping = {item['provenance']['derived_from_annotation_id']: item['id']
               for item in version.get('annotations', [])
               if isinstance(item.get('provenance'), dict)
               and item['provenance'].get('derived_from_annotation_id')}
    for record in version.get('migrations', []):
        record['annotation_ids'] = [mapping.get(item, item) for item in record['annotation_ids']]


def derived_version(store, version, image, label, provenance):
    result = copy.deepcopy(version)
    result.update(id=uid(), parent=version['id'], label=label, asset=store.asset_put(encode(image)),
                  pixels=list(image.size), created=now(), status='unreviewed', reviews=[], source=provenance)
    return result


def transform(store, document, page_id, version_id, crop, rotation):
    crop = v.rectangle(crop)
    rotation = v.choice(rotation, '顺时针旋转', [0, 90, 180, 270])
    page = store.page(document, page_id)
    source = store.version(page, version_id)
    original = load(store, source)
    image, box = region_pixels(original, crop)
    actual = [box[0]/original.width, box[1]/original.height, box[2]/original.width, box[3]/original.height]
    turns = rotation // 90
    if rotation:
        image = image.rotate(-rotation, expand=True)
    version = derived_version(store, source, image, source['label'] + ' · 裁切校样',
                              {'kind': 'transform', 'crop': actual, 'rotation': rotation})
    version['annotations'] = transform_annotations(source['annotations'], actual, turns)
    # Migration records are historical facts about the version they ran on.
    # Keep them intact on the derived version and point their annotation links
    # at this version's copies where a copied annotation still exists.
    version['migrations'] = copy.deepcopy(source.get('migrations', []))
    reanchor_derived(version)
    # A record may only link to annotations that this derived version kept.
    # Its source_annotation_ids still point into the version the migration ran
    # from, which is unchanged by the crop.
    kept = {item['id'] for item in version['annotations']}
    for record in version['migrations']:
        record['annotation_ids'] = [item for item in record['annotation_ids'] if item in kept]
    version['migrations'] = [record for record in version['migrations']
                             if record['annotation_ids']]
    source_size = source['trim_mm']
    width = source_size[0] * (actual[2]-actual[0])
    height = source_size[1] * (actual[3]-actual[1])
    version['trim_mm'] = [height, width] if turns % 2 else [width, height]
    version['dpi'] = source['dpi'][::-1] if turns % 2 else source['dpi'][:]
    version['bleed_mm'] = [0., 0., 0., 0.]
    page['versions'].append(version)
    page['current'] = version['id']
    return version

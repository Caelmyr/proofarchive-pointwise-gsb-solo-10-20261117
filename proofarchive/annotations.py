from PIL import ImageDraw
from . import validation as v
from .store import uid, now
from .images import paper_rgb


COLORS = {'issue': '#ed594e', 'note': '#387bd1', 'approved': '#289e6a'}


class Annotations:
    def __init__(self, store):
        self.store = store

    def add(self, key, page_id, version_id, values, expected=None):
        values = v.object_value(values)
        annotation = {'id': uid(), 'bounds': v.rectangle(values.get('bounds')),
                      'text': v.text(values.get('text', ''), '批注', 3000),
                      'kind': v.choice(values.get('kind', 'issue'), '批注类型', list(COLORS)),
                      'resolved': False, 'created': now(), 'updated': now()}
        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            if len(version['annotations']) >= 200:
                raise v.Invalid('每版最多200条批注')
            version['annotations'].append(annotation)
        return self.store.mutate(key, expected, '添加区域批注', change)

    def update(self, key, page_id, version_id, annotation_id, values, expected=None):
        values = v.object_value(values)
        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            annotation = next((a for a in version['annotations'] if a['id'] == annotation_id), None)
            if annotation is None:
                raise v.Invalid('批注不存在')
            if 'bounds' in values:
                annotation['bounds'] = v.rectangle(values['bounds'])
            if 'text' in values:
                annotation['text'] = v.text(values['text'], '批注', 3000)
            if 'kind' in values:
                annotation['kind'] = v.choice(values['kind'], '批注类型', list(COLORS))
            if 'resolved' in values:
                if not isinstance(values['resolved'], bool):
                    raise v.Invalid('批注解决状态需要布尔值')
                annotation['resolved'] = values['resolved']
            annotation['updated'] = now()
        return self.store.mutate(key, expected, '更新区域批注', change)

    def remove(self, key, page_id, version_id, annotation_id, expected=None):
        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            if not any(a['id'] == annotation_id for a in version['annotations']):
                raise v.Invalid('批注不存在')
            version['annotations'] = [a for a in version['annotations'] if a['id'] != annotation_id]
        return self.store.mutate(key, expected, '移除区域批注', change)


def overlay(image, annotations, resolved=True, highlight_id=None):
    output = paper_rgb(image)
    draw = ImageDraw.Draw(output)
    width, height = output.size
    for index, annotation in enumerate(annotations):
        if annotation['resolved'] and not resolved:
            continue
        x0, y0, x1, y1 = annotation['bounds']
        box = [round(x0 * width), round(y0 * height), round(x1 * width), round(y1 * height)]
        highlighted = annotation['id'] == highlight_id
        if highlighted:
            color = '#f5a623'
        elif annotation['resolved']:
            color = '#8b949e'
        else:
            color = COLORS[annotation['kind']]
        line_width = max(2, round(width / 300))
        if highlighted:
            draw.rectangle([max(0, box[0] - 5), max(0, box[1] - 5),
                            min(width, box[2] + 5), min(height, box[3] + 5)],
                           outline='#f5a623', width=line_width + 3)
        draw.rectangle(box, outline=color, width=line_width)
        label = str(index + 1)
        badge = [box[0], box[1], box[0] + 25, box[1] + 20]
        draw.rectangle(badge, fill=color)
        draw.text((box[0] + 5, box[1] + 3), label, fill='white')
    return output

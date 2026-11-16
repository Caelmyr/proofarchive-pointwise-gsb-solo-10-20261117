import copy
from . import validation as v
from .store import uid, now
from .images import decode, encode


class Catalog:
    def __init__(self, store):
        self.store = store

    def import_page(self, key, raw, filename, expected=None, page_id=None, label=None):
        image, metadata = decode(raw)
        asset = self.store.asset_put(encode(image))
        version = {'id': uid(), 'asset': asset, 'label': v.text(label or filename, '版次名'),
                   'created': now(), 'pixels': list(image.size), 'dpi': metadata['dpi'],
                   'trim_mm': [image.width / metadata['dpi'][0] * 25.4, image.height / metadata['dpi'][1] * 25.4],
                   'bleed_mm': [0., 0., 0., 0.], 'annotations': [], 'redactions': [], 'status': 'unreviewed',
                   'reviews': [], 'source': {'filename': filename, 'metadata': metadata}, 'parent': None}
        def change(document):
            if page_id:
                page = self.store.page(document, page_id)
                version['parent'] = page['current']
                page['versions'].append(version)
                page['current'] = version['id']
            else:
                if len(document['pages']) >= 200:
                    raise v.Invalid('每个档案最多200页')
                document['pages'].append({'id': uid(), 'name': v.text(filename, '页面名'), 'current': version['id'], 'versions': [version]})
        return self.store.mutate(key, expected, '导入版面图像' if not page_id else '新增页面版次', change)

    def set_current(self, key, page_id, version_id, expected=None):
        def change(document):
            page = self.store.page(document, page_id)
            self.store.version(page, version_id)
            page['current'] = version_id
        return self.store.mutate(key, expected, '切换当前版次', change)

    def configure(self, key, page_id, version_id, settings, expected=None):
        settings = v.object_value(settings)
        def change(document):
            page = self.store.page(document, page_id)
            version = self.store.version(page, version_id)
            if 'label' in settings:
                version['label'] = v.text(settings['label'], '版次名')
            if 'trim_mm' in settings:
                dimensions = v.list_value(settings['trim_mm'], 2)
                if len(dimensions) != 2:
                    raise v.Invalid('成品尺寸需要宽高')
                version['trim_mm'] = [v.number(x, '成品尺寸mm', 1, 5000) for x in dimensions]
            if 'bleed_mm' in settings:
                edges = v.list_value(settings['bleed_mm'], 4)
                if len(edges) != 4:
                    raise v.Invalid('出血需要左上右下四个值')
                version['bleed_mm'] = [v.number(x, '出血mm', 0, 100) for x in edges]
            if 'name' in settings:
                page['name'] = v.text(settings['name'], '页面名')
        return self.store.mutate(key, expected, '编辑页面规格', change)

    def reorder(self, key, page_ids, expected=None):
        ids = v.list_value(page_ids)
        def change(document):
            existing = {p['id']: p for p in document['pages']}
            if len(ids) != len(existing) or len(set(ids)) != len(ids) or set(ids) != set(existing):
                raise v.Invalid('页面顺序必须恰好包含当前所有页面')
            document['pages'] = [existing[key] for key in ids]
        return self.store.mutate(key, expected, '调整页序', change)

    def remove(self, key, page_id, expected=None):
        def change(document):
            self.store.page(document, page_id)
            document['pages'] = [page for page in document['pages'] if page['id'] != page_id]
        return self.store.mutate(key, expected, '移除页面', change)

    def review(self, key, page_id, version_id, status, note, expected=None):
        status = v.choice(status, '审校状态', ['unreviewed', 'changes', 'approved'])
        note = v.text(note, '审校意见', 3000, True)
        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            version['status'] = status
            version['reviews'].append({'at': now(), 'status': status, 'note': note})
        return self.store.mutate(key, expected, '记录版次审校', change)

    def duplicate_page(self, key, page_id, expected=None):
        def change(document):
            if len(document['pages']) >= 200:
                raise v.Invalid('每个档案最多200页')
            source = self.store.page(document, page_id)
            copied = copy.deepcopy(source)
            copied['id'] = uid()
            copied['name'] += ' 副本'
            mapping = {version['id']: uid() for version in copied['versions']}
            for version in copied['versions']:
                old = version['id']
                version['id'] = mapping[old]
                version['parent'] = mapping.get(version.get('parent'))
                for annotation in version['annotations']:
                    annotation['id'] = uid()
                for redaction in version.get('redactions', []):
                    redaction['id'] = uid()
            copied['current'] = mapping[copied['current']]
            document['pages'].append(copied)
        return self.store.mutate(key, expected, '复制页面', change)

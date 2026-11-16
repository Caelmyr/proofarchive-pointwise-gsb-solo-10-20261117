import copy
import hashlib
import io
import json
import zipfile
from . import validation as v
from .store import uid, now


def export_package(store, document):
    content = copy.deepcopy(document)
    content['snapshots'] = []
    manifest = {'format': 'proofarchive-1', 'document': content, 'assets': {}}
    assets = {version['asset'] for page in document['pages'] for version in page['versions']}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for digest in sorted(assets):
            raw = store.asset_get(digest)
            manifest['assets'][digest] = {'path': 'assets/'+digest+'.png', 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
            archive.writestr('assets/'+digest+'.png', raw)
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    return output.getvalue()


def import_package(store, raw):
    if len(raw) > 192*1024*1024:
        raise v.Invalid('档案包不得超过192MiB')
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise v.Invalid('不是有效ZIP档案包') from error
    with archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 1000:
            raise v.Invalid('档案包文件重复或过多')
        if sum(info.file_size for info in archive.infolist()) > 384*1024*1024:
            raise v.Invalid('档案包解压容量过大')
        try:
            manifest = json.loads(archive.read('manifest.json'))
        except (KeyError, ValueError) as error:
            raise v.Invalid('档案包清单不可读取') from error
        if manifest.get('format') != 'proofarchive-1':
            raise v.Invalid('不支持此档案格式')
        document = v.object_value(manifest.get('document'))
        pages = v.list_value(document.get('pages'), 200)
        name = v.text(document.get('name'), '档案名')
        assets = v.object_value(manifest.get('assets'))
        restored_assets = {}
        for digest, entry in assets.items():
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise v.Invalid('档案资源编号无效')
            path = 'assets/'+digest+'.png'
            if entry.get('path') != path:
                raise v.Invalid('档案资源路径不匹配')
            image = archive.read(path)
            if hashlib.sha256(image).hexdigest() != digest or entry.get('sha256') != digest:
                raise v.Invalid('档案资源校验失败')
            from .images import decode
            decode(image)
            restored_assets[digest] = store.asset_put(image)
        copied = copy.deepcopy(pages)
        page_ids = set()
        version_ids = set()
        page_mapping = {}
        version_mapping = {}
        for page in copied:
            old_page_id = page['id']
            if old_page_id in page_ids:
                raise v.Invalid('档案页身份重复')
            page_ids.add(old_page_id)
            page['id'] = uid()
            page_mapping[old_page_id] = page['id']
            page['name'] = v.text(page['name'], '页面名')
            versions = v.list_value(page.get('versions'), 100)
            mapping = {}
            for version in versions:
                old_id = version['id']
                if old_id in version_ids:
                    raise v.Invalid('档案版次身份重复')
                version_ids.add(old_id)
                mapping[old_id] = uid()
            version_mapping.update(mapping)
            if page['current'] not in mapping:
                raise v.Invalid('档案当前版次缺失')
            page['current'] = mapping[page['current']]
            for version in versions:
                old_id = version['id']
                version['id'] = mapping[old_id]
                version['parent'] = mapping.get(version.get('parent'))
                if version['asset'] not in restored_assets:
                    raise v.Invalid('档案版次缺少像素资源')
                version['asset'] = restored_assets[version['asset']]
                if len(version.get('trim_mm', [])) != 2:
                    raise v.Invalid('档案物理尺寸缺失')
                version['trim_mm'] = [v.number(x, '成品尺寸', 1, 5000) for x in version['trim_mm']]
                for annotation in version.get('annotations', []):
                    annotation['id'] = uid()
                    annotation['bounds'] = v.rectangle(annotation['bounds'])
                    annotation['text'] = v.text(annotation['text'], '批注', 3000)
        from .reviews import STATUSES
        restored_reviews = []
        for record in v.list_value(document.get('comparison_reviews', []), 5000):
            if not isinstance(record, dict):
                raise v.Invalid('区域审阅记录格式无效')
            if (record.get('page_id') not in page_mapping or record.get('version_a') not in version_mapping
                    or record.get('version_b') not in version_mapping):
                continue
            bounds = record.get('bounds')
            if not isinstance(bounds, list) or len(bounds) != 4:
                raise v.Invalid('区域审阅记录边界无效')
            restored_reviews.append({'id': uid(), 'page_id': page_mapping[record['page_id']],
                'version_a': version_mapping[record['version_a']], 'version_b': version_mapping[record['version_b']],
                'anchor': v.choice(record.get('anchor'), '对齐方式', ['center', 'top_left']),
                'region': v.integer(record.get('region'), '区域编号', 1, 100000),
                'bounds': [v.number(x, '区域边界', 0, 16000) for x in bounds],
                'changed_pixels': v.integer(record.get('changed_pixels'), '变化像素', 0, 16000000),
                'status': v.choice(record.get('status'), '区域审阅状态', STATUSES),
                'note': v.text(record.get('note', ''), '区域审阅意见', 1000, True), 'updated': now()})
        created = store.create(name+' · 导入', document.get('notes', ''))
        def restore(target):
            target['pages'] = copied
            target['comparison_reviews'] = restored_reviews
            target['settings'] = copy.deepcopy(document.get('settings', {'paper': [210., 297.], 'bleed_mm': 3.}))
        return store.mutate(created['id'], 0, '从校样档案包导入', restore)

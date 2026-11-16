import copy
import hashlib
import io
import json
import zipfile
from . import validation as v
from .store import uid, now


def _restore_provenance(annotation, versions, annotations):
    provenance = annotation.get('provenance')
    if provenance is None:
        return
    if not isinstance(provenance, dict) or provenance.get('kind') != 'migration':
        raise v.Invalid('档案批注来源信息无效')
    source_version_id = provenance.get('source_version_id')
    source_annotation_id = provenance.get('source_annotation_id')
    if source_version_id not in versions or source_annotation_id not in annotations:
        raise v.Invalid('档案批注来源关联缺失')
    provenance['source_version_id'] = versions[source_version_id]
    provenance['source_annotation_id'] = annotations[source_annotation_id]
    provenance['source_bounds'] = v.rectangle(provenance.get('source_bounds'))
    provenance['migration_id'] = v.text(provenance.get('migration_id'), '迁移编号', 64)
    if {'method', 'confidence', 'migrated_at'} - set(provenance):
        raise v.Invalid('档案批注来源信息不完整')
    provenance['confidence'] = v.number(provenance.get('confidence'), '迁移置信度', 0, 1)
    provenance['migrated_at'] = v.text(provenance.get('migrated_at'), '迁移时间', 40)
    provenance['method'] = v.choice(provenance.get('method'), '迁移方式', ['registration', 'template'])
    derived = provenance.get('derived_from_annotation_id')
    if derived is not None:
        if derived not in annotations:
            raise v.Invalid('档案批注派生关联缺失')
        provenance['derived_from_annotation_id'] = annotations[derived]
    annotation['provenance'] = provenance


def _restore_migration(record, versions, annotations):
    if not isinstance(record, dict):
        raise v.Invalid('档案迁移记录无效')
    record['id'] = v.text(record.get('id'), '迁移编号', 64)
    for field in ('source_version_id', 'target_version_id'):
        if record.get(field) not in versions:
            raise v.Invalid('档案迁移记录版次缺失')
        record[field] = versions[record[field]]
    for field in ('source_annotation_ids', 'annotation_ids'):
        values = v.list_value(record.get(field, []), 200)
        if any(item not in annotations for item in values):
            raise v.Invalid('档案迁移记录批注关联缺失')
        record[field] = [annotations[item] for item in values]
    skipped = v.list_value(record.get('skipped', []), 200)
    for item in skipped:
        if not isinstance(item, dict):
            raise v.Invalid('档案迁移待处理记录无效')
        if item.get('source_annotation_id') not in annotations:
            raise v.Invalid('档案迁移待处理批注关联缺失')
        item['source_annotation_id'] = annotations[item['source_annotation_id']]
        item['reason'] = v.text(item.get('reason') or 'manual', '迁移待处理原因', 40)
        item['message'] = v.text(item.get('message') or '待人工处理', '迁移待处理说明', 300)
    record['skipped'] = skipped
    record['created'] = v.text(record.get('created'), '迁移时间', 40)
    if 'updated' in record:
        record['updated'] = v.text(record['updated'], '迁移时间', 40)
    return record


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
        for page in copied:
            old_page_id = page['id']
            if old_page_id in page_ids:
                raise v.Invalid('档案页身份重复')
            page_ids.add(old_page_id)
            page['id'] = uid()
            page['name'] = v.text(page['name'], '页面名')
            versions = v.list_value(page.get('versions'), 100)
            mapping = {}
            all_annotation_ids = {}
            for version in versions:
                old_id = version['id']
                if old_id in version_ids:
                    raise v.Invalid('档案版次身份重复')
                version_ids.add(old_id)
                mapping[old_id] = uid()
                for annotation in version.get('annotations', []):
                    old_annotation_id = annotation.get('id')
                    if not isinstance(old_annotation_id, str) or old_annotation_id in all_annotation_ids:
                        raise v.Invalid('档案批注身份重复')
                    all_annotation_ids[old_annotation_id] = uid()
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
                migrations = version.get('migrations', [])
                if not isinstance(migrations, list):
                    raise v.Invalid('档案迁移记录无效')
                for annotation in version.get('annotations', []):
                    annotation['id'] = all_annotation_ids[annotation['id']]
                    annotation['bounds'] = v.rectangle(annotation['bounds'])
                    annotation['text'] = v.text(annotation['text'], '批注', 3000)
                    _restore_provenance(annotation, mapping, all_annotation_ids)
                version['migrations'] = [
                    _restore_migration(record, mapping, all_annotation_ids)
                    for record in migrations]
                # Drop possible duplicate migration records that appeared in
                # hand-edited manifests; identity is the source/target/selection.
                seen = set()
                kept = []
                for record in version['migrations']:
                    if record['id'] in seen:
                        continue
                    seen.add(record['id'])
                    kept.append(record)
                version['migrations'] = kept
        created = store.create(name+' · 导入', document.get('notes', ''))
        def restore(target):
            target['pages'] = copied
            target['settings'] = copy.deepcopy(document.get('settings', {'paper': [210., 297.], 'bleed_mm': 3.}))
        return store.mutate(created['id'], 0, '从校样档案包导入', restore)

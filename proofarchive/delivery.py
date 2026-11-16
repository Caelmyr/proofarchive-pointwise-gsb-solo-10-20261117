import hashlib
import io
import json
import re
import time
import zipfile
from . import validation as v
from .images import load, encode
from .redactions import apply, LIMIT


ID = re.compile(r'[a-f0-9]{24}')


def selection(values):
    """Read the per-page selection: [{page_id, version_id, redaction_ids}]."""
    items = v.list_value(values.get('pages'), 200)
    if not items:
        raise v.Invalid('请至少选择一个页面')
    pages = []
    seen = set()
    for item in items:
        item = v.object_value(item)
        page_id = item.get('page_id')
        if not isinstance(page_id, str) or not ID.fullmatch(page_id):
            raise v.Invalid('页面编号无效')
        if page_id in seen:
            raise v.Invalid('页面不能重复选择')
        seen.add(page_id)
        version_id = item.get('version_id')
        if version_id is not None and (not isinstance(version_id, str) or not ID.fullmatch(version_id)):
            raise v.Invalid('版次编号无效')
        ids = v.list_value(item.get('redaction_ids'), LIMIT)
        if len(set(ids)) != len(ids):
            raise v.Invalid('遮敏区域不能重复选择')
        for item_id in ids:
            if not isinstance(item_id, str) or not ID.fullmatch(item_id):
                raise v.Invalid('遮敏区域编号无效')
        pages.append({'page_id': page_id, 'version_id': version_id, 'redaction_ids': ids})
    if not any(item['redaction_ids'] for item in pages):
        raise v.Invalid('请至少选择一处遮敏区域')
    return pages


def plan(document, values):
    """Resolve a selection against each page's *current* version.

    A page whose selected version is no longer current, or whose requested
    regions no longer exist on the current version, is reported and skipped.
    Coordinates are never borrowed from another version.
    """
    pages = selection(values)
    by_id = {page['id']: page for page in document['pages']}
    versions = {version['id']: (page, version)
                for page in document['pages'] for version in page['versions']}
    entries = []
    issues = []
    stale = {}
    for item in pages:
        page = by_id.get(item['page_id'])
        if page is None:
            issues.append({'page_id': item['page_id'], 'reason': '页面已被移除，无法交付'})
            continue
        current = next((version for version in page['versions'] if version['id'] == page['current']), None)
        if current is None:
            issues.append({'page_id': page['id'], 'page_name': page['name'], 'reason': '此页当前版次不存在'})
            continue
        chosen_version = current
        if item['version_id'] and item['version_id'] != page['current']:
            old = versions.get(item['version_id'])
            if old is None or old[0]['id'] != page['id']:
                issues.append({'page_id': page['id'], 'page_name': page['name'],
                               'reason': '所选版次不属于此页，已停止；不会套用其他版次的位置。'})
                stale[page['id']] = sorted(item['redaction_ids'])
                continue
            issues.append({'page_id': page['id'], 'page_name': page['name'], 'stale': len(item['redaction_ids']),
                           'reason': '此页当前版次已从「%s」变为「%s」。所选遮挡属于旧版次，不适用于本次交付；请在当前版次重新框选。'
                                     % (old[1]['label'], current['label'])})
            stale[page['id']] = sorted(item['redaction_ids'])
            continue
        present = {redaction['id'] for redaction in chosen_version.get('redactions', [])}
        missing = [item_id for item_id in item['redaction_ids'] if item_id not in present]
        if missing:
            issues.append({'page_id': page['id'], 'page_name': page['name'], 'stale': len(missing),
                           'reason': '当前版次「%s」缺少 %d 处所选遮挡（可能已删除或改属其他版次）；请重新框选。'
                                     % (chosen_version['label'], len(missing))})
            stale[page['id']] = sorted(missing)
            continue
        redactions = [redaction for redaction in chosen_version.get('redactions', [])
                      if redaction['id'] in set(item['redaction_ids'])]
        if not redactions:
            issues.append({'page_id': page['id'], 'page_name': page['name'], 'reason': '「%s」的当前版次没有所选遮敏区域' % page['name']})
            continue
        entries.append({'page': page, 'version': chosen_version, 'redactions': redactions})
    return entries, issues, stale


def render(store, entry):
    return apply(load(store, entry['version']), entry['redactions'])


def describe(document, entries, notes=True):
    """Verification rows for a delivery.

    Internal reports keep each region's purpose note. The external package is
    built with notes=False so no review wording can travel with the images.
    """
    rows = []
    for number, entry in enumerate(entries, 1):
        page, version = entry['page'], entry['version']
        redactions = [{'bounds': list(item['bounds']),
                       **({'purpose': item['purpose']} if notes else {})}
                      for item in entry['redactions']]
        position = next((index for index, item in enumerate(document['pages'], 1)
                         if item['id'] == page['id']), None)
        rows.append({'sequence': number, 'page_number': position, 'page_id': page['id'],
                     'page_name': page['name'], 'version_id': version['id'],
                     'version_label': version['label'], 'pixels': list(version['pixels']),
                     'redaction_count': len(entry['redactions']), 'redactions': redactions})
    return rows


def summary(document, entries, issues=()):
    rows = describe(document, entries)
    return {'format': 'proofarchive-redaction-delivery-1', 'document_id': document['id'],
            'document_name': document['name'], 'revision': document['revision'],
            'page_count': len(rows), 'redaction_count': sum(row['redaction_count'] for row in rows),
            'pages': rows, 'issues': list(issues),
            'ready': bool(rows) and not issues,
            'note': '仅含所选遮敏图与核对清单；不包含原始像素、未遮敏版本、缩略图或批注文字。'}


def filename(number, version):
    return 'redacted-%02d-%s.png' % (number, version['id'])


def bundle(store, document, entries):
    rows = describe(document, entries, notes=False)
    manifest = {'format': 'proofarchive-redaction-delivery-1',
                'generated': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'document': {'id': document['id'], 'name': document['name'], 'revision': document['revision']},
                'pages': rows, 'totals': {'page_count': len(rows),
                                          'redaction_count': sum(row['redaction_count'] for row in rows)},
                'note': '每个交付图已把遮敏区域替换为不透明色块。清单供外部校对核对页码、版次、区域位置与遮挡数量；不含原图、缩略图、未遮敏版本、批注文字或内部遮挡用途备注。'}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for number, entry in enumerate(entries, 1):
            raw = encode(render(store, entry))
            row = manifest['pages'][number - 1]
            row['file'] = filename(number, entry['version'])
            row['sha256'] = hashlib.sha256(raw).hexdigest()
            row['bytes'] = len(raw)
            archive.writestr(row['file'], raw)
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    return output.getvalue()


class Delivery:
    def __init__(self, store):
        self.store = store

    def resolve(self, key, values):
        values = v.object_value(values)
        document = self.store.read(key)
        entries, issues, stale = plan(document, values)
        return document, entries, issues, stale

    def report(self, key, values):
        "Dry run used by the UI. Returns what is applicable, what is stale, and what blocks."
        document, entries, issues, stale = self.resolve(key, values)
        result = summary(document, entries, issues)
        result['stale'] = stale
        return result

    def preview(self, key, values):
        document, entries, issues, _ = self.resolve(key, values)
        if issues:
            raise v.Invalid(issues[0]['reason'])
        page_id = values.get('page_id') or entries[0]['page']['id']
        entry = next((item for item in entries if item['page']['id'] == page_id), None)
        if entry is None:
            raise v.Invalid('预览页面不在本次交付范围内')
        return render(self.store, entry)

    def package(self, key, values):
        document, entries, issues, _ = self.resolve(key, values)
        if issues:
            raise v.Invalid(issues[0]['reason'])
        if not entries:
            raise v.Invalid('没有可交付的遮敏图')
        return bundle(self.store, document, entries)

import copy
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from . import validation as v


def uid():
    return secrets.token_hex(12)


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


class ArchiveStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets = self.root / 'objects'
        self.assets.mkdir(exist_ok=True)
        self.documents = self.root / 'documents'
        self.documents.mkdir(exist_ok=True)
        self.lock = threading.RLock()

    def path(self, key):
        return self.documents / (v.identifier(key) + '.json')

    def read(self, key):
        with self.lock:
            path = self.path(key)
            if not path.exists():
                raise v.Invalid('档案不存在')
            return json.loads(path.read_text())

    def write(self, document):
        payload = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        target = self.path(document['id'])
        temporary = target.with_suffix('.' + uid() + '.tmp')
        with temporary.open('w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return copy.deepcopy(document)

    def list(self):
        result = []
        with self.lock:
            for path in self.documents.glob('*.json'):
                data = json.loads(path.read_text())
                result.append(self.summary(data))
        return sorted(result, key=lambda d: d['updated'], reverse=True)

    def summary(self, document):
        return {key: copy.deepcopy(document[key]) for key in (
            'id', 'name', 'notes', 'revision', 'created', 'updated') } | {
            'page_count': len(document['pages']),
            'version_count': sum(len(p['versions']) for p in document['pages'])}

    def create(self, name, notes=''):
        with self.lock:
            document = {'id': uid(), 'name': v.text(name, '档案名'),
                        'notes': v.text(notes, '备注', 3000, True), 'revision': 0,
                        'created': now(), 'updated': now(), 'pages': [],
                        'history': [], 'snapshots': [], 'comparison_reviews': [],
                        'settings': {'paper': [210., 297.], 'bleed_mm': 3.}}
            return self.write(document)

    def mutate(self, key, expected, label, action):
        with self.lock:
            document = self.read(key)
            if expected is not None and expected != document['revision']:
                raise v.Conflict('档案已更新，请刷新后重试')
            action(document)
            document['revision'] += 1
            document['updated'] = now()
            document['history'].append({'revision': document['revision'], 'at': now(), 'action': label})
            document['history'] = document['history'][-500:]
            return self.write(document)

    def asset_put(self, raw):
        digest = hashlib.sha256(raw).hexdigest()
        path = self.assets / (digest + '.png')
        with self.lock:
            if not path.exists():
                temporary = path.with_suffix('.' + uid() + '.tmp')
                temporary.write_bytes(raw)
                os.replace(temporary, path)
        return digest

    def asset_get(self, digest):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise v.Invalid('图像资源编号无效')
        path = self.assets / (digest + '.png')
        if not path.exists():
            raise v.Invalid('图像资源丢失')
        return path.read_bytes()

    def snapshot(self, key, expected, name):
        def change(document):
            content = {key: copy.deepcopy(document[key]) for key in ('name', 'notes', 'pages', 'settings')}
            document['snapshots'].append({'id': uid(), 'name': v.text(name, '快照名'), 'at': now(), 'content': content})
            if len(document['snapshots']) > 30:
                raise v.Invalid('每个档案最多30个快照')
        return self.mutate(key, expected, '保存快照', change)

    def restore(self, key, expected, snapshot_id):
        def change(document):
            snapshot = next((s for s in document['snapshots'] if s['id'] == snapshot_id), None)
            if snapshot is None:
                raise v.Invalid('快照不存在')
            for field in ('name', 'notes', 'pages', 'settings'):
                document[field] = copy.deepcopy(snapshot['content'][field])
        return self.mutate(key, expected, '恢复快照', change)

    def page(self, document, page_id):
        page = next((p for p in document['pages'] if p['id'] == page_id), None)
        if page is None:
            raise v.Invalid('页面不存在')
        return page

    def version(self, page, version_id=None):
        target = version_id or page['current']
        version = next((version for version in page['versions'] if version['id'] == target), None)
        if version is None:
            raise v.Invalid('版次不存在')
        return version

"""端到端测试：连续批注审阅、审阅包导入导出与旧档案兼容性。

直接以 Flask test client 驱动，不依赖运行中的服务；临时数据目录彼此隔离。
"""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, '/opt/pointwise-env/site-packages')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from image_lab import create_app  # noqa: E402
from proofarchive import install  # noqa: E402
from proofarchive.images import encode  # noqa: E402


def png(size=(640, 900), color='white'):
    return encode(Image.new('RGBA', size, color))


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app = create_app(os.path.join(self.tmp.name, 'img'))
        install(app, os.path.join(self.tmp.name, 'arc'))
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def create_document_with_annotations(self):
        r = self.client.post('/archive/documents', json={'name': '审阅测试手册'})
        key = r.get_json()['id']
        # 两页：竖版与横版，批注包含贴边框
        self.client.post(f'/archive/documents/{key}/upload',
                         data={'file': (io.BytesIO(png((640, 900))), 'p1.png')},
                         content_type='multipart/form-data')
        self.client.post(f'/archive/documents/{key}/upload',
                         data={'file': (io.BytesIO(png((900, 640))), 'p2.png')},
                         content_type='multipart/form-data')
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        p1, p2 = doc['pages']
        specs = [
            (p1, [0.10, 0.10, 0.40, 0.20], '第一条问题', 'issue', False),
            (p1, [0.0, 0.95, 0.25, 1.0], '贴左上角的问题', 'issue', False),
            (p1, [0.70, 0.80, 0.98, 0.95], '普通备注', 'note', True),
        ]
        ids = []
        for page, bounds, text, kind, resolved in specs:
            r = self.client.post(
                f'/archive/documents/{key}/pages/{page["id"]}/annotations',
                json={'version_id': page['current'], 'bounds': bounds, 'text': text, 'kind': kind})
            self.assertEqual(r.status_code, 200)
            doc = self.client.get(f'/archive/documents/{key}').get_json()
            target = next(p for p in doc['pages'] if p['id'] == page['id'])
            ann = target['versions'][0]['annotations'][-1]
            ids.append(ann['id'])
            if resolved:
                self.client.patch(
                    f'/archive/documents/{key}/pages/{page["id"]}/annotations/{ann["id"]}',
                    json={'version_id': target['current'], 'resolved': True})
        r = self.client.post(
            f'/archive/documents/{key}/pages/{p2["id"]}/annotations',
            json={'version_id': p2['current'], 'bounds': [0.85, 0.0, 1.0, 0.3],
                  'text': '横版贴边问题', 'kind': 'issue'})
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        p2fresh = next(p for p in doc['pages'] if p['id'] == p2['id'])
        ids.append(p2fresh['versions'][0]['annotations'][-1]['id'])
        return key, ids

    def load_package(self, response):
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        manifest = json.loads(archive.read('review.json'))
        return archive, manifest

    def test_queue_follows_page_order_and_filters_resolved(self):
        key, ids = self.create_document_with_annotations()
        q = self.client.get(f'/archive/documents/{key}/review-queue').get_json()
        self.assertEqual(q['unresolved_count'], 3)
        self.assertEqual(q['total'], 3)
        pages = [it['page_number'] for it in q['items']]
        self.assertEqual(pages, [1, 1, 2])
        self.assertEqual(q['items'][0]['annotation']['id'], ids[0])
        self.assertEqual(q['items'][1]['annotation']['bounds'], [0.0, 0.95, 0.25, 1.0])
        self.assertEqual(q['items'][2]['orientation'], 'landscape')
        all_q = self.client.get(f'/archive/documents/{key}/review-queue?scope=all').get_json()
        self.assertEqual(all_q['total'], 4)
        self.assertTrue(any(it['annotation']['resolved'] for it in all_q['items']))

    def test_crop_keeps_context_for_edge_boxes_and_orientations(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        # 竖版贴边框
        edge = next(a for p in doc['pages'] for ver in p['versions'] for a in ver['annotations']
                    if a['bounds'] == [0.0, 0.95, 0.25, 1.0])
        page = doc['pages'][0]
        r = self.client.get(
            f'/archive/documents/{key}/pages/{page["id"]}/annotations/{edge["id"]}/crop'
            f'?version={page["current"]}')
        self.assertEqual(r.status_code, 200)
        image = Image.open(io.BytesIO(r.data))
        self.assertLessEqual(image.width, 960)
        self.assertLessEqual(image.height, 760)
        # 贴边取景必须补出白色上下文边，而非把框裁到画布边缘
        self.assertEqual(image.convert('RGB').getpixel((2, image.height // 2)), (255, 255, 255))
        # 横版画幅的局部图
        landscape_page = doc['pages'][1]
        r = self.client.get(
            f'/archive/documents/{key}/pages/{landscape_page["id"]}/annotations/'
            f'{landscape_page["versions"][0]["annotations"][0]["id"]}'
            f'/crop?version={landscape_page["current"]}')
        self.assertEqual(r.status_code, 200)
        self.assertLess(Image.open(io.BytesIO(r.data)).height, 760)

    def test_highlight_overlay_and_legacy_preview(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        page = doc['pages'][0]
        plain = self.client.get(
            f'/archive/documents/{key}/pages/{page["id"]}/image?version={page["current"]}')
        highlighted = self.client.get(
            f'/archive/documents/{key}/pages/{page["id"]}/image?version={page["current"]}'
            f'&annotations=1&highlight={ids[0]}')
        self.assertEqual(plain.status_code, highlighted.status_code, 200)
        self.assertNotEqual(plain.data, highlighted.data)

    def test_export_package_structure(self):
        key, ids = self.create_document_with_annotations()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        self.assertEqual(manifest['format'], 'proofarchive-review-2')
        self.assertEqual(manifest['entry_count'], 4)
        self.assertEqual([e['sequence'] for e in manifest['entries']], [1, 2, 3, 4])
        first = manifest['entries'][0]
        for field in ('page_name', 'version_label', 'annotation_number', 'bounds',
                      'text', 'resolved', 'review_note', 'annotation_id', 'page_id', 'version_id'):
            self.assertIn(field, first)
        self.assertIn('crops/' + first['annotation_id'] + '.png', archive.namelist())
        crop = Image.open(io.BytesIO(archive.read('crops/' + first['annotation_id'] + '.png')))
        self.assertTrue(crop.width > 0 and crop.height > 0)
        csv_text = archive.read('index.csv').decode()
        self.assertIn('页名', csv_text)
        self.assertIn('版次', csv_text)
        self.assertIn('处理状态', csv_text)

    def reimport(self, key, manifest, files, revision):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as out:
            for name, raw in files.items():
                if name == 'review.json':
                    out.writestr(name, json.dumps(manifest, ensure_ascii=False).encode())
                else:
                    out.writestr(name, raw)
        return self.client.post(
            f'/archive/documents/{key}/review-package/import',
            data={'file': (io.BytesIO(buf.getvalue()), 'pack.zip'), 'revision': revision},
            content_type='multipart/form-data')

    def test_import_applies_only_matched_and_is_idempotent(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files = {n: archive.read(n) for n in archive.namelist()}
        manifest['entries'][0]['resolved'] = True
        manifest['entries'][0]['review_note'] = '同事确认已修改'
        r = self.reimport(key, manifest, files, doc['revision'])
        self.assertEqual(r.status_code, 200)
        summary = r.get_json()['summary']
        self.assertEqual(summary['applied'], 1)
        self.assertEqual(summary['unchanged'], 3)
        self.assertEqual(summary['skipped'], 0)
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        updated = next(a for p in doc['pages'] for ver in p['versions'] for a in ver['annotations']
                       if a['id'] == ids[0])
        self.assertTrue(updated['resolved'])
        self.assertEqual(updated['review_note'], '同事确认已修改')
        self.assertIn('review_export_id', updated)
        # 批注总数不变：绝不重复建批注
        total = sum(len(ver['annotations']) for p in doc['pages'] for ver in p['versions'])
        self.assertEqual(total, 4)
        # 再次导回同一包 -> 重复，不产生新写入
        r2 = self.reimport(key, manifest, files, doc['revision'])
        summary2 = r2.get_json()['summary']
        self.assertEqual(summary2['duplicate'], 1)
        self.assertEqual(summary2['applied'], 0)
        self.assertEqual(r2.get_json()['document']['revision'], doc['revision'])

    def test_import_stale_revision_conflicts(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files = {n: archive.read(n) for n in archive.namelist()}
        manifest['entries'][0]['resolved'] = True
        r = self.reimport(key, manifest, files, doc['revision'] - 1)
        self.assertEqual(r.status_code, 409)

    def test_import_skips_when_annotation_deleted(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files = {n: archive.read(n) for n in archive.namelist()}
        # 删除第一条批注，再导回指向它的结论
        page = doc['pages'][0]
        self.client.delete(
            f'/archive/documents/{key}/pages/{page["id"]}/annotations/{ids[0]}',
            json={'version_id': page['current']})
        manifest['entries'][0]['resolved'] = True
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        r = self.reimport(key, manifest, files, doc['revision'])
        summary = r.get_json()['summary']
        self.assertEqual(summary['applied'], 0)
        reasons = {x['annotation_id']: x['reason'] for x in summary['results']}
        self.assertIn('对应批注已删除', reasons[ids[0]])
        total = sum(len(ver['annotations']) for p in doc['pages'] for ver in p['versions'])
        after = self.client.get(f'/archive/documents/{key}').get_json()
        total_after = sum(len(ver['annotations']) for p in after['pages'] for ver in p['versions'])
        self.assertEqual(total, total_after)

    def test_import_skips_when_version_changed(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files = {n: archive.read(n) for n in archive.namelist()}
        page = doc['pages'][0]
        target_version_id = page['current']
        # 在存储层直接移除该版次，模拟导回时原批注所属版次已不存在
        store = self.client.application.extensions['proofarchive']
        stored = store.read(key)
        target_page = next(p for p in stored['pages'] if p['id'] == page['id'])
        target_page['versions'] = [v for v in target_page['versions'] if v['id'] != target_version_id]
        if target_page['versions']:
            target_page['current'] = target_page['versions'][0]['id']
        store.write(stored)
        manifest['entries'][0]['resolved'] = True
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        r = self.reimport(key, manifest, files, doc['revision'])
        summary = r.get_json()['summary']
        reasons = [x['reason'] or '' for x in summary['results']]
        self.assertTrue(any('版次' in reason for reason in reasons))
        self.assertEqual(summary['applied'], 0)

    def test_import_rejects_bounds_mismatch(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files = {n: archive.read(n) for n in archive.namelist()}
        entry = manifest['entries'][0]
        entry['resolved'] = True
        entry['bounds'] = [0.5, 0.5, 0.9, 0.9]  # 人为改动位置，结论不得套走
        r = self.reimport(key, manifest, files, doc['revision'])
        summary = r.get_json()['summary']
        self.assertEqual(summary['applied'], 0)
        self.assertEqual(summary['skipped'], 1)
        self.assertIn('原图位置', summary['results'][0]['reason'])

    def test_import_rejects_foreign_and_duplicate_entries(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files = {n: archive.read(n) for n in archive.namelist()}
        # 包内重复条目
        manifest['entries'].append(dict(manifest['entries'][0]))
        r = self.reimport(key, manifest, files, doc['revision'])
        summary = r.get_json()['summary']
        self.assertEqual(summary['skipped'], 1)
        self.assertIn('重复', summary['results'][-1]['reason'])
        # 来自其它档案：重新取一份未改动的包
        archive2, manifest2 = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files2 = {n: archive2.read(n) for n in archive2.namelist()}
        manifest2['document_id'] = 'a' * 24
        r = self.reimport(key, manifest2, files2, doc['revision'])
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(x['status'] == 'skipped' for x in r.get_json()['summary']['results']))
        self.assertIn('其它档案', r.get_json()['summary']['results'][0]['reason'])

    def test_import_rejects_bad_payloads(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        archive, manifest = self.load_package(
            self.client.get(f'/archive/documents/{key}/review-package'))
        files = {n: archive.read(n) for n in archive.namelist()}
        # 非法 resolved
        manifest['entries'][0]['resolved'] = 'yes'
        r = self.reimport(key, manifest, files, doc['revision'])
        self.assertEqual(r.get_json()['summary']['results'][0]['status'], 'skipped')
        # 非 ZIP
        r = self.client.post(
            f'/archive/documents/{key}/review-package/import',
            data={'file': (io.BytesIO(b'not a zip'), 'x.zip'), 'revision': doc['revision']},
            content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)
        # 缺少文件
        r = self.client.post(f'/archive/documents/{key}/review-package/analyze')
        self.assertEqual(r.status_code, 400)
        # 错误的包格式
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('review.json', json.dumps({'format': 'other'}))
        r = self.client.post(
            f'/archive/documents/{key}/review-package/analyze',
            data={'file': (io.BytesIO(bad.getvalue()), 'x.zip')},
            content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def test_legacy_annotation_crud_and_archive_package_still_work(self):
        key, ids = self.create_document_with_annotations()
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        page = doc['pages'][0]
        # 普通页面批注框的增改删仍然可用
        r = self.client.post(
            f'/archive/documents/{key}/pages/{page["id"]}/annotations',
            json={'version_id': page['current'], 'bounds': [0.2, 0.2, 0.6, 0.4],
                  'text': '新增批注', 'kind': 'note'})
        self.assertEqual(r.status_code, 200)
        new_id = r.get_json()  # document
        doc = self.client.get(f'/archive/documents/{key}').get_json()
        ann = doc['pages'][0]['versions'][0]['annotations'][-1]
        r = self.client.patch(
            f'/archive/documents/{key}/pages/{page["id"]}/annotations/{ann["id"]}',
            json={'version_id': page['current'], 'text': '改过的内容'})
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(
            f'/archive/documents/{key}/pages/{page["id"]}/annotations/{ann["id"]}',
            json={'version_id': page['current']})
        self.assertEqual(r.status_code, 200)
        # 旧档案包导出/导入不受影响
        r = self.client.get(f'/archive/documents/{key}/package')
        self.assertEqual(r.status_code, 200)
        r = self.client.post('/archive/import',
                             data={'file': (io.BytesIO(r.data), 'legacy.zip')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        imported = r.get_json()
        self.assertEqual(len(imported['pages']), 2)
        # 导入的旧档案仍可生成审阅队列与审阅包
        rq = self.client.get(f'/archive/documents/{imported["id"]}/review-queue')
        self.assertEqual(rq.status_code, 200)
        rp = self.client.get(f'/archive/documents/{imported["id"]}/review-package')
        self.assertEqual(rp.status_code, 200)

    def test_demo_data_supports_full_flow(self):
        r = self.client.post('/archive/demo', json={})
        self.assertEqual(r.status_code, 200)
        key = r.get_json()['id']
        q = self.client.get(f'/archive/documents/{key}/review-queue').get_json()
        self.assertGreaterEqual(q['total'], 6)
        orientations = {it['orientation'] for it in q['items']}
        self.assertEqual(orientations, {'portrait', 'landscape'})


if __name__ == '__main__':
    unittest.main(verbosity=2)

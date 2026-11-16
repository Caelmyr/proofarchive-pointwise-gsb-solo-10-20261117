import io
import os
import sys
import tempfile
import time
import unittest

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from image_lab import create_app  # noqa: E402
from proofarchive import install  # noqa: E402


def layout(index=0, scale=1.0, margin=0, color='#165e83', transparent=False, variant='same'):
    width, height = round(640 * scale), round(900 * scale)
    offset = round(margin * scale)
    background = (255, 255, 255, 0) if transparent else (255, 255, 255, 255)
    image = Image.new('RGBA', (width, height), background)
    draw = ImageDraw.Draw(image)

    def rect(x0, y0, x1, y1, fill):
        draw.rectangle([offset + round(x0 * scale), offset + round(y0 * scale),
                        offset + round(x1 * scale), offset + round(y1 * scale)], fill=fill)

    if variant == 'same':
        rect(35, 35, 605, 350, color)
        for line in range(12):
            rect(65, 420 + line * 25, 575 - (line % 3) * 35, 427 + line * 25, '#8393a5')
        rect(65, 760, 260, 820, color)
        if index:
            rect(400, 600, 580, 780, '#e6b44b')
    elif variant == 'moved':
        rect(35, 550, 605, 865, color)
        for line in range(12):
            rect(65, 60 + line * 25, 575 - (line % 3) * 35, 67 + line * 25, '#8393a5')
        rect(380, 420, 575, 480, color)
    else:
        rect(35, 35, 300, 430, color)
        rect(335, 35, 605, 430, '#8393a5')
        for line in range(6):
            rect(65, 500 + line * 40, 575 - line * 40, 510 + line * 40, '#253447')
    return image


class SimilarSearchTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.app = create_app(os.path.join(self.root, 'lab'))
        install(self.app, os.path.join(self.root, 'archive'))
        self.client = self.app.test_client()

    def document(self, name):
        response = self.client.post('/archive/documents', json={'name': name})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def upload(self, key, image, name, page=None):
        buffer = io.BytesIO()
        image.save(buffer, 'PNG')
        buffer.seek(0)
        data = {'file': (buffer, name)}
        if page:
            data['page_id'] = page
        response = self.client.post('/archive/documents/' + key + '/upload', data=data,
                                    content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def search(self, key, page_id, version_id, scope, limit=None):
        payload = {'version_id': version_id, 'scope': scope}
        if limit is not None:
            payload['limit'] = limit
        response = self.client.post('/archive/documents/' + key + '/pages/' + page_id + '/similar',
                                    json=payload)
        self.assertEqual(response.status_code, 202, response.get_json())
        job_id = response.get_json()['id']
        deadline = time.time() + 30
        while time.time() < deadline:
            job = self.client.get('/archive/similar/' + job_id).get_json()
            if job['status'] not in ('queued', 'running', 'cancelling'):
                return job
            time.sleep(.02)
        self.fail('检索未在预期时间内结束')

    def test_variants_rank_above_different_layout(self):
        document = self.document('变体档案')
        document = self.upload(document['id'], layout(), 'original.png')
        page_id = document['pages'][0]['id']
        version_id = document['pages'][0]['versions'][0]['id']
        for image, name in [
                (layout(scale=1.5), 'resized.png'),
                (layout(margin=24), 'white-border.png'),
                (layout(transparent=True), 'transparent.png'),
                (layout(color='#ce6c48'), 'recolored.png'),
                (layout(variant='moved'), 'same-colors-moved.png'),
                (layout(variant='blocks'), 'same-colors-other.png')]:
            document = self.upload(document['id'], image, name)
        job = self.search(document['id'], page_id, version_id, 'document')
        self.assertEqual(job['status'], 'completed')
        rows = {row['page_name']: row for row in job['results']}
        for name in ('resized.png', 'white-border.png', 'transparent.png'):
            self.assertGreater(rows[name]['score'], .97, name)
        for name in ('same-colors-moved.png', 'same-colors-other.png'):
            self.assertLess(rows[name]['score'], rows['transparent.png']['score'] - .3, name)

    def test_exact_assets_and_cross_document_scope(self):
        first = self.document('档案甲')
        first = self.upload(first['id'], layout(), 'sample.png')
        page_id = first['pages'][0]['id']
        version_id = first['pages'][0]['versions'][0]['id']
        second = self.document('档案乙')
        second = self.upload(second['id'], layout(), 'copy.png')
        job = self.search(first['id'], page_id, version_id, 'all')
        exact = [row for row in job['results'] if row['exact']]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]['document_name'], '档案乙')
        self.assertEqual(job['results'][0]['exact'], True)
        self.assertTrue(all(not row['exact'] for row in job['results'][1:]))

    def test_search_does_not_mutate_archive(self):
        document = self.document('只读档案')
        document = self.upload(document['id'], layout(), 'one.png')
        document = self.upload(document['id'], layout(index=1), 'two.png')
        page = document['pages'][0]
        before = self.client.get('/archive/documents/' + document['id']).get_json()
        self.search(document['id'], page['id'], page['versions'][0]['id'], 'document')
        after = self.client.get('/archive/documents/' + document['id']).get_json()
        self.assertEqual(before, after)

    def test_scope_current_lists_each_version_once(self):
        document = self.document('版次档案')
        page = self.upload(document['id'], layout(), 'base.png')
        page_id = page['pages'][0]['id']
        first_version = page['pages'][0]['versions'][0]['id']
        page = self.upload(document['id'], layout(index=1), 'second.png', page=page_id)
        second_version = page['pages'][0]['versions'][-1]['id']
        self.upload(document['id'], layout(variant='moved'), 'other.png')
        job = self.search(document['id'], page_id, first_version, 'current')
        identities = [(row['page_id'], row['version_id']) for row in job['results']]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertNotIn((page_id, first_version), identities)
        self.assertIn((page_id, second_version), identities)
        job = self.search(document['id'], page_id, first_version, 'document')
        versions = [row['version_id'] for row in job['results'] if row['page_id'] == page_id]
        self.assertEqual(sorted(versions), [second_version])
        job = self.search(document['id'], page_id, second_version, 'document')
        versions = [row['version_id'] for row in job['results'] if row['page_id'] == page_id]
        self.assertEqual(sorted(versions), [first_version])

    def test_cancel_keeps_completed_results_and_hides_archive(self):
        document = self.document('大档案')
        page = self.upload(document['id'], layout(), 'base.png')
        page_id = page['pages'][0]['id']
        version_id = page['pages'][0]['versions'][0]['id']
        for index in range(1, 25):
            self.upload(document['id'], layout(index=index), f'scan-{index}.png')
        response = self.client.post('/archive/documents/' + document['id'] + '/pages/' + page_id + '/similar',
                                    json={'version_id': version_id, 'scope': 'document'})
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()['id']
        response = self.client.post('/archive/similar/' + job_id + '/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.get_json()['status'], ('cancelling', 'cancelled'))
        deadline = time.time() + 30
        while time.time() < deadline:
            job = self.client.get('/archive/similar/' + job_id).get_json()
            if job['status'] not in ('queued', 'running', 'cancelling'):
                break
            time.sleep(.02)
        self.assertEqual(job['status'], 'cancelled')
        self.assertLess(job['scanned'], job['total'])
        for row in job['results']:
            self.assertEqual(row['document_name'], '大档案')

    def test_pair_view_reports_and_renders(self):
        document = self.document('并排档案')
        page = self.upload(document['id'], layout(), 'base.png')
        page_id = page['pages'][0]['id']
        version_id = page['pages'][0]['versions'][0]['id']
        self.upload(document['id'], layout(scale=1.5), 'resized.png')
        job = self.search(document['id'], page_id, version_id, 'document')
        index = next(index for index, row in enumerate(job['results']) if not row['exact'])
        report = self.client.get('/archive/similar/' + job['id'] + '/pair/' + str(index))
        self.assertEqual(report.status_code, 200)
        body = report.get_json()
        self.assertLess(body['changed_fraction'], .2)
        for view in ('side_by_side', 'overlay', 'heat'):
            response = self.client.get('/archive/similar/' + job['id'] + '/pair/' + str(index) + '?view=' + view)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'image/png')
        self.assertEqual(self.client.get('/archive/similar/' + job['id'] + '/pair/999').status_code, 400)


if __name__ == '__main__':
    unittest.main()

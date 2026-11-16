"""分张校样打印（tiling）后端测试：几何、标记约束、清单与 HTTP 接口。

直接运行：python3 -I tests/test_tiling.py
"""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 运行环境可能把第三方包装在 PYTHONPATH 指向的独立目录；隔离模式（-I）下需显式加入
for candidate in ('/opt/pointwise-env/site-packages',):
    if Path(candidate).is_dir() and candidate not in sys.path:
        sys.path.append(candidate)

from PIL import Image  # noqa: E402

from image_lab import create_app  # noqa: E402
from proofarchive import tiling  # noqa: E402
from proofarchive import validation as v  # noqa: E402
from proofarchive.images import encode  # noqa: E402


def make_board(width=800, height=600, color=(22, 94, 131, 255)):
    image = Image.new('RGBA', (width, height), 'white')
    for index in range(6):
        Image.Image.paste(image, Image.new('RGBA', (120, 24), color),
                          (40, 60 + index * 60))
    return image


class TilingGeometryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from proofarchive.store import ArchiveStore
        from proofarchive.catalog import Catalog
        self.store = ArchiveStore(self.tmp.name + '/archive')
        self.catalog = Catalog(self.store)
        self.doc = self.store.create('分张测试', '')
        self.image = make_board()
        self.doc = self.catalog.import_page(self.doc['id'], encode(self.image), 'board.png')
        self.page = self.doc['pages'][0]
        self.version = self.store.version(self.page)
        self.version['trim_mm'] = [1200.0, 800.0]

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, **overrides):
        args = {'paper': 'A4', 'orientation': 'landscape', 'margin': 10,
                'overlap': 8, 'dpi': 96}
        args.update(overrides)
        return tiling.parse_args(args)

    def plan(self, **overrides):
        settings = self.settings(**overrides)
        return tiling.build_plan(self.version, self.image.size, settings)

    def test_full_coverage_without_gaps_or_stretch(self):
        plan = self.plan()
        self.assertGreater(plan['cols'], 1)
        xs = sorted({(t['board_coverage_mm'][0], t['board_coverage_mm'][2])
                     for t in plan['tiles'] if t['row'] == 1})
        ys = sorted({(t['board_coverage_mm'][1], t['board_coverage_mm'][3])
                     for t in plan['tiles'] if t['col'] == 1})
        self.assertEqual(xs[0][0], 0.0)
        self.assertEqual(ys[0][0], 0.0)
        self.assertAlmostEqual(xs[-1][1], 1200.0)
        self.assertAlmostEqual(ys[-1][1], 800.0)
        for earlier, later in zip(xs, xs[1:]):
            self.assertAlmostEqual(earlier[1] - later[0], plan['overlap_mm'])
        for earlier, later in zip(ys, ys[1:]):
            self.assertAlmostEqual(earlier[1] - later[0], plan['overlap_mm'])
        # 末张窄条不拉伸：所有分张纸像素完全一致，窄条只占内容区一部分
        self.assertEqual(len({t['content_used_mm'][0] for t in plan['tiles']}),
                         len({t['board_coverage_mm'][2] - t['board_coverage_mm'][0]
                              for t in plan['tiles']}))
        self.assertTrue(any(t['narrow'] for t in plan['tiles']))

    def test_all_tiles_same_pixel_size_and_dpi(self):
        plan = self.plan()
        rendered = [tiling.render_tile(self.store, self.page, self.version, plan, t, self.doc['name'])
                    for t in plan['tiles'][:4]]
        self.assertEqual(len({im.size for im in rendered}), 1)
        self.assertEqual(rendered[0].size, tuple(plan['sheet_pixels']))

    def test_marks_keep_clear_of_finished_content(self):
        plan = self.plan()
        tile = next(t for t in plan['tiles'] if t['col'] < plan['cols'] and t['row'] < plan['rows'])
        image = tiling.render_tile(self.store, self.page, self.version, plan, tile, self.doc['name'])
        ox, oy, box_r, box_b = plan['content_box_px']
        # 留白区域必须保持纸白（页号文字只占顶部留白的小部分，取角落采样）
        self.assertEqual(image.getpixel((2, 2)), (255, 255, 255))
        self.assertEqual(image.getpixel((image.width - 2, 2)), (255, 255, 255))
        # 内容框四角仍在纸内
        self.assertTrue(0 <= ox < box_r <= image.width)
        self.assertTrue(0 <= oy < box_b <= image.height)

    def test_overview_and_manifest(self):
        plan = self.plan()
        overview = tiling.render_overview(plan)
        self.assertEqual(overview.mode, 'RGB')
        manifest = tiling.manifest(self.page, self.version, plan, self.doc['name'])
        self.assertEqual(manifest['sheet_count'], len(manifest['tiles']))
        self.assertEqual(manifest['tiles'][0]['page_no'], 1)
        self.assertEqual(manifest['tiles'][-1]['page_no'], plan['sheet_count'])
        csv_text = tiling.manifest_csv(plan).decode('utf-8-sig')
        self.assertIn('page_no,row,col,file', csv_text)
        self.assertEqual(len(csv_text.strip().splitlines()), plan['sheet_count'] + 1)

    def test_bundle_contents_and_dpi(self):
        plan = self.plan()
        raw = tiling.build_bundle(self.store, self.page, self.version, plan, self.doc['name'])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            self.assertIn('manifest.json', names)
            self.assertIn('manifest.csv', names)
            self.assertIn('assembly.txt', names)
            self.assertIn('overview.png', names)
            self.assertEqual(sum(1 for n in names if n.startswith('tile-')), plan['sheet_count'])
            first = archive.read(plan['tiles'][0]['file'])
            with Image.open(io.BytesIO(first)) as tile_image:
                self.assertAlmostEqual(tile_image.info['dpi'][0], plan['print_dpi'], delta=0.2)
            manifest = json.loads(archive.read('manifest.json'))
            self.assertTrue(all('sha256' in t for t in manifest['tiles']))

    def test_single_sheet_needs_no_overlap(self):
        self.version['trim_mm'] = [260.0, 170.0]
        plan = self.plan(overlap=0, dpi=72)
        self.assertEqual(plan['sheet_count'], 1)
        self.assertEqual(plan['tiles'][0]['board_coverage_mm'], [0.0, 0.0, 260.0, 170.0])
        self.assertEqual([w['code'] for w in plan['warnings']], [])

    def test_auto_orientation_picks_aspect_match(self):
        self.version['trim_mm'] = [400.0, 250.0]
        self.assertEqual(self.plan(orientation='auto')['orientation'], 'landscape')
        self.version['trim_mm'] = [250.0, 400.0]
        self.assertEqual(self.plan(orientation='auto')['orientation'], 'portrait')

    def test_validation_errors(self):
        with self.assertRaises(v.Invalid):
            self.plan(margin=110)                       # 留白占满纸张
        with self.assertRaises(v.Invalid):
            self.plan(margin=60, overlap=40, paper='A5')  # 重叠大于有效短边
        # 单张像素过多：900x1000mm 自定义纸在 150 DPI 下已超 2400 万
        with self.assertRaises(v.Invalid):
            self.plan(dpi=150, paper='custom', paper_w=900, paper_h=1000,
                      orientation='portrait', margin=5, overlap=0)
        with self.assertRaises(v.Invalid):
            tiling.parse_args({'paper': 'A2'})
        with self.assertRaises(v.Invalid):
            tiling.parse_args({'dpi': 40})

    def test_warns_on_upscale_and_narrow_strip(self):
        plan = self.plan(dpi=200)
        codes = {item['code'] for item in plan['warnings']}
        self.assertIn('upscale', codes)
        self.assertIn('narrow_strip', codes)

    def test_original_versions_untouched(self):
        stored = self.store.read(self.doc['id'])
        before = json.dumps(stored, sort_keys=True)
        self.plan()
        tiling.build_bundle(self.store, self.page, self.version, self.plan(), self.doc['name'])
        reread = json.dumps(self.store.read(self.doc['id']), sort_keys=True)
        self.assertEqual(before, reread)


class TilingApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name + '/lab')
        from proofarchive import install
        install(self.app, self.tmp.name + '/archive')
        self.client = self.app.test_client()
        created = self.client.post('/archive/demo')
        self.doc = created.get_json()
        self.page_id = self.doc['pages'][0]['id']
        self.version_id = self.doc['pages'][0]['current']

    def tearDown(self):
        self.tmp.cleanup()

    def base(self, suffix):
        return f'/archive/documents/{self.doc["id"]}/pages/{self.page_id}' + suffix

    def test_plan_tile_manifest_bundle_endpoints(self):
        query = f'version={self.version_id}&paper=A3&orientation=landscape&margin=12&overlap=10&dpi=90'
        plan = self.client.get(self.base(f'/tiling/plan?{query}')).get_json()
        self.assertEqual(plan['format'], 'proofarchive-tiling-1')
        self.assertGreaterEqual(plan['sheet_count'], 1)
        overview = self.client.get(self.base(f'/tiling/overview?{query}'))
        self.assertEqual(overview.mimetype, 'image/png')
        tile = self.client.get(self.base(f'/tiling/tile?{query}&row=1&col=1'))
        self.assertEqual(tile.status_code, 200)
        self.assertEqual(tile.mimetype, 'image/png')
        manifest = self.client.get(self.base(f'/tiling/manifest?{query}')).get_json()
        self.assertEqual(manifest['tiles'][0]['row'], 1)
        csv_resp = self.client.get(self.base(f'/tiling/manifest?{query}&format=csv'))
        self.assertEqual(csv_resp.status_code, 200)
        bundle = self.client.get(self.base(f'/tiling/bundle?{query}'))
        self.assertEqual(bundle.mimetype, 'application/zip')
        self.assertEqual(bundle.status_code, 200)

    def test_bad_request_returns_400(self):
        resp = self.client.get(self.base(
            f'/tiling/plan?version={self.version_id}&paper=A4&margin=110&overlap=0'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

    def test_contact_sheet_still_works(self):
        resp = self.client.get(f'/archive/documents/{self.doc["id"]}/contact?columns=2&width=240')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, 'image/png')


if __name__ == '__main__':
    unittest.main(verbosity=2)

import io
import json
import os
from pathlib import Path
from flask import Blueprint, Response, jsonify, request, render_template, send_file
from PIL import Image, ImageDraw
from . import validation as v
from .store import ArchiveStore
from .catalog import Catalog
from .annotations import Annotations, overlay
from .images import load, encode, thumbnail, region_statistics
from .geometry import transform
from .compare import compare_images
from .separations import separation_report, channel_preview, ink_preview
from .preflight import inspect_document
from .layout import contact_sheet
from .packages import export_package, import_package
from . import review as review_service


def body():
    return v.object_value(request.get_json(silent=True))


def image_response(image):
    return Response(encode(image), mimetype='image/png', headers={'Cache-Control': 'no-store'})


def download(raw, name, mimetype):
    return send_file(io.BytesIO(raw), as_attachment=True, download_name=name, mimetype=mimetype)


def install(app, root):
    store = ArchiveStore(root)
    catalog = Catalog(store)
    annotations = Annotations(store)
    app.extensions['proofarchive'] = store
    api = Blueprint('archive', __name__, url_prefix='/archive')

    def get_version(key, page_id, version_id=None):
        document = store.read(key)
        page = store.page(document, page_id)
        version = store.version(page, version_id)
        return document, page, version

    @api.get('/documents')
    def list_documents():
        return jsonify(store.list())

    @api.post('/documents')
    def create_document():
        values = body()
        return jsonify(store.create(values.get('name'), values.get('notes', '')))

    @api.get('/documents/<key>')
    def get_document(key):
        return jsonify(store.read(key))

    @api.patch('/documents/<key>')
    def edit_document(key):
        values = body()
        def change(document):
            if 'name' in values:
                document['name'] = v.text(values['name'], '档案名')
            if 'notes' in values:
                document['notes'] = v.text(values['notes'], '备注', 3000, True)
        return jsonify(store.mutate(key, values.get('revision'), '编辑档案信息', change))

    @api.post('/documents/<key>/upload')
    def upload(key):
        file = request.files.get('file')
        if file is None:
            raise v.Invalid('请选择图像')
        revision = request.form.get('revision')
        revision = int(revision) if revision is not None else None
        return jsonify(catalog.import_page(key, file.read(), file.filename or 'Untitled', revision,
                                           request.form.get('page_id'), request.form.get('label')))

    @api.post('/documents/<key>/order')
    def reorder(key):
        values = body()
        return jsonify(catalog.reorder(key, values.get('page_ids'), values.get('revision')))

    @api.delete('/documents/<key>/pages/<page_id>')
    def remove_page(key, page_id):
        values = body()
        return jsonify(catalog.remove(key, page_id, values.get('revision')))

    @api.post('/documents/<key>/pages/<page_id>/duplicate')
    def duplicate_page(key, page_id):
        values = body()
        return jsonify(catalog.duplicate_page(key, page_id, values.get('revision')))

    @api.post('/documents/<key>/pages/<page_id>/current')
    def current(key, page_id):
        values = body()
        return jsonify(catalog.set_current(key, page_id, values.get('version_id'), values.get('revision')))

    @api.patch('/documents/<key>/pages/<page_id>/versions/<version_id>')
    def configure(key, page_id, version_id):
        values = body()
        return jsonify(catalog.configure(key, page_id, version_id, values, values.get('revision')))

    @api.get('/documents/<key>/pages/<page_id>/image')
    def preview(key, page_id):
        _, _, version = get_version(key, page_id, request.args.get('version'))
        image = load(store, version)
        if request.args.get('annotations') == '1':
            highlight = request.args.get('highlight')
            image = overlay(image, version['annotations'], highlight_id=highlight)
        return image_response(thumbnail(image, v.integer(request.args.get('size', 1200), '预览尺寸', 64, 2400)))

    @api.get('/documents/<key>/review-queue')
    def review_queue(key):
        document = store.read(key)
        scope = request.args.get('scope', 'unresolved')
        v.choice(scope, '审阅范围', ['unresolved', 'all'])
        return jsonify(review_service.build_queue(document, store, scope == 'unresolved'))

    @api.get('/documents/<key>/pages/<page_id>/annotations/<annotation_id>/crop')
    def annotation_crop(key, page_id, annotation_id):
        _, _, version = get_version(key, page_id, request.args.get('version'))
        annotation = next((a for a in version['annotations'] if a['id'] == annotation_id), None)
        if annotation is None:
            raise v.Invalid('批注不存在')
        return image_response(review_service.crop_image(store, version, annotation))

    @api.get('/documents/<key>/review-package')
    def review_package(key):
        document = store.read(key)
        raw, export_id = review_service.export_review_package(store, document)
        return download(raw, 'review-pack-' + key + '-' + export_id[:8] + '.zip', 'application/zip')

    @api.post('/documents/<key>/review-package/analyze')
    def review_package_analyze(key):
        file = request.files.get('file')
        if file is None:
            raise v.Invalid('请选择审阅包ZIP')
        return jsonify(review_service.analyze_import(store.read(key), file.read()))

    @api.post('/documents/<key>/review-package/import')
    def review_package_import(key):
        file = request.files.get('file')
        if file is None:
            raise v.Invalid('请选择审阅包ZIP')
        revision = request.form.get('revision')
        revision = int(revision) if revision is not None else None
        document, summary = review_service.apply_import(store, key, file.read(), revision)
        return jsonify({'document': document, 'summary': summary})

    @api.get('/documents/<key>/pages/<page_id>/download')
    def original(key, page_id):
        _, _, version = get_version(key, page_id, request.args.get('version'))
        return download(store.asset_get(version['asset']), 'page-'+version['id']+'.png', 'image/png')

    @api.post('/documents/<key>/pages/<page_id>/transform')
    def transform_page(key, page_id):
        values = body()
        def change(document):
            transform(store, document, page_id, values.get('version_id'), values.get('crop', [0, 0, 1, 1]), values.get('rotation', 0))
        return jsonify(store.mutate(key, values.get('revision'), '生成裁切旋转校样', change))

    @api.post('/documents/<key>/pages/<page_id>/review')
    def review(key, page_id):
        values = body()
        return jsonify(catalog.review(key, page_id, values.get('version_id'), values.get('status'), values.get('note', ''), values.get('revision')))

    @api.post('/documents/<key>/pages/<page_id>/annotations')
    def add_annotation(key, page_id):
        values = body()
        return jsonify(annotations.add(key, page_id, values.get('version_id'), values, values.get('revision')))

    @api.patch('/documents/<key>/pages/<page_id>/annotations/<annotation_id>')
    def edit_annotation(key, page_id, annotation_id):
        values = body()
        return jsonify(annotations.update(key, page_id, values.get('version_id'), annotation_id, values, values.get('revision')))

    @api.delete('/documents/<key>/pages/<page_id>/annotations/<annotation_id>')
    def delete_annotation(key, page_id, annotation_id):
        values = body()
        return jsonify(annotations.remove(key, page_id, values.get('version_id'), annotation_id, values.get('revision')))

    @api.post('/documents/<key>/pages/<page_id>/statistics')
    def statistics(key, page_id):
        values = body()
        _, _, version = get_version(key, page_id, values.get('version_id'))
        return jsonify(region_statistics(load(store, version), values.get('bounds', [0, 0, 1, 1])))

    @api.get('/documents/<key>/pages/<page_id>/compare')
    def comparison(key, page_id):
        document, page, version = get_version(key, page_id, request.args.get('a'))
        second = store.version(page, request.args.get('b'))
        output = compare_images(load(store, version), load(store, second), request.args.get('anchor', 'center'))
        view = request.args.get('view', 'report')
        if view == 'report':
            return jsonify(output['report'] | {'version_a': version['id'], 'version_b': second['id'], 'revision': document['revision']})
        v.choice(view, '比较图层', ['heat', 'overlay', 'side_by_side'])
        return image_response(output[view])

    @api.get('/documents/<key>/pages/<page_id>/separation')
    def separation(key, page_id):
        _, _, version = get_version(key, page_id, request.args.get('version'))
        image = load(store, version)
        black = v.number(request.args.get('black', 1), '黑版生成', 0, 1)
        limit = v.number(request.args.get('limit', 300), '总墨量', 100, 400)
        report, channels, total = separation_report(image, black, limit)
        view = request.args.get('view', 'report')
        if view == 'report':
            return jsonify(report)
        if view == 'ink':
            return image_response(ink_preview(image, total, limit))
        return image_response(channel_preview(channels, view))

    @api.get('/documents/<key>/preflight')
    def preflight(key):
        return jsonify(inspect_document(store, store.read(key)))

    @api.get('/documents/<key>/contact')
    def contact(key):
        image, index = contact_sheet(store, store.read(key), request.args.get('columns', 3), request.args.get('width', 300))
        if request.args.get('view') == 'index':
            return jsonify(index)
        return image_response(image)

    @api.get('/documents/<key>/package')
    def package(key):
        return download(export_package(store, store.read(key)), 'proofarchive-'+key+'.zip', 'application/zip')

    @api.post('/import')
    def import_archive():
        file = request.files.get('file')
        if file is None:
            raise v.Invalid('请选择档案ZIP')
        return jsonify(import_package(store, file.read()))

    @api.post('/documents/<key>/snapshots')
    def create_snapshot(key):
        values = body()
        return jsonify(store.snapshot(key, values.get('revision'), values.get('name')))

    @api.post('/documents/<key>/snapshots/<snapshot_id>/restore')
    def restore_snapshot(key, snapshot_id):
        values = body()
        return jsonify(store.restore(key, values.get('revision'), snapshot_id))

    @api.get('/documents/<key>/report')
    def report(key):
        document = store.read(key)
        output = {'format': 'proofarchive-review-1', 'document': document, 'preflight': inspect_document(store, document)}
        return download(json.dumps(output, ensure_ascii=False, indent=2).encode(), 'review-'+key+'.json', 'application/json')

    @api.post('/demo')
    def demo():
        document = store.create('春季展览手册 · 合成校样', '程序绘制的示范素材，不代表真实印刷观测。包含三页及封面两个版次。')
        key = document['id']
        for index, color in enumerate(['#165e83', '#ce6c48', '#468575']):
            landscape = index == 2
            size = (900, 640) if landscape else (640, 900)
            image = Image.new('RGBA', size, 'white')
            draw = ImageDraw.Draw(image)
            w, h = size
            draw.rectangle([35, 35, w - 35, round(h * .39)], fill=color)
            draw.text((65, 90), 'SPRING / 2026', fill='white', font_size=38)
            draw.text((65, 160), 'DESIGN EXHIBITION', fill='white', font_size=26)
            draw.text((65, h - 60), f'PAGE {index+1:02}', fill='white', font_size=22)
            for line in range(12):
                draw.rectangle([65, round(h * .47) + line * 25,
                                w - 65 - (line % 3) * 35, round(h * .47) + 7 + line * 25],
                               fill='#8393a5')
            draw.rectangle([65, h - 140, 260, h - 80], fill=color)
            document = catalog.import_page(key, encode(image), f'Page-{index+1:02}.png')
            page = document['pages'][index]
            if index == 0:
                cover = page
                draw.rectangle([65, h - 140, 260, h - 80], fill='#e6b44b')
                draw.text((75, h - 125), 'UPDATED TIME', fill='#253447', font_size=19)
                document = catalog.import_page(key, encode(image), 'Cover-v2.png', page_id=cover['id'])
                annotations.add(key, cover['id'], None,
                                {'bounds': [.09, .83, .43, .93], 'text': '请核对活动时间区域的底色与文字。', 'kind': 'issue'})
                annotations.add(key, cover['id'], None,
                                {'bounds': [.0, .02, .22, .10], 'text': '左上角贴边标识需要留出安全距离。', 'kind': 'issue'})
                annotations.add(key, cover['id'], None,
                                {'bounds': [.62, .46, .94, .55], 'text': '标题行距与设计稿核对。', 'kind': 'note'})
            elif index == 1:
                annotations.add(key, page['id'], None,
                                {'bounds': [.78, .92, 1.0, 1.0], 'text': '右下角页码过于贴边，请确认出血位置。', 'kind': 'issue'})
                annotations.add(key, page['id'], None,
                                {'bounds': [.10, .47, .48, .54], 'text': '正文第二段文字颜色偏浅。', 'kind': 'issue'})
                document = annotations.add(key, page['id'], None,
                                {'bounds': [.10, .70, .40, .76], 'text': '此条已与排版确认无误。', 'kind': 'approved'})
                fresh_page = next(p for p in document['pages'] if p['id'] == page['id'])
                annotations.update(
                    key, page['id'], None,
                    fresh_page['versions'][0]['annotations'][2]['id'], {'resolved': True})
            else:
                annotations.add(key, page['id'], None,
                                {'bounds': [.02, .05, .30, .28], 'text': '横版页眉的图文间距需复核。', 'kind': 'issue'})
                annotations.add(key, page['id'], None,
                                {'bounds': [.66, .60, .98, .92], 'text': '横版右下图片边缘请核对对齐。', 'kind': 'note'})
        return jsonify(store.read(key))

    app.register_blueprint(api)
    app.view_functions['index'] = lambda: render_template('archive.html')

    @app.get('/image-lab')
    def image_lab():
        return render_template('index.html')

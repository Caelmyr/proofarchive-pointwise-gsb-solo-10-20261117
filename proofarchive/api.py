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
from .redactions import Redactions, apply, items as redaction_items
from .delivery import Delivery
from .images import load, encode, thumbnail, region_statistics
from .geometry import transform
from .compare import compare_images
from .separations import separation_report, channel_preview, ink_preview
from .preflight import inspect_document
from .layout import contact_sheet
from .packages import export_package, import_package


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
    redactions = Redactions(store)
    delivery = Delivery(store)
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
            image = overlay(image, version['annotations'])
        return image_response(thumbnail(image, v.integer(request.args.get('size', 1200), '预览尺寸', 64, 2400)))

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

    @api.get('/documents/<key>/pages/<page_id>/redacted')
    def redacted_preview(key, page_id):
        _, _, version = get_version(key, page_id, request.args.get('version'))
        mask = request.args.get('mask', '1')
        if mask not in ('0', '1'):
            raise v.Invalid('遮敏显示开关无效')
        redaction_ids = request.args.get('ids')
        chosen = None
        if redaction_ids is not None:
            chosen = []
            for item_id in redaction_ids.split(','):
                if not item_id:
                    continue
                if not any(item['id'] == item_id for item in redaction_items(version)):
                    raise v.Invalid('所选遮敏区域不属于该版次')
                chosen.append(item_id)
            chosen = [item for item in redaction_items(version) if item['id'] in chosen]
        else:
            chosen = redaction_items(version)
        image = load(store, version)
        if mask == '1':
            image = apply(image, chosen)
        else:
            from PIL import ImageDraw
            from .redactions import pixel_box
            draw = ImageDraw.Draw(image)
            width, height = image.size
            for item in chosen:
                left, top, right, bottom = pixel_box(item['bounds'], (width, height))
                draw.rectangle([left, top, right - 1, bottom - 1], outline='#e0362a', width=max(2, round(width / 400)))
        if request.args.get('annotations') == '1':
            image = overlay(image, version['annotations'])
        return image_response(thumbnail(image, v.integer(request.args.get('size', 1200), '预览尺寸', 64, 2400)))

    @api.post('/documents/<key>/pages/<page_id>/redactions')
    def add_page_redaction(key, page_id):
        values = body()
        return jsonify(redactions.add(key, page_id, values.get('version_id'), values, values.get('revision')))

    @api.patch('/documents/<key>/pages/<page_id>/redactions/<redaction_id>')
    def edit_redaction(key, page_id, redaction_id):
        values = body()
        return jsonify(redactions.update(key, page_id, values.get('version_id'), redaction_id, values, values.get('revision')))

    @api.delete('/documents/<key>/pages/<page_id>/redactions/<redaction_id>')
    def delete_redaction(key, page_id, redaction_id):
        values = body()
        return jsonify(redactions.remove(key, page_id, values.get('version_id'), redaction_id, values.get('revision')))

    @api.post('/documents/<key>/delivery/plan')
    def delivery_plan_route(key):
        return jsonify(delivery.report(key, body()))

    @api.post('/documents/<key>/delivery/preview')
    def delivery_preview(key):
        values = body()
        image = delivery.preview(key, values)
        return image_response(thumbnail(image, v.integer(values.get('size', 1200), '预览尺寸', 64, 2400)))

    @api.post('/documents/<key>/delivery/package')
    def delivery_package(key):
        key = v.identifier(key)
        return download(delivery.package(key, body()), 'redacted-'+key+'.zip', 'application/zip')

    @api.get('/documents/<key>/pages/<page_id>/statistics')
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
            image = Image.new('RGBA', (640, 900), 'white')
            draw = ImageDraw.Draw(image)
            draw.rectangle([35, 35, 605, 350], fill=color)
            draw.text((65, 90), 'SPRING / 2026', fill='white', font_size=38)
            draw.text((65, 160), 'DESIGN EXHIBITION', fill='white', font_size=26)
            draw.text((65, 290), f'PAGE {index+1:02}', fill='white', font_size=22)
            for line in range(12):
                draw.rectangle([65, 420+line*25, 575-(line%3)*35, 427+line*25], fill='#8393a5')
            draw.rectangle([65, 760, 260, 820], fill=color)
            document = catalog.import_page(key, encode(image), f'Page-{index+1:02}.png')
            if index == 0:
                cover = document['pages'][0]
                draw.rectangle([65, 760, 260, 820], fill='#e6b44b')
                draw.text((75, 775), 'UPDATED TIME', fill='#253447', font_size=19)
                document = catalog.import_page(key, encode(image), 'Cover-v2.png', page_id=cover['id'])
                document = annotations.add(key, cover['id'], None, {'bounds': [.09, .83, .43, .93], 'text': '请核对活动时间区域的底色与文字。', 'kind': 'issue'})
        return jsonify(document)

    app.register_blueprint(api)
    app.view_functions['index'] = lambda: render_template('archive.html')

    @app.get('/image-lab')
    def image_lab():
        return render_template('index.html')

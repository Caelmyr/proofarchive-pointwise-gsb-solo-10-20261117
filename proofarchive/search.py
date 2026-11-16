"""版面相似检索任务：可取消、有进度、按档案当前内容实时扫描。

检索只读档案，不修改、合并或删除任何页面。候选在任务运行中从档案内容生成，
像素特征按资源 SHA256 缓存；新增档案、导入档案包或新增版次后再次检索即可看到新内容。
读取目录与扫描都在后台执行，大档案库可看到进度并随时取消。
"""
import copy
import io
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from . import similarity
from . import validation as v
from .store import uid

MAX_RESULTS = 200
DEFAULT_RESULTS = 60
MAX_FEATURES = 2048
ACTIVE = ('queued', 'running', 'cancelling')
SCOPES = ['current', 'document', 'all']
SCOPE_LABELS = {'current': '各档案的当前版次', 'document': '当前档案的全部版次', 'all': '全部档案的全部版次'}
DISCOVERY_SHARE = .1


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _load(store, asset):
    with Image.open(io.BytesIO(store.asset_get(asset))) as image:
        return image.convert('RGBA').copy()


class SimilaritySearch:
    def __init__(self, store, workers=1):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='archive-similarity')
        self.lock = threading.RLock()
        self.jobs = {}
        self.events = {}
        self.features = OrderedDict()

    # ---- 像素特征缓存 -------------------------------------------------
    def _feature(self, asset):
        with self.lock:
            cached = self.features.get(asset)
            if cached is not None:
                self.features.move_to_end(asset)
                return cached
        computed = similarity.features(_load(self.store, asset))
        with self.lock:
            self.features[asset] = computed
            self.features.move_to_end(asset)
            while len(self.features) > MAX_FEATURES:
                self.features.popitem(last=False)
        return computed

    def compare_images(self, first, second):
        return similarity.pair_view(_load(self.store, first['asset']),
                                    _load(self.store, second['asset']),
                                    first['asset'] == second['asset'])

    # ---- 任务生命周期 -------------------------------------------------
    def get(self, job_id):
        v.identifier(job_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise v.Invalid('检索任务不存在或已过期')
            return copy.deepcopy({key: value for key, value in job.items() if key != 'cancel'})

    def cancel(self, job_id):
        v.identifier(job_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise v.Invalid('检索任务不存在或已过期')
            if job['status'] not in ACTIVE:
                return copy.deepcopy({key: value for key, value in job.items() if key != 'cancel'})
            self.events[job_id].set()
            if job['status'] == 'queued':
                job.update(status='cancelled', stage='已取消', finished=stamp())
            else:
                job.update(status='cancelling', stage='等待当前页面处理结束')
            return copy.deepcopy({key: value for key, value in job.items() if key != 'cancel'})

    def _document_keys(self):
        with self.store.lock:
            return sorted(path.stem for path in self.store.documents.glob('*.json'))

    def _read(self, key):
        try:
            return self.store.read(key)
        except v.Invalid:
            return None

    def _sample(self, document_id, page_id, version_id):
        document = self.store.read(document_id)
        page = self.store.page(document, page_id)
        version = self.store.version(page, version_id)
        return document, page, version

    @staticmethod
    def _image_url(document_id, page_id, version_id, size=320):
        return f'/archive/documents/{document_id}/pages/{page_id}/image?size={size}&version={version_id}'

    def start(self, document_id, page_id, version_id, scope, limit=None):
        scope = v.choice(scope, '检索范围', SCOPES)
        limit = v.integer(DEFAULT_RESULTS if limit is None else limit, '结果数量', 1, MAX_RESULTS)
        document_id = v.identifier(document_id)
        page_id = v.identifier(page_id)
        version_id = v.identifier(version_id) if version_id is not None else None
        document, page, sample = self._sample(document_id, page_id, version_id)
        sample_meta = {'document_id': document['id'], 'document_name': document['name'],
                       'page_id': page['id'], 'page_name': page['name'],
                       'version_id': sample['id'], 'version_label': sample['label'],
                       'asset': sample['asset'], 'pixels': sample['pixels'], 'scope': scope,
                       'scope_label': SCOPE_LABELS[scope], 'revision': document['revision'],
                       'image': self._image_url(document['id'], page['id'], sample['id'])}
        with self.lock:
            for key in [key for key, item in self.jobs.items() if item['status'] in ACTIVE]:
                self.events[key].set()
                self.jobs[key].update(status='cancelled', stage='被新的检索取代', finished=stamp())
            job = {'id': uid(), 'status': 'queued', 'stage': '等待读取档案目录', 'progress': 0.,
                   'scanned': 0, 'total': 0, 'documents_read': 0, 'documents_total': 0,
                   'limit': limit, 'results': [], 'sample': sample_meta,
                   'started': stamp(), 'finished': None, 'error': None,
                   'note': '按版面结构比较；忽略导出分辨率、白边与透明纸白，完全相同单独标记。'}
            event = threading.Event()
            self.jobs[job['id']] = job
            self.events[job['id']] = event
            self._prune()
            self.pool.submit(self._run, job['id'], document, sample_meta, limit, event)
        return self.get(job['id'])

    def _prune(self):
        stale = [key for key, job in self.jobs.items() if job['status'] not in ACTIVE]
        for key in stale[:-20]:
            self.jobs.pop(key, None)
            self.events.pop(key, None)

    def _update(self, job_id, **fields):
        with self.lock:
            self.jobs[job_id].update(fields)

    # ---- 后台任务 -----------------------------------------------------
    def _candidates(self, job_id, document, sample_meta, event):
        """按任务启动时的档案内容生成候选；读取目录阶段同样可取消。"""
        scope = sample_meta['scope']
        if scope == 'document':
            documents = [document]
        else:
            keys = self._document_keys()
            if document['id'] not in keys:
                keys.append(document['id'])
            self._update(job_id, documents_total=len(keys))
            documents = []
            for index, key in enumerate(keys):
                if event.is_set():
                    return None
                if key == document['id']:
                    documents.append(document)
                else:
                    found = self._read(key)
                    if found is not None:
                        documents.append(found)
                self._update(job_id, documents_read=index + 1,
                             progress=round(DISCOVERY_SHARE * 100. * (index + 1) / max(1, len(keys)), 1),
                             stage=f'读取档案目录 {index + 1}/{len(keys)}')
        candidates = []
        seen = set()
        for document in documents:
            for page in document['pages']:
                if scope == 'current':
                    versions = [version for version in page['versions'] if version['id'] == page['current']]
                else:
                    versions = page['versions']
                for version in versions:
                    identity = (document['id'], page['id'], version['id'])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if identity == (sample_meta['document_id'], sample_meta['page_id'], sample_meta['version_id']):
                        continue
                    candidates.append({
                        'document_id': document['id'], 'document_name': document['name'],
                        'document_revision': document['revision'],
                        'page_id': page['id'], 'page_name': page['name'], 'page_current': page['current'],
                        'version_id': version['id'], 'version_label': version['label'],
                        'asset': version['asset'], 'pixels': version['pixels']})
        return candidates

    def _run(self, job_id, document, sample_meta, limit, event):
        try:
            self._update(job_id, status='running', stage='读取档案目录')
            candidates = self._candidates(job_id, document, sample_meta, event)
            if candidates is None:
                self._finish_cancelled(job_id, [])
                return
            total = len(candidates)
            self._update(job_id, total=total, scanned=0, stage='提取样本特征', progress=DISCOVERY_SHARE * 100.)
            sample_feature = self._feature(sample_meta['asset'])
            rows = []
            for index, candidate in enumerate(candidates):
                if event.is_set():
                    self._finish_cancelled(job_id, self._rank(rows, limit))
                    return
                exact = candidate['asset'] == sample_meta['asset']
                if exact:
                    parts = {'edge': 1., 'luminance': 1., 'color': 1., 'aspect': 1., 'score': 1.}
                else:
                    parts = similarity.score(sample_feature, self._feature(candidate['asset']))
                rows.append({
                    'document_id': candidate['document_id'], 'document_name': candidate['document_name'],
                    'document_revision': candidate['document_revision'],
                    'page_id': candidate['page_id'], 'page_name': candidate['page_name'],
                    'version_id': candidate['version_id'], 'version_label': candidate['version_label'],
                    'current': candidate['version_id'] == candidate['page_current'],
                    'exact': exact, 'score': round(parts['score'], 4),
                    'components': {key: round(parts[key], 4) for key in ('edge', 'luminance', 'color', 'aspect')},
                    'asset': candidate['asset'], 'pixels': candidate['pixels'],
                    'image': self._image_url(candidate['document_id'], candidate['page_id'], candidate['version_id'])})
                self._update(job_id, scanned=index + 1, total=total,
                             progress=round(DISCOVERY_SHARE * 100. + (1 - DISCOVERY_SHARE) * 100. * (index + 1) / max(1, total), 1),
                             stage=f'扫描 {index + 1}/{total} · {candidate["document_name"]} / {candidate["page_name"]}')
            self._update(job_id, status='completed', progress=100., stage='检索完成',
                         scanned=total, total=total, results=self._rank(rows, limit), finished=stamp())
        except Exception as error:  # noqa: BLE001 - 任务边界把失败报告给客户端
            with self.lock:
                job = self.jobs.get(job_id)
                if job is not None:
                    job.update(status='failed', stage='检索失败', error=str(error), finished=stamp())

    def _finish_cancelled(self, job_id, results):
        self._update(job_id, status='cancelled', results=results,
                     stage='已取消，已完成的结果保留', finished=stamp())

    @staticmethod
    def _rank(rows, limit):
        ordered = sorted(rows, key=lambda row: (not row['exact'], -row['score'],
                                                row['document_name'], row['page_name']))
        return ordered[:limit]

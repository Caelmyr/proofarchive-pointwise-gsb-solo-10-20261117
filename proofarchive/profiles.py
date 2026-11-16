from . import validation as v
from .preflight import SEVERITIES, ISSUE_LABELS, DEFAULT_PROFILE
from .store import uid, now

MAX_PROFILES = 50


def normalize_profile(values, profile_id=None):
    values = v.object_value(values)
    resolution = v.object_value(values.get('resolution', {}))
    bleed = v.object_value(values.get('bleed', {}))
    ink = v.object_value(values.get('ink', {}))
    edges = v.list_value(bleed.get('edges', DEFAULT_PROFILE['bleed']['edges']), 4)
    if len(edges) != 4:
        raise v.Invalid('出血要求需要左上右下四个值')
    return {'id': profile_id or uid(),
            'name': v.text(values.get('name'), '方案名'),
            'resolution': {'minimum_dpi': v.number(resolution.get('minimum_dpi', DEFAULT_PROFILE['resolution']['minimum_dpi']), '有效分辨率阈值', 10, 2400),
                           'severity': v.choice(resolution.get('severity', 'warning'), '分辨率问题严重程度', SEVERITIES)},
            'bleed': {'edges': [v.number(edge, '出血要求mm', 0, 100) for edge in edges],
                      'severity': v.choice(bleed.get('severity', 'warning'), '出血问题严重程度', SEVERITIES)},
            'ink': {'limit_percent': v.number(ink.get('limit_percent', DEFAULT_PROFILE['ink']['limit_percent']), '总墨量限制%', 100, 400),
                    'severity': v.choice(ink.get('severity', 'warning'), '墨量问题严重程度', SEVERITIES)}}


def profiles_of(document):
    return document['settings'].setdefault('profiles', [])


def find_profile(document, profile_id):
    profile = next((item for item in profiles_of(document) if item['id'] == profile_id), None)
    if profile is None:
        raise v.Invalid('用途方案不存在')
    return profile


class Profiles:
    def __init__(self, store):
        self.store = store

    def create(self, key, values, expected=None):
        profile = normalize_profile(values)
        def change(document):
            if len(profiles_of(document)) >= MAX_PROFILES:
                raise v.Invalid(f'每个档案最多{MAX_PROFILES}个用途方案')
            profiles_of(document).append(profile)
        return self.store.mutate(key, expected, '新建预检用途方案', change)

    def update(self, key, profile_id, values, expected=None):
        profile = normalize_profile(values, profile_id)
        def change(document):
            existing = profiles_of(document)
            index = next((i for i, item in enumerate(existing) if item['id'] == profile_id), None)
            if index is None:
                raise v.Invalid('用途方案不存在')
            existing[index] = profile
        return self.store.mutate(key, expected, '编辑预检用途方案', change)

    def remove(self, key, profile_id, expected=None):
        def change(document):
            find_profile(document, profile_id)
            document['settings']['profiles'] = [item for item in profiles_of(document) if item['id'] != profile_id]
            for page in document['pages']:
                if page.get('profile') == profile_id:
                    page['profile'] = None
        return self.store.mutate(key, expected, '删除预检用途方案', change)

    def assign(self, key, page_id, profile_id, expected=None):
        def change(document):
            page = self.store.page(document, page_id)
            if profile_id is not None:
                find_profile(document, profile_id)
            page['profile'] = profile_id
        return self.store.mutate(key, expected, '分配页面预检方案', change)

    def add_exception(self, key, page_id, version_id, code, reason, expected=None):
        code = v.choice(code, '例外问题类型', list(ISSUE_LABELS))
        exception = {'id': uid(), 'code': code, 'reason': v.text(reason, '例外理由', 1000), 'created': now()}
        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            exceptions = version.setdefault('exceptions', [])
            if len(exceptions) >= 50:
                raise v.Invalid('每版最多50条预检例外')
            if any(item['code'] == code for item in exceptions):
                raise v.Invalid('该问题类型在此版次已有例外')
            exceptions.append(exception)
        return self.store.mutate(key, expected, '记录预检放行例外', change)

    def remove_exception(self, key, page_id, version_id, exception_id, expected=None):
        def change(document):
            version = self.store.version(self.store.page(document, page_id), version_id)
            exceptions = version.get('exceptions', [])
            if not any(item['id'] == exception_id for item in exceptions):
                raise v.Invalid('例外不存在')
            version['exceptions'] = [item for item in exceptions if item['id'] != exception_id]
        return self.store.mutate(key, expected, '撤销预检放行例外', change)

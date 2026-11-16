from . import validation as v
from .store import uid, now


STATUSES = ['pending', 'confirmed', 'ignored']
RECORD_LIMIT = 5000


def matching(document, page_id, version_a, version_b, anchor):
    """审阅结论只属于具体的页面、版次对（有序）与对齐方式。"""
    return [r for r in document.get('comparison_reviews', [])
            if r['page_id'] == page_id and r['version_a'] == version_a
            and r['version_b'] == version_b and r['anchor'] == anchor]


def merge(document, page_id, version_a, version_b, anchor, regions):
    """把持久化的结论并回当次计算的区域；边界或像素数对不上的旧记录不套用。"""
    records = {r['region']: r for r in matching(document, page_id, version_a, version_b, anchor)}
    merged = []
    for region in regions:
        record = records.get(region['id'])
        if record and record['bounds'] == region['bounds'] and record['changed_pixels'] == region['changed_pixels']:
            merged.append(region | {'status': record['status'], 'note': record['note'], 'updated': record['updated']})
        else:
            merged.append(region | {'status': 'pending', 'note': '', 'updated': None})
    return merged


def summary(regions):
    result = {'regions': len(regions), 'changed_pixels': sum(r['changed_pixels'] for r in regions)}
    for status in STATUSES:
        selected = [r for r in regions if r['status'] == status]
        result[status] = len(selected)
        result[status + '_pixels'] = sum(r['changed_pixels'] for r in selected)
    return result


class RegionReviews:
    def __init__(self, store):
        self.store = store

    def decide(self, key, page_id, version_a, version_b, anchor, region, status, note, expected=None):
        status = v.choice(status, '区域审阅状态', STATUSES)
        note = v.text(note, '区域审阅意见', 1000, True)
        anchor = v.choice(anchor, '对齐方式', ['center', 'top_left'])
        def change(document):
            reviews = document.setdefault('comparison_reviews', [])
            existing = next((r for r in reviews if r['page_id'] == page_id and r['version_a'] == version_a
                             and r['version_b'] == version_b and r['anchor'] == anchor
                             and r['region'] == region['id']), None)
            if status == 'pending' and not note:
                if existing:
                    reviews.remove(existing)
                return
            if existing is None:
                if len(reviews) >= RECORD_LIMIT:
                    raise v.Invalid('区域审阅记录已达上限')
                reviews.append({'id': uid(), 'page_id': page_id, 'version_a': version_a,
                                'version_b': version_b, 'anchor': anchor, 'region': region['id'],
                                'bounds': list(region['bounds']), 'changed_pixels': region['changed_pixels'],
                                'status': status, 'note': note, 'updated': now()})
            else:
                existing.update({'bounds': list(region['bounds']), 'changed_pixels': region['changed_pixels'],
                                 'status': status, 'note': note, 'updated': now()})
        return self.store.mutate(key, expected, '标记变化区域审阅', change)

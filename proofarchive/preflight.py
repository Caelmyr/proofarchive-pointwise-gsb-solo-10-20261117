import numpy as np
from .images import load, array
from .separations import separation_report


def inspect_version(store, page, version, minimum_dpi=150., required_bleed=3.):
    image = load(store, version)
    trim_w, trim_h = version['trim_mm']
    dpi = [image.width / trim_w * 25.4, image.height / trim_h * 25.4]
    issues = []
    if min(dpi) < minimum_dpi:
        issues.append({'code': 'resolution', 'severity': 'warning', 'message': '有效分辨率低于用途阈值', 'actual': dpi, 'required': minimum_dpi})
    bleed = version['bleed_mm']
    if min(bleed) < required_bleed:
        issues.append({'code': 'bleed', 'severity': 'warning', 'message': '声明出血不足；此检查不推断边缘图案是否可延展', 'actual': bleed, 'required': required_bleed})
    unresolved = [a for a in version['annotations'] if a['kind'] == 'issue' and not a['resolved']]
    if unresolved:
        issues.append({'code': 'annotations', 'severity': 'error', 'message': '仍有未解决的问题批注', 'count': len(unresolved)})
    if version['status'] != 'approved':
        issues.append({'code': 'review', 'severity': 'warning', 'message': '此版尚未批准交付', 'actual': version['status']})
    alpha = np.asarray(image.getchannel('A'))
    if np.any(alpha < 255):
        issues.append({'code': 'transparency', 'severity': 'info', 'message': '含透明像素，预览及墨量检查按纸白合成', 'fraction': float((alpha < 255).mean())})
    ink, _, _ = separation_report(image)
    if ink['over_limit_pixels']:
        issues.append({'code': 'ink', 'severity': 'warning', 'message': '简化分色存在总墨量超限', 'fraction': ink['over_limit_fraction']})
    return {'page_id': page['id'], 'page_name': page['name'], 'version_id': version['id'],
            'label': version['label'], 'pixels': list(image.size), 'trim_mm': version['trim_mm'],
            'effective_dpi': dpi, 'issues': issues, 'ink': ink,
            'passed': not any(issue['severity'] == 'error' for issue in issues)}


def inspect_document(store, document):
    reports = [inspect_version(store, page, store.version(page)) for page in document['pages']]
    return {'document_id': document['id'], 'revision': document['revision'], 'name': document['name'],
            'pages': reports, 'page_count': len(reports),
            'error_count': sum(issue['severity'] == 'error' for row in reports for issue in row['issues']),
            'warning_count': sum(issue['severity'] == 'warning' for row in reports for issue in row['issues']),
            'note': '栅格版面预检，不读取PDF字体、叠印或专色信息。'}

import numpy as np
from .images import load
from .separations import separation_report


SEVERITIES = ['error', 'warning', 'info', 'off']

ISSUE_LABELS = {'resolution': '有效分辨率', 'bleed': '四边出血', 'ink': '总墨量',
                'annotations': '未解决批注', 'review': '审校状态', 'transparency': '透明像素'}

# 未配置用途方案的旧档案按此默认要求检查，与历史行为一致。
DEFAULT_PROFILE = {'id': None, 'name': '默认要求',
                   'resolution': {'minimum_dpi': 150., 'severity': 'warning'},
                   'bleed': {'edges': [3., 3., 3., 3.], 'severity': 'warning'},
                   'ink': {'limit_percent': 300., 'severity': 'warning'}}

EDGE_NAMES = ['左', '上', '右', '下']


def profiles_of(document):
    profiles = document.get('settings', {}).get('profiles', [])
    return profiles if isinstance(profiles, list) else []


def profile_for(document, page):
    target = page.get('profile')
    for profile in profiles_of(document):
        if profile.get('id') == target:
            return profile
    return DEFAULT_PROFILE


def check_resolution(image, version, requirement):
    trim_w, trim_h = version['trim_mm']
    dpi = [image.width / trim_w * 25.4, image.height / trim_h * 25.4]
    declared = version.get('dpi') or [None, None]
    actual = {'effective_dpi': dpi, 'pixels': [image.width, image.height], 'trim_mm': [trim_w, trim_h],
              'declared_dpi': declared}
    required = {'minimum_dpi': requirement['minimum_dpi']}
    declared_text = '、'.join(f'{value:.1f}' if isinstance(value, (int, float)) else '未知' for value in declared)
    detail = (f'按像素 {image.width}×{image.height} 与成品尺寸 {trim_w:.1f}×{trim_h:.1f}mm 实测有效分辨率 '
              f'{dpi[0]:.1f}×{dpi[1]:.1f} DPI，要求不低于 {requirement["minimum_dpi"]:.0f} DPI；'
              f'文件声明 DPI（{declared_text}）仅作记录，不作为判定依据。')
    return {'code': 'resolution', 'label': ISSUE_LABELS['resolution'], 'severity': requirement['severity'],
            'actual': actual, 'required': required, 'detail': detail,
            'passed': min(dpi) >= requirement['minimum_dpi'] - 1e-6}


def check_bleed(version, requirement):
    bleed = [float(edge) for edge in version['bleed_mm']]
    edges = requirement['edges']
    short = [f'{EDGE_NAMES[index]}侧 {bleed[index]:.1f}mm 低于要求 {edges[index]:.1f}mm'
             for index in range(4) if bleed[index] < edges[index] - 1e-6]
    actual = {'bleed_mm': bleed}
    required = {'edges_mm': list(edges)}
    detail = ('声明出血 左{0:.1f} 上{1:.1f} 右{2:.1f} 下{3:.1f}mm，要求四边分别不低于 '
              '左{4:.1f} 上{5:.1f} 右{6:.1f} 下{7:.1f}mm。').format(*bleed, *edges)
    detail += ('不足：' + '；'.join(short) + '。' if short else '四边均满足。')
    detail += '此检查只核对声明值，不推断边缘图案是否可延展。'
    return {'code': 'bleed', 'label': ISSUE_LABELS['bleed'], 'severity': requirement['severity'],
            'actual': actual, 'required': required, 'detail': detail, 'passed': not short}


def check_ink(image, requirement):
    ink, _, _ = separation_report(image, ink_limit=requirement['limit_percent'])
    passed = ink['over_limit_pixels'] == 0
    detail = (f'简化软分色实测总墨量峰值 {ink["max_total_ink"]:.0f}%、均值 {ink["mean_total_ink"]:.0f}%，'
              f'要求不超过 {ink["ink_limit"]:.0f}%；超限像素 {ink["over_limit_pixels"]} 个'
              f'（占比 {ink["over_limit_fraction"]*100:.2f}%）。')
    return {'code': 'ink', 'label': ISSUE_LABELS['ink'], 'severity': requirement['severity'],
            'actual': {'max_total_ink': ink['max_total_ink'], 'mean_total_ink': ink['mean_total_ink'],
                       'over_limit_pixels': ink['over_limit_pixels'],
                       'over_limit_fraction': ink['over_limit_fraction']},
            'required': {'limit_percent': ink['ink_limit']}, 'detail': detail, 'passed': passed}, ink


def inspect_version(store, page, version, profile=None, document=None):
    if profile is None:
        profile = profile_for(document, page) if document is not None else DEFAULT_PROFILE
    image = load(store, version)
    checks = []
    ink = None
    configurable = [('resolution', lambda: check_resolution(image, version, profile['resolution'])),
                    ('bleed', lambda: check_bleed(version, profile['bleed'])),
                    ('ink', lambda: check_ink(image, profile['ink']))]
    for code, evaluate in configurable:
        requirement = profile[code]
        if requirement['severity'] == 'off':
            checks.append({'code': code, 'label': ISSUE_LABELS[code], 'severity': 'off',
                           'status': 'skipped', 'actual': None, 'required': None,
                           'detail': '此用途方案未启用该项检查。'})
            continue
        result = evaluate()
        if code == 'ink':
            result, ink = result
        result['status'] = 'pass' if result.pop('passed') else 'fail'
        checks.append(result)
    unresolved = [a for a in version['annotations'] if a['kind'] == 'issue' and not a['resolved']]
    checks.append({'code': 'annotations', 'label': ISSUE_LABELS['annotations'], 'severity': 'error',
                   'status': 'fail' if unresolved else 'pass',
                   'actual': {'unresolved': len(unresolved)}, 'required': {'unresolved': 0},
                   'detail': f'仍有 {len(unresolved)} 条未解决的问题批注。' if unresolved else '问题批注均已解决。'})
    checks.append({'code': 'review', 'label': ISSUE_LABELS['review'], 'severity': 'warning',
                   'status': 'pass' if version['status'] == 'approved' else 'fail',
                   'actual': {'status': version['status']}, 'required': {'status': 'approved'},
                   'detail': '此版已批准交付。' if version['status'] == 'approved' else f'此版审校状态为 {version["status"]}，尚未批准交付。'})
    alpha = np.asarray(image.getchannel('A'))
    fraction = float((alpha < 255).mean())
    checks.append({'code': 'transparency', 'label': ISSUE_LABELS['transparency'], 'severity': 'info',
                   'status': 'fail' if fraction > 0 else 'pass',
                   'actual': {'alpha_fraction': fraction}, 'required': {'alpha_fraction': 0.},
                   'detail': f'含透明像素（占比 {fraction*100:.2f}%），预览及墨量检查按纸白合成。' if fraction > 0 else '不含透明像素。'})
    exceptions = version.get('exceptions', [])
    issues = []
    for check in checks:
        if check['status'] != 'fail':
            continue
        issue = {'code': check['code'], 'label': check['label'], 'severity': check['severity'],
                 'message': check['detail'], 'actual': check['actual'], 'required': check['required'],
                 'waived': False, 'exception': None}
        match = next((item for item in exceptions if item.get('code') == check['code']), None)
        if match is not None:
            issue['waived'] = True
            issue['exception'] = match
        issues.append(issue)
    trim_w, trim_h = version['trim_mm']
    dpi = [image.width / trim_w * 25.4, image.height / trim_h * 25.4]
    return {'page_id': page['id'], 'page_name': page['name'], 'version_id': version['id'],
            'label': version['label'], 'pixels': list(image.size), 'trim_mm': version['trim_mm'],
            'effective_dpi': dpi, 'profile': {'id': profile['id'], 'name': profile['name']},
            'checks': checks, 'issues': issues, 'ink': ink,
            'waived_count': sum(1 for issue in issues if issue['waived']),
            'passed': not any(issue['severity'] == 'error' and not issue['waived'] for issue in issues)}


def summarize(reports):
    by_code = {}
    for row in reports:
        for issue in row['issues']:
            entry = by_code.setdefault(issue['code'], {'code': issue['code'], 'label': issue['label'],
                                                       'count': 0, 'waived': 0, 'pages': [],
                                                       'severities': {'error': 0, 'warning': 0, 'info': 0}})
            entry['count'] += 1
            entry['waived'] += 1 if issue['waived'] else 0
            entry['severities'][issue['severity']] += 1
            entry['pages'].append({'page_id': row['page_id'], 'page_name': row['page_name']})
    return by_code


def inspect_document(store, document):
    reports = [inspect_version(store, page, store.version(page), profile_for(document, page), document)
               for page in document['pages']]
    active = [issue for row in reports for issue in row['issues'] if not issue['waived']]
    return {'document_id': document['id'], 'revision': document['revision'], 'name': document['name'],
            'pages': reports, 'page_count': len(reports),
            'passed_pages': sum(1 for row in reports if row['passed']),
            'error_count': sum(issue['severity'] == 'error' for issue in active),
            'warning_count': sum(issue['severity'] == 'warning' for issue in active),
            'info_count': sum(issue['severity'] == 'info' for issue in active),
            'waived_count': sum(row['waived_count'] for row in reports),
            'summary': {'by_code': summarize(reports)},
            'note': '栅格版面预检，不读取PDF字体、叠印或专色信息。有效分辨率按像素与成品尺寸实测，不采用文件声明DPI。'}

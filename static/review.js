/* 批注连续审阅：按页序队列浏览、整页定位、局部图、处理与审阅包导入导出。 */
const rv = {
    queue: null,
    index: 0,
    scopeAll: false,
    docId: null,
};

function rvApi(url, method = 'GET', data) {
    let options = { method };
    if (data instanceof FormData) options.body = data;
    else if (data !== undefined) {
        options.headers = { 'Content-Type': 'application/json' };
        options.body = JSON.stringify(data);
    }
    return fetch(url, options).then(async response => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw Error(payload.error || '操作失败');
        return payload;
    });
}

function rvBase() {
    return '/archive/documents/' + rv.docId;
}

function rvItem() {
    return rv.queue?.items[rv.index] || null;
}

function rvKindLabel(kind) {
    return { issue: '问题', note: '备注', approved: '确认' }[kind] || kind;
}

async function rvRefreshDoc() {
    doc = await rvApi('/archive/documents/' + rv.docId);
    if (typeof documents === 'function') documents();
    if (typeof render === 'function') render();
}

async function rvOpen() {
    if (!doc) return toast('先选择档案');
    rv.docId = doc.id;
    await rvLoadQueue();
    $('#reviewOverlay').hidden = false;
}

async function rvLoadQueue(keepItemId) {
    const scope = rv.scopeAll ? 'all' : 'unresolved';
    const queue = await rvApi(rvBase() + '/review-queue?scope=' + scope);
    let nextIndex = 0;
    if (keepItemId) {
        const found = queue.items.findIndex(it => it.annotation.id === keepItemId);
        nextIndex = found >= 0 ? found : Math.min(rv.index, Math.max(0, queue.items.length - 1));
    }
    rv.queue = queue;
    rv.index = nextIndex;
    rvRender();
}

function rvRender() {
    const q = rv.queue;
    $('#rvTitle').textContent = '批注连续审阅 · ' + (doc?.name || '');
    $('#rvSub').textContent = '档案页序连续排列 · 待解决 ' + q.unresolved_count +
        ' 条 / 当前清单 ' + q.total + ' 条（REV ' + q.revision + '）';
    const list = $('#rvList');
    if (!q.items.length) {
        list.innerHTML = '<div class="rv-empty">没有待解决的批注。<br>可勾选“显示已解决”查看全部。</div>';
    } else {
        list.innerHTML = q.items.map((it, i) => {
            const a = it.annotation;
            return '<div class="rv-item ' + (i === rv.index ? 'active ' : '') + (a.resolved ? 'resolved' : '') +
                '" data-i="' + i + '"><div class="rv-item-head"><span>P' + String(it.page_number).padStart(2, '0') +
                ' · #' + it.annotation_number + (it.is_current ? '' : ' · 旧版') +
                '</span><span class="rv-kind ' + a.kind + '">' + rvKindLabel(a.kind) + '</span></div>' +
                '<div class="rv-item-text">' + esc(a.text) + '</div></div>';
        }).join('');
        list.querySelectorAll('.rv-item').forEach(el => {
            el.onclick = () => { rv.index = Number(el.dataset.i); rvRender(); };
        });
    }
    const item = rvItem();
    if (!item) {
        $('#rvCrumb').textContent = '清单已空';
        $('#rvState').textContent = '';
        $('#rvPage').removeAttribute('src');
        $('#rvCrop').removeAttribute('src');
        $('#rvText').textContent = '';
        $('#rvBounds').textContent = '';
        $('#rvNoteWrap').hidden = true;
        $('#rvResolve').disabled = true;
        $('#rvEdit').disabled = true;
        $('#rvJump').textContent = '';
        return;
    }
    $('#rvResolve').disabled = false;
    $('#rvEdit').disabled = false;
    const a = item.annotation;
    const pageUrl = rvBase() + '/pages/' + item.page_id + '/image?version=' + item.version_id +
        '&annotations=1&highlight=' + a.id + '&size=1400&r=' + q.revision;
    const cropUrl = rvBase() + '/pages/' + item.page_id + '/annotations/' + a.id +
        '/crop?version=' + item.version_id + '&r=' + q.revision;
    $('#rvPage').src = pageUrl;
    $('#rvCrop').src = cropUrl;
    $('#rvCrumb').textContent = '第 ' + item.page_number + ' 页 · ' + item.page_name +
        ' / 版次：' + item.version_label + (item.is_current ? '' : '（非当前版次）') +
        ' / 批注 #' + item.annotation_number + ' / ' + (item.orientation === 'landscape' ? '横图' : '竖图');
    const state = $('#rvState');
    state.textContent = a.resolved ? '已解决' : '待解决';
    state.className = 'rv-pill' + (a.resolved ? ' resolved' : '');
    $('#rvText').textContent = a.text;
    $('#rvBounds').textContent = '原图归一化位置 [左,上,右,下]：' +
        a.bounds.map(x => x.toFixed(3)).join(', ') + '　原图像素：' + item.pixels.join(' × ');
    const note = a.review_note || '';
    $('#rvNoteWrap').hidden = !note;
    $('#rvNote').value = note;
    const resolveBtn = $('#rvResolve');
    resolveBtn.textContent = a.resolved ? '重开本批注' : '解决本批注';
    resolveBtn.className = 'rv-primary' + (a.resolved ? '' : ' done');
    $('#rvPrev').disabled = rv.index === 0;
    $('#rvNext').disabled = rv.index >= q.items.length - 1;
    $('#rvJump').textContent = '清单位置 ' + (rv.index + 1) + ' / ' + q.items.length +
        '（批注编号为版次内序号；解决后自动跳到下一条待办）';
    list.querySelector('.rv-item.active')?.scrollIntoView({ block: 'nearest' });
}

async function rvMutate(payload, label) {
    const item = rvItem();
    if (!item) return;
    const url = rvBase() + '/pages/' + item.page_id + '/annotations/' + item.annotation.id;
    await rvApi(url, 'PATCH', Object.assign({
        revision: doc.revision,
        version_id: item.version_id,
    }, payload));
    await rvRefreshDoc();
    await rvLoadQueue(item.annotation.id);
    toast(label);
}

async function rvToggleResolved() {
    const item = rvItem();
    if (!item) return;
    const willResolve = !item.annotation.resolved;
    const id = item.annotation.id;
    await rvMutate({ resolved: willResolve }, willResolve ? '已解决，跳到下一条' : '已重开');
    if (willResolve && !rv.scopeAll) {
        const still = rv.queue.items.find(it => it.annotation.id === id);
        if (!still) {
            rv.index = Math.max(0, Math.min(rv.index, rv.queue.items.length - 1));
            rvRender();
        }
    }
}

function rvEditCurrent() {
    const item = rvItem();
    if (!item) return;
    const a = item.annotation;
    modal('修改批注 #' + item.annotation_number,
        field('批注内容', 'rvEditText', a.text) +
        field('区域 [左,上,右,下]', 'rvEditBounds', JSON.stringify(a.bounds)) +
        '<label>类型<select id="rvEditKind"><option value="issue"' + (a.kind === 'issue' ? ' selected' : '') +
        '>问题</option><option value="note"' + (a.kind === 'note' ? ' selected' : '') +
        '>备注</option><option value="approved"' + (a.kind === 'approved' ? ' selected' : '') +
        '>确认</option></select></label>' + submitButton());
    $('#save').onclick = run(async () => {
        await rvMutate({
            text: $('#rvEditText').value,
            bounds: JSON.parse($('#rvEditBounds').value),
            kind: $('#rvEditKind').value,
        }, '批注已修改');
        $('#dialog').close();
    });
}

function rvGo(delta) {
    if (!rv.queue) return;
    rv.index = Math.max(0, Math.min(rv.queue.items.length - 1, rv.index + delta));
    rvRender();
}

function rvReportModal(summary, imported) {
    const cards = [
        ['applied', '已更新'], ['unchanged', '无需更新'],
        ['duplicate', '重复导回'], ['skipped', '已跳过'],
    ].map(([key, label]) => '<div><b>' + summary[key] + '</b><span>' + label + '</span></div>').join('');
    const rows = summary.results.map((r, i) => {
        const tag = { applied: '已更新', unchanged: '无需更新', duplicate: '重复导回', skipped: '已跳过' }[r.status];
        const e = r.entry || {};
        return '<tr><td>' + (i + 1) + '</td><td>P' + (r.page_number ?? e.page_number ?? '-') +
            ' #' + (r.annotation_number ?? e.annotation_number ?? '-') + '</td>' +
            '<td><span class="rv-tag ' + r.status + '">' + tag + '</span></td><td>' +
            esc(r.reason || '状态与意见已写回原批注') + '</td></tr>';
    }).join('');
    modal(imported ? '审阅包导回结果（逐条可核对）' : '审阅包预检结果',
        '<p class="small">审阅包编号：' + esc(summary.export_id || '-') +
        '　档案匹配：' + (summary.document_match ? '是' : '否（不会写回）') + '</p>' +
        '<div class="rv-summary-cards">' + cards + '</div>' +
        '<div class="rv-report-scroll"><table class="rv-report"><thead><tr><th>#</th><th>位置</th><th>结果</th><th>核对说明</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>' +
        (imported ? '' : '<div class="dialogactions"><button id="rvConfirmImport">确认导回本档案</button></div>'));
}

let rvPendingFile = null;
function rvPickZip(onFile) {
    rvPendingFile = onFile || rvAnalyzeFile;
    const input = $('#reviewZip');
    input.value = '';
    input.click();
}

async function rvAnalyzeFile(file) {
    const form = new FormData();
    form.append('file', file);
    const summary = await rvApi(rvBase() + '/review-package/analyze', 'POST', form);
    rvReportModal(summary, false);
    $('#rvConfirmImport').onclick = run(async () => {
        const importForm = new FormData();
        importForm.append('file', file);
        importForm.append('revision', doc.revision);
        const result = await rvApi(rvBase() + '/review-package/import', 'POST', importForm);
        doc = result.document;
        await documents();
        render();
        $('#dialog').close();
        rvReportModal(result.summary, true);
        if ($('#reviewOverlay').hidden === false) await rvLoadQueue();
    });
}

document.addEventListener('DOMContentLoaded', () => {
    $('#reviewFlow').onclick = run(rvOpen);
    $('#reviewExport').onclick = run(async () => {
        if (!doc) throw Error('先选择档案');
        window.open('/archive/documents/' + doc.id + '/review-package');
    });
    $('#reviewImport').onclick = run(async () => {
        if (!doc) throw Error('先选择档案');
        rv.docId = doc.id;
        rvPickZip(null);
    });
    $('#reviewZip').onchange = run(async () => {
        const input = $('#reviewZip');
        if (input.files[0] && rvPendingFile) {
            const handler = rvPendingFile;
            rvPendingFile = null;
            await handler(input.files[0]);
        }
    });
    $('#rvClose').onclick = () => { $('#reviewOverlay').hidden = true; };
    $('#rvScope').onchange = run(async () => {
        rv.scopeAll = $('#rvScope').checked;
        await rvLoadQueue();
    });
    $('#rvExport').onclick = () => window.open(rvBase() + '/review-package');
    $('#rvResolve').onclick = run(rvToggleResolved);
    $('#rvEdit').onclick = rvEditCurrent;
    $('#rvPrev').onclick = () => rvGo(-1);
    $('#rvNext').onclick = () => rvGo(1);
    document.addEventListener('keydown', event => {
        if ($('#reviewOverlay').hidden || $('#dialog').open) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowLeft') { event.preventDefault(); rvGo(-1); }
        if (event.key === 'ArrowUp' || event.key === 'ArrowRight') { event.preventDefault(); rvGo(1); }
    });
});

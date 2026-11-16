const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let doc=null,pageId=null,uploadVersion=false,deliverySelection=null;
async function api(url,method='GET',data){let options={method};if(data instanceof FormData)options.body=data;else if(data!==undefined){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(data)}let response=await fetch(url,options);if(!response.ok){let error=await response.json();throw Error(error.error||'操作失败')}return response.json()}
function toast(text){$('#toast').textContent=text;$('#toast').style.display='block';setTimeout(()=>$('#toast').style.display='none',3500)}
function run(action){return async(...args)=>{try{await action(...args)}catch(error){toast(error.message)}}}
function page(){return doc?.pages.find(p=>p.id===pageId)}function version(){const p=page();return p?.versions.find(v=>v.id===p.current)}function redactions(v){return v.redactions||[]}
function base(){if(!doc)throw Error('先选择档案');return '/archive/documents/'+doc.id}function pb(){if(!page())throw Error('先选择页面');return base()+'/pages/'+pageId}
function modal(title,content){$('#dialogTitle').textContent=title;$('#dialogBody').innerHTML=content;$('#dialog').showModal()}
function jsonModal(title,data){modal(title,'<pre>'+esc(JSON.stringify(data,null,2))+'</pre>')}
function imageModal(title,url,extra=''){modal(title,extra+'<img src="'+esc(url)+'">')}
function field(label,id,value){return '<label>'+esc(label)+'<input type="text" id="'+id+'" value="'+esc(value)+'"></label>'}
function submitButton(){return '<div class="dialogactions"><button id="save">保存</button></div>'}
function values(id){return JSON.parse($('#'+id).value)}
async function documents(){let items=await api('/archive/documents');$('#documents').innerHTML=items.map(d=>'<div class="document '+(d.id===doc?.id?'active':'')+'" data-id="'+d.id+'"><strong>'+esc(d.name)+'</strong><p class="small">'+d.page_count+' 页 / '+d.version_count+' 个版次</p></div>').join('');$('#documents').querySelectorAll('.document').forEach(el=>el.onclick=run(async()=>{doc=await api('/archive/documents/'+el.dataset.id);pageId=doc.pages[0]?.id;render();await documents()}))}
function setDoc(next){doc=next;if(!page())pageId=doc.pages[0]?.id;render();documents()}
function render(){if(!doc)return;$('#title').textContent=doc.name;$('#notes').textContent=doc.notes;$('#revision').textContent='REV '+doc.revision;$('#pageCount').textContent=doc.pages.length;$('#pages').innerHTML=doc.pages.map((p,i)=>'<div class="page '+(p.id===pageId?'active':'')+'" data-id="'+p.id+'"><img src="'+base()+'/pages/'+p.id+'/redacted?size=240&mask='+($('#maskToggle').checked?'1':'0')+'&r='+doc.revision+'"><strong>'+String(i+1).padStart(2,'0')+' '+esc(p.name)+'</strong></div>').join('');$('#pages').querySelectorAll('.page').forEach(el=>el.onclick=()=>{pageId=el.dataset.id;render()});const p=page(),v=version();$('#image').hidden=!v;$('#empty').hidden=!!v;if(!v){$('#versions').innerHTML='';$('#annotations').innerHTML='';$('#redactions').innerHTML='';return}$('#pageTitle').textContent=p.name;$('#image').src=pb()+'/redacted?version='+v.id+'&mask='+($('#maskToggle').checked?'1':'0')+'&annotations='+($('#showAnnotations').checked?'1':'0')+'&r='+doc.revision;$('#dimensions').textContent=v.pixels.join(' × ')+' px / '+v.trim_mm.map(x=>x.toFixed(1)).join(' × ')+' mm';$('#versions').innerHTML=p.versions.map(item=>'<option value="'+item.id+'" '+(item.id===p.current?'selected':'')+'>'+esc(item.label)+'</option>').join('');$('#status').value=v.status;
$('#redactions').innerHTML=redactions(v).length?redactions(v).map((r,i)=>'<div class="redaction"><div class="purpose">'+(i+1)+' · '+esc(r.purpose)+'</div><small>'+r.bounds.map(x=>x.toFixed(3)).join(', ')+'</small><br><button data-id="'+r.id+'" data-action="edit">编辑</button> <button data-id="'+r.id+'" data-action="delete">删除</button></div>').join(''):'<p class="small">本版次暂无遮敏区域。</p>';
$('#redactions').querySelectorAll('button').forEach(button=>button.onclick=run(async()=>{const r=redactions(v).find(r=>r.id===button.dataset.id),url=pb()+'/redactions/'+r.id;if(button.dataset.action==='delete'){setDoc(await api(url,'DELETE',{revision:doc.revision,version_id:v.id}));return}modal('编辑遮敏区域',field('遮挡用途','redactionPurpose',r.purpose)+field('区域 [左,上,右,下]','redactionBounds',JSON.stringify(r.bounds))+submitButton());$('#save').onclick=run(async()=>{setDoc(await api(url,'PATCH',{revision:doc.revision,version_id:v.id,purpose:$('#redactionPurpose').value,bounds:values('redactionBounds')}));$('#dialog').close()})}));$('#annotations').innerHTML=v.annotations.map((a,i)=>'<div class="annotation '+(a.resolved?'resolved':'')+'"><b>'+(i+1)+' · '+esc(a.kind)+'</b><div>'+esc(a.text)+'</div><small>'+a.bounds.map(x=>x.toFixed(3)).join(', ')+'</small><br><button data-id="'+a.id+'" data-action="resolve">'+(a.resolved?'重开':'解决')+'</button> <button data-id="'+a.id+'" data-action="edit">编辑</button> <button data-id="'+a.id+'" data-action="delete">删除</button></div>').join('');$('#annotations').querySelectorAll('button').forEach(button=>button.onclick=run(async()=>{const a=v.annotations.find(a=>a.id===button.dataset.id),url=pb()+'/annotations/'+a.id;if(button.dataset.action==='delete'){setDoc(await api(url,'DELETE',{revision:doc.revision,version_id:v.id}));return}if(button.dataset.action==='resolve'){setDoc(await api(url,'PATCH',{revision:doc.revision,version_id:v.id,resolved:!a.resolved}));return}modal('编辑区域批注',field('内容','annotationText',a.text)+field('区域 [左,上,右,下]','annotationBounds',JSON.stringify(a.bounds))+submitButton());$('#save').onclick=run(async()=>{setDoc(await api(url,'PATCH',{revision:doc.revision,version_id:v.id,text:$('#annotationText').value,bounds:values('annotationBounds')}));$('#dialog').close()})}))}
$('#close').onclick=()=>$('#dialog').close();$('#showAnnotations').onchange=render;$('#maskToggle').onchange=render;
function beginDraw(){
  const image=$('#image'),layer=$('#drawLayer');if(image.hidden)throw Error('先选择页面');
  layer.hidden=false;
  const box=image.getBoundingClientRect(),stage=$('#stage').getBoundingClientRect();
  layer.width=image.clientWidth;layer.height=image.clientHeight;
  layer.style.width=image.clientWidth+'px';layer.style.height=image.clientHeight+'px';
  layer.style.left=(box.left-stage.left)+'px';layer.style.top=(box.top-stage.top)+'px';
  const context=layer.getContext('2d');context.clearRect(0,0,layer.width,layer.height);
  let start=null;
  const point=event=>{const rect=layer.getBoundingClientRect();return [Math.min(1,Math.max(0,(event.clientX-rect.left)/rect.width)),Math.min(1,Math.max(0,(event.clientY-rect.top)/rect.height))]};
  const paint=event=>{const [x,y]=point(event);if(!start)return;context.clearRect(0,0,layer.width,layer.height);context.fillStyle='rgba(200,64,46,.35)';context.strokeStyle='#c8402e';context.lineWidth=2;const x0=Math.min(start[0],x),x1=Math.max(start[0],x),y0=Math.min(start[1],y),y1=Math.max(start[1],y);context.fillRect(x0*layer.width,y0*layer.height,(x1-x0)*layer.width,(y1-y0)*layer.height);context.strokeRect(x0*layer.width,y0*layer.height,(x1-x0)*layer.width,(y1-y0)*layer.height)};
  const cleanup=()=>{layer.hidden=true;layer.onpointerdown=layer.onpointermove=layer.onpointerup=null;document.removeEventListener('keydown',escape)};
  const escape=event=>{if(event.key==='Escape')cleanup()};
  document.addEventListener('keydown',escape);
  const finish=run(async event=>{
    if(!start)return;
    const [x,y]=point(event);const bounds=[Math.min(start[0],x),Math.min(start[1],y),Math.max(start[0],x),Math.max(start[1],y)];
    cleanup();start=null;
    if(bounds[2]-bounds[0]<0.005||bounds[3]-bounds[1]<0.005){toast('框选区域太小，请重新拖动');return}
    const v=version();
    modal('新增遮敏区域',field('遮挡用途（仅内部可见，不进入交付包）','redactionPurpose','')+'<p class="small">用途仅内部留存，不写入交付包。交付图会把该区域整体替换为不透明色块。</p>'+submitButton());
    $('#redactionPurpose').focus();
    $('#save').onclick=run(async()=>{
      const purpose=$('#redactionPurpose').value.trim();
      if(!purpose){toast('请填写遮挡用途，例如“客户电话”或“未公开报价”');$('#redactionPurpose').focus();return}
      setDoc(await api(pb()+'/redactions','POST',{revision:doc.revision,version_id:v.id,bounds,purpose}));
      deliverySelection=null;$('#dialog').close();toast('已记录遮敏区域');
    });
  });
  layer.onpointerdown=event=>{event.preventDefault();layer.setPointerCapture(event.pointerId);start=point(event)};
  layer.onpointermove=paint;
  layer.onpointerup=finish;
  layer.onpointercancel=cleanup;
  toast('在图上拖动框住需要遮挡的区域，Esc 取消');
}
$('#redact').onclick=run(async()=>{pb();if(!$('#image').complete||!$('#image').clientWidth)throw Error('请等待图像加载后再框选');beginDraw()});
$('#new').onclick=run(async()=>{modal('新建校样档案',field('档案名','name','新校样档案')+field('备注','note','')+submitButton());$('#save').onclick=run(async()=>{setDoc(await api('/archive/documents','POST',{name:$('#name').value,notes:$('#note').value}));$('#dialog').close()})});
$('#delivery').onclick=run(async()=>{
  base();const pages=doc.pages.filter(p=>p.versions.length);
  if(!pages.length)throw Error('档案暂无页面');
  // Selection is kept per page and per version, so a version switch stays visible instead of reusing old coordinates.
  const selection=deliverySelection||{};
  pages.forEach(p=>{if(selection[p.id])return;const current=p.versions.find(item=>item.id===p.current);
    selection[p.id]={version_id:current.id,ids:redactions(current).map(r=>r.id),chosen:redactions(current).length>0}});
  deliverySelection=selection;
  const currentOf=p=>p.versions.find(item=>item.id===p.current);
  const rows=()=>pages.map(p=>{const current=currentOf(p),picked=selection[p.id];
    const stale=picked.version_id!==current.id;
    const existing=redactions(current).map(r=>r.id);
    const missing=stale?[]:picked.ids.filter(id=>!existing.includes(id));
    const oldLabel=stale?(p.versions.find(v=>v.id===picked.version_id)?.label||'已不存在'):'';
    const warning=stale?'所选区域来自旧版次「'+esc(oldLabel)+'」，不适用于当前版次'
      :(missing.length?'所选区域中有 '+missing.length+' 处已从当前版次移除':'');
    return '<div class="deliver'+((stale||missing.length)?' missing':'')+'"><label><input type="checkbox" data-page="'+p.id+'" '+(picked.chosen?'checked':'')+'><span><b>'+String(doc.pages.indexOf(p)+1).padStart(2,'0')+' '+esc(p.name)+'</b><small>当前版次 '+esc(current.label)+' · 遮敏 '+redactions(current).length+' 处'+(warning?' · <span style="color:#a5482e">'+warning+'</span>':'')+'</small></span></label>'
      +((stale||missing.length)?(existing.length
        ?'<button data-adopt="'+p.id+'" style="margin-left:20px;font-size:11px;padding:4px 7px">改用当前版次「'+esc(current.label)+'」的 '+existing.length+' 处区域</button>'
        :'<p class="small" style="margin:6px 0 0 20px">当前版次没有任何遮敏区域：请先关闭此窗，在本页重新框选后再交付。</p>'):'')
      + redactions(current).map(r=>'<label style="margin-left:20px"><input type="checkbox" data-redaction="'+r.id+'" '+(!stale&&picked.ids.includes(r.id)?'checked':'')+'><span>'+esc(r.purpose)+' <small>'+r.bounds.map(x=>x.toFixed(3)).join(', ')+'</small></span></label>').join('')
      +'</div>'}).join('');
  modal('遮敏交付', '<p class="small">按具体版次选择遮挡区域。若页面换了版次，旧版区域会标为不适用并阻止生成；点击“改用当前版次”才会采用新版区域，工具不会把旧版位置挪用于新版。</p>'
    +'<div id="deliverPages"></div><div class="banner" id="deliverCheck">正在核对…</div>'
    +'<div class="formrow"><button id="deliverPreview">查看遮挡前后</button><button id="deliverRun">生成遮敏交付包 ↓</button></div>');
  const payload=()=>({pages:pages.filter(p=>selection[p.id]?.chosen).map(p=>({page_id:p.id,version_id:selection[p.id].version_id,redaction_ids:selection[p.id].ids}))});
  async function check(){
    const box=$('#deliverCheck');
    if(!payload().pages.length){box.className='banner block';box.textContent='请至少选择一个页面。';return null}
    let result;try{result=await api(base()+'/delivery/plan','POST',payload())}catch(error){box.className='banner block';box.textContent=error.message;return null}
    if(result.issues.length){box.className='banner block';box.innerHTML='本次不能生成：<br>'+result.issues.map(i=>'• '+esc(i.reason)).join('<br>')+(result.page_count?'<br>另有 '+result.page_count+' 页 / '+result.redaction_count+' 处遮挡已通过核对。':'')}
    else if(!result.page_count){box.className='banner block';box.textContent='所选页面的当前版次没有遮敏区域。'}
    else{box.className='banner warn';box.textContent='将交付 '+result.page_count+' 页、'+result.redaction_count+' 处遮挡。清单含页码、版次、区域位置与遮挡数量；下载包只含遮敏图与清单，不含原图、批注或内部用途备注。'}
    return {...result,ready:!result.issues.length&&result.page_count>0,payload:payload()};
  }
  const bind=()=>{
    $('#deliverPages').querySelectorAll('[data-page]').forEach(input=>input.onchange=()=>{selection[input.dataset.page].chosen=input.checked;check()});
    $('#deliverPages').querySelectorAll('[data-redaction]').forEach(input=>input.onchange=()=>{
      const owner=pages.find(p=>redactions(currentOf(p)).some(r=>r.id===input.dataset.redaction));
      const picked=selection[owner.id];
      if(picked.version_id!==currentOf(owner).id)return;
      picked.ids=input.checked?[...picked.ids,input.dataset.redaction]:picked.ids.filter(id=>id!==input.dataset.redaction);
      picked.chosen=true;check();
    });
    $('#deliverPages').querySelectorAll('[data-adopt]').forEach(button=>button.onclick=run(async()=>{
      const p=pages.find(p=>p.id===button.dataset.adopt),current=currentOf(p);
      selection[p.id]={version_id:current.id,ids:redactions(current).map(r=>r.id),chosen:redactions(current).length>0};
      renderRows();toast('已改用当前版次「'+current.label+'」的遮敏区域');
    }));
  };
  const renderRows=()=>{$('#deliverPages').innerHTML=rows();bind()};
  const downloadPackage=async data=>{
    const response=await fetch(base()+'/delivery/package',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(!response.ok){const error=await response.json();throw Error(error.error||'交付包生成失败')}
    const link=document.createElement('a');link.href=URL.createObjectURL(await response.blob());link.download='redacted-'+doc.id+'.zip';link.click();URL.revokeObjectURL(link.href);toast('已生成遮敏交付包');
  };
  $('#deliverPreview').onclick=run(async()=>{
    const data=await check();if(!data||!data.ready)return;
    const first=data.payload.pages.find(p=>p.redaction_ids.length);
    const picked=doc.pages.find(p=>p.id===first.page_id);
    const versionId=first.version_id;
    const response=await fetch(base()+'/delivery/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data.payload,page_id:first.page_id,size:900})});
    if(!response.ok){const error=await response.json();throw Error(error.error||'预览失败')}
    const maskedUrl=URL.createObjectURL(await response.blob());
    const beforeUrl=base()+'/pages/'+first.page_id+'/redacted?version='+versionId+'&mask=0&annotations=0&size=900&ids='+first.redaction_ids.join(',');
    $('#dialogBody').innerHTML='<p class="small">'+esc(picked.name)+' · 左为标出遮挡区域的内部视图，右为交付图的实际像素。</p><div class="compare"><figure><img src="'+esc(beforeUrl)+'"><figcaption>遮挡前 · 红框为遮敏区域</figcaption></figure><figure><img src="'+esc(maskedUrl)+'"><figcaption>遮挡后 · 交付图像素</figcaption></figure></div><div class="dialogactions"><button id="deliverRun">生成遮敏交付包 ↓</button><button id="deliverBack">返回选择</button></div>';
    $('#deliverRun').onclick=run(async()=>downloadPackage(data.payload));
    $('#deliverBack').onclick=()=>{$('#dialog').close();$('#delivery').click()};
  });
  $('#deliverRun').onclick=run(async()=>{const data=await check();if(!data||!data.ready)return;await downloadPackage(data.payload)});
  renderRows();await check();
});
$('#demo').onclick=run(async()=>{setDoc(await api('/archive/demo','POST',{}));toast('已装载程序合成的三页校样')});
$('#upload').onclick=run(async()=>{base();uploadVersion=false;$('#files').click()});$('#versionUpload').onclick=run(async()=>{pb();uploadVersion=true;$('#files').click()});
$('#files').onchange=run(async()=>{for(const file of $('#files').files){const form=new FormData();form.append('file',file);form.append('revision',doc.revision);if(uploadVersion)form.append('page_id',pageId);setDoc(await api(base()+'/upload','POST',form));if(!uploadVersion)pageId=doc.pages.at(-1).id}render();$('#files').value=''});
$('#versions').onchange=run(async()=>setDoc(await api(pb()+'/current','POST',{revision:doc.revision,version_id:$('#versions').value})));
$('#download').onclick=run(async()=>window.open(pb()+'/download?version='+version().id));
$('#package').onclick=run(async()=>window.open(base()+'/package'));$('#report').onclick=run(async()=>window.open(base()+'/report'));
$('#import').onclick=()=>$('#zipfile').click();$('#zipfile').onchange=run(async()=>{const form=new FormData();form.append('file',$('#zipfile').files[0]);setDoc(await api('/archive/import','POST',form));$('#zipfile').value=''});
$('#preflight').onclick=run(async()=>jsonModal('版面预检',await api(base()+'/preflight')));
$('#contact').onclick=run(async()=>imageModal('页面联系样张',base()+'/contact?columns=3&width=300','<p>当前按档案页序生成单张联系表；完整页名与图像放置坐标保存在 <a target="_blank" href="'+base()+'/contact?view=index">联系表索引</a>。</p>'));
$('#spec').onclick=run(async()=>{const v=version();pb();modal('版面规格',field('页面名','pageName',page().name)+field('版次名','versionLabel',v.label)+field('成品尺寸 mm [宽,高]','trim',JSON.stringify(v.trim_mm))+field('声明出血 mm [左,上,右,下]','bleed',JSON.stringify(v.bleed_mm))+submitButton());$('#save').onclick=run(async()=>{setDoc(await api(pb()+'/versions/'+v.id,'PATCH',{revision:doc.revision,name:$('#pageName').value,label:$('#versionLabel').value,trim_mm:values('trim'),bleed_mm:values('bleed')}));$('#dialog').close()})});
$('#transform').onclick=run(async()=>{pb();modal('生成裁切与旋转校样',field('原图归一化裁切框','crop','[0,0,1,1]')+field('顺时针角度 0 / 90 / 180 / 270','rotation','0')+'<p>生成新版本，保留原版。与裁切相交的批注随图变换，裁切外批注不复制。</p>'+submitButton());$('#save').onclick=run(async()=>{setDoc(await api(pb()+'/transform','POST',{revision:doc.revision,version_id:version().id,crop:values('crop'),rotation:Number($('#rotation').value)}));$('#dialog').close()})});
$('#review').onclick=run(async()=>{pb();modal('记录审校意见',field('意见','reviewNote','')+submitButton());$('#save').onclick=run(async()=>{setDoc(await api(pb()+'/review','POST',{revision:doc.revision,version_id:version().id,status:$('#status').value,note:$('#reviewNote').value}));$('#dialog').close()})});
$('#annotate').onclick=run(async()=>{pb();modal('新增区域批注',field('内容','annotationText','此处需要核对')+field('区域 [左,上,右,下]','annotationBounds','[0.1,0.1,0.5,0.3]')+'<label>类型<select id="kind"><option value="issue">问题</option><option value="note">备注</option><option value="approved">确认</option></select></label>'+submitButton());$('#save').onclick=run(async()=>{setDoc(await api(pb()+'/annotations','POST',{revision:doc.revision,version_id:version().id,text:$('#annotationText').value,bounds:values('annotationBounds'),kind:$('#kind').value}));$('#dialog').close()})});
$('#stats').onclick=run(async()=>{pb();modal('区域像素统计',field('区域 [左,上,右,下]','statsBounds','[0,0,1,1]')+submitButton());$('#save').onclick=run(async()=>jsonModal('区域统计结果',await api(pb()+'/statistics','POST',{version_id:version().id,bounds:values('statsBounds')})))});
$('#compare').onclick=run(async()=>{const p=page();pb();if(p.versions.length<2)throw Error('当前页需要两个版次');const options=p.versions.map(v=>'<option value="'+v.id+'">'+esc(v.label)+'</option>').join('');modal('比较版次','<div class="formrow"><label>基准<select id="compareA">'+options+'</select></label><label>对照<select id="compareB">'+options+'</select></label></div><label>定位<select id="anchor"><option value="center">像素居中</option><option value="top_left">左上角</option></select></label><button id="compareRun">显示差分</button><div id="compareResult"></div>');$('#compareB').value=p.current;$('#compareRun').onclick=run(async()=>{const url=pb()+'/compare?a='+$('#compareA').value+'&b='+$('#compareB').value+'&anchor='+$('#anchor').value;const result=await api(url);$('#compareResult').innerHTML='<p>变化像素 '+result.changed_pixels+' / '+result.compared_pixels+' ('+(result.changed_fraction*100).toFixed(2)+'%)</p><div class="formrow"><a href="'+url+'&view=heat" target="_blank">热图 ↗</a><a href="'+url+'&view=side_by_side" target="_blank">并排 ↗</a></div><img src="'+url+'&view=overlay"><pre>'+esc(JSON.stringify(result,null,2))+'</pre>'});$('#compareRun').click()});
$('#separation').onclick=run(async()=>{pb();const url=pb()+'/separation?version='+version().id;const report=await api(url);modal('软分色与总墨量','<p>简化软分色，透明像素按纸白合成。输出用于版面诊断，不替代印厂 ICC。</p><div class="formrow">'+['c','m','y','k','ink'].map(view=>'<button data-channel="'+view+'">'+view.toUpperCase()+'</button>').join('')+'</div><img id="channelImage" src="'+url+'&view=ink"><pre>'+esc(JSON.stringify(report,null,2))+'</pre>');$('#dialogBody').querySelectorAll('[data-channel]').forEach(button=>button.onclick=()=>$('#channelImage').src=url+'&view='+button.dataset.channel)});
$('#snapshots').onclick=run(async()=>{base();modal('档案快照',field('新快照名','snapshotName','审校前')+'<button id="createSnapshot">保存快照</button>'+doc.snapshots.map(s=>'<div class="snap">'+esc(s.name)+' <small>'+esc(s.at)+'</small><button data-snapshot="'+s.id+'">恢复</button></div>').join(''));$('#createSnapshot').onclick=run(async()=>{setDoc(await api(base()+'/snapshots','POST',{revision:doc.revision,name:$('#snapshotName').value}));$('#dialog').close()});$('#dialogBody').querySelectorAll('[data-snapshot]').forEach(button=>button.onclick=run(async()=>{setDoc(await api(base()+'/snapshots/'+button.dataset.snapshot+'/restore','POST',{revision:doc.revision}));$('#dialog').close()}))});
$('#moveUp').onclick=run(async()=>{pb();const ids=doc.pages.map(p=>p.id),i=ids.indexOf(pageId);if(i<1)return;[ids[i-1],ids[i]]=[ids[i],ids[i-1]];setDoc(await api(base()+'/order','POST',{revision:doc.revision,page_ids:ids}))});
$('#duplicate').onclick=run(async()=>setDoc(await api(pb()+'/duplicate','POST',{revision:doc.revision})));$('#remove').onclick=run(async()=>setDoc(await api(pb(),'DELETE',{revision:doc.revision})));
documents().catch(error=>toast(error.message));

import {useEffect,useRef,useState} from 'react';
import {importProgressSummary} from '../utils/importProgress.js';

async function request(path='',body) {
  const response=await fetch('/__serein/imports'+path,{method:body?'POST':'GET',
    ...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
  const result=await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'导入服务暂不可用，请检查文件大小和格式后重试。');
  return result;
}

export function ConversationImport({onImported,active=true}) {
  const [mode,setMode]=useState('auto'),[tagging,setTagging]=useState(true),[job,setJob]=useState(null);
  const [history,setHistory]=useState([]),[tags,setTags]=useState({}),[status,setStatus]=useState(''),[busy,setBusy]=useState(false);
  const [dragging,setDragging]=useState(false);
  const [largeBatch,setLargeBatch]=useState(false);
  const running=['queued','running'].includes(job?.task?.status);
  const mounted=useRef(true),notified=useRef(new Set()),polling=useRef(false);
  const [tagErrors,setTagErrors]=useState([]);
  useEffect(()=>{setLargeBatch(job?.task?.arguments?.large_batch===true);},[job?.id]);
  async function refresh() {
    const result=await request();if(!mounted.current)return;
    setHistory(result.items);setTags(result.tagging);setTagErrors(result.tagging_errors||[]);
    setJob(current=>result.items.find(item=>item.id===current?.id)||current||result.items.find(item=>['queued','running'].includes(item.task?.status))||result.items[0]||null);
    for(const item of result.items)if(item.task?.status==='completed'&&item.format!=='operit'&&item.inserted&&!notified.current.has(item.id)){
      notified.current.add(item.id);onImported?.();
    }
    return result;
  }
  useEffect(()=>{
    mounted.current=true;
    return()=>{mounted.current=false;};
  },[]);
  useEffect(()=>{
    if(!active)return;
    const poll=async()=>{if(document.hidden||polling.current)return;polling.current=true;
      try{await refresh();}catch(error){if(mounted.current)setStatus(error.message);}finally{polling.current=false;}};
    poll();const timer=setInterval(poll,2000);
    const visible=()=>{if(!document.hidden)poll();};
    document.addEventListener('visibilitychange',visible);
    return()=>{clearInterval(timer);document.removeEventListener('visibilitychange',visible);};
  },[active]);
  async function preview(file) {
    if(!file || busy)return;
    if(file.size>32_000_000){setStatus('文件超过 32 MB，请按会话拆分后导入。');return;}
    setBusy(true);setStatus('正在识别文件…');
    try {
      const result=await request('/preview',{filename:file.name,content:await file.text(),mode,tagging});
      if(mounted.current){setJob(result);setStatus(result.status==='completed'?'这份文件已处理过，请查看下方导入结果。':'已识别文件。核对预览后点击开始导入。');await refresh();}
    }catch(error){if(mounted.current)setStatus(error.message);}finally{if(mounted.current)setBusy(false);}
  }
  async function run() {
    if(!job || busy || running)return;
    setBusy(true);setStatus('正在提交后台导入任务…');
    try {
      const task=await request('/'+encodeURIComponent(job.id)+'/continue',{large_batch:largeBatch});
      if(mounted.current){setJob(current=>({...current,task}));setStatus('后台任务已启动，可切换页面；回来会恢复当前进度。');await refresh();}
    }catch(error){if(mounted.current)setStatus(error.message);}
    finally{if(mounted.current)setBusy(false);}
  }
  async function pause() {
    try{await request('/'+encodeURIComponent(job.id)+'/pause',{});setStatus('将在当前批次保存后暂停。');await refresh();}
    catch(error){if(mounted.current)setStatus(error.message);}
  }
  async function includeInEvents(skip=false) {
    if(!job || busy || job.format==='operit' || (skip&&!job.event_boundary_active))return;
    setBusy(true);setStatus(skip?'正在保存处理边界…':'正在提交后台摘要任务…');
    try{
      await request('/'+encodeURIComponent(job.id)+(skip?'/skip-summary':'/summarize'),{});
      await refresh();
      setStatus(skip?'已跳过这份导入至最后一句；原文保留，后续新聊天可正常整理。':'已提交后台摘要，从这份导入的开头处理尚未摘要的原文；可离开页面，在配置页查看进度。无需开启自动摘要。');
      onImported?.();
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  async function retryTagging(limit) {
    if(busy)return;
    setBusy(true);
    try{const result=await request('/retry-tagging',limit?{limit}:{});await refresh();setStatus(`已重新加入 ${result.queued} 条打标任务，每条只尝试一次。`);}
    catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  return <div className="conversation-import">
    <div className="settings-group__heading"><h4>对话与记忆导入</h4>
      <p>历史聊天导入后，可立即在后台从头摘要，或暂不处理并跳到末尾；Operit 记忆备份逐条保留为 Scene，正文不改写。</p></div>
    <label className="settings-field"><span>文件类型</span><select disabled={busy} value={mode} onChange={event=>setMode(event.target.value)}>
      <option value="auto">自动识别</option><option value="conversation">聊天记录</option><option value="operit">Operit 记忆库</option></select></label>
    <label className="settings-toggle"><span><strong>Operit 导入后自动打标</strong>
      <small>补充主域、实体和召回线索 cues，保留正文与已有 cues。在“配置”页选择“打标”模型后执行，留空则等待配置。</small></span>
      <input type="checkbox" role="switch" checked={tagging} disabled={busy} onChange={event=>setTagging(event.target.checked)}/></label>
    <label className={'import-upload'+(dragging?' is-dragging':'')} onDragOver={event=>{event.preventDefault();if(!busy)setDragging(true);}}
      onDragLeave={()=>setDragging(false)} onDrop={event=>{event.preventDefault();setDragging(false);preview(event.dataTransfer.files[0]);}}>
      <strong>选择文件，或拖到这里</strong><span>Claude、ChatGPT、SillyTavern（酒馆）、DeepSeek / 通用 JSON、JSONL、Markdown、TXT、Operit 备份 · 最大 32 MB</span>
      <input type="file" aria-label="导入聊天记录或 Operit 记忆库" accept=".json,.jsonl,.md,.txt" disabled={busy}
        onChange={event=>{preview(event.target.files[0]);event.target.value='';}}/>
    </label>
    {history.length>0&&<label className="settings-field"><span>最近的导入</span><select disabled={busy} value={job?.id||''}
      onChange={event=>{setJob(history.find(item=>item.id===event.target.value));setStatus('');}}>
      {history.map(item=><option key={item.id} value={item.id}>{item.filename} · {item.processed}/{item.total}</option>)}</select></label>}
    {job&&<div className="import-preview">
      <p><strong>{job.filename}</strong> · {job.format==='operit'?'Operit 记忆库':`${job.sessions} 个对话`} · {job.total} 条</p>
      {job.format!=='operit'&&<p className="import-help">{job.event_boundary_active
        ?(job.summary_choice==='skip'?'已暂不处理：这份导入已跳到最后一句，原文仍可搜索、读取和绑定证据。':'历史聊天已保留，尚未摘要。导入完成后请选择处理方式。')
        :'这份历史聊天已加入 Event 整理池，会和后续新聊天一样按原话归线、切分。'}</p>}
      {job.warnings.map((message,index)=><p key={index} className="import-help">{message}</p>)}
      <details><summary>查看内容预览</summary>{job.preview.map((item,index)=><blockquote key={index}>
        <strong>{item.title || (item.role==='user'?'用户':'AI')}</strong><p>{item.text}</p></blockquote>)}</details>
      <progress aria-label="导入进度" max={job.total} value={job.processed}/>
      <p>当前阶段：{({queued:'等待后台处理',running:'正在导入',completed:'导入结束',paused:'已暂停',interrupted:'已中断，可继续',failed:'导入失败'})[job.task?.status]||'等待导入'}</p>
      {job.task?.error&&<p className="import-error">{job.task.error}</p>}
      <p>已处理 {job.processed}/{job.total} · 新增 {job.inserted} · 重复 {job.duplicate} · 失败 {job.failed}</p>
      {importProgressSummary(job.task)&&<p className="import-help">{importProgressSummary(job.task)}</p>}
      {job.status!=='completed'&&<label className="settings-toggle"><span><strong>大批量导入</strong>
        <small>适合较大的历史文件。每批最多 250 条；关闭时最多 25 条。长内容会自动缩小批次，暂停在当前批次保存后生效。</small></span>
        <input type="checkbox" role="switch" checked={largeBatch} disabled={busy||running} onChange={event=>setLargeBatch(event.target.checked)}/></label>}
      {job.errors.map(error=><p className="import-error" key={error.entry}>第 {error.entry} 条：{error.message}</p>)}
      <div className="settings-actions">{job.status!=='completed'&&<button type="button" disabled={busy||running} onClick={run}>{job.processed?'继续导入':'开始导入'}</button>}
        {running&&<button type="button" onClick={pause}>暂停</button>}
        {job.status==='completed'&&job.format!=='operit'&&job.inserted>0&&
          <><button type="button" disabled={busy} onClick={()=>includeInEvents()}>{job.event_boundary_active?'立即从头摘要（后台）':'继续后台摘要'}</button>
          <button type="button" disabled={busy||!job.event_boundary_active||job.summary_choice==='skip'} onClick={()=>includeInEvents(true)}>暂不处理，并推游标到最后一句</button></>}</div>
    </div>}
    {Object.keys(tags).length>0&&<p className="import-help">记忆打标：等待 {tags.pending||0} · 完成 {tags.done||0} · 失败 {tags.failed||0} · 因编辑跳过 {tags.stale||0}
      <button className="import-refresh" type="button" disabled={busy} onClick={()=>refresh().catch(error=>setStatus(error.message))}>刷新</button>
      {tags.failed>0&&<><button className="import-refresh" type="button" disabled={busy} onClick={()=>retryTagging(1)}>先重试 1 条</button>
        <button className="import-refresh" type="button" disabled={busy} onClick={()=>retryTagging()}>重试全部失败（{tags.failed} 条）</button></>}</p>}
    {tags.failed>0&&<p className="import-help">失败不会自动重试，原文与成功结果保留。模型可能已生成并计费，只是返回格式未通过校验；请先核对错误，再重试 1 条，确认成功后再处理其余失败项。</p>}
    <p role="status">{status}</p>
    {tagErrors.map(item=><p className="import-error" key={item.document_id}>{item.document_id}：{item.error}</p>)}
  </div>;
}

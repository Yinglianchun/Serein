import {useEffect,useRef,useState} from 'react';
import {ConversationImport} from './ConversationImport.jsx';
import {instanceSettings} from '../storage/instanceStore.js';

export function PipelineSettings({onOpenSummary}) {
  const [task,setTask]=useState(null),[output,setOutput]=useState(''),[status,setStatus]=useState(''),[busy,setBusy]=useState(false);
  const [work,setWork]=useState(null);
  const [limits,setLimits]=useState(null);
  const mounted=useRef(true),polling=useRef(false),rebuildDialog=useRef(null);
  const [rebuildTarget,setRebuildTarget]=useState('');
  const running=['queued','running'].includes(work?.status);
  const needsRepair=work?.status==='needs_repair'||work?.result?.status==='needs_repair';
  const failure=work?.error||(needsRepair?work?.result?.reason:'');
  const stages={idle:'尚未开始',queued:'等待后台处理',starting:'正在准备',track_router:'归线',event_curator:'切分整理',event_writer:'Event 写作',awaiting_agent:'等待 Agent',processed:'已保存',current:'整理完成',needs_repair:'归线材料待修复',rebuilt:'计划已重建'};
  async function call(action,body) {
    const response=await fetch('/__serein/pipeline/'+action,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok)throw new Error(result.detail || '整理暂未完成，原话会保留，请稍后重试。');
    return result;
  }
  function accept(result){setWork(result);setTask(result.status==='awaiting_agent'?result.result:null);}
  useEffect(()=>{
    mounted.current=true;
    instanceSettings().then(value=>{if(mounted.current){setLimits(value.pipeline);}}).catch(error=>{if(mounted.current)setStatus(error.message);});
    const refresh=async()=>{
      if(polling.current)return;polling.current=true;
      try{const result=await call('status');if(mounted.current)accept(result);}
      catch(error){if(mounted.current)setStatus(error.message);}finally{polling.current=false;}
    };
    const saved=event=>setLimits(event.detail.pipeline);window.addEventListener('serein:settings-saved',saved);
    refresh();const timer=setInterval(refresh,2000);
    const visible=()=>{if(!document.hidden)refresh();};document.addEventListener('visibilitychange',visible);
    return()=>{window.removeEventListener('serein:settings-saved',saved);mounted.current=false;clearInterval(timer);document.removeEventListener('visibilitychange',visible);};
  },[]);
  async function next() {
    if(busy||running)return;
    setBusy(true);setStatus(needsRepair?'正在请求重新校验归线材料…':'正在提交后台整理任务…');
    try {
      const result=await call('next',{include_recent:true});accept(result);setOutput('');
      setStatus('后台任务已提交，可离开页面；已完成步骤会保留。');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  function beginRebuild() {
    setRebuildTarget(work?.result?.batch_id||work?.batch_id||'');
    rebuildDialog.current.showModal();
  }
  async function confirmRebuild() {
    if(busy||!rebuildTarget)return;
    setBusy(true);
    try {
      const result=await call('rebuild',{batch_id:rebuildTarget,confirm:'REBUILD_PIPELINE_BATCH'});
      if(result.status!=='rebuilt')throw new Error('整理任务正在运行，请稍后刷新再操作。');
      rebuildDialog.current.close();setRebuildTarget('');setTask(null);setOutput('');
      accept(await call('status'));
      accept(await call('next',{include_recent:true}));
      setStatus('旧计划与模型结果已保留，正在重新归线；已保存的 Event 不变。');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  function download() {
    const url=URL.createObjectURL(new Blob([JSON.stringify(task,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=task.role+'-task.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function downloadAttempt(id){
    try{
      const result=await call('attempts/'+id);
      const url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download='pipeline-attempt-'+id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){setStatus(error.message);}
  }
  async function submit() {
    setBusy(true);
    try {await call('submit',{job_id:task.job_id,output:JSON.parse(output)});setTask(null);setOutput('');accept(await call('next',{include_recent:true}));setStatus('结果已校验并保存，后台继续下一步。');}
    catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  return <section className="settings-group"><div className="settings-group__heading"><h3>原话整理</h3>
    <p>归线 → 切分与转录 → Event 写作。在“配置”页选择执行方式、三阶段模型和整理参数。Scene 由聊天中的 agent 主动写。</p></div>
    <ConversationImport onImported={()=>setStatus('原话已导入。点击继续整理，进入下一步。')} />
    {limits&&<p>{limits.auto_enabled===false?'自动整理已暂停；手动点击“继续整理”仍可启动任务。':'自动整理已开启。'}</p>}
    <button type="button" className="settings-link" onClick={onOpenSummary}>自动摘要配置</button>
    {work&&<div aria-live="polite"><p>当前阶段：{stages[work.stage]||work.stage} · 本批已完成 {work.completed||0} 个步骤 · 已保存 {work.events??work.result?.events??0} 条 Event</p>
      {work.prompt_chars>0&&<p>本次提示词 {work.prompt_chars} 字符 · 超时 {work.timeout_seconds} 秒 · 第 {work.attempt||1} 次尝试</p>}
      {work.status==='interrupted'&&<p>任务已中断，可以继续。</p>}
      {work.result?.note&&<p>{work.result.note}</p>}
      {work.result?.pending>0&&<p>本批仍有 {work.result.pending} 条原话等待后续处理。</p>}
      {work.result?.deferred>0&&<p>暂缓 {work.result.deferred} 条原话；其中 {work.result.protected_deferrals?.length||0} 条事件提案涉及已有内容保护。可对照原话与已有事件人工处理。</p>}
      {work.result?.skipped>0&&<p>本批跳过 {work.result.skipped} 条原话，原始记录仍保留。</p>}
      {failure&&<p className="import-error">{needsRepair?'待修复原因':'失败原因'}：{failure}</p>}
      {needsRepair&&<p>批次：<code>{work.result?.batch_id||work.batch_id}</code>。原话与已完成步骤保留。先重新校验以恢复历史归线；无法恢复时，可明确作废本批计划并重新归线。不会跳过原话或删除已保存的 Event。</p>}
      {task&&<p>等待 {stages[task.role]||task.role}：下载任务交给 Agent，再提交返回的 JSON。</p>}</div>}
    {work?.attempts?.length>0&&<details><summary>模型返回与纠错记录</summary>{work.attempts.map(item=><p key={item.id}>
      第 {item.attempt} 次：{item.error||'校验通过'} · {item.output_chars} 字符 <button type="button" onClick={()=>downloadAttempt(item.id)}>下载返回</button></p>)}</details>}
    <div className="settings-actions"><button type="button" disabled={busy||running} onClick={next}>{running?'后台整理中…':needsRepair?'重新校验并继续':'继续整理'}</button>
      {needsRepair&&<button type="button" disabled={busy||running} onClick={beginRebuild}>作废本批计划并重新归线</button>}
      {task&&<button type="button" onClick={download}>下载 agent 任务</button>}</div>
    {task&&<><label className="settings-field"><span>Agent 返回的 JSON</span><textarea rows={8} value={output} onChange={event=>setOutput(event.target.value)}/></label>
      <div className="settings-actions"><button type="button" disabled={busy||!output.trim()} onClick={submit}>提交并校验</button></div></>}
    <dialog ref={rebuildDialog} className="agent-guide" aria-labelledby="pipeline-rebuild-title" onCancel={()=>setRebuildTarget('')}>
      <h3 id="pipeline-rebuild-title">重新生成这批原话的整理计划？</h3>
      <p>批次：<code>{rebuildTarget}</code></p>
      <p>旧的归线、切分和 Writer 结果会保留为历史记录，但不会再用于新计划。尚未处理的原话将重新归线，可能需要重新调用模型。</p>
      <p>原话和已经保存的 Event 不会被删除。此操作不会把原话标成已处理或跳过。</p>
      <div className="settings-actions"><button type="button" disabled={busy} onClick={()=>{rebuildDialog.current.close();setRebuildTarget('');}}>取消</button>
        <button type="button" disabled={busy||!rebuildTarget} onClick={confirmRebuild}>确认作废旧计划并重新归线</button></div>
      <p role="status">{status}</p>
    </dialog>
    <p role="status">{status}</p></section>;
}

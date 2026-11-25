import {useEffect,useRef,useState} from 'react';
import {ConversationImport} from './ConversationImport.jsx';
import {instanceSettings} from '../storage/instanceStore.js';
import {PendingMaterials,displayPipelineReason} from './PendingMaterials.jsx';

function progressMessage(report) {
  if(report.stage==='done')return '已完成整理。';
  if(report.stage==='empty')return '目前没有已进入整理的原话。';
  if(report.stage==='blocked')return '当前整理已暂停或受阻，暂不估算剩余时间。';
  const stage=report.stage==='routing'?'归线':'结算';
  if(report.eta_reliable)return `${stage}阶段预计还需约 ${report.eta_human}（按近期速度估算，不含后续阶段）。`;
  if(report.eta_reason==='stalled')return `${stage}阶段近期没有进展，暂不估算剩余时间。`;
  if(report.eta_reason==='paused_or_throttled')return `${stage}阶段近期没有新的处理记录，暂不估算剩余时间。`;
  return `${stage}阶段近期样本不足，暂不估算剩余时间。`;
}

export function PipelineSettings({onOpenSummary,active=true}) {
  const [task,setTask]=useState(null),[output,setOutput]=useState(''),[status,setStatus]=useState(''),[busy,setBusy]=useState(false);
  const [work,setWork]=useState(null);
  const [progressReport,setProgressReport]=useState(null),[progressBusy,setProgressBusy]=useState(false);
  const [limits,setLimits]=useState(null);
  const mounted=useRef(true),polling=useRef(false),rebuildDialog=useRef(null),restoreDialog=useRef(null);
  const [rebuildTarget,setRebuildTarget]=useState('');
  const recoveryDialog=useRef(null);
  const [recovery,setRecovery]=useState(null),[recoveryLimit,setRecoveryLimit]=useState('');
  const running=['queued','running'].includes(work?.status);
  const holds=work?.blocked_scopes||[];
  const repairHold=holds.find(item=>item.hold_status==='needs_repair');
  const needsRepair=work?.status==='needs_repair'||work?.result?.status==='needs_repair'||Boolean(repairHold);
  const repairInfo=repairHold||(work?.result?.status==='needs_repair'?work.result:work);
  const runtimeContractRepair=needsRepair&&repairInfo?.repair_kind==='runtime_contract';
  const failure=work?.error||(needsRepair?work?.result?.reason:'');
  const candidateOverflows=work?.result?.candidate_overflow_deferrals||[];
  const curatorOmissions=work?.result?.curator_omission_deferrals||[];
  const failedBatch=['failed','interrupted'].includes(work?.status)&&work.batch_id&&!holds.some(item=>item.batch_id===work.batch_id)?work.batch_id:null;
  const roleNames={track_router:'归线',event_curator:'切分',event_writer:'Event 写作'};
  const autoMessage=limits?.auto_enabled===false?'自动整理已暂停；手动点击“继续整理”仍可启动任务。'
    :work?.pending_materials?.some(item=>['open','running'].includes(item.status))?'自动整理已开启；有材料留在下方待处理区，独立的新材料可继续整理。'
    :work?.auto_boundary_originals>0?`自动整理已开启，但 ${work.auto_boundary_originals} 条旧原话在启用边界外，需要手动恢复。`
    :work?.status==='awaiting_agent'?'自动整理已开启，当前等待 Agent 领取任务。'
    :work?.status==='failed'||work?.status==='paused'||needsRepair?'自动整理已开启，当前批次需要处理下方的失败原因。'
    :running?'自动整理正在后台运行。'
    :work?.unassigned_roles?.length===3?'自动整理已开启，但三个阶段都未配置模型，自动模型任务不会启动；请到“自动摘要配置”选择阶段模型，或切换 Agent 模式并让 Agent 领取任务。'
    :work?.unassigned_roles?.length?`自动整理已开启，但${work.unassigned_roles.map(role=>roleNames[role]||role).join('、')}未配置模型；运行到这些阶段会等待 Agent。请到“自动摘要配置”选择阶段模型。`
    :work?.execution_mode==='agent'?'自动整理已开启，当前为 Agent 模式；点击“继续整理”可创建任务，等待 Agent 领取。'
    :work?.stage==='settled_today'?'今天的自动检查已完成；新原话会在下一轮检查。'
    :'自动整理已开启；后台在每天 03:00 后检查新原话。要立即运行可点击“继续整理”。';
  const stages={paused:'本批已暂停',idle:'尚未开始',queued:'等待后台处理',starting:'正在准备',track_router:'归线',event_curator:'切分整理',event_writer:'Event 写作',awaiting_agent:'等待 Agent',processed:'本批处理完成',current:'本轮检查完成',retained:'已移入待处理材料',needs_repair:'归线材料待修复',rebuilt:'计划已重建',blocked:'部分聊天等待处理',retry_wait:'等待有限重试'};
  async function call(action,body) {
    const response=await fetch('/__serein/pipeline/'+action,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok)throw new Error(result.detail || '整理暂未完成，原话会保留，请稍后重试。');
    return result;
  }
  function accept(result){setWork(result);setTask(result.status==='awaiting_agent'?result.result:null);}
  async function showProgress() {
    setProgressBusy(true);
    try {setProgressReport(await call('progress'));setStatus('');}
    catch(error){setStatus(error.message);}finally{setProgressBusy(false);}
  }
  useEffect(()=>{
    mounted.current=true;
    return()=>{mounted.current=false;};
  },[]);
  useEffect(()=>{
    if(!active)return;
    instanceSettings().then(value=>{if(mounted.current){setLimits(value.pipeline);}}).catch(error=>{if(mounted.current)setStatus(error.message);});
    const refresh=async()=>{
      if(document.hidden||polling.current)return;polling.current=true;
      try{const result=await call('status');if(mounted.current)accept(result);}
      catch(error){if(mounted.current)setStatus(error.message);}finally{polling.current=false;}
    };
    const saved=event=>setLimits(event.detail.pipeline);window.addEventListener('serein:settings-saved',saved);
    refresh();const timer=setInterval(refresh,2000);
    const visible=()=>{if(!document.hidden)refresh();};document.addEventListener('visibilitychange',visible);
    return()=>{window.removeEventListener('serein:settings-saved',saved);clearInterval(timer);document.removeEventListener('visibilitychange',visible);};
  },[active]);
  async function next() {
    if(busy||running)return;
    setBusy(true);setStatus(runtimeContractRepair?'正在请求继续整理其他聊天；旧计划仍等待明确重建…':needsRepair?'正在请求重新校验归线材料…':'正在提交后台整理任务…');
    try {
      const result=await call('next',{include_recent:true});accept(result);setOutput('');
      setStatus(runtimeContractRepair?'已提交继续请求；版本变化的旧计划仍需明确重建，其他可处理聊天继续。':'后台任务已提交，可离开页面；已完成步骤会保留。');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  function beginRebuild(batchId) {
    setRebuildTarget(typeof batchId==='string'?batchId:repairHold?.batch_id||work?.result?.batch_id||work?.batch_id||'');
    rebuildDialog.current.showModal();
  }
  async function retryBatch(batchId) {
    if(busy||running)return;
    setBusy(true);
    try {
      const result=await call('retry-batch',{batch_id:batchId});
      if(result.status==='busy')throw new Error('整理任务正在运行，请稍后重试。');
      accept(await call('next',{include_recent:true}));
      setStatus('已恢复批次，从失败步骤继续，成功结果保留。');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  async function retryImage(sha256) {
    if(busy||running)return;
    setBusy(true);
    try {
      const result=await call('retry-image',{sha256});
      if(result.status==='busy')throw new Error('整理任务正在运行，请稍后重试。');
      accept(await call('status'));
      accept(await call('next',{include_recent:true}));
      setStatus('已重新开放这张图片的三次转录机会，正在继续整理。');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  async function restoreBoundary() {
    if(busy||running)return;
    setBusy(true);
    try {
      const result=await call('restore-auto-boundary',{confirm:'RESTORE_AUTO_BOUNDARY'});
      if(result.status==='busy')throw new Error('整理任务正在运行，请稍后重试。');
      restoreDialog.current.close();
      accept(await call('next',{include_recent:true}));
      setStatus(`已恢复 ${result.restored_originals} 条自动边界外原话，后台开始整理；已结算记录不变。`);
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
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
  async function beginRecovery(action,batchId) {
    if(busy||running)return;
    setBusy(true);setStatus('');
    try {
      const preview=await call('recovery-preview',{batch_id:batchId});
      setRecovery({action,preview});setRecoveryLimit(String(preview.max_input_chars));
      recoveryDialog.current.showModal();
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  async function previewRecovery() {
    if(busy||!recovery)return;
    setBusy(true);
    try {
      const preview=await call('recovery-preview',{batch_id:recovery.preview.batch_id,max_input_chars:Number(recoveryLimit)});
      setRecovery({...recovery,preview});setStatus('');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  async function confirmRecovery() {
    if(busy||!recovery)return;
    setBusy(true);
    try {
      const skip=recovery.action==='skip';
      const result=await call(skip?'skip-batch':'rebatch',{batch_id:recovery.preview.batch_id,
        confirm:skip?'SKIP_PIPELINE_BATCH':'REBATCH_PIPELINE_BATCH',...(!skip?{max_input_chars:recovery.preview.max_input_chars}:{})});
      if(result.status!==(skip?'skipped_batch':'rebatched'))throw new Error('整理任务正在运行，请稍后刷新再操作。');
      recoveryDialog.current.close();setRecovery(null);setTask(null);setOutput('');
      const message=skip?`已跳过本批 ${result.skipped_originals} 条原话的 Event 整理，原文保留。`:`已拆成 ${result.batches} 批，旧计划与模型结果保留。`;
      setStatus(message);
      try {accept(await call('next',{include_recent:true}));setStatus(message+'后台继续整理。');}
      catch(error){accept(await call('status'));setStatus(message+'后续任务未启动：'+error.message);}
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  function recoveryButtons(batchId) {
    return <><button type="button" disabled={busy||running} onClick={()=>beginRecovery('rebatch',batchId)}>按更小材料重新整理</button>
      <button type="button" disabled={busy||running} onClick={()=>beginRecovery('skip',batchId)}>跳过本批，保留原话</button></>;
  }
  function download() {
    const url=URL.createObjectURL(new Blob([JSON.stringify(task,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=task.role+'-task.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function submit() {
    setBusy(true);
    try {const saved=await call('submit',{job_id:task.job_id,output:JSON.parse(output)});setTask(null);setOutput('');accept(await call('next',{include_recent:true}));setStatus(saved.status==='host_deferred'?'Curator 再次遗漏原话，所在审阅范围已留待下轮；原话没有跳过。':'结果已校验并保存，后台继续下一步。');}
    catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  return <section className="settings-group"><div className="settings-group__heading"><h3>原话整理</h3>
    <p>归线 → 切分与转录 → Event 写作。在“自动摘要管理”页选择执行方式、三阶段模型和整理参数。Scene 由聊天中的 agent 主动写。</p></div>
    <ConversationImport active={active} onImported={()=>setStatus('原话已导入。点击继续整理，进入下一步。')} />
    <p><button type="button" className="settings-link" disabled={progressBusy} onClick={showProgress}>查看 Event 整理进度与预计时间</button></p>
    {progressReport&&<p role="status">本次查看：原话档案 {progressReport.archived} 条 · 已进入整理 {progressReport.admitted} 条 · 已归线 {progressReport.routed} 条 · 已结算 {progressReport.processed} 条 · 当前阶段剩余 {progressReport.pending} 条。{progressMessage(progressReport)}</p>}
    {limits&&<p>{autoMessage}</p>}
    {work?.auto_boundary_originals>0&&<p>有 {work.auto_boundary_originals} 条原话在开启自动整理时被划到边界外。它们仍保存在原话档案中，点击“继续整理”不会处理这些原话。<button type="button" disabled={busy||running} onClick={()=>restoreDialog.current.showModal()}>恢复并整理这批原话</button></p>}
    <button type="button" className="settings-link" onClick={onOpenSummary}>自动摘要管理</button>
    {work&&<div aria-live="polite"><p>当前阶段：{stages[work.stage]||work.stage} · 本批已完成 {work.completed||0} 个步骤 · 已保存 {work.events??work.result?.events??0} 条 Event</p>
      {work.prompt_chars>0&&<p>本次提示词 {work.prompt_chars} 字符 · 超时 {work.timeout_seconds} 秒 · 第 {work.attempt||1} 次尝试</p>}
      {work.status==='interrupted'&&<p>任务已中断，可以继续。</p>}
      {work.result?.note&&<p>{work.result.note}</p>}
      {work.result?.pending>0&&<p>本批仍有 {work.result.pending} 条原话等待后续处理。</p>}
      {work.result?.deferred>0&&<p>暂缓 {work.result.deferred} 条原话；其中 {work.result.protected_deferrals?.length||0} 条事件提案涉及已有内容保护。可对照原话与已有事件人工处理。</p>}
      {curatorOmissions.map((item,index)=><p key={index} className="import-error">Curator {item.reason==='curator_omission_dependency'?'所在范围与漏项共享原话，已一起留待下轮':'未说明原话去向，已将所在审阅范围留待下轮'}：{item.missing_source_message_ids.join('、')}。本轮没有将这些原话标为跳过。</p>)}
      {candidateOverflows.map(item=><p key={item.track_id} className="import-error">Track <code>{item.track_id}</code> 有 {item.eligible_active_leaf_count} 条 active Event leaves，超过上限 {item.limit}；本批未调用 Curator 或 Writer。请先归档误归线或不再需要的 Event，或人工安全合并相关 leaves。</p>)}
      {work.result?.skipped>0&&<p>本批跳过 {work.result.skipped} 条原话，原始记录仍保留。</p>}
      {work.result?.missing_images?.length>0&&<p>已跳过 {work.result.missing_images.length} 个缺失的图片附件；原话文字保留，未猜补图片内容。</p>}
      {work.failed_images?.length>0&&<details className="settings-disclosure"><summary>图片转录记录（{work.failed_images.length}）</summary>
        <p>缺少转录的图片只记录张数，文字照常参与整理。</p>
        {work.failed_images.map(image=><p key={image.sha256}>图片 {image.sha256.slice(0,8)}{' '}
          <button type="button" disabled={busy||running} onClick={()=>retryImage(image.sha256)}>重试这张图片</button></p>)}
      </details>}
      {holds.filter(item=>item.hold_status!=='paused_failure').map(item=><p className="import-error" key={item.batch_id}>
        聊天 <code>{item.scope}</code>：{item.hold_status==='needs_repair'?(item.repair_kind==='runtime_contract'?'旧计划版本已变化':'归线证据待修复'):item.hold_status==='retry_wait'?'等待有限重试':'归线尚未完成'}。{item.repair_kind==='runtime_contract'?'重新校验不会迁移旧计划；请先查看重建确认。':displayPipelineReason(item.reason)}
        {item.next_retry_at&&<> 下次重试：{new Date(item.next_retry_at).toLocaleString()}。</>}
        {' '}原文保留，此聊天按序等待，其他聊天可继续；当日尚未全部结算。
        {item.hold_status==='needs_repair'&&<button type="button" disabled={busy||running} onClick={()=>beginRebuild(item.batch_id)}>查看重建确认</button>}
        {recoveryButtons(item.batch_id)}
      </p>)}
      {(work.paused_batches||[]).map(batch=><p className="import-error" key={batch.batch_id}>本批已暂停：{displayPipelineReason(batch.reason)} 同一聊天的后续整理等待它恢复，其他聊天可继续。<button type="button" disabled={busy||running} onClick={()=>retryBatch(batch.batch_id)}>重试此批次</button>{recoveryButtons(batch.batch_id)}</p>)}
      {failedBatch&&<p className="import-error">批次：<code>{failedBatch}</code>。{recoveryButtons(failedBatch)}</p>}
      {failure&&<p className="import-error">{needsRepair?'待修复原因':'失败原因'}：{displayPipelineReason(failure)}</p>}
      {needsRepair&&<p>批次：<code>{repairHold?.batch_id||work.result?.batch_id||work.batch_id}</code>。原话与已完成步骤保留。{runtimeContractRepair?'当前程序与冻结旧计划的版本不一致，重新校验不会迁移旧计划。请查看重建确认，明确同意后才重新归线；也可继续整理其他聊天。':'先重新校验以恢复历史归线；无法恢复时，可明确作废本批计划并重新归线。'}不会跳过原话或删除已保存的 Event。</p>}
      {task&&<p>等待 {stages[task.role]||task.role}：下载任务交给 Agent，再提交返回的 JSON。</p>}</div>}
    <div className="settings-actions"><button type="button" disabled={busy||running} onClick={next}>{running?'后台整理中…':runtimeContractRepair?'继续整理其他聊天':needsRepair?'重新校验并继续':'继续整理'}</button>
      {needsRepair&&<button type="button" disabled={busy||running} onClick={beginRebuild}>{runtimeContractRepair?'查看重建确认':'作废本批计划并重新归线'}</button>}
      {task&&<button type="button" onClick={download}>下载 agent 任务</button>}</div>
    {task&&<><label className="settings-field"><span>Agent 返回的 JSON</span><textarea rows={8} value={output} onChange={event=>setOutput(event.target.value)}/></label>
      <div className="settings-actions"><button type="button" disabled={busy||!output.trim()} onClick={submit}>提交并校验</button></div></>}
    <dialog ref={rebuildDialog} className="agent-guide" aria-labelledby="pipeline-rebuild-title" onCancel={()=>setRebuildTarget('')}>
      <h3 id="pipeline-rebuild-title">重新生成这批原话的整理计划？</h3>
      <p>若缺少可验证的历史 Track，此次确认会从本批原话重新开始归线；该聊天后续批次也不再引用截断点之前的归线历史。</p>
      <p>批次：<code>{rebuildTarget}</code></p>
      <p>旧的归线、切分和 Writer 结果会保留为历史记录，但不会再用于新计划。尚未处理的原话将重新归线，可能需要重新调用模型。</p>
      <p>原话和已经保存的 Event 不会被删除。此操作不会把原话标成已处理或跳过。</p>
      <div className="settings-actions"><button type="button" disabled={busy} onClick={()=>{rebuildDialog.current.close();setRebuildTarget('');}}>取消</button>
        <button type="button" disabled={busy||!rebuildTarget} onClick={confirmRebuild}>确认作废旧计划并重新归线</button></div>
      <p role="status">{status}</p>
    </dialog>
    <dialog ref={recoveryDialog} className="agent-guide" aria-labelledby="pipeline-recovery-title" onCancel={()=>setRecovery(null)}>
      {recovery&&<><h3 id="pipeline-recovery-title">{recovery.action==='skip'?'跳过这批 Event 整理？':'按更小材料重新整理'}</h3>
        <p>批次：<code>{recovery.preview.batch_id}</code></p>
        <p>范围：{recovery.preview.originals} 条未整理原话，共 {recovery.preview.original_chars} 字符。<br/>
          {new Date(recovery.preview.first_at).toLocaleString()} — {new Date(recovery.preview.last_at).toLocaleString()}</p>
        <p>原文、旧任务记录和已保存的 Event 保留。只处理上述范围，后续新聊天仍会继续整理。</p>
        {recovery.action==='skip'?<p>确认后，这批原话不再自动生成 Event。尚未完成的对话尾巴不在跳过范围内。</p>:<>
          <label className="settings-field"><span>本批每次最多读取的原话字符</span><input type="number" min="2000" max="100000" value={recoveryLimit} onChange={event=>setRecoveryLimit(event.target.value)}/></label>
          <button type="button" disabled={busy} onClick={previewRecovery}>查看拆分结果</button>
          {Number(recoveryLimit)!==recovery.preview.max_input_chars?<p>上限已修改，请重新查看拆分结果。</p>:<>
            <p>按完整问答拆成 {recovery.preview.batches} 批，最大一批 {Math.max(...recovery.preview.batch_chars)} 字符。此上限仅用于本批，不改变全局设置。</p>
            {!recovery.preview.can_rebatch&&<p>当前上限无法继续拆分，可降低上限，或取消后选择跳过本批。</p>}
            {recovery.preview.oversized_batches>0&&<p>其中 {recovery.preview.oversized_batches} 批包含超长完整问答，仍会整包保留，可能再次失败。</p>}
          </>}
          <p>新计划使用当前设置，按顺序重新归线和写作，可能产生模型调用费用。</p>
        </>}
        <div className="settings-actions"><button type="button" disabled={busy} onClick={()=>{recoveryDialog.current.close();setRecovery(null);}}>取消</button>
          <button type="button" disabled={busy||(recovery.action==='rebatch'&&(!recovery.preview.can_rebatch||Number(recoveryLimit)!==recovery.preview.max_input_chars))} onClick={confirmRecovery}>{recovery.action==='skip'?'确认跳过本批':'确认拆小并整理'}</button></div>
        <p role="status">{status}</p></>}
    </dialog>
    <dialog ref={restoreDialog} className="agent-guide" aria-labelledby="pipeline-restore-title">
      <h3 id="pipeline-restore-title">恢复自动边界外的原话？</h3>
      <p>将 {work?.auto_boundary_originals||0} 条原话重新放入待整理池，并启动一次后台整理。后续模型调用可能产生费用；已结算和主动跳过的原话不会重跑。</p>
      <div className="settings-actions"><button type="button" onClick={()=>restoreDialog.current.close()}>取消</button><button type="button" disabled={busy||running} onClick={restoreBoundary}>确认恢复并整理</button></div>
    </dialog>
    <PendingMaterials work={work} onChange={accept}/>
    <p role="status">{status}</p></section>;
}

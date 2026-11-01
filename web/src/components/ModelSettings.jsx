import { useEffect, useRef, useState } from "react";
import { instanceSettings } from "../storage/instanceStore.js";
import {UpstreamSettings} from "./UpstreamSettings.jsx";
import {upstreamModels,taskModelOptions} from "../modelOptions.js";
import {AgentGuide} from './AgentGuide.jsx';
import {PendingMaterials} from './PendingMaterials.jsx';
import {RecallThresholdSettings} from './RecallThresholdSettings.jsx';
import {upstreamsForSave} from '../upstreamSecrets.js';
import {memoryAvailabilitySummary,prepareMemoryIndex,indexSafetySummary,resumeIndex,readIndexStatus} from '../memoryPreparation.js';

const tasks = {writer:"Narrative Writer",embedding:"Embedding",reranker:"Reranker",
  relations:"Scene 关系",dreams:"梦境",narrative_scout:"叙事卷找材料",persona:"心绪/防撤退",
  track_router:"原话 · 归线",image_transcription:"图片转录 / 眼睛（聊天 / 自动摘要）",event_curator:"原话 · 切分",event_writer:"原话 · Event 写作",operit_tagging:"打标",arc_linker:"Event · Arc 归档"};

const taskGroups = [
  {key:"creation",title:"对话与创作",help:"陪伴状态、梦境和叙事内容使用的模型。",tasks:["writer","persona","dreams","narrative_scout"]},
  {key:"retrieval",title:"记忆检索与整理",help:"负责检索、关系判断、打标和归档。",tasks:["embedding","reranker","relations","operit_tagging","arc_linker"]},
  {key:"events",title:"原话自动摘要",help:"从原话归线、读图、切分，再写成 Event。",tasks:["image_transcription","track_router","event_curator","event_writer"]},
];

const taskLinks = {
  image_transcription:{href:"https://www.agnes-ai.com/zh-Hans/docs/agnes-30-flash",label:"Agnes 3.0 Flash（暂时免费）"},
  embedding:{href:"https://cloud.siliconflow.cn/i/NCXr2PLP",label:"硅基流动（邀请链接）"},
  reranker:{href:"https://cloud.siliconflow.cn/i/NCXr2PLP",label:"硅基流动（邀请链接）"},
};

export function ModelSettings({page,onOpenEventGuide,onOpenPipeline,onOpenCatalog,onOpenAssignments,
  recallThreshold,setRecallThreshold,candidateThresholdDraft,setCandidateThresholdDraft,
  passageDraft,setPassageDraft}) {
  const [config,setConfig]=useState(null);
  const [indexDraft,setIndexDraft]=useState({});
  const [pipelineDraft,setPipelineDraft]=useState({});
  const embeddingBatchSize=indexDraft.embedding_batch_size ?? config?.recall?.embedding_batch_size ?? 16;
  const embeddingConcurrency=indexDraft.embedding_concurrency ?? config?.recall?.embedding_concurrency ?? 1;
  const passagesEnabled=passageDraft.passages_enabled ?? config?.recall?.passages_enabled ?? false;
  const passageMinChars=passageDraft.passage_min_chars ?? config?.recall?.passage_min_chars ?? 500;
  const threshold=recallThreshold ?? config?.recall?.direct_threshold ?? 0.65;
  const bodyCandidateThreshold=candidateThresholdDraft.body_candidate_threshold ?? config?.recall?.body_candidate_threshold ?? 0.50;
  const cueCandidateThreshold=candidateThresholdDraft.cue_candidate_threshold ?? config?.recall?.cue_candidate_threshold ?? 0.55;
  const directPoolLimit=candidateThresholdDraft.direct_pool_limit ?? config?.recall?.direct_pool_limit ?? 20;
  const validThreshold=threshold!=='' && Number.isFinite(Number(threshold)) && Number(threshold)>=0 && Number(threshold)<=1;
  const validCandidateThresholds=[bodyCandidateThreshold,cueCandidateThreshold].every(value=>
    value!=='' && Number.isFinite(Number(value)) && Number(value)>=0 && Number(value)<=1);
  const upstreamForm=useRef(null);
  const [agentDraft,setAgentDraft]=useState(null);
  const [eventAssignments,setEventAssignments]=useState({});
  const [status,setStatus]=useState("");
  const [busy,setBusy]=useState(false);
  const [pendingWork,setPendingWork]=useState(null);
  const pendingCount=pendingWork?.pending_materials?.filter(item=>['open','running'].includes(item.status)).length||0;
  useEffect(()=>{
    let active=true;
    instanceSettings().then(value=>{if(active)setConfig(value);}).catch(error=>{if(active)setStatus(error.message);});
    const saved=event=>setConfig(current=>current?{...current,settings_version:event.detail.settings_version,recall:event.detail.recall,features:event.detail.features,pipeline:current.pipeline}:current);
    window.addEventListener('serein:settings-saved',saved);
    return()=>{active=false;window.removeEventListener('serein:settings-saved',saved);};
  },[]);
  useEffect(()=>{
    if(page !== 'configuration')return;
    let active=true, pending=false;
    const poll=async()=>{
      if(pending)return;
      pending=true;
      try {
        const state=await readIndexStatus();
        if(active)setConfig(current=>current?{...current,index_status:state}:current);
      } catch { /* Keep the last observed state; manual refresh reports failures. */ }
      finally{pending=false;}
    };
    poll();
    const timer=window.setInterval(poll,15000);
    return()=>{active=false;window.clearInterval(timer);};
  },[page]);
  async function refreshIndexStatus() {
    const state=await readIndexStatus();
    setConfig(current=>({...current,index_status:state}));
  }
  function option(key,value) {setConfig(current=>({...current,upstream:{...current.upstream,[key]:value}}));}
  function pipelineOption(key,value) {
    setPipelineDraft(current=>({...current,[key]:value}));
    setConfig(current=>({...current,pipeline:{...current.pipeline,[key]:value}}));
  }
  const availableModels=config?[...config.models,...upstreamModels(config.upstreams || [])]:[];
  function changeUpstreams(upstreams) {
    const ids=new Set([...config.models,...upstreamModels(upstreams)].map(model=>model.id));
    setConfig(current=>({...current,upstreams,assignments:Object.fromEntries(Object.entries(current.assignments).map(([key,value])=>[key,ids.has(value)?value:""]))}));
    setStatus("上游设置尚未保存。");
  }
  async function save(event,catalogOnly=false) {
    event.preventDefault();setBusy(true);setStatus("");
    try {
      const models=config.models.map(({api_key_configured,clear_key,...model})=>{
        if(clear_key)model.api_key="";
        else if(!model.api_key)delete model.api_key;
        if(!model.dimension)delete model.dimension;
        return model;
      });
      const upstreams=upstreamsForSave(config.upstreams || [],upstreamForm.current);
      if(catalogOnly){
        const result=await instanceSettings({expected_version:config.settings_version,models,upstreams,assignments:Object.fromEntries(Object.entries(config.assignments).filter(([key])=>!(key in eventAssignments)))});
        setConfig(current=>({...current,settings_version:result.settings_version,models:result.models,upstreams:result.upstreams,assignments:{...result.assignments,...eventAssignments},
          memory_ready:result.memory_ready,index_status:result.index_status,upstream:{...current.upstream,memory_enabled:result.upstream.memory_enabled}}));
        setStatus("上游与模型已保存。");
        return;
      }
      if(!validThreshold)throw new Error('召回阈值需填写 0 到 1 之间的数字。');
      if(!validCandidateThresholds)throw new Error('候选扩展门槛需填写 0 到 1 之间的数字。');
      if(directPoolLimit===''||!Number.isInteger(Number(directPoolLimit))||Number(directPoolLimit)<6||Number(directPoolLimit)>100)
        throw new Error('候选池上限需填写 6 到 100 之间的整数。');
      if(passageMinChars===''||!Number.isInteger(Number(passageMinChars))||Number(passageMinChars)<1||Number(passageMinChars)>100000)
        throw new Error('长文起切字数需填写 1 到 100000 之间的整数。');
      for(const [value,max,label] of [[embeddingBatchSize,64,'向量每批条数'],[embeddingConcurrency,4,'向量请求并发数']])
        if(value===''||!Number.isInteger(Number(value))||Number(value)<1||Number(value)>max)throw new Error(`${label}需填写 1 到 ${max} 之间的整数。`);
      const result=await instanceSettings({expected_version:config.settings_version,models,upstreams,assignments:Object.fromEntries(Object.entries(config.assignments).filter(([key])=>!taskGroups.find(group=>group.key==="events").tasks.includes(key))),dream:config.dream,
        recall:{...passageDraft,...Object.fromEntries(Object.entries(indexDraft).map(([key,value])=>[key,key==='index_catch_up'?value:Number(value)])),...('passage_min_chars' in passageDraft?{passage_min_chars:Number(passageMinChars)}:{}),
          ...(recallThreshold!==null?{direct_threshold:Number(threshold)}:{}),
          ...Object.fromEntries(Object.entries(candidateThresholdDraft).map(([key,value])=>[key,Number(value)]))},upstream:{
        writer_enabled:config.upstream.writer_enabled,memory_enabled:config.upstream.memory_enabled,operit_enabled:config.upstream.operit_enabled}});
      setConfig(current=>({...result,pipeline:current.pipeline,assignments:{...result.assignments,...eventAssignments}}));setIndexDraft({});setRecallThreshold(null);setCandidateThresholdDraft({});setPassageDraft({});setStatus("设置已保存。");
    } catch(error){setStatus(error.message);}
    finally{setBusy(false);}
  }
  function eventOption(key,value) {
    setEventAssignments(current=>({...current,[key]:value}));
    setConfig(current=>({...current,assignments:{...current.assignments,[key]:value}}));
  }
  async function saveSummary(event) {
    event.preventDefault();setBusy(true);setStatus('');
    try {
      const result=await instanceSettings({expected_version:config.settings_version,
        pipeline:pipelineDraft,assignments:eventAssignments,
        ...(agentDraft!==null?{features:{pipeline_agent:agentDraft}}:{})});
      setConfig(current=>({...current,settings_version:result.settings_version,pipeline:result.pipeline,features:result.features,
        assignments:{...current.assignments,...Object.fromEntries(taskGroups.find(group=>group.key==='events').tasks.map(key=>[key,result.assignments[key]||'']))}}));
      setPipelineDraft({});setEventAssignments({});setAgentDraft(null);setStatus('自动摘要设置已保存。');
    } catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  async function recoverIndex() {
    if(busy || !window.confirm('恢复将授权最多三次索引尝试，可能产生 embedding 费用。请先修正错误；若上次操作中断，请核对提供方用量。'))return;
    setBusy(true);
    try {
      await resumeIndex(config.index_status);
      await refreshIndexStatus();
      setStatus('索引已恢复。未完成队列保留，后台将继续处理。');
    } catch(error){setStatus(error.message);}
    finally{setBusy(false);}
  }
  async function prepare() {
    if(busy)return;
    setBusy(true);setStatus("正在准备路由和记忆向量，这会调用你选择的 embedding 模型…");
    setConfig(current=>({...current,memory_ready:false}));
    try {
      const result=await prepareMemoryIndex({loadSettings:instanceSettings});
      if(result.config)setConfig(result.config);
      setStatus(result.message);
    } finally{setBusy(false);}
  }
  return <>
    <div role="tabpanel" id="settings-content-models" aria-labelledby="settings-tab-models" aria-hidden={page!=="models"} inert={page!=="models"}>
      <section className="settings-group" aria-label="上游与模型">
        {config&&<form ref={upstreamForm} onSubmit={event=>save(event,true)}>
      <div className="settings-group__heading"><h3>上游与模型</h3></div>
      <p className="model-connection-help">按上游管理模型，密钥只保存在服务端。</p>
      <UpstreamSettings modelIds={availableModels.map(m=>m.id)} assignments={config.assignments} upstreams={config.upstreams || []} onChange={changeUpstreams} onImported={value=>{setConfig(value);setPipelineDraft({});}} busy={busy} setBusy={setBusy} setStatus={setStatus} />
          <button type="button" className="settings-link" onClick={onOpenAssignments}>选择各功能使用的模型</button>
          <div className="settings-actions"><button disabled={busy} type="submit">{busy?"处理中…":"保存上游与模型"}</button></div>
        </form>}
        <p role="status">{status}</p>
      </section>
    </div>
    <div role="tabpanel" id="settings-content-configuration" aria-labelledby="settings-tab-configuration" aria-hidden={page!=="configuration"} inert={page!=="configuration"}>
      <section className="settings-group" aria-label="各功能使用的模型">
        {config&&<form onSubmit={save}>
      <div className="settings-group__heading"><h3>基础配置</h3><p>先选择检索模型并准备索引，再开启聊天记忆。</p></div>
      <div className="model-assignments">{['embedding','reranker'].map(key=><div className="settings-field model-assignment" key={key}>
        <span><label htmlFor={`task-model-${key}`}>{tasks[key]}</label> · <a className="settings-link model-assignment__link" href={taskLinks[key].href} target="_blank" rel="noreferrer">{taskLinks[key].label}</a></span>
        <select id={`task-model-${key}`} aria-label={tasks[key]} value={config.assignments[key] || ''} onChange={event=>setConfig(current=>({...current,assignments:{...current.assignments,[key]:event.target.value}}))}>
          <option value="">未选择</option>{taskModelOptions(availableModels,config.assignments,key).map(model=><option key={model.id} value={model.id}>{model.upstream_name}/{model.label || model.model || '新模型'}</option>)}
        </select></div>)}</div>
      <p><button type="button" className="settings-link" onClick={onOpenCatalog}>管理上游与模型</button></p>
      <label className="settings-toggle"><span><strong>聊天时自动带入记忆</strong><small>{memoryAvailabilitySummary(config)}</small></span>
        <input type="checkbox" role="switch" disabled={!config.memory_ready} checked={config.upstream.memory_enabled} onChange={event=>option("memory_enabled",event.target.checked)} /></label>
      <div className="settings-actions"><button disabled={busy || !config.assignments.embedding || (config.index_status && config.index_status.status !== "ready")} type="button" onClick={prepare}>建立 / 补齐检索索引</button>
        <button type="button" disabled={busy} onClick={async()=>{try{await refreshIndexStatus();}catch(error){setStatus(error.message);}}}>刷新索引状态</button>
        {(config.index_status?.status === 'paused' || config.index_status?.status === 'running') && <button type="button" disabled={busy} onClick={recoverIndex}>修正后恢复索引</button>}
      </div>
      <p role="status">{indexSafetySummary(config.index_status)}</p>
      <p className="model-connection-help">先保存配置；首次使用或更换 Embedding 后准备索引。日常新增、修改记忆由后台处理。</p>
      <details className="settings-menu">
      <summary><span>其他功能使用的模型<small>叙事卷、心绪、梦境、打标与归档</small></span></summary>
      <div className="model-assignment-groups">{taskGroups.filter(group=>group.key!=="events").map(group=><section className="model-assignment-group" key={group.key} aria-labelledby={`model-group-${group.key}`}>
        <div className="model-assignment-group__heading"><h4 id={`model-group-${group.key}`}>{group.title}</h4><p>{group.help}</p></div>
        <div className="model-assignments">{group.tasks.filter(key=>!['embedding','reranker'].includes(key)).map(key=>{const link=taskLinks[key];return <div className="settings-field model-assignment" key={key}>
          <span><label htmlFor={`task-model-${key}`}>{tasks[key]}</label>{link&&<> · <a className="settings-link model-assignment__link" href={link.href} target="_blank" rel="noreferrer">{link.label}</a></>}</span>
          <select id={`task-model-${key}`} aria-label={tasks[key]} value={config.assignments[key] || ""} onChange={event=>setConfig(current=>({...current,assignments:{...current.assignments,[key]:event.target.value}}))}>
            <option value="">未选择</option>{taskModelOptions(availableModels,config.assignments,key).map(model=><option key={model.id} value={model.id}>{model.upstream_name}/{model.label || model.model || "新模型"}</option>)}
          </select></div>})}</div>
      </section>)}</div>
      <p className="model-connection-help">“打标”为事件和 Scene 补充主域大标签、提取有原文出处的实体，也为长记忆已有的 cues 绑定 passage。已有主域和正文保持不变；实体别名只留作建议。主域与短描述在地下室的“主域边界”管理。</p>
      <p className="model-connection-help">“Event · Arc 归档”为可选任务：先按 Event 正文中的关键词缩小已有 Arc，再让模型判断是否归入。它不读取聊天原话或叙事卷正文，不创建新 Arc；留空即关闭。</p>
      {config.assignments.dreams&&<><label className="settings-field"><span>每日做梦概率（%）</span>
        <input type="number" min="0" max="100" step="1" value={Math.round((config.dream?.daily_probability??0.4)*100)}
          onChange={event=>setConfig(current=>({...current,dream:{...current.dream,daily_probability:Number(event.target.value)/100}}))}/>
        <small>每天凌晨 4 点后检查一次；0% 不做梦，100% 有新材料就做梦。最近新建的 Event／Scene 最多 5 条；都没有才读新日记。修改和召回不会成为新材料。</small></label>
      <label className="settings-field"><span>梦境 · 主模型 Prompt</span>
        <textarea rows={8} maxLength={40000} value={config.dream?.main_prompt||''} placeholder="填入聊天主模型的身份、性格和表达设定；留空沿用默认梦境规则。"
          onChange={event=>setConfig(current=>({...current,dream:{...current.dream,main_prompt:event.target.value}}))}/>
        <small>作为梦境模型的背景设定，保存后下一次做梦生效。梦境写作规则会一同发送。</small></label></>}
      <label className="settings-toggle"><span><strong>启用 API Writer</strong><small>{config.features.narrative_tools?'已由主模型通过工具读写叙事卷，自动 Writer 已关闭。':'生成预览，确认保存后才写入叙事卷。使用自己的 Agent，可打开“配置”页的接入说明。'}</small></span><input type="checkbox" role="switch" disabled={config.features.narrative_tools} checked={config.upstream.writer_enabled} onChange={event=>option("writer_enabled",event.target.checked)} /></label>
      </details>
      <details className="settings-menu">
      <summary><span>长文分段检索<small>{passagesEnabled?'已开启':'未开启'} · Passage 与起切字数</small></span></summary>
      <label className="settings-toggle"><span><strong>长文分段检索（Passage）</strong>
        <small>帮助找回长 Event / Scene 中的具体细节，避免只匹配整篇主题。预先分段、后台补向量，聊天只读已有索引；独立日记和叙事卷暂不适用。详见“使用说明 → 长文分段”。</small></span>
        <input type="checkbox" role="switch" aria-label="长文分段检索" disabled={busy} checked={passagesEnabled}
          onChange={event=>setPassageDraft(current=>({...current,passages_enabled:event.target.checked}))}/></label>
      <label className="settings-field">长文起切字数（默认 500）
        <input type="number" min="1" max="100000" step="1" disabled={busy} value={passageMinChars}
          onChange={event=>setPassageDraft(current=>({...current,passage_min_chars:event.target.value}))}/></label>
      <small>有效正文超过此字数才分段，汉字、字母和标点均按字符计。保存后应用于新写入或正文修改的记忆；已有分段保留，不自动全库重切。开启后可点“建立 / 补齐检索索引”一次性补齐旧记忆缺失的分段。</small>
      </details>
      <details className="settings-menu"><summary><span>索引处理速度<small>每批 {embeddingBatchSize} 条 · 并发 {embeddingConcurrency}</small></span></summary>
        <p>默认每批最多 16 条、同时 1 个向量请求。按提供方的批量与限流要求调整，正文和长文分段共用这些设置；保存后生效。</p>
        <label className="settings-field">向量每批条数<input type="number" min="1" max="64" step="1" disabled={busy} value={embeddingBatchSize}
          onChange={event=>setIndexDraft(current=>({...current,embedding_batch_size:event.target.value}))}/></label>
        <label className="settings-field">向量请求并发数<input type="number" min="1" max="4" step="1" disabled={busy} value={embeddingConcurrency}
          onChange={event=>setIndexDraft(current=>({...current,embedding_concurrency:event.target.value}))}/></label>
        <label className="settings-toggle"><span><strong>加快后台索引追赶</strong><small>有积压时，每轮最多处理 500 条待办，并连续推进；关闭时沿用每轮 100 条、间隔 5 秒。失败暂停和显式恢复规则保持不变。</small></span>
          <input type="checkbox" role="switch" disabled={busy} checked={indexDraft.index_catch_up ?? config.recall?.index_catch_up ?? false}
            onChange={event=>setIndexDraft(current=>({...current,index_catch_up:event.target.checked}))}/></label>
      </details>
      <details className="settings-menu"><summary><span>召回门槛<small>候选范围与最终筛选 · 当前阈值 {threshold}</small></span></summary>
      <RecallThresholdSettings config={config} draft={recallThreshold} setDraft={setRecallThreshold}
        candidateDraft={candidateThresholdDraft} setCandidateDraft={setCandidateThresholdDraft} disabled={busy}
        onSaved={result=>setConfig(current=>({...current,settings_version:result.settings_version,recall:result.recall}))}/>
      </details>
      <details className="settings-menu"><summary><span>客户端与 Agent 接入<small>Operit 上下文与叙事卷 Agent</small></span></summary>
      <label className="settings-toggle"><span><strong>整理 Operit 注入上下文</strong><small>识别系统前缀、附件和工作区，保留工具续轮的稳定上下文。</small></span><input type="checkbox" role="switch" checked={config.upstream.operit_enabled} onChange={event=>option("operit_enabled",event.target.checked)} /></label>
          <AgentGuide initial="writer" label="配置 Agent 撰写叙事卷" />
      </details>
          <details className="settings-menu"><summary><span>索引准备说明<small>何时需要准备、复用范围与模型开销</small></span></summary>
            <p>同一模型下，会复用当前有效的正文向量、已有分段和分段向量，只补缺失内容；不会重写记忆，也不会全库重切分段。已有空分段布局同样保留，降低起切门槛后再按也不会自动把旧短文重切。</p>
            <p>但每次仍会重新探测维度、计算路由例句向量、扫描记忆并更新实体索引，会产生模型调用和服务器开销。它不是无成本的刷新，也不是只补缺口的轻量操作。</p>
            <p>更换 Embedding 后会为新模型准备独立索引，正文和分段向量需要重新生成；已保存的共享分段布局可复用，尚未迁入共享布局的旧分段不保证复用。</p>
            <p>首次配置、更换 Embedding、页面提示检索规则需要更新、首次给旧记忆开启 Passage，或确认有缺失向量时再按。按钮读取已保存的模型和 Passage 设置。准备期间会暂时撤下检索就绪标记，失败需重试完成；正常使用时不必反复点击。</p>
          </details>
          <div className="settings-actions"><button disabled={busy} type="submit">{busy?"处理中…":"保存配置"}</button></div>
        </form>}
        <p role="status">{status}</p>
      </section>
    </div>
    <div role="tabpanel" id="settings-content-summary" aria-labelledby="settings-tab-summary" aria-hidden={page!=="summary"} inert={page!=="summary"}>
      <section className="settings-group" aria-label="自动摘要管理"><div className="settings-group__heading"><h3>自动摘要管理</h3><p>开关、模型、输入范围与执行方式在这里统一保存。</p></div>
      {pendingCount>0&&<div className="pending-materials__notice" role="status">有 {pendingCount} 批材料待处理，原话与错误记录已保留。{' '}
        <button type="button" className="settings-link" onClick={()=>{const target=document.getElementById('summary-pending-materials');const panel=document.getElementById('settings-content-summary');if(target&&panel)panel.scrollTop+=target.getBoundingClientRect().top-panel.getBoundingClientRect().top-16;target?.focus({preventScroll:true});}}>定位并处理卡住的材料</button></div>}
      <button type="button" className="settings-link" onClick={onOpenEventGuide}>了解模型调用与费用</button>
      <p className="model-connection-help">Event Writer 要核对原话、人物、因果和修订，再写出自然正文；建议为“原话 · Event 写作”选择理解和写作能力较强的模型。</p>
      {config&&<form onSubmit={saveSummary}><fieldset className="model-settings-fields" disabled={busy}>
      <label className="settings-toggle"><span><strong>自动摘要</strong><small>自动把新聊天整理为 Event。每次从关闭改为开启时，从保存后的新原话开始，不补跑此前积压。</small></span><input type="checkbox" role="switch" checked={config.pipeline.auto_enabled!==false} onChange={event=>pipelineOption('auto_enabled',event.target.checked)}/></label>
      <label className="settings-field"><span>自动 Event 执行方式</span><select value={config.pipeline.execution_mode||'legacy'} onChange={event=>pipelineOption('execution_mode',event.target.value)}>
        {(!config.pipeline.execution_mode||config.pipeline.execution_mode==='legacy')&&<option value="legacy">沿用旧配置（各阶段分别执行）</option>}<option value="api">API</option><option value="agent">Agent</option></select></label>
      <p className="model-connection-help">API 模式选好归线、切分和 Event 写作三个阶段模型即可，无需配置 Agent。“API Writer”只控制叙事卷写作。</p>
      {config.pipeline.execution_mode==='agent'&&<p className="model-connection-help">由已认证的 Agent 执行器领取任务；仅切换此选项不会启动本机 CLI。</p>}
      <div className="model-assignment-groups">{taskGroups.filter(group=>group.key==="events").map(group=><section className="model-assignment-group" key={group.key} aria-labelledby={`model-group-${group.key}`}>
        <div className="model-assignment-group__heading"><h4 id={`model-group-${group.key}`}>阶段模型</h4><p>{group.help}</p></div>
        <div className="model-assignments">{group.tasks.map(key=>{const link=taskLinks[key];return <div className="settings-field model-assignment" key={key}>
          <span><label htmlFor={`task-model-${key}`}>{tasks[key]}</label>{link&&<> · <a className="settings-link model-assignment__link" href={link.href} target="_blank" rel="noreferrer">{link.label}</a></>}</span>
          <select id={`task-model-${key}`} aria-label={tasks[key]} value={config.assignments[key] || ""} onChange={event=>eventOption(key,event.target.value)}>
            <option value="">未选择</option>{taskModelOptions(availableModels,config.assignments,key).map(model=><option key={model.id} value={model.id}>{model.upstream_name}/{model.label || model.model || "新模型"}</option>)}
          </select></div>})}</div>
      </section>)}</div>
      <p className="model-connection-help">重要 Event 请对照原话核对。下方设置按需展开；待处理材料保留在本页。</p>
      <details className="settings-menu">
      <summary><span>调用预算与超时<small>{config.pipeline.max_prompt_chars?.toLocaleString()} 字符 · {config.pipeline.timeout_seconds} 秒</small></span></summary>
      <p className="model-connection-help">“完整提示词字符上限”是最终模型调用保护，可按所用模型上下文提高到 4000000；修改后下一次继续当前批次即可生效。</p>
      {Object.entries({max_prompt_chars:['完整提示词字符上限',8000,4000000],timeout_seconds:['模型读取超时（秒）',30,1800]}).map(([key,[label,min,max]])=>
        <label className="settings-field" key={key}><span>{label}</span><input type="number" min={min} max={max} value={config.pipeline[key] ?? ''} onChange={event=>pipelineOption(key,Number(event.target.value))}/></label>)}
      </details>
      <details className="settings-menu">
      <summary><span>归线与写作<small>Track 回看 {config.pipeline.track_lookback_days??3} 天 · Writer 并发 {config.pipeline.event_writer_concurrency??1}</small></span></summary>
      <div className="summary-settings-notes"><p>有时间按 20 分钟沉默切块；无时间按 20 轮完整问答切块。每块再受字符量限制，保留完整问答和原始编号。时间切块只控制输入，不直接决定 Event 边界。</p>
      <p>只并发 Curator 已冻结计划后的第一轮 Event Writer；Router、Curator、补读与最终结算保持串行。Agent 模式仍一次领取一个 Writer 任务。默认 1。</p>
      <p>默认回看 3 天（72 小时）：归线读取这段时间内实际归入过原话的 Track，不依赖聊天窗口。旧 Event 不按时间过期；同一 Track 超过 8 条 active leaves 时只 defer 该 Track，避免截断候选后误写。天数修改对新批次生效，已冻结任务保持原材料。</p></div>
      {Object.entries({event_writer_concurrency:['Event Writer 首轮并发数',1,8],track_lookback_days:['归线 Track 回看天数',1,365]}).map(([key,[label,min,max]])=>
        <label className="settings-field" key={key}><span>{label}</span><input type="number" min={min} max={max} value={config.pipeline[key] ?? (key==='track_lookback_days'?3:'')} onChange={event=>pipelineOption(key,Number(event.target.value))}/></label>)}
      </details>
      <details className="settings-menu">
      <summary><span>审阅与写入<small>旧 Event 回看{(config.pipeline.event_history_enabled??true)?'已开启':'已关闭'} · 材料取舍与追加规则</small></span></summary>
      <p className="model-connection-help">回看开启时，切分先读旧 Event 正文，需要时再补读原话。</p>
      <label className="settings-toggle"><span><strong>回看旧 Event</strong><small>默认开启。关闭后不加载旧 Event 及其来源候选，新原话只生成新 Event，不续写或合并旧 Event。归线仍可回看 Track，普通聊天的记忆召回不变；保存后用于新批次。</small></span><input type="checkbox" role="switch" checked={config.pipeline.event_history_enabled??true} onChange={event=>pipelineOption('event_history_enabled',event.target.checked)}/></label>
      {[
        ['joint_review_enabled','跨线联合审阅','有明确桥接时，让 Curator 一次核对相连的少量 Track；合并 Event 仍须逐字证据。'],
        ['material_review_enabled','逐条材料取舍','让 Curator 标明每条 owned 原话的写作用途，再传给 Writer；来源绑定仍完整保留。'],
        ['round_gate_enabled','普通交流轮次门槛','至少两轮完整交流才进入 Writer；未结束的短段等待后续原话，明确结束的短段跳过。'],
        ['append_protected_enabled','受保护 Event 后续追加','若旧 Event 只因叙事卷或 Scene 引用受保护，后续新经历可追加为新版本；旧正文、标题和召回设置保持原样。'],
      ].map(([key,label,help])=><label className="settings-toggle" key={key}><span><strong>{label}</strong><small>{help}</small></span>
        <input type="checkbox" role="switch" checked={config.pipeline[key]===true}
          onChange={event=>pipelineOption(key,event.target.checked)}/></label>)}</details>
      <details className="settings-menu">
      <summary><span>归线候选实验<small>{config.pipeline.router_compact_cards||config.pipeline.track_candidates_enabled?'有实验项已开启':'默认关闭'} · 精简候选与候选检索</small></span></summary>
      <label className="settings-toggle"><span><strong>精简归线候选（实验）</strong><small>候选卡不附旧原话，本轮原话和最近上下文保留。可减少输入量；省略指代较多时可能影响续接判断。</small></span><input type="checkbox" role="switch" checked={config.pipeline.router_compact_cards ?? false} onChange={event=>pipelineOption('router_compact_cards',event.target.checked)}/></label>
      <label className="settings-toggle"><span><strong>检索归线候选（实验）</strong><small>默认关闭，完整带入回看范围内的旧卡。开启后，近线完整带入，较早的卡由程序检索补充；可能漏接或改变分线。</small></span><input type="checkbox" role="switch" checked={config.pipeline.track_candidates_enabled ?? false} onChange={event=>pipelineOption('track_candidates_enabled',event.target.checked)}/></label>
      {config.pipeline.track_candidates_enabled && <>
        <label className="settings-field"><span>直接带入最近活动的 Track</span><select value={config.pipeline.track_direct_hours ?? 12} onChange={event=>pipelineOption('track_direct_hours',Number(event.target.value))}>{[12,24,48,72].map(hours=><option key={hours} value={hours}>最近 {hours} 小时</option>)}</select></label>
        <label className="settings-field"><span>额外检索候选上限（默认 8 张）</span><input type="number" min="1" max="50" value={config.pipeline.track_candidate_limit ?? 8} onChange={event=>pipelineOption('track_candidate_limit',Number(event.target.value))}/></label>
        <small>使用整批新原文和附近前文，搜索旧卡的标题、续接线索及近期原文；程序排除已直接带入的卡并去重。只筛选本次输入，卡片和原文仍完整保存。对新批次生效，已冻结任务保留原输入。</small>
      </>}
      </details>
      <details className="settings-menu">
      <summary><span>图片与 Agent 接入说明<small>图片兜底、外部执行器与工具开关</small></span></summary>
      <p className="model-connection-help">图片逐张转录后交给切分和 Writer；异步转录失败或图片模型留空时，流水线尝试用归线模型转录；无法读取图片则只保留发送图片的数量。图片转录 API 需支持图片和 JSON；Writer 只读文字与转录，不需要视觉能力。</p>
      <p className="model-connection-help">Agent 模式下，阶段模型选择作为执行提示，执行器需按提示使用相应模型。</p>
      <label className="settings-toggle"><span><strong>自动摘要 Agent 工具</strong><small>允许外部 Agent 领取任务、提交结果和重建计划；与自动摘要开关独立。</small></span><input type="checkbox" role="switch" checked={agentDraft??!!config.features.pipeline_agent} onChange={event=>setAgentDraft(event.target.checked)}/></label>
          <AgentGuide label="配置 Agent 整理 Event" />
      </details>
      <p><button type="button" className="settings-link" onClick={onOpenPipeline}>查看整理进度与导入原话</button></p>
      <div className="settings-actions"><button disabled={busy} type="submit">{busy?'处理中…':'保存自动摘要设置'}</button></div>
      </fieldset></form>}<p role="status">{status}</p></section>
      {page==='summary'&&<PendingMaterials id="summary-pending-materials" onStatus={setPendingWork}/>}
    </div>
  </>;
}


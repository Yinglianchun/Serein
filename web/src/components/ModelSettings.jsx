import { useEffect, useRef, useState } from "react";
import { instanceSettings } from "../storage/instanceStore.js";
import {UpstreamSettings} from "./UpstreamSettings.jsx";
import {upstreamModels,taskModelOptions} from "../modelOptions.js";
import {AgentGuide} from './AgentGuide.jsx';
import {RecallThresholdSettings} from './RecallThresholdSettings.jsx';
import {upstreamsForSave} from '../upstreamSecrets.js';

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

export function ModelSettings({page,summaryRequest=0,onOpenPipeline,onOpenCatalog,onOpenAssignments,
  recallThreshold,setRecallThreshold,candidateThresholdDraft,setCandidateThresholdDraft,
  passageDraft,setPassageDraft}) {
  const [config,setConfig]=useState(null);
  const passagesEnabled=passageDraft.passages_enabled ?? config?.recall?.passages_enabled ?? false;
  const passageMinChars=passageDraft.passage_min_chars ?? config?.recall?.passage_min_chars ?? 500;
  const threshold=recallThreshold ?? config?.recall?.direct_threshold ?? 0.65;
  const bodyCandidateThreshold=candidateThresholdDraft.body_candidate_threshold ?? config?.recall?.body_candidate_threshold ?? 0.50;
  const cueCandidateThreshold=candidateThresholdDraft.cue_candidate_threshold ?? config?.recall?.cue_candidate_threshold ?? 0.55;
  const validThreshold=threshold!=='' && Number.isFinite(Number(threshold)) && Number(threshold)>=0 && Number(threshold)<=1;
  const validCandidateThresholds=[bodyCandidateThreshold,cueCandidateThreshold].every(value=>
    value!=='' && Number.isFinite(Number(value)) && Number(value)>=0 && Number(value)<=1);
  const summaryConfig=useRef(null);
  const upstreamForm=useRef(null);
  const ready=!!config;
  useEffect(()=>{
    if(!summaryRequest||!summaryConfig.current)return;
    summaryConfig.current.open=true;
    summaryConfig.current.querySelector('summary').focus();
    summaryConfig.current.scrollIntoView({block:'start'});
  },[summaryRequest,ready]);
  const [status,setStatus]=useState("");
  const [busy,setBusy]=useState(false);
  useEffect(()=>{
    let active=true;
    instanceSettings().then(value=>{if(active)setConfig(value);}).catch(error=>{if(active)setStatus(error.message);});
    const saved=event=>setConfig(current=>current?{...current,settings_version:event.detail.settings_version,recall:event.detail.recall,features:event.detail.features,pipeline:{...current.pipeline,auto_enabled:event.detail.pipeline.auto_enabled}}:current);
    window.addEventListener('serein:settings-saved',saved);
    return()=>{active=false;window.removeEventListener('serein:settings-saved',saved);};
  },[]);
  function option(key,value) {setConfig(current=>({...current,upstream:{...current.upstream,[key]:value}}));}
  const availableModels=config?[...config.models,...upstreamModels(config.upstreams || [])]:[];
  function changeUpstreams(upstreams) {
    const ids=new Set([...config.models,...upstreamModels(upstreams)].map(model=>model.id));
    setConfig(current=>({...current,upstreams,assignments:Object.fromEntries(Object.entries(current.assignments).map(([key,value])=>[key,ids.has(value)?value:""]))}));
    setStatus("上游设置尚未保存。");
  }
  async function save(event) {
    event.preventDefault();setBusy(true);setStatus("");
    try {
      const models=config.models.map(({api_key_configured,clear_key,...model})=>{
        if(clear_key)model.api_key="";
        else if(!model.api_key)delete model.api_key;
        if(!model.dimension)delete model.dimension;
        return model;
      });
      const upstreams=upstreamsForSave(config.upstreams || [],upstreamForm.current);
      if(!validThreshold)throw new Error('召回阈值需填写 0 到 1 之间的数字。');
      if(!validCandidateThresholds)throw new Error('候选扩展门槛需填写 0 到 1 之间的数字。');
      if(passageMinChars===''||!Number.isInteger(Number(passageMinChars))||Number(passageMinChars)<1||Number(passageMinChars)>100000)
        throw new Error('长文起切字数需填写 1 到 100000 之间的整数。');
      const result=await instanceSettings({expected_version:config.settings_version,models,upstreams,assignments:config.assignments,pipeline:Object.fromEntries(Object.entries(config.pipeline).filter(([key])=>key!=='auto_enabled')),dream:config.dream,
        recall:{...passageDraft,...('passage_min_chars' in passageDraft?{passage_min_chars:Number(passageMinChars)}:{}),
          ...(recallThreshold!==null?{direct_threshold:Number(threshold)}:{}),
          ...Object.fromEntries(Object.entries(candidateThresholdDraft).map(([key,value])=>[key,Number(value)]))},upstream:{
        writer_enabled:config.upstream.writer_enabled,memory_enabled:config.upstream.memory_enabled,operit_enabled:config.upstream.operit_enabled}});
      setConfig(result);setRecallThreshold(null);setCandidateThresholdDraft({});setPassageDraft({});setStatus("设置已保存。");
    } catch(error){setStatus(error.message);}
    finally{setBusy(false);}
  }
  async function prepare() {
    setBusy(true);setStatus("正在准备路由和记忆向量，这会调用你选择的 embedding 模型…");
    try {
      const response=await fetch("/__serein/settings/prepare-memory",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
      if(!response.ok)throw new Error("准备失败，请核对 embedding 模型、接口和密钥后重试。");
      const result=await response.json();setConfig(await instanceSettings());setStatus(`记忆检索已准备好，向量维度 ${result.dimension}。`);
    } catch(error){setStatus(error.message);}
    finally{setBusy(false);}
  }
  return <>
    <div role="tabpanel" id="settings-content-models" aria-labelledby="settings-tab-models" aria-hidden={page!=="models"} inert={page!=="models"}>
      <section className="settings-group" aria-label="上游与模型">
        {config&&<form ref={upstreamForm} onSubmit={save}>
      <div className="settings-group__heading"><h3>上游与模型</h3></div>
      <p className="model-connection-help">按上游管理模型，密钥只保存在服务端。</p>
      <UpstreamSettings modelIds={availableModels.map(m=>m.id)} assignments={config.assignments} upstreams={config.upstreams || []} onChange={changeUpstreams} onImported={setConfig} busy={busy} setBusy={setBusy} setStatus={setStatus} />
          <button type="button" className="settings-link" onClick={onOpenAssignments}>选择各功能使用的模型</button>
          <div className="settings-actions"><button disabled={busy} type="submit">{busy?"处理中…":"保存上游与模型"}</button></div>
        </form>}
        <p role="status">{status}</p>
      </section>
    </div>
    <div role="tabpanel" id="settings-content-configuration" aria-labelledby="settings-tab-configuration" aria-hidden={page!=="configuration"} inert={page!=="configuration"}>
      <section className="settings-group" aria-label="各功能使用的模型">
        {config&&<form onSubmit={save}>
      <div className="settings-group__heading model-assignments-heading"><h3>功能使用的模型</h3><p>已选为向量或重排的模型不出现在聊天模型选项中，各功能可在此选择已配置的模型。留空的可选任务保持关闭。</p></div>
      <div className="model-assignment-groups">{taskGroups.map(group=><section className="model-assignment-group" key={group.key} aria-labelledby={`model-group-${group.key}`}>
        <div className="model-assignment-group__heading"><h4 id={`model-group-${group.key}`}>{group.title}</h4><p>{group.help}</p></div>
        <div className="model-assignments">{group.tasks.map(key=>{const link=taskLinks[key];return <div className="settings-field model-assignment" key={key}>
          <span><label htmlFor={`task-model-${key}`}>{tasks[key]}</label>{link&&<> · <a className="settings-link model-assignment__link" href={link.href} target="_blank" rel="noreferrer">{link.label}</a></>}</span>
          <select id={`task-model-${key}`} aria-label={tasks[key]} value={config.assignments[key] || ""} onChange={event=>setConfig(current=>({...current,assignments:{...current.assignments,[key]:event.target.value}}))}>
            <option value="">未选择</option>{taskModelOptions(availableModels,config.assignments,key).map(model=><option key={model.id} value={model.id}>{model.upstream_name}/{model.label || model.model || "新模型"}</option>)}
          </select></div>})}</div>
      </section>)}</div>
      <p className="model-connection-help">Event Writer 要核对原话、人物、因果和修订，再写出自然正文；建议为“原话 · Event 写作”选择理解和写作能力较强的模型。</p>
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
          <button type="button" className="settings-link" onClick={onOpenCatalog}>管理上游与模型</button>
      <label className="settings-toggle"><span><strong>启用 API Writer</strong><small>{config.features.narrative_tools?'已由主模型通过工具读写叙事卷，自动 Writer 已关闭。':'生成预览，确认保存后才写入叙事卷。使用自己的 Agent，可打开“配置”页的接入说明。'}</small></span><input type="checkbox" role="switch" disabled={config.features.narrative_tools} checked={config.upstream.writer_enabled} onChange={event=>option("writer_enabled",event.target.checked)} /></label>
      <label className="settings-toggle"><span><strong>聊天时自动带入记忆</strong><small>{config.memory_ready?"检索已就绪。":"选择并保存 embedding 和 reranker 后，点击下方建立 / 补齐检索索引。"}</small></span>
        <input type="checkbox" role="switch" disabled={!config.memory_ready} checked={config.upstream.memory_enabled} onChange={event=>option("memory_enabled",event.target.checked)} /></label>
      <div className="settings-group__heading"><h3>长文分段</h3></div>
      <label className="settings-toggle"><span><strong>长文分段检索（Passage）</strong>
        <small>帮助找回长 Event / Scene 中的具体细节，避免只匹配整篇主题。预先分段、后台补向量，聊天只读已有索引；独立日记和叙事卷暂不适用。详见“使用说明 → 长文分段”。</small></span>
        <input type="checkbox" role="switch" aria-label="长文分段检索" disabled={busy} checked={passagesEnabled}
          onChange={event=>setPassageDraft(current=>({...current,passages_enabled:event.target.checked}))}/></label>
      <label className="settings-field">长文起切字数（默认 500）
        <input type="number" min="1" max="100000" step="1" disabled={busy} value={passageMinChars}
          onChange={event=>setPassageDraft(current=>({...current,passage_min_chars:event.target.value}))}/></label>
      <small>有效正文超过此字数才分段，汉字、字母和标点均按字符计。保存后应用于新写入或正文修改的记忆；已有分段保留，不自动全库重切。开启后可点“建立 / 补齐检索索引”一次性补齐旧记忆缺失的分段。</small>
      <RecallThresholdSettings config={config} draft={recallThreshold} setDraft={setRecallThreshold}
        candidateDraft={candidateThresholdDraft} setCandidateDraft={setCandidateThresholdDraft} disabled={busy}
        onSaved={result=>setConfig(current=>({...current,settings_version:result.settings_version,recall:result.recall}))}/>
      <label className="settings-toggle"><span><strong>整理 Operit 注入上下文</strong><small>识别系统前缀、附件和工作区，保留工具续轮的稳定上下文。</small></span><input type="checkbox" role="switch" checked={config.upstream.operit_enabled} onChange={event=>option("operit_enabled",event.target.checked)} /></label>
    <details className="settings-disclosure" ref={summaryConfig}><summary>自动摘要配置</summary><p>有时间按 20 分钟沉默切块；无时间按 20 轮完整问答切块。每块再受字符量限制，保留完整问答和原始编号。时间切块只控制输入，不直接决定 Event 边界。</p>
      <p>自动摘要可能有遗漏或误解，重要内容请对照原始对话核对。</p>
      <label className="settings-field"><span>自动 Event 执行方式</span><select value={config.pipeline.execution_mode||'legacy'} onChange={event=>setConfig(current=>({...current,pipeline:{...current.pipeline,execution_mode:event.target.value}}))}>
        {(!config.pipeline.execution_mode||config.pipeline.execution_mode==='legacy')&&<option value="legacy">沿用旧配置（各阶段分别执行）</option>}<option value="api">API</option><option value="agent">Agent</option></select></label>
      <p>{config.pipeline.execution_mode==='agent'?'通过已认证的 Agent 执行器领取任务。阶段模型选择作为执行提示，执行器需按提示使用相应模型；仅切换此选项不会启动本机 CLI。':'在本页为归线、图片转录、切分、Event 写作分别选模型。选择独立图片转录模型后，切分器读取已落库的转录；不选择则仍由切分器直接读图。图片转录与 Writer 所选 API 需支持图片和 JSON 输出。'}</p>
      {Object.entries({max_prompt_chars:['完整提示词字符上限',8000,4000000],timeout_seconds:['模型读取超时（秒）',30,1800],event_writer_concurrency:['Event Writer 首轮并发数',1,8],track_lookback_days:['归线 Track 回看天数',1,365]}).map(([key,[label,min,max]])=>
        <label className="settings-field" key={key}><span>{label}</span><input type="number" min={min} max={max} value={config.pipeline[key] ?? (key==='track_lookback_days'?3:'')} onChange={event=>setConfig(current=>({...current,pipeline:{...current.pipeline,[key]:Number(event.target.value)}}))}/></label>)}
      <small>“完整提示词字符上限”是最终模型调用保护，可按所用模型上下文提高到 4000000；修改后下一次继续当前批次即可生效。</small>
      <small>只并发 Curator 已冻结计划后的第一轮 Event Writer；Router、Curator、补读与最终结算保持串行。Agent 模式仍一次领取一个 Writer 任务。默认 1。</small>
      <small>默认回看 3 天（72 小时）：归线读取这段时间内实际归入过原话的 Track，不依赖聊天窗口。旧 Event 不按时间过期；同一 Track 超过 8 条 active leaves 时只 defer 该 Track，避免截断候选后误写。天数修改对新批次生效，已冻结任务保持原材料。</small>
      <button type="button" className="settings-link" onClick={onOpenPipeline}>查看整理进度与导入原话</button>
    </details>
          <AgentGuide label="配置 Agent 整理 Event" />
          <AgentGuide initial="writer" label="配置 Agent 撰写叙事卷" />
          <div className="settings-actions"><button disabled={busy} type="submit">{busy?"处理中…":"保存配置"}</button>
            <button disabled={busy || !config.assignments.embedding} type="button" onClick={prepare}>建立 / 补齐检索索引</button></div>
          <p className="model-connection-help">首次使用建立索引；再次点击检查并补齐，已有有效向量会复用。先保存配置；日常新增、修改记忆由后台处理，不用每次按。</p>
          <details className="settings-disclosure"><summary>再次点击，会重建吗？</summary>
            <p>同一模型下，会复用当前有效的正文向量、已有分段和分段向量，只补缺失内容；不会重写记忆，也不会全库重切分段。已有空分段布局同样保留，降低起切门槛后再按也不会自动把旧短文重切。</p>
            <p>但每次仍会重新探测维度、计算路由例句向量、扫描记忆并更新实体索引，会产生模型调用和服务器开销。它不是无成本的刷新，也不是只补缺口的轻量操作。</p>
            <p>更换 Embedding 后会为新模型准备独立索引，正文和分段向量需要重新生成；已保存的共享分段布局可复用，尚未迁入共享布局的旧分段不保证复用。</p>
            <p>首次配置、更换 Embedding、页面提示检索规则需要更新、首次给旧记忆开启 Passage，或确认有缺失向量时再按。按钮读取已保存的模型和 Passage 设置。准备期间会暂时撤下检索就绪标记，失败需重试完成；正常使用时不必反复点击。</p>
          </details>
        </form>}
        <p role="status">{status}</p>
      </section>
    </div>
  </>;
}

import { identityName, instanceSettings } from "../storage/instanceStore.js";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowClockwise,
  Check,
  Eye,
  Flask,
  GitDiff,
  PencilSimple,
  Plus,
  ShareNetwork,
  Trash,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { BasementRecallObservation } from "../components/BasementRecallObservation.jsx";
import { BasementRelationshipProposals } from "../components/BasementRelationshipProposals.jsx";
import { BasementRevisionInbox } from "../components/BasementRevisionInbox.jsx";
import {
  basementRecallExamples,
  canonicalDomainPolicies,
  semanticRouteSnapshot,
} from "../data/basement.js";
import {
  clearDomainPolicyDraft,
  domainPolicyDraftVersion,
  clearServerSemanticRouteDraft,
  clearSemanticRouteDraft,
  hasDomainPolicyDraft,
  inspectServerSemanticRouteDraft,
  readDomainPolicyDraft,
  readServerSemanticRouteDraft,
  readSemanticRouteDraft,
  saveServerSemanticRouteDraft,
  saveDomainPolicyDraft,
  saveSemanticRouteDraft,
} from "../storage/basementStore.js";
import { upsertRecallSimulationTrainingLabel } from "../storage/recallSimulationTraining.js";
import { recallScore, recallSimulationDiagnostics } from "../utils/recallSimulationDiagnostics.js";

const routeLabels = {
  simple_contact: "陪伴与贴近",
  present_chitchat: "此刻闲聊",
  intimate_contact: "亲密联系",
  recall_needed: "明确回望",
};

const roleLabels = {
  typical: "典型例句",
  boundary: "边界例句",
};

const originLabels = {
  manual: "人工添加",
  online_false_positive: "线上误召",
  online_false_negative: "线上漏召",
  import: "导入",
};

const exampleStatusLabels = {
  draft: "草稿",
  published: "已发布",
  retired: "已停用",
};

const reasonLabels = {
  recall_route_won: "召回路线胜出",
  matched_skip_route: "命中不召回路线",
  boundary_veto: "边界护栏撤销跳过",
  below_threshold: "最高分未过阈值，继续召回",
  insufficient_margin: "路线差距不足，继续召回",
  route_index_stale: "例句已变化，向量需要重建",
  uncertain_route: "路线未达到明确判断条件，继续召回",
  daily_surface_without_memory_intent: "当前话语未触发记忆需求",
};

const actionLabel = (action) => ({skip: "no-recall", recall: "recall"}[action] || "未返回");
const percent = (value) => value == null || value === "" ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
const clone = (value) => JSON.parse(JSON.stringify(value));
const defaultRouteThreshold = 0.72;

function recommendedRouteThreshold(route) {
  const name = String(route?.name || "").trim();
  const label = String(route?.label || "").trim();
  if (name === "present_chitchat" || label === "此刻闲聊") return 0.60;
  if (name === "技术闲聊" || label === "技术闲聊") return 0.58;
  return null;
}

function applyRecommendedRouteThresholds(routes) {
  return routes.map((route) => {
    const recommended = recommendedRouteThreshold(route);
    return recommended == null || route.threshold != null ? route : { ...route, threshold: recommended };
  });
}

function routeSnapshotFromApi(payload) {
  if (!payload || !Array.isArray(payload.routes)) throw new Error("route_dataset_invalid");
  return {
    datasetVersion: Number(payload.dataset_version),
    deploymentState: payload.deployment_state || "production",
    capturedAt: payload.published?.published_at || new Date().toISOString(),
    model: payload.embedding?.model || semanticRouteSnapshot.model,
    boundaryExampleCount: Number(payload.boundary_example_count || payload.published?.boundary_example_count || 0),
    indexedBoundaryExampleCount: Number(payload.indexed_boundary_example_count || 0),
    boundaryIndexReady: payload.boundary_index_ready !== false,
    routes: payload.routes.map((route) => ({
      ...route,
      label: route.label || routeLabels[route.name] || route.name,
      enabled: route.enabled !== false,
      utterances: Array.isArray(route.utterances) ? route.utterances.map((item) => ({
        text: String(item.text || "").trim(),
        role: item.role === "boundary" ? "boundary" : "typical",
        origin: ["manual", "online_false_positive", "online_false_negative", "import"].includes(item.origin)
          ? item.origin
          : "import",
        status: item.status === "retired" ? "retired" : "published",
      })) : [],
    })),
  };
}

function routePublishError(error) {
  if (error.startsWith("route_publish_version_conflict:")) return "线上数据集已经变化。请重新载入后检查草稿，再发布。";
  if (error.startsWith("route_source_active_center_missing:")) return "每条启用路线都至少需要一条典型例句；边界例句不会单独形成路线中心。";
  if (error.startsWith("route_source_utterance_duplicate:")) return "同一句不能同时出现在两条路线里。";
  if (error === "route_publish_confirmation_required") return "这次发布缺少确认标记。";
  return error || "发布没有完成。";
}

async function readRouteApiResponse(response) {
  const text = await response.text();
  try {
    return JSON.parse(text || "{}");
  } catch {
    if (response.status === 404) {
      throw new Error("Gateway 还没有部署 Router 发布接口；本机草稿仍然保留。");
    }
    throw new Error(text || `Router 接口返回 ${response.status}`);
  }
}

function openSceneInMemory(item) {
  if (item.source_kind === "event" || String(item.id || "").startsWith("event:")) return;
  const sceneId = item.bucket_id || item.moment_id || String(item.id || "").replace(/^scene:/, "").split("#").pop();
  if (!sceneId) return;
  window.localStorage.setItem("serein.memory.open-source-id", sceneId);
  window.location.hash = "#memory";
  window.dispatchEvent(new CustomEvent("serein:open-memory-scene", { detail: { sourceId: sceneId } }));
}

const recallAblationOptions = [
  { value: "normal", label: "正常", description: "cue 词面/embedding + 正文 embedding" },
  { value: "without_cues", label: "关闭 cues", description: "只看正文 embedding / 词面" },
  { value: "without_embedding", label: "关闭 embedding", description: "只看 cue / 正文词面" },
];

const recallSimulationScopeOptions = [
  { value: "live_mirror", label: "按 live 跑", description: "线上会提前停就提前停，不展开 shadow 诊断" },
  { value: "full_shadow", label: "展开 shadow", description: "额外运行候选池、reranker 与局部证据观察" },
];

const recallCandidateSourceLabels = {
  exact_anchor: "精确锚点",
  title_anchor: "标题锚点",
  lexical: "正文词面",
  cue_lexical: "cue 词面",
  cue_semantic: "cue embedding",
  body_semantic: "正文 embedding",
  retrieval_alias: "检索别名",
  cue_passage_embedding: "cue 绑定片段",
  passage_embedding: "局部原文 embedding",
  cue_passage_query_view_embedding: "分句 cue 绑定片段",
  passage_query_view_embedding: "分句局部原文 embedding",
  fact_event_body_embedding: "Fact / Event 正文 embedding",
  fact_event_lexical: "Fact / Event 词语命中",
  scene_whole_embedding: "Scene 整体 embedding",
  scene_passage_embedding: "Scene passage embedding",
  scene_cue_candidate: "Scene cue 候选",
  event_whole_embedding: "Event 整体 embedding",
  event_passage_embedding: "Event passage embedding",
  event_lexical_candidate: "Event 词语候选",
};

const passageCandidateLaneLabels = {
  cue_passage: "cue 绑定片段",
  passage: "局部原文",
  cue_passage_query_view: "分句 cue 绑定片段",
  passage_query_view: "分句局部原文",
  fact_event_body: "Fact / Event 正文",
  fact_event_lexical: "Fact / Event 词语",
};

const passageCandidateKindLabels = {
  scene: "Scene",
  event: "Event",
  fact: "Fact",
};

function passageCandidateScore(candidate) {
  const score = Number(candidate?.score || 0);
  return (candidate?.candidate_sources || []).includes("fact_event_lexical")
    ? score.toFixed(4)
    : percent(score);
}

const rerankerShadowLabels = {
  eligible_not_called: "达到 floor，shadow 未调用",
  eligible_gray_zone_not_called: "进入灰区，shadow 未调用",
  ineligible_below_entry_floor: "低于 reranker 入口，不进入",
  pending_reranker_shadow_gray_zone: "灰区候选，等待 reranker",
  pending_reranker_shadow_route_guard: "低置信 route guard 已延后，等待 reranker",
  pending_reranker_shadow_candidate_only: "候选发现证据，等待 reranker",
  scored_shadow_only: "已评分，仅观察",
  called_without_score: "已调用，未返回分数",
};

const rerankerShadowReasonLabels = {
  simulation_shadow_disabled: "simulation shadow 未启用",
  simulation_shadow_credentials_unavailable: "shadow 凭据不可用",
  simulation_shadow_failed: "shadow 调用失败，已 fail-open",
  simulation_shadow_call_pending: "shadow 未返回分数",
  simulation_shadow_no_scores: "shadow 未返回分数",
  scored_shadow_only: "已评分，仅观察",
};

const episodeVerifierLabels = {
  same_episode: "同一件事",
  symbolic_resonance: "意象呼应",
  same_topic_only: "只是同主题",
  unrelated: "无关",
};

const candidateRelevanceOptions = [
  { value: "core", label: "核心相关" },
  { value: "weak", label: "弱相关" },
  { value: "irrelevant", label: "无关" },
];

const typedAdmissionModeLabels = {
  direct_evidence_rerank: "直接证据 rerank",
  structured_latest: "取最新相关成员",
  timeline_scope_material: "Arc 时间线材料",
  defer_to_narrative: "交给叙事卷",
  defer_to_exact_evidence: "等待精确证据下钻",
};

const typedAdmissionReasonLabels = {
  reranker_direct_evidence: "reranker 通过直接证据门",
  reranker_below_direct_threshold: "低于直接证据阈值",
  reranker_score_missing: "reranker 没有返回分数",
  candidate_without_scored_evidence: "没有可评分正文证据",
  latest_dated_relevant_member: "最新的相关成员",
  not_latest_relevant_member: "不是最新相关成员",
  timeline_candidate_material: "作为时间线候选材料",
  defer_to_narrative: "本轮应读取叙事卷",
  defer_to_exact_evidence: "本轮应继续下钻精确证据",
};

function RecallSimulator({thresholdTrial,onClearThresholdTrial}) {
  const [query, setQuery] = useState("");
  const [resultScope, setResultScope] = useState(null);
  const [status, setStatus] = useState("idle");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const requestSequence=useRef(0);
  const [comparison,setComparison]=useState(null);
  const [trialThreshold,setTrialThreshold]=useState(null);
  const [savedThreshold,setSavedThreshold]=useState(null);
  const [thresholdStatus,setThresholdStatus]=useState('');
  const [savingThreshold,setSavingThreshold]=useState(false);
  useEffect(()=>{
    requestSequence.current+=1;
    setTrialThreshold(thresholdTrial?.threshold ?? null);
    if(thresholdTrial){setResult(null);setComparison(null);setStatus('idle');setThresholdStatus('');}
    let active=true;
    const settingsSaved=event=>setSavedThreshold(event.detail.recall?.direct_threshold ?? 0.65);
    window.addEventListener('serein:settings-saved',settingsSaved);
    instanceSettings().then(value=>{if(active)setSavedThreshold(value.recall?.direct_threshold ?? 0.65);})
      .catch(()=>{if(active)setSavedThreshold(null);});
    return()=>{active=false;window.removeEventListener('serein:settings-saved',settingsSaved);};
  },[thresholdTrial]);
  async function saveTrialThreshold(){
    if(trialThreshold===null)return;
    setSavingThreshold(true);setThresholdStatus('');
    try{
      const current=await instanceSettings();
      const saved=await instanceSettings({expected_version:current.settings_version,recall:{direct_threshold:trialThreshold}});
      setSavedThreshold(saved.recall.direct_threshold);setTrialThreshold(null);onClearThresholdTrial();
      setThresholdStatus('阈值已保存，后续聊天使用此值。');
    }catch(error){setThresholdStatus(error.message);}
    finally{setSavingThreshold(false);}
  }
  const [simulationScope, setSimulationScope] = useState("live_mirror");
  const [recallAblation, setRecallAblation] = useState("normal");
  const [trainingForm, setTrainingForm] = useState({ expectedAction: "", expectedRoute: "", memoryIds: "" });
  const [candidateJudgments, setCandidateJudgments] = useState({});
  const [trainingNotice, setTrainingNotice] = useState("");

  const runSimulation = async (event) => {
    event?.preventDefault();
    const text = query.trim();
    if (!text || status === "loading" || savedThreshold===null) return;
    const sequence=++requestSequence.current;
    setStatus("loading");
    setError("");
    setComparison(null);
    const savedValue=savedThreshold,trialValue=trialThreshold??0.65;
    const requestAt=async threshold=>{
      const response=await fetch("/__serein/gateway/recall",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({query:text,simulation:true,simulation_scope:simulationScope,
          include_debug:true,recall_mode:"full",recall_ablation:recallAblation,direct_threshold:threshold}),
      });
      const payload=await response.json();
      if(!response.ok)throw new Error(payload?.message||payload?.error||"Gateway 没有返回结果");
      return payload;
    };
    try {
      const [savedRun,trialRun]=await Promise.allSettled([requestAt(savedValue),requestAt(trialValue)]);
      if(sequence!==requestSequence.current)return;
      setComparison({saved:{threshold:savedValue,result:savedRun.status==='fulfilled'?savedRun.value:null,error:savedRun.status==='rejected'?savedRun.reason?.message:''},
        trial:{threshold:trialValue,label:trialThreshold===null?'默认':'试调',result:trialRun.status==='fulfilled'?trialRun.value:null,error:trialRun.status==='rejected'?trialRun.reason?.message:''}});
      if(trialRun.status==='rejected'&&savedRun.status==='rejected')throw new Error('两档模拟都没有完成，请稍后重试。');
      setResult(trialRun.status==='fulfilled'?trialRun.value:savedRun.value);
      setResultScope(simulationScope);
      setTrainingForm({ expectedAction: "", expectedRoute: "", memoryIds: "" });
      setCandidateJudgments({});
      setTrainingNotice("");
      setStatus("done");
    } catch (requestError) {
      if(sequence!==requestSequence.current)return;
      setResult(null);
      setError(requestError instanceof Error ? requestError.message : "无法连接 Gateway");
      setStatus("error");
    }
  };

  const debug = result?.debug ?? {};
  const semantic = debug.semantic_recall_debug ?? {};
  const diagnostics = recallSimulationDiagnostics(result);
  const routeScores = diagnostics.routeScores;
  const boundaryVeto = semantic.boundary_veto ?? {};
  const boundaryCandidate = boundaryVeto.candidate ?? null;
  const injected = debug.recall_why_summary?.injected ?? [];
  const suppressed = debug.recall_why_summary?.suppressed ?? [];
  const cards = result?.cards ?? [];
  const retrievalBudget = semantic.retrieval_budget ?? {};
  const prototypePrior = retrievalBudget.prototype_prior ?? {};
  const sentinel = retrievalBudget.sentinel ?? {};
  const cheapRetrieval = retrievalBudget.cheap_retrieval ?? {};
  const rerankerShadow = retrievalBudget.rerank ?? {};
  const cueSemanticShadow = retrievalBudget.cue_semantic ?? {};
  const eventProbe = retrievalBudget.event_probe ?? retrievalBudget.fact_event_probe ?? {};
  const passageCandidateShadow = retrievalBudget.passage_candidate_shadow ?? {};
  const passageCandidatePolicy = passageCandidateShadow.policy ?? {};
  const passageCandidateLanes = passageCandidateShadow.lanes ?? {};
  const passageCandidates = Array.isArray(passageCandidateShadow.candidates)
    ? passageCandidateShadow.candidates
    : [];
  const passageQueryViewShadow = passageCandidateShadow.query_view_shadow ?? {};
  const weakCandidateTriggerShadow = passageCandidateShadow.weak_candidate_trigger_shadow ?? {};
  const passageQueryViewCandidates = Array.isArray(passageQueryViewShadow.candidates)
    ? passageQueryViewShadow.candidates
    : [];
  const typedPreview = retrievalBudget.typed_event_scene_preview ?? {};
  const typedScope = typedPreview.entity_scope ?? {};
  const typedSurfaceGate = typedPreview.surface_reranker_gate ?? {};
  const typedAdmission = typedPreview.admission ?? {};
  const typedAdmissionCandidates = Array.isArray(typedAdmission.candidates)
    ? typedAdmission.candidates
    : [];
  const typedPreviewCards = Array.isArray(typedPreview.cards) ? typedPreview.cards : [];
  const passageCandidateByRef = new Map(passageCandidates.map((candidate) => [
    `${candidate.owner_kind}:${candidate.owner_id}`,
    candidate,
  ]));
  const episodeVerifier = retrievalBudget.episode_verifier ?? {};
  const ablationDebug = retrievalBudget.recall_ablation ?? semantic.recall_ablation ?? {
    mode: recallAblation,
  };
  const resultSimulationScope = semantic.simulation_scope || resultScope || "live_mirror";
  const candidateEvidence = Array.isArray(cheapRetrieval.candidates)
    ? cheapRetrieval.candidates
    : [];
  const momentDebugByBucketId = new Map(
    (debug.recalled_moment_debug ?? []).map((item) => [item.bucket_id, item]),
  );
  const trainingRouteOptions = [...new Map([
    ...semanticRouteSnapshot.routes.map((route) => [route.name, route.label || routeLabels[route.name] || route.name]),
    ...routeScores.map((route) => [route.route, routeLabels[route.route] || route.route]),
  ].filter(([name]) => name)).entries()];

  const chooseExpectedAction = (expectedAction) => {
    setTrainingForm((current) => ({
      ...current,
      expectedAction,
      expectedRoute: current.expectedRoute
        || (expectedAction === "recall" ? "recall_needed" : diagnostics.route || "present_chitchat"),
    }));
    setTrainingNotice("");
  };

  const saveTrainingLabel = async () => {
    try {
    const saved = await upsertRecallSimulationTrainingLabel({
      query: result?.query || query,
      expectedAction: trainingForm.expectedAction,
      expectedRoute: trainingForm.expectedRoute,
      expectedMemoryIds: trainingForm.memoryIds.split(/[\s,，]+/),
      observedAction: diagnostics.appliedAction,
      observedRoute: diagnostics.route,
      ablationMode: ablationDebug.mode || recallAblation,
      candidateTelemetry: candidateEvidence,
      candidateJudgments: candidateEvidence.flatMap((candidate, index) => {
        const relevance = candidateJudgments[candidate.bucket_id];
        return relevance ? [{ memoryId: candidate.bucket_id, rank: index + 1, relevance }] : [];
      }),
      simulationTelemetry: {
        semantic: {...semantic, applied_action: diagnostics.appliedAction},
        retrievalBudget,
        sentinel,
        ablation: ablationDebug,
      },
    });
    setTrainingNotice(saved.status === "added"
      ? "已保存为人工模拟训练标注；下次导出会与真实观察合并，并保留来源。"
      : saved.status === "updated"
        ? "这句的人工模拟标注已更新，不会重复堆一条。"
        : "先选择这句话应该召回还是应该跳过。");
    } catch (error) { setTrainingNotice(error.message); }
  };

  return (
    <section className="basement-workbench" aria-labelledby="recall-simulator-title">
      <header className="basement-workbench__header">
        <div>
          <span className="basement-kicker">真实 Gateway 路径</span>
          <h2 id="recall-simulator-title">召回模拟</h2>
          <p>输入原句，查看当前召回流程在两档阈值下返回的卡片、候选和筛选原因。测试不会留下正式注入记录。</p>
        </div>
        <div className="basement-live-note">
          <i aria-hidden="true" />
          <span>{semantic.model || semanticRouteSnapshot.model}</span>
        </div>
      </header>

      <div className="recall-threshold-bar">
        <p>每次运行两档对照：已保存 {savedThreshold??'…'} · {trialThreshold===null?'默认':'试调'} {trialThreshold??0.65}。试调不会更改配置。</p>
        <div className="settings-actions threshold-actions">
          <button type="button" disabled={trialThreshold===null||trialThreshold===savedThreshold||savedThreshold===null||savingThreshold||status==='loading'} onClick={saveTrialThreshold}>保存此阈值</button>
          <button type="button" onClick={()=>{
            window.location.hash='#settings';
            window.dispatchEvent(new CustomEvent('serein:open-settings-tab',{detail:'configuration'}));
          }}>返回配置调节</button>
        </div>
        {thresholdStatus&&<p role="status">{thresholdStatus}</p>}
      </div>

      <form className="recall-simulator-form" onSubmit={runSimulation}>
        <label htmlFor="recall-simulator-query">原句</label>
        <textarea
          id="recall-simulator-query"
          value={query}
          rows={3}
          placeholder="把这一轮真正会说的话放进来。"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runSimulation(event);
          }}
        />
        <div className="recall-simulator-form__footer">
          <div className="recall-example-prompts" aria-label="测试原句">
            {basementRecallExamples.map((example) => (
              <button type="button" key={example} onClick={() => setQuery(example)}>{example}</button>
            ))}
          </div>
          <button className="basement-primary-action" type="submit" disabled={!query.trim() || status === "loading" || savedThreshold===null}>
            {status === "loading" ? <ArrowClockwise size={17} className="is-spinning" aria-hidden="true" /> : <Flask size={17} aria-hidden="true" />}
            {status === "loading" ? "正在跑两档" : "运行两档对照"}
          </button>
        </div>
        <fieldset className="recall-ablation-control">
          <legend>模拟范围</legend>
          <div className="recall-ablation-control__options recall-simulation-scope__options">
            {recallSimulationScopeOptions.map((option) => (
              <label key={option.value} className={simulationScope === option.value ? "is-active" : ""}>
                <input
                  type="radio"
                  name="recall-simulation-scope"
                  value={option.value}
                  checked={simulationScope === option.value}
                  onChange={(event) => {
                    setSimulationScope(event.target.value);
                    if (event.target.value === "live_mirror") setRecallAblation("normal");
                  }}
                />
                <span><strong>{option.label}</strong><small>{option.description}</small></span>
              </label>
            ))}
          </div>
          <p>两档都不写正式注入记录。默认档复现线上停点；完整档只用于排查为什么某条候选被捞起。</p>
        </fieldset>
        <fieldset className="recall-ablation-control">
          <legend>消融观察</legend>
          <div className="recall-ablation-control__options">
            {recallAblationOptions.map((option) => (
              <label key={option.value} className={recallAblation === option.value ? "is-active" : ""}>
                <input
                  type="radio"
                  name="recall-ablation"
                  value={option.value}
                  checked={recallAblation === option.value}
                  disabled={simulationScope !== "full_shadow"}
                  onChange={(event) => setRecallAblation(event.target.value)}
                />
                <span><strong>{option.label}</strong><small>{option.description}</small></span>
              </label>
            ))}
          </div>
          <p>{simulationScope === "full_shadow" ? "Route 与 evidence veto 保持不变；这里只切换 shadow 候选通道，不把 cue 混进 Scene 正文向量。" : "切到“展开 shadow”后才能使用消融。"}</p>
        </fieldset>
      </form>

      {comparison&&<section className="recall-threshold-comparison" aria-label="阈值对照结果">
        <div className="recall-result-section__heading"><h3>两档结果</h3><span>同一句原话 · 两次独立模拟</span></div>
        <div className="recall-threshold-comparison__grid">
          {[['saved','已保存'],['trial',comparison.trial.label]].map(([key,label])=>{
            const run=comparison[key],cards=run.result?.cards??[],typed=run.result?.debug?.typed_event_scene_live??{};
            return <article key={key} className="recall-threshold-comparison__card">
              <div><span>{label}</span><strong>{run.threshold.toFixed(2)}</strong></div>
              {run.error?<p role="alert">{run.error}</p>:<><p>进入卡片 {cards.length} 张</p>
                {cards.length?<ul>{cards.map((card,index)=><li key={card.id??index}>{card.title??card.id}</li>)}</ul>
                  :<small>{typed.reason||typed.status||'没有放行记忆'}</small>}</>}
            </article>;
          })}
        </div>
        <p>下方显示{comparison.trial.result?'试调档':'已保存档'}的详细诊断；两次模型评分可能有小幅波动。</p>
      </section>}

      {status === "idle" && (
        <div className="basement-empty-state">
          <span>这里显示真实结果</span>
          <p>Route 只是入口判断。最终有没有记忆出现，还要继续经过候选、证据与放行。</p>
        </div>
      )}

      {status === "error" && (
        <div className="basement-error" role="alert">
          <WarningCircle size={19} aria-hidden="true" />
          <div><strong>没有走到 Gateway</strong><p>{error}</p></div>
        </div>
      )}

      {status === "done" && (
        <div className="recall-result" aria-live="polite">
          <section className="recall-decision">
            <div className="recall-decision__route">
              <span>ROUTE</span>
              <strong>{routeLabels[diagnostics.route] || diagnostics.route || "未返回"}</strong>
              <em className={`route-action route-action--${diagnostics.routeAction || "unknown"}`}>
                {actionLabel(diagnostics.routeAction)}
              </em>
            </div>
            <dl className="recall-decision__facts">
              <div><dt>路由分数</dt><dd>{recallScore(diagnostics.routeScore)}</dd></div>
              <div><dt>候选记忆</dt><dd>{diagnostics.candidateCountLabel}</dd></div>
              <div><dt>本档模拟卡片</dt><dd>{diagnostics.cardCount ?? "未返回"}</dd></div>
              <div><dt>路由原因</dt><dd>{reasonLabels[diagnostics.routeReason] || diagnostics.routeReason || "未返回"}</dd></div>
              <div><dt>处理阶段</dt><dd>{diagnostics.stage}</dd></div>
              {diagnostics.reason && <div><dt>处理原因</dt><dd>{reasonLabels[diagnostics.reason] || diagnostics.reason}</dd></div>}
              <div><dt>模拟范围</dt><dd>{resultSimulationScope === "full_shadow"
                ? Object.keys(retrievalBudget).length ? "完整 shadow 诊断" : "当前流程（未返回额外 shadow）"
                : "live mirror"}</dd></div>
            </dl>
          </section>

          {diagnostics.hasLive && (
          <section className="recall-result-section recall-evidence-decomposition recall-typed-diagnostics" aria-label="本档候选与筛选">
            <div className="recall-result-section__heading">
              <h3>本档候选与筛选</h3><span>{diagnostics.stage}</span>
            </div>
            <dl className="recall-decision__facts">
              <div><dt>筛选方式</dt><dd>{typedAdmissionModeLabels[diagnostics.admission.mode] || diagnostics.admission.mode || "未进入"}</dd></div>
              <div><dt>最终筛选阈值</dt><dd>{recallScore(diagnostics.admission.direct_threshold)}</dd></div>
              <div><dt>重排实际用句</dt><dd>{diagnostics.rerankQuery ?? "未返回"}</dd></div>
            </dl>
            {diagnostics.candidates.length ? <div className="recall-evidence-list">
              {diagnostics.candidates.map((candidate) => <article
                className={`recall-evidence-row ${candidate.selected ? "is-qualified" : "is-suppressed"}`} key={candidate.ref}>
                <header><strong>{candidate.title || candidate.ref}</strong>
                  <span>{candidate.ref.split(":")[0]} · {candidate.selected ? "已生成卡片" : candidate.disposition || "未返回筛选结果"}</span></header>
                <dl>
                  <div><dt>候选分数</dt><dd>{recallScore(candidate.candidate_score)}</dd></div>
                  <div><dt>重排分数</dt><dd>{recallScore(candidate.rerank_score)}</dd></div>
                  <div><dt>筛选原因</dt><dd>{typedAdmissionReasonLabels[candidate.reason] || candidate.reason || "未返回"}</dd></div>
                  <div><dt>候选来源</dt><dd>{(candidate.candidate_sources || []).map((source) => recallCandidateSourceLabels[source] || source).join(" · ") || "未返回"}</dd></div>
                </dl>
                <small>{candidate.ref}</small>
              </article>)}
            </div> : <p className="recall-none">{diagnostics.candidateCountLabel === "未运行"
              ? "候选检索未运行。" : diagnostics.candidateCount === 0 ? "本轮候选数为 0。" : "接口未返回候选明细。"}</p>}
          </section>
          )}

          {resultSimulationScope === "full_shadow" && Object.keys(retrievalBudget).length > 0 && (
          <section className="recall-result-section">
            <div className="recall-result-section__heading">
              <h3>预算 Router（simulation shadow）</h3>
              <span>{retrievalBudget.final_budget || retrievalBudget.effective_budget || "未返回"}</span>
            </div>
            <dl className="recall-decision__facts">
              <div><dt>surface_route</dt><dd>{retrievalBudget.surface_route || "—"}</dd></div>
              <div><dt>route_budget</dt><dd>{retrievalBudget.route_budget || "—"}</dd></div>
              <div><dt>effective_budget</dt><dd>{retrievalBudget.effective_budget || "—"}</dd></div>
              <div><dt>三态预算</dt><dd>{retrievalBudget.initial_budget || "—"} → {retrievalBudget.final_budget || "—"}</dd></div>
              <div><dt>升级原因</dt><dd>{retrievalBudget.escalation_reason || retrievalBudget.budget_decision_source || "默认浅查"}</dd></div>
              <div><dt>anchor_override</dt><dd>{retrievalBudget.anchor_override ? "是" : "否"}</dd></div>
              <div><dt>pure chitchat prior</dt><dd>{retrievalBudget.pure_chitchat_prior ? "高置信候选" : "否"}</dd></div>
              <div><dt>prototype confidence</dt><dd>{prototypePrior.confidence == null ? "—" : percent(prototypePrior.confidence)}</dd></div>
              <div><dt>sentinel top1/2</dt><dd>{sentinel.called ? `${sentinel.floor_qualified_count ?? 0} / ${sentinel.candidate_count ?? 0}` : sentinel.reason || "未运行"}</dd></div>
              <div><dt>absolute floor</dt><dd>{cheapRetrieval.floor_qualified_count ?? 0} / {cheapRetrieval.candidate_count ?? 0}</dd></div>
              <div><dt>reranker gray zone</dt><dd>{cheapRetrieval.gray_zone_count ?? 0} / {cheapRetrieval.reranker_eligible_count ?? 0} eligible</dd></div>
              <div><dt>cue embedding</dt><dd>{cueSemanticShadow.status === "available" ? `${cueSemanticShadow.candidate_count ?? 0} 条候选 · v${cueSemanticShadow.dataset_version ?? "?"}` : cueSemanticShadow.reason || "未建立索引"}</dd></div>
              <div><dt>Event probe</dt><dd>{eventProbe.status === "ok" ? `${eventProbe.candidate_count ?? 0} 条 · ${(eventProbe.matches || []).slice(0, 3).map((item) => `${item.memory_kind}:${percent(item.score)}`).join(" · ") || "无达标候选"}` : eventProbe.reason || eventProbe.status || "未启用"}</dd></div>
              <div><dt>passage candidate shadow</dt><dd>{passageCandidateShadow.status === "ok" ? `${passageCandidateShadow.candidate_count ?? 0} / ${passageCandidatePolicy.pool_limit ?? 7} 条占席 · 不参与决定` : passageCandidateShadow.reason || passageCandidateShadow.status || "未启用"}</dd></div>
              <div><dt>reranker shadow</dt><dd>{rerankerShadow.called ? `${rerankerShadow.score_count ?? 0} / ${rerankerShadow.candidate_count ?? 0} 已评分 · 不参与决定` : rerankerShadow.would_call ? rerankerShadowReasonLabels[rerankerShadow.reason] || rerankerShadow.reason || "有资格，尚未调用" : rerankerShadowReasonLabels[rerankerShadow.reason] || rerankerShadow.reason || "未进入"}</dd></div>
              <div><dt>同一事件核对</dt><dd>{episodeVerifier.called ? `${episodeVerifier.decisions?.length ?? 0} / ${episodeVerifier.candidate_count ?? 0} 已核对 · ${episodeVerifier.timing_ms ?? 0} ms` : episodeVerifier.reason || "未进入"}</dd></div>
              <div><dt>query_facets</dt><dd>{(retrievalBudget.query_facets || []).map((facet) => `${facet.kind}:${facet.value}`).join(" · ") || "—"}</dd></div>
            </dl>
            {rerankerShadow.score_count > 0 && rerankerShadow.decision_applied === false && (
              <p className="recall-evidence-decomposition__note">
                shadow 高分只记录为观察证据；不会参与放行、排序、cards、recalled_ids 或 context，也不会把未召回的候选拉起。
              </p>
            )}
            <p className="recall-evidence-decomposition__note">
              sentinel 只复用现有 query vector 做 top1/2 救援检查，不扩图、不 rerank、不注入、不写正式记录；异常一律 fail-open。
            </p>
          </section>
          )}

          {resultSimulationScope === "full_shadow" && Object.keys(passageCandidateShadow).length > 0 && (
          <section className="recall-result-section recall-evidence-decomposition">
            <div className="recall-result-section__heading">
              <h3>局部证据候选（simulation shadow）</h3>
              <span>{passageCandidates.length} 条 · live injection 关闭</span>
            </div>
            <p className="recall-evidence-decomposition__note">
              Scene / Event 都只用整体与长文本 passage 两路 embedding，按 owner 取最高分；cue 和词语只负责带入候选，不参与评分。Fact 已退出召回，importance 不参与候选或排序；shadow 仍观察全部 active Event。
            </p>
            {weakCandidateTriggerShadow.status === "observed" && (
              <p className="recall-evidence-decomposition__note">
                弱候选触发 shadow：{weakCandidateTriggerShadow.would_trigger ? "会启动分句检索" : "不会启动"}
                {` · ${weakCandidateTriggerShadow.reason || "—"} · top body ${percent(weakCandidateTriggerShadow.top_body_semantic)} · ${weakCandidateTriggerShadow.multi_clause ? "多分句" : "单一短句"}`}
                {" · 只记录判断，不改变 live 或本轮 shadow 执行"}
              </p>
            )}
            {passageQueryViewShadow.status === "ok" && (
              <div className="recall-shadow-query-views">
                <p className="recall-evidence-decomposition__note">
                  分句 query shadow：{(passageQueryViewShadow.views || []).map((view) => typeof view === "string" ? view : view.query).filter(Boolean).join(" · ") || "—"}
                  {` · ${passageQueryViewShadow.timing_ms ?? 0} ms · 只扩候选`}
                </p>
                <p className="recall-evidence-decomposition__note">
                  分句池前列：{passageQueryViewCandidates.slice(0, 5).map((candidate) => `${candidate.title || candidate.owner_id}${(passageQueryViewShadow.added_owner_ids || []).includes(candidate.owner_id) ? "（新增）" : ""}`).join(" · ") || "无"}
                </p>
              </div>
            )}
            {passageCandidateShadow.status === "ok" ? (
              passageCandidates.length ? (
                <div className="recall-evidence-list">
                  {passageCandidates.map((candidate) => {
                    const evidence = candidate.passages?.[0]?.evidence_text
                      || candidate.passages?.[0]?.text
                      || candidate.matched_spans?.[0]?.text
                      || "未返回精确片段";
                    return (
                      <article className="recall-evidence-row is-qualified" key={`${candidate.owner_kind}:${candidate.owner_id}`}>
                        <header>
                          <strong>{candidate.title || candidate.owner_id}</strong>
                          <span>{passageCandidateKindLabels[candidate.owner_kind] || candidate.owner_kind} · {passageCandidateLaneLabels[candidate.candidate_lane] || candidate.candidate_lane}</span>
                        </header>
                        <dl>
                          <div><dt>候选分数</dt><dd>{passageCandidateScore(candidate)}</dd></div>
                          <div><dt>自动浮现</dt><dd>{candidate.owner_kind !== "event" ? "Scene 按自身合同" : candidate.recallable === true ? "允许" : candidate.recallable === false ? "关闭" : "未审核（仅 shadow）"}</dd></div>
                          <div><dt>发现来源</dt><dd>{(candidate.candidate_sources || []).map((source) => recallCandidateSourceLabels[source] || source).join(" · ") || "未记录"}</dd></div>
                          <div><dt>命中 cue / 词语</dt><dd>{[...(candidate.matched_cues || []), ...(candidate.specific_terms || [])].join(" · ") || "—"}</dd></div>
                        </dl>
                        <p className="recall-shadow-evidence-text">{evidence}</p>
                        {candidate.owner_kind === "scene" && (
                          <button type="button" className="recall-card__memory-link" onClick={() => openSceneInMemory({ bucket_id: candidate.owner_id })}>
                            在记忆卡里查看 Scene
                          </button>
                        )}
                      </article>
                    );
                  })}
                </div>
              ) : <p className="recall-none">四路都没有捞到候选。</p>
            ) : <p className="recall-none">{passageCandidateShadow.reason || passageCandidateShadow.status || "局部证据索引尚未建立"}</p>}
            <p className="recall-evidence-decomposition__note">
              各路原始候选数：cue 绑定 {passageCandidateLanes.cue_passage?.candidate_count ?? 0} · passage {passageCandidateLanes.passage?.candidate_count ?? 0} · Fact/Event 正文 {passageCandidateLanes.fact_event_body?.candidate_count ?? 0} · Fact/Event 词语 {passageCandidateLanes.fact_event_lexical?.candidate_count ?? 0}。
            </p>
          </section>
          )}

          {resultSimulationScope === "full_shadow" && Object.keys(typedPreview).length > 0 && (
          <section className="recall-result-section recall-evidence-decomposition typed-recall-preview">
            <div className="recall-result-section__heading">
              <h3>Event / Scene 预计注入</h3>
              <span>{typedPreview.status === "would_inject" ? `${typedPreviewCards.length} 张卡 · 仅模拟` : typedPreview.reason || typedPreview.status || "未运行"}</span>
            </div>
            <p className="recall-evidence-decomposition__note">
              这里复用 typed live 的 scope、recallable、surface gate、reranker 与 admission；只返回反事实预览，不写 injection，不消耗 Arc 菜单冷却。
            </p>
            <dl className="recall-decision__facts">
              <div><dt>scope</dt><dd>{typedScope.scope_anchor?.arc_key || typedScope.status || "无 Arc"}</dd></div>
              <div><dt>intent / operator</dt><dd>{typedScope.intent || "none"} / {typedScope.operator || "none"}</dd></div>
              <div><dt>surface gate</dt><dd>{typedSurfaceGate.applied ? typedSurfaceGate.reason || "已拦截" : typedSurfaceGate.reason || "允许进入"}</dd></div>
              <div><dt>admission</dt><dd>{typedAdmissionModeLabels[typedAdmission.mode] || typedAdmission.mode || "未进入"}</dd></div>
              <div><dt>预计选择</dt><dd>{typedPreview.selected_refs?.length ?? 0} / {typedPreview.candidate_count ?? passageCandidates.length}</dd></div>
              <div><dt>耗时</dt><dd>{typedPreview.timing_ms == null ? "—" : `${typedPreview.timing_ms} ms`}</dd></div>
            </dl>
            {(typedPreview.excluded_event_refs_by_recallable?.length > 0) && (
              <p className="recall-evidence-decomposition__note">
                recallable 已排除：{typedPreview.excluded_event_refs_by_recallable.join(" · ")}
              </p>
            )}
            {typedAdmissionCandidates.length ? (
              <div className="recall-evidence-list">
                {typedAdmissionCandidates.map((candidate) => {
                  const source = passageCandidateByRef.get(candidate.ref) || {};
                  const accepted = (typedPreview.selected_refs || []).includes(candidate.ref)
                    || (typedAdmission.material_refs || []).includes(candidate.ref);
                  return (
                    <article className={`recall-evidence-row ${accepted ? "is-qualified" : "is-suppressed"}`} key={candidate.ref}>
                      <header>
                        <strong>{source.title || candidate.ref}</strong>
                        <span>{passageCandidateKindLabels[candidate.owner_kind] || candidate.owner_kind} · {candidate.disposition}</span>
                      </header>
                      <dl>
                        <div><dt>候选分数</dt><dd>{candidate.candidate_score == null ? "—" : percent(candidate.candidate_score)}</dd></div>
                        <div><dt>reranker</dt><dd>{candidate.rerank_score == null ? "未调用" : `${percent(candidate.rerank_score)} / ${percent(typedAdmission.direct_threshold)}`}</dd></div>
                        <div><dt>admission</dt><dd>{typedAdmissionReasonLabels[candidate.reason] || candidate.reason || "未判断"}</dd></div>
                        <div><dt>发现来源</dt><dd>{(source.candidate_sources || []).map((item) => recallCandidateSourceLabels[item] || item).join(" · ") || "未记录"}</dd></div>
                      </dl>
                    </article>
                  );
                })}
              </div>
            ) : <p className="recall-none">{typedPreview.reason || "没有 Event / Scene 进入 admission。"}</p>}
            <div className="recall-result-section__heading typed-recall-preview__cards-heading">
              <h3>这次模拟返回的卡片</h3>
              <span>{typedPreviewCards.length} 张记忆卡{typedPreview.menus_included?.length ? ` · ${typedPreview.menus_included.length} 个 Arc 菜单` : ""}</span>
            </div>
            {typedPreviewCards.length ? (
              <div className="recall-card-list">
                {typedPreviewCards.map((item) => (
                  <article className="recall-card" key={item.id}>
                    <div><strong>{item.title || item.id}</strong><span>{passageCandidateKindLabels[item.source_kind] || item.source_kind}</span></div>
                    {item.text && <p>{item.text}</p>}
                    <small>{item.id}</small>
                  </article>
                ))}
              </div>
            ) : <p className="recall-none">这次额外模拟没有返回 Event / Scene 卡片。</p>}
          </section>
          )}

          <section className="recall-result-section">
            <div className="recall-result-section__heading"><h3>路线对照</h3><span>同一原句只做一次 query embedding</span></div>
            <div className="route-score-list">
              {routeScores.map((score) => (
                <div className="route-score" key={score.route}>
                  <div><strong>{routeLabels[score.route] || score.route}</strong><span>{actionLabel(score.action)}</span></div>
                  <output>{recallScore(score.score)}</output>
                  {score.threshold != null && <small>路由阈值：{recallScore(score.threshold)}</small>}
                  {score.top_examples?.[0]?.text && <small>最近例句：{score.top_examples[0].text}</small>}
                </div>
              ))}
            </div>
            {!routeScores.length && <p className="recall-none">接口未返回路线对照分数。</p>}
          </section>

          {resultSimulationScope === "full_shadow" && Object.keys(cheapRetrieval).length > 0 && (
          <section className="recall-result-section recall-evidence-decomposition">
            <div className="recall-result-section__heading">
              <h3>候选证据拆解</h3>
              <span>{candidateEvidence.length} 条 · {recallAblationOptions.find((option) => option.value === ablationDebug.mode)?.label || ablationDebug.mode || "正常"}</span>
            </div>
            <p className="recall-evidence-decomposition__note">
              canonical Scene 的 body semantic 仍是正文原文向量；cue semantic 来自独立索引且只负责候选发现，不是注入证据。索引不可用时明确显示 unavailable，不用 0 冒充。
            </p>
            {candidateEvidence.length ? (
              <div className="recall-evidence-list">
                {candidateEvidence.map((candidate) => (
                  <article className={`recall-evidence-row ${candidate.reranker_eligible ? "is-qualified" : "is-suppressed"}`} key={candidate.bucket_id}>
                    <header>
                      <strong>{candidate.title || candidate.bucket_id}</strong>
                      <span>{candidate.final_admission_source || "pending"}</span>
                    </header>
                    <dl>
                      <div><dt>body semantic</dt><dd>{candidate.body_semantic_score == null ? "—" : percent(candidate.body_semantic_score)}</dd></div>
                      <div><dt>semantic profile</dt><dd>{candidate.semantic_profile || "unknown"}</dd></div>
                      <div><dt>cue semantic</dt><dd>{candidate.cue_semantic?.score == null ? candidate.cue_semantic?.status || "unknown" : `${percent(candidate.cue_semantic.score)} · candidate only`}</dd></div>
                      <div><dt>cue lexical</dt><dd>{candidate.cue_lexical_match ? `${candidate.cue_lexical_role || "matched"} · 命中` : "未命中"}</dd></div>
                      <div><dt>title anchor</dt><dd>{candidate.title_anchor_match ? "命中" : "未命中"}</dd></div>
                      <div><dt>候选来源</dt><dd>{(candidate.candidate_sources || []).map((source) => recallCandidateSourceLabels[source] || source).join(" · ") || "未记录"}</dd></div>
                      <div><dt>discovery / absolute / reranker entry</dt><dd>{percent(candidate.discovery_score ?? candidate.combined_score)} / {percent(candidate.absolute_floor)} / {percent(candidate.reranker_entry_floor)}</dd></div>
                      <div><dt>reranker shadow</dt><dd>{candidate.reranker_shadow?.score == null ? rerankerShadowLabels[candidate.reranker_shadow?.status] || candidate.reranker_shadow?.status || "未调用" : percent(candidate.reranker_shadow.score)}</dd></div>
                      <div><dt>未调用原因</dt><dd>{candidate.reranker_shadow?.called === false ? candidate.reranker_shadow?.called_false_reason || candidate.reranker_shadow?.reason || "未记录" : "—"}</dd></div>
                      <div><dt>原文证据</dt><dd>{candidate.reranker_shadow?.evidence_status === "bound" ? `${candidate.reranker_shadow.evidence_count ?? 0} 条绑定片段` : "unknown（不等于 unsupported）"}</dd></div>
                      <div><dt>同一事件核对</dt><dd>{candidate.episode_verifier?.verdict ? `${episodeVerifierLabels[candidate.episode_verifier.verdict] || candidate.episode_verifier.verdict} · ${percent(candidate.episode_verifier.confidence)}` : "未调用"}</dd></div>
                      <div><dt>核对依据</dt><dd>{candidate.episode_verifier?.grounded_cue || candidate.episode_verifier?.current_evidence_span || candidate.episode_verifier?.reason || "—"}</dd></div>
                    </dl>
                    {candidate.reranker_shadow?.score != null && candidate.reranker_shadow?.decision_applied === false && (
                      <p className="recall-evidence-decomposition__note">
                        shadow 分数仅供校准；本候选不会因高分被放行或拉起。
                      </p>
                    )}
                    <div className="recall-candidate-review" role="group" aria-label={`候选相关度：${candidate.title || candidate.bucket_id}`}>
                      <span>候选相关度</span>
                      {candidateRelevanceOptions.map((option) => (
                        <button
                          type="button"
                          className={candidateJudgments[candidate.bucket_id] === option.value ? "is-active" : ""}
                          key={option.value}
                          onClick={() => setCandidateJudgments((current) => ({
                            ...current,
                            [candidate.bucket_id]: current[candidate.bucket_id] === option.value ? undefined : option.value,
                          }))}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                    <button type="button" className="recall-card__memory-link" onClick={() => openSceneInMemory(candidate)}>
                      在记忆卡里查看召回入口
                    </button>
                  </article>
                ))}
              </div>
            ) : <p className="recall-none">本轮没有进入廉价候选池的记忆。</p>}
          </section>
          )}

          {boundaryCandidate && (
            <section className="recall-result-section">
              <div className="recall-result-section__heading">
                <h3>边界护栏</h3>
                <span>{boundaryVeto.applied ? "已撤销 skip" : "本轮未触发"}</span>
              </div>
              <div className="route-score-list">
                <div className="route-score">
                  <div>
                    <strong>{routeLabels[boundaryCandidate.route] || boundaryCandidate.route}</strong>
                    <span>{actionLabel(boundaryCandidate.action)}</span>
                  </div>
                  <output>{percent(boundaryCandidate.score)}</output>
                  <small>边界例句：{boundaryCandidate.text || "无"}</small>
                  <small>
                    {boundaryCandidate.passes_threshold ? "已过护栏阈值" : `未过护栏阈值 ${percent(boundaryVeto.threshold)}`}
                    {" · "}
                    {boundaryCandidate.beats_skip
                      ? "强于 skip 路线"
                      : boundaryCandidate.within_deficit
                        ? `落后 ${percent(boundaryCandidate.deficit)}，仍在护栏差值内`
                        : `落后 ${percent(boundaryCandidate.deficit)}，超过护栏差值 ${percent(boundaryVeto.max_deficit)}`}
                  </small>
                </div>
              </div>
            </section>
          )}

          <section className="recall-result-section">
            <div className="recall-result-section__heading"><h3>本档模拟卡片</h3><span>{diagnostics.cardCount ?? "未返回"} 条 · 仅模拟</span></div>
            {(cards.length || injected.length) ? (
              <div className="recall-card-list">
                {(cards.length ? cards : injected).map((item, index) => (
                  <article className="recall-card" key={item.id || item.bucket_id || index}>
                    <div><strong>{item.title || item.bucket_name || item.id || item.bucket_id}</strong><span>{item.source || item.final_status || "direct"}</span></div>
                    {(item.text || item.content) && <p>{item.text || item.content}</p>}
                    {(item.admission_reasons?.length > 0) && <small>{item.admission_reasons.join(" / ")}</small>}
                    {momentDebugByBucketId.get(item.bucket_id)?.authored_cue_match ? (
                      <small className="recall-card__cue-hit">
                        cue 命中（正文不导出）
                      </small>
                    ) : null}
                    {item.source_kind !== "event" && !String(item.id || "").startsWith("event:") && <button type="button" className="recall-card__memory-link" onClick={() => openSceneInMemory(item)}>
                      在记忆卡里查看召回入口
                    </button>}
                  </article>
                ))}
              </div>
            ) : <p className="recall-none">{diagnostics.stage}。{diagnostics.reason ? `原因：${reasonLabels[diagnostics.reason] || diagnostics.reason}` : "本档没有返回记忆卡片。"}</p>}
          </section>

          <section className="recall-training-label">
            <div className="recall-result-section__heading">
              <h3>保存为训练标注</h3>
              <span>人工模拟 · 不冒充真实 Hook</span>
            </div>
            <p>判断这一句本来应该做什么。重复保存同一句会更新原标注，不会越堆越多。</p>
            <div className="recall-training-label__actions" role="group" aria-label="预期召回动作">
              <button
                type="button"
                className={trainingForm.expectedAction === "recall" ? "is-active" : ""}
                onClick={() => chooseExpectedAction("recall")}
              >应该召回</button>
              <button
                type="button"
                className={trainingForm.expectedAction === "skip" ? "is-active" : ""}
                onClick={() => chooseExpectedAction("skip")}
              >应该跳过</button>
            </div>
            <div className="recall-training-label__fields">
              <label>
                <span>预期路线</span>
                <select
                  value={trainingForm.expectedRoute}
                  onChange={(event) => setTrainingForm((current) => ({ ...current, expectedRoute: event.target.value }))}
                >
                  <option value="">不标路线</option>
                  {trainingRouteOptions.map(([name, label]) => <option value={name} key={name}>{label}</option>)}
                </select>
              </label>
              <label>
                <span>目标记忆 ID（可选）</span>
                <input
                  value={trainingForm.memoryIds}
                  placeholder="scene_…；多个可用空格分开"
                  onChange={(event) => setTrainingForm((current) => ({ ...current, memoryIds: event.target.value }))}
                />
              </label>
              <button
                className="basement-primary-action"
                type="button"
                disabled={!trainingForm.expectedAction || !diagnostics.appliedAction}
                onClick={saveTrainingLabel}
              ><Check size={16} aria-hidden="true" />保存标注</button>
            </div>
            {trainingNotice && <small className="recall-training-label__notice" role="status">{trainingNotice}</small>}
          </section>

          {suppressed.length > 0 && (
            <details className="recall-suppressed">
              <summary>被拒绝的候选 <span>{suppressed.length}</span></summary>
              <div>
                {suppressed.slice(0, 12).map((item) => (
                  <p key={item.bucket_id}>
                    <strong>{item.bucket_name || item.bucket_id}</strong>
                    <span>{item.admission_reasons?.join(" / ") || "未通过当前证据门"}</span>
                    <button type="button" onClick={() => openSceneInMemory(item)}>打开记忆卡</button>
                  </p>
                ))}
              </div>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

const domainPolicyLabels = {
  normal: "允许自动浮现",
  explicit_only: "仅明确召回",
  excluded: "不浮现",
};

const domainPolicyDescriptions = {
  normal: "可以参与普通候选、证据放行与关系扩散。",
  explicit_only: "只有明确提到标题、编号或已写入的召回线索时，才可参与召回。",
  excluded: "候选、加分、直召回与扩散全部禁止，恢复正常前不会进入注入。",
};

export function DomainPolicyEditor() {
  const [kind, setKind] = useState("event");
  const [editing, setEditing] = useState(false);
  const dialog = useRef(null);
  const [editingKey, setEditingKey] = useState(null);
  const [entry, setEntry] = useState({ key: "", label: "", description: "", policy: "normal" });
  const [entryError, setEntryError] = useState("");
  const [snapshot, setSnapshot] = useState({ datasetVersion: 1, active: false, catalog: canonicalDomainPolicies, domains: {event: canonicalDomainPolicies, scene: canonicalDomainPolicies} });
  const [drafts, setDrafts] = useState(() => Object.fromEntries(["event", "scene"].map(kind => [kind, readDomainPolicyDraft(canonicalDomainPolicies, kind)])));
  const [versions, setVersions] = useState({event: null, scene: null});
  const domains = drafts[kind];
  const setDomains = next => setDrafts(previous => ({...previous, [kind]: next}));
  const [datasetState, setDatasetState] = useState({ status: "loading", message: "正在核对线上主域策略……" });
  const [publishState, setPublishState] = useState({ status: "idle", message: "" });
  const baseline = JSON.stringify(snapshot.domains[kind]);
  const current = JSON.stringify(domains);
  const contentDirty = current !== baseline;
  const dirty = contentDirty || !snapshot.active;
  const excludedCount = domains.filter((domain) => domain.policy === "excluded").length;
  const explicitCount = domains.filter((domain) => domain.policy === "explicit_only").length;

  const openEntry = (domain = null) => {
    setEditingKey(domain?.key ?? null);
    setEntry(domain ? { ...domain } : { key: "", label: "", description: "", policy: "normal" });
    setEntryError("");
    dialog.current.showModal();
  };
  const saveEntry = async (event) => {
    event.preventDefault();
    const normalized = Object.fromEntries(Object.entries(entry).map(([key, value]) => [key, value.trim()]));
    if (domains.some(domain => domain.key === normalized.key && domain.key !== editingKey)) {
      setEntryError("这个标识已存在，请换一个。");
      return;
    }
    const next = editingKey === null ? [...domains, normalized] : domains.map(domain => domain.key === editingKey ? normalized : domain);
    if (await publishPolicies(next)) dialog.current.close();
  };
  const removeEntry = async (domain) => {
    if (!window.confirm(`移除「${domain.label}」？已有记忆的主域不会自动改写，请先处理仍使用它的记忆。`)) return;
    await publishPolicies(domains.filter(item => item.key !== domain.key));
  };

  const setPolicy = (key, policy) => {
    const nextDomains = domains.map((domain) => domain.key === key ? { ...domain, policy } : domain);
    setDomains(nextDomains);
    saveDomainPolicyDraft(nextDomains, kind, versions[kind]);
  };

  const loadPublishedPolicies = async () => {
    setPublishState({ status: "idle", message: "" });
    setDatasetState({ status: "loading", message: "正在核对线上主域策略……" });
    try {
      const response = await fetch("/__serein/settings");
      const payload = await response.json();
      if (!response.ok || !Array.isArray(payload.tagging?.domains)) {
        throw new Error(String(payload?.error || "domain_policy_dataset_unavailable"));
      }
      const publishedDomains = Object.fromEntries(["event", "scene"].map(kind => [kind, payload.tagging.domains.map(domain => ({
        ...domain, policy: payload.tagging.policies?.[kind]?.[domain.key] ?? domain.policy ?? "normal",
      }))]));
      const nextSnapshot = {
        datasetVersion: Number(payload.tagging_version) || 1,
        active: true,
        catalog: payload.tagging.domains,
        domains: publishedDomains,
      };
      setSnapshot(nextSnapshot);
      setDrafts(Object.fromEntries(["event", "scene"].map(kind => [kind, hasDomainPolicyDraft(kind) ? readDomainPolicyDraft(publishedDomains[kind], kind) : publishedDomains[kind]])));
      setVersions(Object.fromEntries(["event", "scene"].map(kind => [kind, domainPolicyDraftVersion(kind) ?? nextSnapshot.datasetVersion])));
      setDatasetState({ status: "ready", message: `已核对线上 v${nextSnapshot.datasetVersion}` });
    } catch (error) {
      setDatasetState({ status: "error", message: error.message || "没有读到线上主域策略。" });
    }
  };

  useEffect(() => {
    loadPublishedPolicies();
  }, []);

  const resetDraft = () => {
    if (contentDirty && !window.confirm(`放弃 ${kind === "event" ? "Event" : "Scene"} 的主域规则草稿？`)) return;
    setDomains(clearDomainPolicyDraft(snapshot.domains[kind], kind));
    setVersions(previous => ({...previous, [kind]: snapshot.datasetVersion}));
    setPublishState({ status: "idle", message: "" });
  };

  const publishPolicies = async (nextDomains = domains) => {
    const nextVersion = snapshot.datasetVersion + 1;
    setPublishState({ status: "publishing", message: `正在发布 v${nextVersion}……` });
    try {
      const response = await fetch("/__serein/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_tagging_version: versions[kind],
          tagging: {
            policies: {[kind]: Object.fromEntries(nextDomains.map(({key, policy}) => [key, policy]))},
            ...(nextDomains !== domains ? {domains: nextDomains.map(({key, label, description}) => ({key, label, description: description || "", policy: snapshot.catalog.find(domain => domain.key === key)?.policy ?? "normal"}))} : {}),
          },
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        const raw = String(payload?.detail || payload?.error || "主域保存失败");
        if (raw.startsWith("domain_policy_publish_version_conflict")) {
          throw new Error("主域规则已在其他页面变化。草稿已保留；刷新后撤销当前草稿，再按最新规则修改。");
        }
        throw new Error(raw);
      }
      const publishedDomains = Object.fromEntries(["event", "scene"].map(kind => [kind, payload.tagging.domains.map(domain => ({
        ...domain, policy: payload.tagging.policies?.[kind]?.[domain.key] ?? domain.policy ?? "normal",
      }))]));
      const nextSnapshot = {
        datasetVersion: Number(payload.tagging_version),
        active: true,
        catalog: payload.tagging.domains,
        domains: publishedDomains,
      };
      clearDomainPolicyDraft(publishedDomains[kind], kind);
      setSnapshot(nextSnapshot);
      const otherKind = kind === "event" ? "scene" : "event";
      const otherDraft = hasDomainPolicyDraft(otherKind);
      const nextOther = otherDraft ? readDomainPolicyDraft(publishedDomains[otherKind], otherKind) : publishedDomains[otherKind];
      setDrafts({[kind]: publishedDomains[kind], [otherKind]: nextOther});
      const otherVersion = !otherDraft || versions[otherKind] === snapshot.datasetVersion ? nextSnapshot.datasetVersion : versions[otherKind];
      if (otherDraft) saveDomainPolicyDraft(nextOther, otherKind, otherVersion);
      setVersions({[kind]: nextSnapshot.datasetVersion, [otherKind]: otherVersion});
      setDatasetState({ status: "ready", message: `已核对线上 v${nextSnapshot.datasetVersion}` });
      setPublishState({ status: "success", message: `v${nextSnapshot.datasetVersion} 已切换生效。` });
      return true;
    } catch (error) {
      setPublishState({ status: "error", message: error.message || "发布没有完成，线上策略未改变。" });
      setEntryError(error.message || "保存没有完成，请重试。");
      return false;
    }
  };

  return (
    <section className="basement-workbench" aria-labelledby="domain-policy-title">
      <header className="basement-workbench__header">
        <div>
          <span className="basement-kicker">标签与召回范围</span>
          <h2 id="domain-policy-title">主域边界</h2>
          <p>Event 与 Scene 分别保存浮现规则。名称和短描述共用，供模型打标；程序按规则决定浮现，已有记忆不会重新打标。</p>
        </div>
        <div className="domain-policy-header-actions">
          <button className="domain-entry-action" type="button" aria-pressed={editing} onClick={() => setEditing(value => !value)} disabled={publishState.status === "publishing"}>{editing ? "完成" : "编辑"}</button>
          <div className="domain-policy-summary" aria-label="主域策略摘要">
          <span><strong>{explicitCount}</strong> 仅明确</span>
          <span><strong>{excludedCount}</strong> 已排除</span>
          </div>
        </div>
      </header>

      <div className="domain-policy-controls" role="group" aria-label="记忆类型">
        {[["event", "Event"], ["scene", "Scene"]].map(([value, label]) => <button key={value} type="button" aria-pressed={kind === value} className={kind === value ? "is-active" : ""} onClick={() => {setKind(value); setPublishState({status:"idle", message:""});}} disabled={publishState.status === "publishing"}>{label}</button>)}
      </div>

      <div className="domain-policy-list">
        {domains.map((domain) => (
          <article className={`domain-policy-row domain-policy-row--${domain.policy}`} key={domain.key}>
            <div className="domain-policy-row__identity">
              <span>{domain.key}</span>
              <h3>{domain.label}</h3>
              <p>{domain.description}</p>
              {editing && <div className="domain-entry-actions">
                <button type="button" onClick={() => openEntry(domain)} disabled={datasetState.status !== "ready" || publishState.status === "publishing"}>编辑</button>
                <button type="button" onClick={() => removeEntry(domain)} disabled={datasetState.status !== "ready" || publishState.status === "publishing"}>移除</button>
              </div>}
            </div>
            <div className="domain-policy-controls" role="group" aria-label={`${domain.label}主域策略`}>
              {Object.entries(domainPolicyLabels).map(([policy, label]) => (
                <button
                  type="button"
                  className={domain.policy === policy ? "is-active" : ""}
                  key={policy}
                  onClick={() => setPolicy(domain.key, policy)}
                  aria-pressed={domain.policy === policy}
                  disabled={datasetState.status !== "ready" || publishState.status === "publishing"}
                >
                  {label}
                </button>
              ))}
            </div>
            <p className="domain-policy-row__explanation">
              {domainPolicyDescriptions[domain.policy]}
            </p>
          </article>
        ))}
      </div>

      <footer className="route-editor-footer">
        <div>
          <strong>{!snapshot.active
            ? "主域配置尚未启用"
            : dirty ? "召回范围尚未保存" : `已保存 · v${snapshot.datasetVersion}`}</strong>
          <span>{publishState.message || datasetState.message || "保存后，当前类型的浮现规则立即生效。"}</span>
        </div>
        <div>
          <button type="button" onClick={loadPublishedPolicies} disabled={datasetState.status === "loading" || publishState.status === "publishing"}>刷新配置</button>
          {editing && <button type="button" onClick={() => openEntry()} disabled={datasetState.status !== "ready" || publishState.status === "publishing" || domains.length >= 50}>添加</button>}
          <button type="button" onClick={resetDraft} disabled={publishState.status === "publishing" || (!contentDirty && versions[kind] === snapshot.datasetVersion)}>撤销草稿</button>
          <button
            type="button"
            className="basement-primary-action"
            onClick={() => publishPolicies()}
            disabled={!dirty || datasetState.status !== "ready" || publishState.status === "publishing"}
            title={datasetState.status === "ready" ? "保存主域配置并立即生效" : "先读取当前主域配置"}
          >{publishState.status === "publishing" ? "正在保存" : `保存 ${kind === "event" ? "Event" : "Scene"} 规则`}</button>
        </div>
      </footer>
      <dialog ref={dialog} className="agent-guide domain-entry-dialog" aria-labelledby="domain-entry-title" onCancel={event => { if (publishState.status === "publishing") event.preventDefault(); }}>
        <header><h3 id="domain-entry-title">{editingKey === null ? "添加主域" : "编辑主域"}</h3>
          <button type="button" aria-label="关闭主域编辑" disabled={publishState.status === "publishing"} onClick={() => dialog.current.close()}>×</button></header>
        <form onSubmit={saveEntry}>
          <label className="settings-field"><span>名称</span><input autoFocus required maxLength={40} value={entry.label} onChange={event => setEntry({ ...entry, label: event.target.value })} /></label>
          <label className="settings-field"><span>标识</span><input required pattern="[a-z][a-z0-9_-]*" maxLength={60} disabled={editingKey !== null} placeholder="例如 learning" value={entry.key} onChange={event => setEntry({ ...entry, key: event.target.value })} /><small>以小写字母开头，可含数字、下划线和短横线；保存后保持不变。</small></label>
          <label className="settings-field"><span>短描述</span><textarea rows={3} maxLength={300} placeholder="这个主域包含哪些内容，与其他主域如何区分" value={entry.description} onChange={event => setEntry({ ...entry, description: event.target.value })} /></label>
          <label className="settings-field"><span>召回范围</span><select value={entry.policy} onChange={event => setEntry({ ...entry, policy: event.target.value })}>{Object.entries(domainPolicyLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          <p>名称和短描述会一起交给打标模型。{contentDirty && "保存时也会应用当前页尚未保存的召回范围。"}</p>
          {entryError && <p role="alert">{entryError}</p>}
          <div className="settings-actions"><button type="button" disabled={publishState.status === "publishing"} onClick={() => dialog.current.close()}>取消</button><button type="submit" disabled={publishState.status === "publishing"}>{publishState.status === "publishing" ? "保存中…" : "保存"}</button></div>
        </form>
      </dialog>
    </section>
  );
}

function RouteExampleEditor() {
  const [routeSnapshot, setRouteSnapshot] = useState(semanticRouteSnapshot);
  const [routes, setRoutes] = useState(() => readSemanticRouteDraft(semanticRouteSnapshot));
  const [selectedRouteName, setSelectedRouteName] = useState("recall_needed");
  const [creatingRoute, setCreatingRoute] = useState(false);
  const [newRouteLabel, setNewRouteLabel] = useState("");
  const [newRouteAction, setNewRouteAction] = useState("skip");
  const [editingIndex, setEditingIndex] = useState(null);
  const [editingText, setEditingText] = useState("");
  const [newText, setNewText] = useState("");
  const [newRole, setNewRole] = useState("typical");
  const [datasetState, setDatasetState] = useState({ status: "loading", message: "正在核对线上版本……" });
  const [publishState, setPublishState] = useState({ status: "idle", message: "" });
  const [draftSyncState, setDraftSyncState] = useState({ status: "idle", message: "" });
  const draftRevisionRef = useRef(0);
  const saveQueueRef = useRef(Promise.resolve());

  const selectedRoute = routes.find((route) => route.name === selectedRouteName) ?? routes[0];
  const baseline = JSON.stringify(routeSnapshot.routes);
  const dirty = JSON.stringify(routes) !== baseline;
  const boundaryRebuildNeeded = routeSnapshot.boundaryExampleCount > 0 && !routeSnapshot.boundaryIndexReady;
  const exampleCount = useMemo(
    () => routes.reduce((total, route) => total + route.utterances.length, 0),
    [routes],
  );

  const loadPublishedDataset = async () => {
    setDatasetState({ status: "loading", message: "正在核对线上版本……" });
    try {
      const response = await fetch("/__serein/gateway/semantic-routes");
      const payload = await readRouteApiResponse(response);
      if (!response.ok) throw new Error(payload.message || payload.error || "route_dataset_unavailable");
      const nextSnapshot = routeSnapshotFromApi(payload);
      setRouteSnapshot(nextSnapshot);
      setRoutes(nextSnapshot.routes);
      const localRoutes = readSemanticRouteDraft(nextSnapshot);
      const serverState = await readServerSemanticRouteDraft();
      const serverDraftState = inspectServerSemanticRouteDraft(serverState, nextSnapshot.datasetVersion);
      let nextRoutes = localRoutes;
      if (serverDraftState.status === "conflict") {
        draftRevisionRef.current = serverDraftState.draft.revision || 0;
        setDraftSyncState({
          status: "conflict",
          message: `服务器草稿基于 v${serverDraftState.baseDatasetVersion}，已隔离保留；当前显示线上 v${nextSnapshot.datasetVersion}。`,
        });
        setDatasetState({
          status: "conflict",
          message: `已读到线上 v${nextSnapshot.datasetVersion}；先处理 v${serverDraftState.baseDatasetVersion} 草稿冲突再编辑。`,
        });
        return;
      }
      if (serverDraftState.status === "current") {
        nextRoutes = serverDraftState.draft.routes;
        draftRevisionRef.current = serverDraftState.draft.revision || 0;
        saveSemanticRouteDraft(nextRoutes, nextSnapshot.datasetVersion);
        setDraftSyncState({ status: "saved", message: "服务器草稿已载入" });
      } else if (JSON.stringify(localRoutes) !== JSON.stringify(nextSnapshot.routes)) {
        const migrated = await saveServerSemanticRouteDraft(localRoutes, nextSnapshot.datasetVersion, 0);
        draftRevisionRef.current = migrated.draft.revision;
        setDraftSyncState({ status: "saved", message: "原浏览器草稿已迁到服务器" });
      } else {
        nextRoutes = nextSnapshot.routes;
        draftRevisionRef.current = 0;
        setDraftSyncState({ status: "saved", message: "服务器暂无草稿" });
      }
      const routesWithRecommendedThresholds = applyRecommendedRouteThresholds(nextRoutes);
      if (JSON.stringify(routesWithRecommendedThresholds) !== JSON.stringify(nextRoutes)) {
        const saved = await saveServerSemanticRouteDraft(
          routesWithRecommendedThresholds,
          nextSnapshot.datasetVersion,
          draftRevisionRef.current,
        );
        draftRevisionRef.current = saved.draft.revision;
        nextRoutes = routesWithRecommendedThresholds;
        saveSemanticRouteDraft(nextRoutes, nextSnapshot.datasetVersion);
        setDraftSyncState({ status: "saved", message: "已加入闲聊路线的建议阈值，等待发布" });
      }
      setRoutes(nextRoutes);
      setDatasetState({ status: "ready", message: `已核对线上 v${nextSnapshot.datasetVersion}` });
    } catch (error) {
      setDatasetState({ status: "error", message: error.message || "没有读到线上 Router 数据集。" });
    }
  };

  useEffect(() => {
    loadPublishedDataset();
  }, []);

  const persistRoutes = (nextRoutes) => {
    if (datasetState.status !== "ready") {
      window.alert(datasetState.status === "conflict"
        ? `旧版服务器草稿仍在保留。请先放弃旧草稿或完成合并，再编辑线上 v${routeSnapshot.datasetVersion}。`
        : "先核对线上 Router 版本，再编辑例句。");
      return Promise.resolve(null);
    }
    setRoutes(nextRoutes);
    saveSemanticRouteDraft(nextRoutes, routeSnapshot.datasetVersion);
    setDraftSyncState({ status: "saving", message: "正在保存到服务器……" });
    const task = saveQueueRef.current.catch(() => {}).then(async () => {
      const saved = await saveServerSemanticRouteDraft(
        nextRoutes,
        routeSnapshot.datasetVersion,
        draftRevisionRef.current,
      );
      draftRevisionRef.current = saved.draft.revision;
      setDraftSyncState({ status: "saved", message: "草稿已保存在服务端" });
      return saved;
    });
    saveQueueRef.current = task;
    task.catch((error) => {
      setDraftSyncState({
        status: "error",
        message: error.message === "route_draft_revision_conflict"
          ? "服务器草稿已在别处变化，重新核对后再编辑。"
          : "草稿没有保存到服务器。",
      });
    });
    return task;
  };

  const commitRoutes = (nextRoutes) => { persistRoutes(nextRoutes); };

  const addRoute = (event) => {
    event.preventDefault();
    const label = newRouteLabel.trim();
    if (!label) return;
    if (routes.some((route) => (route.label || route.name).trim() === label || route.name === label)) {
      window.alert("这个类别已经存在了。");
      return;
    }
    const route = {
      name: label,
      label,
      action: newRouteAction === "recall" ? "recall" : "skip",
      threshold: newRouteAction === "recall" ? defaultRouteThreshold : 0.60,
      enabled: true,
      utterances: [],
    };
    commitRoutes([...routes, route]);
    setSelectedRouteName(route.name);
    setNewRouteLabel("");
    setNewRouteAction("skip");
    setCreatingRoute(false);
  };

  const deleteRoute = () => {
    if (!selectedRoute || routes.length <= 1) return;
    const label = selectedRoute.label || routeLabels[selectedRoute.name] || selectedRoute.name;
    const detail = selectedRoute.utterances.length
      ? `，连同其中 ${selectedRoute.utterances.length} 条例句`
      : "";
    if (!window.confirm(`从发布草稿中删除类别「${label}」${detail}？`)) return;
    const selectedIndex = routes.findIndex((route) => route.name === selectedRoute.name);
    const nextRoutes = routes.filter((route) => route.name !== selectedRoute.name);
    const nextSelected = nextRoutes[Math.min(selectedIndex, nextRoutes.length - 1)];
    commitRoutes(nextRoutes);
    setSelectedRouteName(nextSelected.name);
    setEditingIndex(null);
  };

  const startEditing = (index, text) => {
    setEditingIndex(index);
    setEditingText(text);
  };

  const saveEdit = () => {
    const text = editingText.trim();
    if (!text || editingIndex == null) return;
    const nextRoutes = clone(routes);
    const edited = nextRoutes.find((route) => route.name === selectedRoute.name).utterances[editingIndex];
    edited.text = text;
    edited.status = "draft";
    commitRoutes(nextRoutes);
    setEditingIndex(null);
    setEditingText("");
  };

  const deleteExample = (index) => {
    const item = selectedRoute.utterances[index];
    if (!window.confirm(`从草稿中删除「${item.text}」？`)) return;
    const nextRoutes = clone(routes);
    nextRoutes.find((route) => route.name === selectedRoute.name).utterances.splice(index, 1);
    commitRoutes(nextRoutes);
    setEditingIndex(null);
  };

  const addExample = (event) => {
    event.preventDefault();
    const text = newText.trim();
    if (!text) return;
    const duplicate = routes.some((route) => route.utterances.some((item) => item.text.trim() === text));
    if (duplicate) {
      window.alert("这句已经在例句库里了。");
      return;
    }
    const nextRoutes = clone(routes);
    nextRoutes.find((route) => route.name === selectedRoute.name).utterances.push({
      text,
      role: newRole,
      origin: "manual",
      status: "draft",
    });
    commitRoutes(nextRoutes);
    setNewText("");
  };

  const updateRouteThreshold = (rawValue) => {
    const parsed = Number(rawValue);
    const current = Number(selectedRoute.threshold ?? defaultRouteThreshold);
    if (!Number.isFinite(parsed)) return current;
    const threshold = Math.round(Math.max(0.40, Math.min(0.95, parsed)) * 100) / 100;
    if (threshold === current && selectedRoute.threshold != null) return threshold;
    const nextRoutes = clone(routes);
    nextRoutes.find((route) => route.name === selectedRoute.name).threshold = threshold;
    commitRoutes(nextRoutes);
    return threshold;
  };

  const resetDraft = async () => {
    const draftConflict = datasetState.status === "conflict";
    if ((dirty || draftConflict) && !window.confirm(draftConflict
      ? `放弃服务器上基于旧版本的例句草稿？线上 v${routeSnapshot.datasetVersion} 不会改变。`
      : "放弃本机所有例句草稿？")) return;
    try {
      await saveQueueRef.current.catch(() => {});
      await clearServerSemanticRouteDraft();
      draftRevisionRef.current = 0;
      setRoutes(clearSemanticRouteDraft(routeSnapshot));
      setDraftSyncState({ status: "saved", message: "服务器草稿已清除" });
      setDatasetState({ status: "ready", message: `已核对线上 v${routeSnapshot.datasetVersion}` });
      setEditingIndex(null);
    } catch {
      setDraftSyncState({ status: "error", message: "服务器草稿没有清除。" });
    }
  };

  const publishRoutes = async () => {
    const boundaryCount = routes.reduce((total, route) => (
      total + route.utterances.filter((item) => item.role === "boundary" && item.status !== "retired").length
    ), 0);
    const nextVersion = routeSnapshot.datasetVersion + 1;
    const thresholdSummary = routes
      .filter((route) => route.enabled !== false)
      .map((route) => `${route.label || route.name} ${percent(route.threshold ?? defaultRouteThreshold)}`)
      .join(" · ");
    const confirmed = window.confirm(
      `把完整 Router 数据集从 v${routeSnapshot.datasetVersion} 发布为 v${nextVersion}？\n\n`
      + `路线阈值：${thresholdSummary}\n\n`
      + `将重建全部启用路线的典型例句向量；${boundaryCount} 条边界例句会建立独立护栏向量，但不进入等权路线中心。强反向边界命中只会撤销错误 skip，不会强制注入。构建或校验失败时，当前版本保持不变。`,
    );
    if (!confirmed) return;
    setPublishState({ status: "publishing", message: `正在构建 v${nextVersion} 的全部向量……` });
    try {
      await saveQueueRef.current;
      const response = await fetch("/__serein/gateway/semantic-routes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_dataset_version: routeSnapshot.datasetVersion,
          confirm: "PUBLISH_SEMANTIC_ROUTES",
          routes,
        }),
      });
      const payload = await readRouteApiResponse(response);
      if (!response.ok) throw new Error(routePublishError(String(payload.error || payload.message || "")));
      const nextSnapshot = routeSnapshotFromApi(payload);
      await clearServerSemanticRouteDraft();
      draftRevisionRef.current = 0;
      clearSemanticRouteDraft(nextSnapshot);
      setRouteSnapshot(nextSnapshot);
      setRoutes(readSemanticRouteDraft(nextSnapshot));
      setPublishState({ status: "success", message: `v${nextSnapshot.datasetVersion} 已完整重建并切换生效。` });
      setDraftSyncState({ status: "saved", message: "服务器草稿已在发布后清除" });
      setDatasetState({ status: "ready", message: `已核对线上 v${nextSnapshot.datasetVersion}` });
    } catch (error) {
      setPublishState({ status: "error", message: error.message || "发布没有完成，线上版本未改变。" });
    }
  };

  return (
    <section className="basement-workbench" aria-labelledby="route-editor-title">
      <header className="basement-workbench__header">
        <div>
          <span className="basement-kicker">人工审核后才生效</span>
          <h2 id="route-editor-title">例句维护</h2>
          <p>分别维护样本角色与来源。这里只整理人工审核草稿，生成模型不进入在线请求路径。</p>
        </div>
        <div className="route-draft-state">
          <strong>{exampleCount}</strong>
          <span>{dirty
            ? "服务器草稿未发布"
            : `${routeSnapshot.deploymentState === "production" ? "生产快照" : "本地候选"} v${routeSnapshot.datasetVersion}`}</span>
          {["ready", "conflict"].includes(datasetState.status) && (
            <span>
              边界护栏 {routeSnapshot.indexedBoundaryExampleCount ?? 0}/{routeSnapshot.boundaryExampleCount ?? 0}
              {routeSnapshot.boundaryIndexReady ? " 已索引" : " 等待重建"}
            </span>
          )}
        </div>
      </header>

      <div className="route-editor-layout">
        <nav className="route-family-list" aria-label="召回路线">
          {routes.map((route) => (
            <button
              type="button"
              className={route.name === selectedRoute.name ? "is-active" : ""}
              key={route.name}
              onClick={() => { setSelectedRouteName(route.name); setEditingIndex(null); }}
            >
              <span><strong>{route.label || routeLabels[route.name] || route.name}</strong><small>{route.enabled ? actionLabel(route.action) : "未启用"}</small></span>
              <em>{route.utterances.length}</em>
            </button>
          ))}
          {creatingRoute ? (
            <form className="route-family-create" onSubmit={addRoute}>
              <input
                autoFocus
                value={newRouteLabel}
                onChange={(event) => setNewRouteLabel(event.target.value)}
                placeholder="类别名称"
                aria-label="类别名称"
              />
              <select value={newRouteAction} onChange={(event) => setNewRouteAction(event.target.value)} aria-label="召回行为">
                <option value="skip">no-recall · 直接 skip</option>
                <option value="recall">recall · 继续召回</option>
              </select>
              <div>
                <button type="submit" disabled={!newRouteLabel.trim()}><Check size={15} aria-hidden="true" />保存</button>
                <button type="button" onClick={() => { setCreatingRoute(false); setNewRouteLabel(""); }}><X size={15} aria-hidden="true" />取消</button>
              </div>
            </form>
          ) : (
            <button type="button" className="route-family-add" onClick={() => setCreatingRoute(true)}>
              <Plus size={16} aria-hidden="true" />
              <span><strong>新增类别</strong><small>选择 skip 或 recall</small></span>
            </button>
          )}
        </nav>

        {selectedRoute ? <div className="route-example-editor">
          <div className="route-example-editor__heading">
            <div><h3>{selectedRoute.label || routeLabels[selectedRoute.name]}</h3><p>{selectedRoute.name}</p></div>
            <div className="route-example-editor__heading-actions">
              <label className="route-threshold-control">
                <span>命中阈值</span>
                <input
                  key={`${selectedRoute.name}-${selectedRoute.threshold ?? "default"}`}
                  type="number"
                  min="0.40"
                  max="0.95"
                  step="0.01"
                  defaultValue={Number(selectedRoute.threshold ?? defaultRouteThreshold).toFixed(2)}
                  onBlur={(event) => { event.target.value = updateRouteThreshold(event.target.value).toFixed(2); }}
                  onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }}
                  aria-label={`${selectedRoute.label || selectedRoute.name}命中阈值`}
                />
              </label>
              <span className={`route-action route-action--${selectedRoute.action}`}>{actionLabel(selectedRoute.action)}</span>
              <button type="button" onClick={deleteRoute} disabled={routes.length <= 1} aria-label={`删除类别${selectedRoute.label || selectedRoute.name}`} title="从发布草稿中删除整个类别">
                <Trash size={15} aria-hidden="true" />删除类别
              </button>
            </div>
          </div>

          <form className="route-example-add" onSubmit={addExample}>
            <input value={newText} onChange={(event) => setNewText(event.target.value)} placeholder="新增一条经过判断的原句" />
            <select value={newRole} onChange={(event) => setNewRole(event.target.value)} aria-label="样本角色">
              <option value="typical">典型例句</option>
              <option value="boundary">边界例句</option>
            </select>
            <button type="submit" disabled={!newText.trim()}><Plus size={16} aria-hidden="true" />加入草稿</button>
          </form>

          <div className="route-example-list">
            {selectedRoute.utterances.length ? selectedRoute.utterances.map((item, index) => (
              <div className="route-example-row" key={`${item.text}-${index}`}>
                {editingIndex === index ? (
                  <div className="route-example-row__edit">
                    <input autoFocus value={editingText} onChange={(event) => setEditingText(event.target.value)} onKeyDown={(event) => {
                      if (event.key === "Enter") saveEdit();
                      if (event.key === "Escape") setEditingIndex(null);
                    }} />
                    <button type="button" aria-label="保存修改" onClick={saveEdit}><Check size={16} aria-hidden="true" /></button>
                    <button type="button" aria-label="取消修改" onClick={() => setEditingIndex(null)}><X size={16} aria-hidden="true" /></button>
                  </div>
                ) : (
                  <>
                    <div>
                      <p>{item.text}</p>
                      <span>{roleLabels[item.role] || item.role} · {originLabels[item.origin] || item.origin} · {exampleStatusLabels[item.status] || item.status}</span>
                    </div>
                    <div className="route-example-row__actions">
                      <button type="button" aria-label={`编辑${item.text}`} onClick={() => startEditing(index, item.text)}><PencilSimple size={15} aria-hidden="true" /></button>
                      <button type="button" aria-label={`删除${item.text}`} onClick={() => deleteExample(index)}><Trash size={15} aria-hidden="true" /></button>
                    </div>
                  </>
                )}
              </div>
            )) : <p className="route-example-empty">{selectedRoute.enabled === false
              ? "这条路线还没有例句。未启用路线可以先留空。"
              : "这条路线还没有例句。发布前至少补一条经过判断的原句。"}</p>}
          </div>
        </div> : <div className="route-example-editor basement-empty-state" role="status">
          当前没有召回路线。可以先新增类别；如果已经配置过路线，请核对服务器数据集。
        </div>}
      </div>

      <div className="route-example-contract">
        <strong>典型例句</strong>描述路线中心；<strong>边界例句</strong>使用独立护栏向量，不进入等权路线中心。no-recall 路线达到阈值并领先时准备 skip；若反向边界分数更强，护栏只撤销这次 skip，仍由正常检索与证据门决定是否注入。
      </div>

      <footer className="route-editor-footer">
        <div>
          <strong>{dirty
            ? draftSyncState.status === "saving" ? "正在保存服务器草稿" : "草稿保存在服务端"
            : boundaryRebuildNeeded
              ? "生产数据未变，但边界护栏等待重建"
              : `与${routeSnapshot.deploymentState === "production" ? "生产快照" : "本地候选"}一致`}</strong>
          <span>{publishState.message || (boundaryRebuildNeeded ? `当前 ${routeSnapshot.boundaryExampleCount} 条边界例句尚未进入独立护栏索引。` : "") || draftSyncState.message || datasetState.message || "发布时校验全部类别、递增版本，并原子重建所有已启用类别的 Router 向量；no-recall 命中后直接 skip。"}</span>
        </div>
        <div>
          <button type="button" onClick={loadPublishedDataset} disabled={datasetState.status === "loading" || publishState.status === "publishing"} title="重新读取线上完整数据集"><ArrowClockwise size={14} className={datasetState.status === "loading" ? "is-spinning" : ""} />核对线上</button>
          <button type="button" onClick={resetDraft} disabled={(!dirty && datasetState.status !== "conflict") || publishState.status === "publishing"}>{datasetState.status === "conflict" ? "放弃旧版草稿" : "撤销草稿"}</button>
          <button
            type="button"
            className="basement-primary-action"
            onClick={publishRoutes}
            disabled={(!dirty && !boundaryRebuildNeeded) || datasetState.status !== "ready" || publishState.status === "publishing"}
            title={datasetState.status === "ready" ? "校验完整数据集、重建全部典型例句向量并原子切换" : "先核对线上 Router 版本"}
          >{publishState.status === "publishing" ? <><ArrowClockwise size={14} className="is-spinning" />正在重建</> : boundaryRebuildNeeded && !dirty ? "重建并发布边界护栏" : "发布并重建向量"}</button>
        </div>
      </footer>
    </section>
  );
}

export function BasementPage() {
  const [activeTool, setActiveTool] = useState("recall");
  const [thresholdTrial,setThresholdTrial]=useState(null);
  useEffect(()=>{
    const openSimulation=event=>{
      const value=event.detail?.threshold;
      if(typeof value!=='number'||!Number.isFinite(value)||value<0||value>1)return;
      setThresholdTrial({threshold:value});setActiveTool('recall');
    };
    window.addEventListener('serein:open-recall-simulation',openSimulation);
    return()=>window.removeEventListener('serein:open-recall-simulation',openSimulation);
  },[]);

  useEffect(() => {
    document.querySelector(".basement-experience")?.scrollTo({ top: 0 });
  }, [activeTool]);

  return (
    <div className="basement-experience">
      <header className="basement-page-header">
        <div>
          <span>不常开灯的地方</span>
          <h1>地下室</h1>
        </div>
        <p>先把记忆入口看清楚，再决定要不要改变它。</p>
      </header>

      <div className="basement-layout">
        <aside className="basement-tool-index" aria-label="地下室工具">
          <button type="button" className={activeTool === "recall" ? "is-active" : ""} onClick={() => setActiveTool("recall")}>
            <Flask size={19} weight="light" aria-hidden="true" />
            <span><strong>召回模拟</strong><small>走一遍真实入口</small></span>
          </button>
          <button type="button" className={activeTool === "observations" ? "is-active" : ""} onClick={() => setActiveTool("observations")}>
            <Eye size={19} weight="light" aria-hidden="true" />
            <span><strong>召回观察</strong><small>看真实运行与误差</small></span>
          </button>
          <button type="button" className={activeTool === "domains" ? "is-active" : ""} onClick={() => setActiveTool("domains")}>
            <WarningCircle size={19} weight="light" aria-hidden="true" />
            <span><strong>主域边界</strong><small>标签、描述与召回范围</small></span>
          </button>
          <button type="button" className={activeTool === "examples" ? "is-active" : ""} onClick={() => setActiveTool("examples")}>
            <PencilSimple size={19} weight="light" aria-hidden="true" />
            <span><strong>例句维护</strong><small>审核 Router 边界</small></span>
          </button>
          <button type="button" className={activeTool === "revisions" ? "is-active" : ""} onClick={() => setActiveTool("revisions")}>
            <GitDiff size={19} weight="light" aria-hidden="true" />
            <span><strong>修订箱</strong><small>来源与叙事卷之间</small></span>
          </button>
          <button type="button" className={activeTool === "relationships" ? "is-active" : ""} onClick={() => setActiveTool("relationships")}>
            <ShareNetwork size={19} weight="light" aria-hidden="true" />
            <span><strong>关系提案</strong><small>审核 Scene 之间的边</small></span>
          </button>
        </aside>

        {activeTool === "recall"
          ? <RecallSimulator thresholdTrial={thresholdTrial} onClearThresholdTrial={()=>setThresholdTrial(null)} />
          : activeTool === "observations"
            ? <BasementRecallObservation />
            : activeTool === "domains"
              ? <DomainPolicyEditor />
              : activeTool === "examples"
                ? <RouteExampleEditor />
                : activeTool === "revisions"
                  ? <BasementRevisionInbox />
                  : <BasementRelationshipProposals />}
      </div>
    </div>
  );
}

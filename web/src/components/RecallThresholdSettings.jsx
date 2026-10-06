import {useState} from 'react';
import {instanceSettings} from '../storage/instanceStore.js';

export function RecallThresholdSettings({config,draft,setDraft,candidateDraft,setCandidateDraft,onSaved,disabled=false}) {
  const [busy,setBusy]=useState(false),[status,setStatus]=useState('');
  const value=draft??config.recall?.direct_threshold??0.65;
  const bodyValue=candidateDraft.body_candidate_threshold??config.recall?.body_candidate_threshold??0.50;
  const cueValue=candidateDraft.cue_candidate_threshold??config.recall?.cue_candidate_threshold??0.55;
  const poolValue=candidateDraft.direct_pool_limit??config.recall?.direct_pool_limit??20;
  const values=[value,bodyValue,cueValue];
  const valid=values.every(item=>item!==''&&Number.isFinite(Number(item))&&Number(item)>=0&&Number(item)<=1)&&
    poolValue!==''&&Number.isInteger(Number(poolValue))&&Number(poolValue)>=6&&Number(poolValue)<=100;
  async function save(){
    if(!valid)return;
    setBusy(true);setStatus('');
    try{
      const result=await instanceSettings({expected_version:config.settings_version,recall:{direct_threshold:Number(value),
        body_candidate_threshold:Number(bodyValue),cue_candidate_threshold:Number(cueValue),direct_pool_limit:Number(poolValue)}});
      onSaved(result);setDraft(null);setCandidateDraft({});setStatus('已保存，后续聊天使用这些门槛。');
    }catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  return <section className="settings-group" aria-labelledby="settings-recall-threshold-title">
    <div className="settings-group__heading"><h3 id="settings-recall-threshold-title">召回门槛</h3><p>候选门槛控制哪些弱候选值得交给重排；最终门槛决定重排后是否允许带入。分数不代表正确率。</p></div>
    <label className="settings-field"><span>候选池上限（默认 20 条）</span>
      <input type="number" min="6" max="100" step="1" value={poolValue} disabled={disabled||busy}
        onChange={event=>setCandidateDraft(current=>({...current,direct_pool_limit:event.target.value}))}/>
      <small>可填 6–100。扩大后，排名更后的记忆也有机会进入重排，但仍需通过候选门槛；可能增加耗时。开启关联时最多额外加入 1 条。</small></label>
    <label className="settings-field"><span>正文向量扩展门槛（默认 0.50）</span>
      <input type="number" min="0" max="1" step="0.01" value={bodyValue} disabled={disabled||busy}
        onChange={event=>setCandidateDraft(current=>({...current,body_candidate_threshold:event.target.value}))}/>
      <small>用于向量排名第 7 条起、候选池上限以内的整篇或 Passage 资格；前 6 条宽探针不受它影响。</small></label>
    <label className="settings-field"><span>Scene cue 语义扩展门槛（默认 0.55）</span>
      <input type="number" min="0" max="1" step="0.01" value={cueValue} disabled={disabled||busy}
        onChange={event=>setCandidateDraft(current=>({...current,cue_candidate_threshold:event.target.value}))}/>
      <small>比较查询与 cue 的语义向量，不要求逐字相同；只授予进入重排的资格，不直接加分。</small></label>
    <label className="settings-field"><span>最终筛选阈值（默认 0.65）</span>
      <input type="number" min="0" max="1" step="0.01" value={value} disabled={disabled||busy} onChange={event=>setDraft(event.target.value)}/></label>
    <p className="model-connection-help">关键词与带回忆意图的完整实体名也可提名候选。所有候选仍需通过最终重排；路由、主域和冷却照常执行。保存后下一轮生效，无需重建向量。</p>
    <div className="settings-actions threshold-actions">
      <button type="button" disabled={disabled||busy||!valid||(
        Number(value)===(config.recall?.direct_threshold??0.65)&&
        Number(bodyValue)===(config.recall?.body_candidate_threshold??0.50)&&
        Number(poolValue)===(config.recall?.direct_pool_limit??20)&&
        Number(cueValue)===(config.recall?.cue_candidate_threshold??0.55))} onClick={save}>{busy?'保存中…':'保存召回门槛'}</button>
      <button type="button" disabled={disabled||busy||!valid} onClick={()=>{
        window.dispatchEvent(new CustomEvent('serein:open-recall-simulation',{detail:{threshold:Number(value)}}));
        window.location.hash='#basement';
      }}>去召回模拟试调</button>
    </div>
    {status&&<p role="status">{status}</p>}
  </section>;
}

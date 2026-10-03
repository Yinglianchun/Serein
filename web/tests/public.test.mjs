import {test} from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {callSereinBackend} from '../server/sereinBackend.mjs';
import {buildNarrativeTaskPrompt,normalizeNarrativeWriterResult,runNarrativeCodexTask} from '../server/narrativeCodexRunner.mjs';
import {buildSceneEvidenceRefs} from '../server/sceneEvidenceBridge.mjs';
import {resolveGatewayObservationOutcome,gatewayRequestLabel,resolveBridgeObservationOutcome} from '../src/recallObservationOutcome.js';
import {readFileSync} from 'node:fs';
import {loadWindowShadows,windowShadowViews} from '../src/storage/windowShadowStore.js';
import {readLiveWindowShadows} from '../vite.config.mjs';

test('imported and revised shadow sections reach portraits without syncing instance introductions',async(t)=>{
  const previous={url:process.env.SEREIN_MEMORY_URL,token:process.env.SEREIN_MEMORY_TOKEN};
  process.env.SEREIN_MEMORY_URL='http://synthetic.invalid';
  process.env.SEREIN_MEMORY_TOKEN='synthetic';
  try {
    const sections={user_view:'最新的你\n保留换行。',self_view:'最新的自己',recent_events:'这部分只在窗影全文展示。'};
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      assert.equal(url,'http://synthetic.invalid/api/window-shadows?limit=100&include_content=1');
      assert.equal(options.method,'GET');
      return {ok:true,status:200,json:async()=>({windows:[{window_id:'imported',title:'Imported shadow',created_at:'2026-09-27T01:00:00Z',content:'Full shadow',sections}]})};
    });
    const projection=await readLiveWindowShadows();
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      assert.equal(url,'/__serein/live/window-shadows');
      assert.deepEqual(JSON.parse(options.body),{});
      return {ok:true,json:async()=>projection};
    });
    assert.deepEqual(windowShadowViews((await loadWindowShadows())[0]),{user:sections.user_view,assistant:sections.self_view});
    assert.deepEqual(windowShadowViews({...projection.shadows[0],sections:{user_view:'修订后的你',self_view:''}}),{user:'修订后的你',assistant:''});
    assert.deepEqual(windowShadowViews(null),{user:'',assistant:''});
  } finally {
    for(const [key,value] of [['SEREIN_MEMORY_URL',previous.url],['SEREIN_MEMORY_TOKEN',previous.token]]) {
      if(value===undefined)delete process.env[key];else process.env[key]=value;
    }
  }
});

test('legacy Markdown portraits retain subheadings and exclude recent events',()=>{
  const text='# 窗影\n\n## 我眼中的你\n\n你的正文。\n### 新认识\n还在变化。\n```md\n## 这一窗发生的事\n这只是引用。\n```\n\n## 我眼中的自己\n\n我的正文。\n\n## 这一窗发生的事\n不展示在画像里。';
  const views=windowShadowViews({text,sections:{}});
  assert.match(views.user,/### 新认识\n还在变化/);
  assert.match(views.user,/这只是引用/);
  assert.equal(views.assistant,'我的正文。');
  assert.doesNotMatch(views.user+views.assistant,/不展示在画像/);
  assert.deepEqual(windowShadowViews({text,sections:{user_view:'',self_view:''}}),{user:'',assistant:''});
});

test('darkroom lock copy follows the configured AI name',()=>{
  const source=readFileSync(new URL('../src/data/diary.js',import.meta.url),'utf8');
  assert.match(source,/lockedTitle[\s\S]*identityName\("assistant"\)[\s\S]*锁了门/);
  assert.doesNotMatch(source,/Haven/);
});

test('observation distinguishes disabled recall, preparation, failure and successful injection',()=>{
  const prepared={observation_version:1,prepared_ids:['scene:a'],injected_bucket_ids:[],request_status:'upstream_pending'};
  assert.equal(resolveGatewayObservationOutcome(prepared,'injected'),'pending');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'failed'},'injected'),'failed');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'interrupted'},'injected'),'failed');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'completed',recall_state:'disabled'},'no_match'),'skip');
  assert.match(gatewayRequestLabel({...prepared,request_status:'completed',recall_state:'disabled'}),/已关闭.*未注入/);
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'completed',recall_state:'no_match'},'skip'),'no_match');
  assert.equal(resolveGatewayObservationOutcome({...prepared,request_status:'completed',injected_bucket_ids:['scene:a']},'skip'),'injected');
  assert.match(gatewayRequestLabel({...prepared,request_status:'completed',injected_bucket_ids:['scene:a']}),/注入 1 条记忆.*已完成/);
  assert.match(gatewayRequestLabel({...prepared,request_status:'completed',recall_state:'no_match'}),/没有选出/);
  assert.equal(resolveGatewayObservationOutcome({},'injected'),'injected');
  assert.equal(gatewayRequestLabel({}),'历史记录');
  assert.equal(resolveBridgeObservationOutcome({hookOutcome:'no_match'}),'no_match');
  assert.match(gatewayRequestLabel({...prepared,recall_diagnostics:{reranker_error:'http_401'}}),/认证失败（401）/);
  assert.match(gatewayRequestLabel({...prepared,recall_diagnostics:{reason:'hook_deadline_before_reranker'}}),/时间预算不足/);
});

test('missing configuration never calls a remote fallback',async()=>{
  let called=false;
  await assert.rejects(callSereinBackend('/health',{}, {env:{},fetchImpl:()=>{called=true;}}),/not_configured/);
  assert.equal(called,false);
});
test('backend credentials stay in server request headers',async()=>{
  const result=await callSereinBackend('/v1/memories/test',{}, {
    env:{SEREIN_MEMORY_URL:'http://localhost:9000',SEREIN_MEMORY_TOKEN:'synthetic'},
    fetchImpl:async(url,options)=>{
      assert.equal(url,'http://localhost:9000/v1/memories/test');
      assert.equal(options.headers.Authorization,'Bearer synthetic');
      return {ok:true,status:200,json:async()=>({id:'test'})};
    }});
  assert.deepEqual(result.payload,{id:'test'});
});
test('writer preserves update and rewrite material boundaries',()=>{
  const args={title:'Book club',currentBody:'Previous body',materials:{items:[]},roleRules:'Use original sources'};
  assert.match(buildNarrativeTaskPrompt({...args,mode:'update'}),/Previous body/);
  assert.doesNotMatch(buildNarrativeTaskPrompt({...args,mode:'rewrite'}),/Previous body/);
  const focusedPrompt = buildNarrativeTaskPrompt({...args,mode:'rewrite',writingFocus:'Book'.repeat(200)});
  const focused = JSON.parse(focusedPrompt.split('<narrative_writer_input_json>')[1].split('</narrative_writer_input_json>')[0]);
  assert.equal(focused.writing_focus.length,500);
});
test('writer preserves long source-bound narrative bodies',()=>{
  const roleRules=readFileSync(new URL('../codex_agents/narrative_writer/AGENTS.md',import.meta.url),'utf8');
  assert.match(roleRules,/Do not infer speech acts/);
  assert.doesNotMatch(roleRules,/no more than \d+ characters/);
  const schema=JSON.parse(readFileSync(new URL('../codex_agents/narrative_writer/output.schema.json',import.meta.url),'utf8'));
  assert.deepEqual(schema.properties.body,{type:'string'});
  const self_review={source_bound:true,final_supported_versions:true,no_correction_narration:true,material_relevance:true,
    no_new_inference:true,no_meta_explanation:true,no_forced_closure:true,dates_preserved:true,identity_correct:true};
  const body='The story continues.\n'.repeat(600)+'🌧️ A final paragraph.';
  assert.equal(normalizeNarrativeWriterResult({evidence_sufficient:true,body,issues:[],self_review}).body,body);
});
test('writer disabled means no runner starts',async()=>{
  delete process.env.SEREIN_WRITER_ENABLED;
  await assert.rejects(runNarrativeCodexTask({}, {backend:async()=>({ok:true,payload:{identity:{user_name:"User",ai_name:"AI"},upstream:{writer_enabled:false}}})}),/disabled/);
});
test('main-model authoring blocks both API and external automatic Writer',async()=>{
  const previous=process.env.SEREIN_WRITER_ENABLED;
  process.env.SEREIN_WRITER_ENABLED='1';
  try {
    for(const writer_enabled of [false,true])await assert.rejects(runNarrativeCodexTask({}, {
      backend:async()=>({ok:true,payload:{identity:{},features:{narrative_tools:true},upstream:{writer_enabled}}}),
    }),/disabled_main_model_authoring/);
  } finally {
    if(previous===undefined)delete process.env.SEREIN_WRITER_ENABLED;else process.env.SEREIN_WRITER_ENABLED=previous;
  }
});

test('writer renders the saved identities without rewriting original materials',()=>{
  const prompt=buildNarrativeTaskPrompt({mode:'rewrite',title:'Reading plan',materials:{source:'Alex said the meeting was Monday.'},
    roleRules:'Write as {ai_name}; the user is {user_name}.',identity:{user_name:'Nori',ai_name:'Atlas'}});
  assert.match(prompt,/Write as Atlas; the user is Nori/);
  assert.match(prompt,/Alex said the meeting was Monday/);
  assert.doesNotMatch(prompt,/\{ai_name\}|\{user_name\}/);
});
test('evidence retains original host identities, not archive row IDs',()=>{
  const [ref]=buildSceneEvidenceRefs([{id:1,source_system:'sample-client',source_message_id:'message-99',
    session_id:'session-a',role:'user',created_at:'2025-01-01T00:00:00Z',content:'Original text'}],
    [{messageId:1,evidenceKind:'primary'}]);
  assert.equal(ref.message_id,'message-99');
  assert.equal(ref.source_system,'sample-client');
  assert.equal(ref.content,'Original text');
});

test('configured Writer only sends bound text, without loading referenced images',async(t)=>{
  const review={source_bound:true,final_supported_versions:true,no_correction_narration:true,material_relevance:true,
    no_new_inference:true,no_meta_explanation:true,no_forced_closure:true,dates_preserved:true,identity_correct:true};
  const requests=[];
  const backend=async(path,options,config)=>{
    if(path==='/v1/settings')return {ok:true,payload:{identity:{user_name:'Nori',ai_name:'Atlas'},
      upstream:{writer_enabled:true},models:[{id:'writer',model:'synthetic-writer'}],assignments:{writer:'writer'}}};
    assert.equal(config.timeout,310_000);
    requests.push(options.body);
    return {ok:true,payload:{result:{evidence_sufficient:true,body:'The notice changed the room.',issues:[],self_review:review}}};
  };
  const args={mode:'rewrite',title:'Library notice',currentBody:'',writingFocus:'How the reading group grew',
    roleDir:fileURLToPath(new URL('../codex_agents/narrative_writer/',import.meta.url))};
  const result=await runNarrativeCodexTask({...args,materials:{content:'![notice](https://example.org/notice.png)'}},{backend});
  assert.equal(result.status,'ok');
  assert.equal(Object.hasOwn(requests[0],'image_inputs'),false);
  assert.doesNotMatch(requests[0].prompt,/"images"|必须固定阅读全部图片/);
  assert.match(requests[0].prompt,/Atlas/);
  assert.match(requests[0].prompt,/How the reading group grew/);
  const unavailable=await runNarrativeCodexTask({...args,materials:{content:'![notice](local-notice.png)'}},{backend});
  assert.equal(unavailable.status,'ok');
  assert.equal(requests.length,2);
  await assert.rejects(runNarrativeCodexTask({...args,materials:{}},{backend:async(path)=>
    path==='/v1/settings' ? backend(path) : {ok:false,status:504,payload:{detail:'Writer timed out'}}}),
    /narrative_writer_timeout/);

  const previous = Object.fromEntries(['SEREIN_WRITER_ENABLED','SEREIN_WRITER_MODEL','SEREIN_WRITER_COMMAND']
    .map(key => [key,process.env[key]]));
  try {
    process.env.SEREIN_WRITER_ENABLED='1';
    process.env.SEREIN_WRITER_MODEL='synthetic';
    process.env.SEREIN_WRITER_COMMAND=JSON.stringify([process.execPath,'-e','setInterval(() => {}, 1000)']);
    t.mock.timers.enable({apis:['setTimeout']});
    const run=runNarrativeCodexTask({...args,materials:{}},{backend:async()=>({ok:true,payload:{
      identity:{},upstream:{writer_enabled:false}}})});
    const timedOut=assert.rejects(run,/narrative_writer_timeout/);
    await new Promise(resolve=>setImmediate(resolve));
    t.mock.timers.tick(300_000);
    await timedOut;
  } finally {
    for(const [key,value] of Object.entries(previous)) {
      if(value===undefined)delete process.env[key];else process.env[key]=value;
    }
  }
});

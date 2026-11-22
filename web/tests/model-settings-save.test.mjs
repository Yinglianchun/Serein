import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,sep} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {build,stop} from 'esbuild';
import {JSDOM} from 'jsdom';

test('catalog saves tolerate incompatible pipeline fields and preserve pending configuration edits',async()=>{
  const dom=new JSDOM('<div id="root"></div>',{url:'https://synthetic.invalid'});
  const previous=new Map();
  for(const key of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','CustomEvent','Node']){
    previous.set(key,Object.getOwnPropertyDescriptor(globalThis,key));
    Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
  }
  const nativeFetch=globalThis.fetch,nativeChannel=globalThis.MessageChannel;
  const channels=[];
  globalThis.MessageChannel=class extends nativeChannel {constructor(){super();channels.push(this);}};
  globalThis.IS_REACT_ACT_ENVIRONMENT=true;
  const state={settings_version:4,identity:{user_name:'User',ai_name:'AI'},models:[],upstreams:[],assignments:{},features:{},
    upstream:{memory_enabled:false,writer_enabled:false,operit_enabled:false},recall:{},
    pipeline:{auto_enabled:false,execution_mode:'legacy',max_prompt_chars:200000,timeout_seconds:600,
      event_writer_concurrency:1,track_lookback_days:3,joint_review_enabled:false,material_review_enabled:false,
      round_gate_enabled:false,append_protected_enabled:false,future_read_only_field:'unknown'},
    dream:{daily_probability:.4,main_prompt:''},index_status:{status:'ready'},memory_ready:false};
  const clipboard = [], keyActions = [];
  Object.defineProperty(navigator, 'clipboard', {value:{writeText:async value=>clipboard.push(value)}});
  window.confirm = ()=>true;
  const saves=[];
  let rejectNext=false;
  globalThis.fetch=async(url,options={})=>{
    if(url==='/__serein/pipeline/status')return {ok:true,json:async()=>({pending_materials:[]})};
    if(url==='/__serein/settings/index-status')return {ok:true,json:async()=>structuredClone(state.index_status)};
    if (url.startsWith('/__serein/gateway-key/')) {
      assert.equal(options.method,'POST');
      keyActions.push(url.split('/').at(-1));
      return {ok:true,json:async()=>url.endsWith('/copy')?{key:'synthetic-clipboard-key'}:{status:'rotated'}};
    }
    assert.equal(url,'/__serein/settings');
    if(options.method==='PATCH'){
      const body=JSON.parse(options.body);saves.push(body);
      assert.equal(body.expected_version,state.settings_version);
      // Reproduce an older server that returns fields it cannot accept in PATCH.
      for(const key of Object.keys(body.pipeline||{}))assert(['execution_mode','max_prompt_chars','timeout_seconds','event_history_enabled'].includes(key));
      if(rejectNext){rejectNext=false;return {ok:false,status:409,json:async()=>({detail:'synthetic save conflict'})};}
      for(const key of ['upstreams','models'])if(key in body)state[key]=body[key];
      for(const key of ['assignments','recall','upstream','pipeline','dream'])Object.assign(state[key],body[key]);
      state.settings_version++;
    }
    return {ok:true,json:async()=>structuredClone(state)};
  };
  const directory=await mkdtemp(join(tmpdir(),'serein-model-save-'));
  let root,app;
  try{
    const output=await build({stdin:{contents:`
      import React,{act,useState} from 'react';import {createRoot} from 'react-dom/client';
      import {ModelSettings} from './src/components/ModelSettings.jsx';
      export {act};export let setPage,setThreshold;
      function Harness(){const [page,changePage]=useState('models'),[threshold,changeThreshold]=useState(null),
        [candidates,setCandidates]=useState({}),[passages,setPassages]=useState({});
        setPage=changePage;setThreshold=changeThreshold;
        return <ModelSettings page={page} recallThreshold={threshold} setRecallThreshold={changeThreshold}
          candidateThresholdDraft={candidates} setCandidateThresholdDraft={setCandidates}
          passageDraft={passages} setPassageDraft={setPassages}/>;}
      export function mount(){const root=createRoot(document.getElementById('root'));root.render(<Harness/>);return root;}
    `,resolveDir:fileURLToPath(new URL('..',import.meta.url)),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,
      define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
    const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
    app=await import(pathToFileURL(path));
    await app.act(async()=>{root=app.mount();});
    const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent===text);
    const field=text=>[...document.querySelectorAll('label')].find(node=>node.textContent.startsWith(text)).querySelector('input,select');
    const type=async(label,value)=>app.act(async()=>{
      const input=field(label);Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);
      input.dispatchEvent(new Event('input',{bubbles:true}));
    });
    const click=async(text)=>app.act(async()=>{button(text).click();});

    assert.deepEqual(keyActions,[]);
    await click('复制 Base URL');
    await click('复制 Gateway Key');
    await click('更换 Gateway Key');
    assert.deepEqual(clipboard,['https://synthetic.invalid/v1','synthetic-clipboard-key']);
    assert.deepEqual(keyActions,['copy','rotate']);
    assert.doesNotMatch(document.body.textContent,/synthetic-clipboard-key/);
    await click('添加上游');
    await type('上游名称','Synthetic');await type('API Base URL','https://synthetic.invalid/v1');
    await type('上游模型号','synthetic-model');await type('API Key','synthetic-key');
    // An unrelated invalid recall draft and a pending pipeline edit must not block catalog saving.
    await app.act(async()=>{app.setPage('summary');app.setThreshold('invalid');});
    await app.act(async()=>{const select=field('自动 Event 执行方式');select.value='agent';select.dispatchEvent(new Event('change',{bubbles:true}));});
    await app.act(async()=>{app.setPage('models');});
    await click('保存上游与模型');
    assert.equal(saves.length,1);
    assert.deepEqual(Object.keys(saves[0]).sort(),['expected_version','models','upstreams','assignments'].sort());
    assert.equal(state.upstreams[0].models[0].upstream_model,'synthetic-model');
    assert.equal(state.upstreams[0].api_key,'synthetic-key');
    assert.equal(state.pipeline.execution_mode,'legacy');assert.equal(state.pipeline.auto_enabled,false);
    assert.match(document.body.textContent,/上游与模型已保存/);
    assert.equal(field('自动 Event 执行方式').value,'agent');
    assert.equal(field('最终筛选阈值').value,''); // invalid draft remains unsaved

    await app.act(async()=>{app.setPage('summary');app.setThreshold(null);});
    rejectNext=true;
    await click('保存自动摘要设置');
    assert.equal(state.pipeline.execution_mode,'legacy');
    await click('保存自动摘要设置');
    assert.deepEqual(saves.at(-1).pipeline,{execution_mode:'agent'});
    assert.equal(state.pipeline.execution_mode,'agent');assert.equal(state.pipeline.auto_enabled,false);
    await click('保存自动摘要设置');
    assert.deepEqual(saves.at(-1).pipeline,{}); // successful save clears only the submitted draft
    await type('模型读取超时（秒）','900');
    await click('保存自动摘要设置');
    assert.deepEqual(saves.at(-1).pipeline,{timeout_seconds:900});
    assert.equal(state.pipeline.timeout_seconds,900);
    assert.equal([...document.querySelectorAll('label')].some(node=>node.textContent.startsWith('减小输入')),false);
    assert.match(document.querySelector('#settings-content-summary').textContent,/切分先读旧 Event 正文，需要时再补读原话/);
    assert.equal(document.querySelector('#settings-content-configuration').textContent.includes('自动 Event 执行方式'),false);
    assert.equal(field('回看旧 Event').checked,true);
    await app.act(async()=>{field('回看旧 Event').click();});
    await click('保存自动摘要设置');
    assert.deepEqual(saves.at(-1).pipeline,{event_history_enabled:false});
    assert.equal(state.pipeline.event_history_enabled,false);
    await app.act(async()=>{field('回看旧 Event').click();});
    await click('保存自动摘要设置');
    assert.deepEqual(saves.at(-1).pipeline,{event_history_enabled:true});
  }finally{
    if(root)await app.act(async()=>root.unmount());
    dom.window.close();delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    globalThis.fetch=nativeFetch;globalThis.MessageChannel=nativeChannel;
    for(const channel of channels){channel.port1.close();channel.port2.close();}
    for(const [key,descriptor] of previous){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}
    assert(resolve(directory).startsWith(resolve(tmpdir())+sep));
    await rm(directory,{recursive:true,force:true});await stop();
  }
});

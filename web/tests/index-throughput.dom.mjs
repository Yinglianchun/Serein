import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,sep} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','CustomEvent','Node'])
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const state={settings_version:0,identity:{user_name:'User',ai_name:'AI'},models:[],upstreams:[],assignments:{},features:{},
  upstream:{memory_enabled:false,writer_enabled:false,operit_enabled:false},
  recall:{direct_threshold:.65,body_candidate_threshold:.5,cue_candidate_threshold:.55,direct_pool_limit:20,
    passages_enabled:false,passage_min_chars:500,embedding_batch_size:16,embedding_concurrency:1,index_catch_up:false},
  pipeline:{auto_enabled:false,execution_mode:'agent',max_prompt_chars:200000,timeout_seconds:600,event_writer_concurrency:1,track_lookback_days:3},
  dream:{daily_probability:.4,main_prompt:''},index_status:{status:'ready',pending:0},memory_ready:false};
const saves=[];
globalThis.fetch=async(url,options={})=>{
  if(url==='/__serein/settings/index-status')return {ok:true,json:async()=>structuredClone(state.index_status)};
  assert.equal(url,'/__serein/settings');
  if(options.method==='PATCH'){
    const body=JSON.parse(options.body);saves.push(body);
    for(const key of ['recall','upstream','pipeline'])Object.assign(state[key],body[key]);
    state.settings_version++;
  }
  return {ok:true,json:async()=>structuredClone(state)};
};
const directory=await mkdtemp(join(tmpdir(),'serein-index-settings-dom-'));let root;
try{
  const output=await build({stdin:{contents:`import React,{act,useState} from 'react';import {createRoot} from 'react-dom/client';import {ModelSettings} from './src/components/ModelSettings.jsx';export {act};function Harness(){const [threshold,setThreshold]=useState(null),[candidates,setCandidates]=useState({}),[passages,setPassages]=useState({});return <ModelSettings page="configuration" recallThreshold={threshold} setRecallThreshold={setThreshold} candidateThresholdDraft={candidates} setCandidateThresholdDraft={setCandidates} passageDraft={passages} setPassageDraft={setPassages}/>;}export function mount(){const root=createRoot(document.getElementById('root'));root.render(<Harness/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
  const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
  const {act,mount}=await import(pathToFileURL(path));
  const wait=()=>new Promise(resolve=>setTimeout(resolve,30));
  const input=text=>[...document.querySelectorAll('label')].find(node=>node.textContent.startsWith(text)).querySelector('input');
  await act(async()=>{root=mount();await wait();});
  assert.equal(input('向量每批条数').value,'16');assert.equal(input('向量请求并发数').value,'1');
  assert.equal(input('加快后台索引追赶').checked,false);
  async function number(label,value){await act(async()=>{const field=input(label);Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(field,value);field.dispatchEvent(new Event('input',{bubbles:true}));});}
  await number('向量每批条数','32');await number('向量请求并发数','2');
  await act(async()=>input('加快后台索引追赶').click());
  await act(async()=>{[...document.querySelectorAll('button')].find(node=>node.textContent==='保存配置').click();await wait();});
  assert.equal(saves.length,1);
  assert.deepEqual(saves[0].recall,{embedding_batch_size:32,embedding_concurrency:2,index_catch_up:true});
  assert.equal(saves[0].upstream.memory_enabled,false);assert.equal(state.recall.passages_enabled,false);
  assert(!('pipeline' in saves[0]));assert.equal(state.pipeline.auto_enabled,false);
  const mode=[...document.querySelectorAll('label')].find(node=>node.textContent.startsWith('冷却方式')).querySelector('select');
  await act(async()=>{mode.value='hours';mode.dispatchEvent(new Event('change',{bubbles:true}));});
  await number('冷却小时数','6');
  await act(async()=>{[...document.querySelectorAll('button')].find(node=>node.textContent==='保存配置').click();await wait();});
  assert.deepEqual(saves.at(-1).recall,{cooldown_mode:'hours',cooldown_hours:6});
  await act(async()=>{root.unmount();root=mount();await wait();});
  assert.equal(input('冷却小时数').value,'6');
  assert.equal(input('向量每批条数').value,'32');assert.equal(input('向量请求并发数').value,'2');
  assert.equal(input('加快后台索引追赶').checked,true);
  await act(async()=>root.unmount());root=null;
  console.log('PASS index settings DOM: defaults, typed save, persisted options and independent memory/passage/summary switches');
}finally{
  if(root)root.unmount();dom.window.close();
  assert(resolve(directory).startsWith(resolve(tmpdir())+sep));
  await rm(directory,{recursive:true,force:true});
}
process.exit(0);

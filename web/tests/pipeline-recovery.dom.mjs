import assert from 'node:assert/strict';
import {test,after} from 'node:test';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,sep} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';

const webRoot=resolve(process.env.SEREIN_WEB_ROOT||process.cwd());
const require=createRequire(join(webRoot,'package.json'));
const {build}=require('esbuild'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','Event','Node'])
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const nativeChannel=globalThis.MessageChannel,channels=[];
globalThis.MessageChannel=class extends nativeChannel {constructor(){super();channels.push(this);}};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
dom.window.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
const interval=globalThis.setInterval,clear=globalThis.clearInterval;
const keepAlive=interval(()=>{},1000);
globalThis.setInterval=()=>1;globalThis.clearInterval=()=>{};
let state,root,recoveryBusy=false,unsplittable=false;
const requests=[];
globalThis.fetch=async(url,options={})=>{
  requests.push({url,body:options.body?JSON.parse(options.body):null});
  let result=state;
  if(url.endsWith('/recovery-preview')){
    const body=JSON.parse(options.body),limit=body.max_input_chars||5000;
    result={batch_id:body.batch_id,originals:8,original_chars:16000,first_at:'2025-01-01T00:00:00Z',last_at:'2025-01-01T00:07:00Z',
      max_input_chars:limit,batches:unsplittable?1:limit===8000?2:4,batch_chars:unsplittable?[16000]:[limit===8000?8000:4000],
      can_rebatch:!unsplittable,oversized_batches:unsplittable?1:0};
  }
  else if(url.endsWith('/skip-batch'))result=recoveryBusy?{status:'busy'}:{status:'skipped_batch',skipped_originals:8};
  else if(url.endsWith('/rebatch'))result={status:'rebatched',batches:2};
  else if(url.endsWith('/rebuild'))result={status:'rebuilt'};
  else if(url.endsWith('/retry-batch'))result={status:'resumed'};
  else if(url.endsWith('/next'))result={status:'queued',stage:'queued'};
  else assert.equal(url,'/__serein/pipeline/status');
  return {ok:true,json:async()=>structuredClone(result)};
};
const directory=await mkdtemp(join(tmpdir(),'serein-recovery-dom-'));
const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {PipelineSettings} from './src/components/PipelineSettings.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<PipelineSettings onOpenSummary={()=>{}}/>);return root;}`,resolveDir:webRoot,loader:'jsx'},
  jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"'},
  plugins:[{name:'synthetic-children',setup(build){
    build.onResolve({filter:/ConversationImport\.jsx$/},()=>({path:'imports',namespace:'synthetic'}));
    build.onResolve({filter:/instanceStore\.js$/},()=>({path:'settings',namespace:'synthetic'}));
    build.onLoad({filter:/.*/,namespace:'synthetic'},args=>({contents:args.path==='imports'?'export function ConversationImport(){return null;}':'export async function instanceSettings(){return {pipeline:{auto_enabled:true}};}',loader:'js'}));
  }}]});
const bundle=join(directory,'bundle.mjs');await writeFile(bundle,output.outputFiles[0].text);
const {act,mount}=await import(pathToFileURL(bundle));
const wait=()=>new Promise(resolve=>setTimeout(resolve,20));
const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent.trim()===text);
async function render(value){
  if(root)await act(async()=>{root.unmount();root=null;await wait();});
  state=value;requests.length=0;
  recoveryBusy=false;unsplittable=false;
  await act(async()=>{root=mount();await wait();});
}
async function click(node){assert(node,'button exists');assert(!node.disabled);await act(async()=>{node.click();await wait();});}
function held(kind='routing_evidence'){
  return {status:'needs_repair',stage:'needs_repair',result:{status:'needs_repair',batch_id:'batch-synthetic',repair_kind:kind,reason:'synthetic hold'},
    blocked_scopes:[{batch_id:'batch-synthetic',scope:'synthetic',hold_status:'needs_repair',repair_kind:kind,reason:'synthetic hold'}]};
}

test('runtime contract explains the hold and requires explicit rebuild confirmation',async()=>{
  await render(held('runtime_contract'));
  assert.match(document.body.textContent,/重新校验不会迁移旧计划/);
  assert(button('继续整理其他聊天'));assert(!button('重新校验并继续'));
  await click(button('查看重建确认'));
  assert(document.querySelector('#pipeline-rebuild-title').closest('dialog').open);
  assert.equal(requests.filter(item=>item.body).length,0);
  await click(button('确认作废旧计划并重新归线'));
  assert.deepEqual(requests.find(item=>item.url.endsWith('/rebuild')).body,{batch_id:'batch-synthetic',confirm:'REBUILD_PIPELINE_BATCH'});
});
test('runtime contract in a top-level result still permits other chats to continue',async()=>{
  const value=held('runtime_contract');delete value.blocked_scopes;
  await render(value);
  assert.match(document.body.textContent,/重新校验不会迁移旧计划/);
  await click(button('继续整理其他聊天'));
  assert(requests.some(item=>item.url.endsWith('/next')));
  assert(!requests.some(item=>item.url.endsWith('/rebuild')));
});
test('ordinary repair still revalidates without silently rebuilding',async()=>{
  await render(held());
  assert(!document.body.textContent.includes('重新校验不会迁移旧计划'));
  await click(button('重新校验并继续'));
  assert(requests.some(item=>item.url.endsWith('/next')));
  assert(!requests.some(item=>item.url.endsWith('/rebuild')));
});
test('paused failure retains its batch retry and continuation calls',async()=>{
  await render({status:'blocked',stage:'blocked',paused_batches:[{batch_id:'paused-synthetic',reason:'synthetic failure'}],
    blocked_scopes:[{batch_id:'paused-synthetic',scope:'synthetic',hold_status:'paused_failure'}]});
  assert(!button('查看重建确认'));
  await click(button('重试此批次'));
  assert.deepEqual(requests.find(item=>item.url.endsWith('/retry-batch')).body,{batch_id:'paused-synthetic'});
  assert(requests.some(item=>item.url.endsWith('/next')));
  assert(!requests.some(item=>item.url.endsWith('/rebuild')));
});
test('image history stays collapsed and a paused batch still has its own recovery action',async()=>{
  const images=['355e6438','70da2a73','674655d8','a30ec923'].map(sha256=>({sha256:sha256.repeat(8),failures:3}));
  await render({status:'completed',stage:'processed',failed_images:images});
  const history=button('重试这张图片').closest('details');
  assert.equal(history.open,false);
  assert.equal(history.querySelector('summary').textContent,'图片转录记录（4）');
  assert.equal(document.querySelectorAll('.import-error').length,0);
  assert(!button('重试此批次'));
  assert.equal(requests.filter(item=>item.body).length,0);
  await render({status:'blocked',stage:'blocked',failed_images:images,paused_batches:[
    {batch_id:'old-batch',reason:'每张输入图片必须恰有一份 image_transcriptions 转录'}]});
  assert.doesNotMatch(document.body.textContent,/image_transcriptions/);
  assert.match(button('重试此批次').closest('p').textContent,/本批已暂停.*转录条数/);
});
test('each runtime hold is explained when ordinary and runtime repairs coexist',async()=>{
  const value=held();value.blocked_scopes.push({batch_id:'runtime-second',scope:'other',hold_status:'needs_repair',repair_kind:'runtime_contract',reason:'synthetic old version'});
  await render(value);
  assert.match(document.body.textContent,/重新校验不会迁移旧计划/);
  assert(button('重新校验并继续'));
});

function paused(){return {status:'blocked',stage:'blocked',paused_batches:[{batch_id:'paused-synthetic',reason:'synthetic failure'}],
  blocked_scopes:[{batch_id:'paused-synthetic',scope:'synthetic',hold_status:'paused_failure'}]};}
test('skipping previews the exact scope, cancellation is harmless, and confirmation continues work',async()=>{
  await render(paused());
  await click(button('跳过本批，保留原话'));
  assert.match(document.querySelector('#pipeline-recovery-title').closest('dialog').textContent,/8 条未整理原话，共 16000 字符/);
  assert(!requests.some(item=>item.url.endsWith('/skip-batch')));
  await click([...document.querySelectorAll('dialog[open] button')].find(node=>node.textContent==='取消'));
  assert(!requests.some(item=>item.url.endsWith('/skip-batch')));
  await click(button('跳过本批，保留原话'));
  await click(button('确认跳过本批'));
  assert.deepEqual(requests.find(item=>item.url.endsWith('/skip-batch')).body,{batch_id:'paused-synthetic',confirm:'SKIP_PIPELINE_BATCH'});
  assert(requests.some(item=>item.url.endsWith('/next')));
  assert.match(document.body.textContent,/已跳过本批 8 条原话/);
});
test('rebatching requires a fresh preview after editing the scoped input limit',async()=>{
  await render(paused());
  await click(button('按更小材料重新整理'));
  const input=document.querySelector('#pipeline-recovery-title').closest('dialog').querySelector('input');
  await act(async()=>{
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(input,'8000');
    input.dispatchEvent(new Event('input',{bubbles:true}));await wait();
  });
  assert(button('确认拆小并整理').disabled);
  assert(!requests.some(item=>item.url.endsWith('/rebatch')));
  await click(button('查看拆分结果'));
  assert.match(document.body.textContent,/按完整问答拆成 2 批/);
  await click(button('确认拆小并整理'));
  assert.deepEqual(requests.find(item=>item.url.endsWith('/rebatch')).body,
    {batch_id:'paused-synthetic',confirm:'REBATCH_PIPELINE_BATCH',max_input_chars:8000});
  assert(requests.some(item=>item.url.endsWith('/next')));
});
test('one oversized complete exchange is explained and cannot be submitted as smaller batches',async()=>{
  await render(paused());unsplittable=true;
  await click(button('按更小材料重新整理'));
  assert(button('确认拆小并整理').disabled);
  assert.match(document.body.textContent,/包含超长完整问答/);
  assert(!requests.some(item=>item.url.endsWith('/rebatch')));
});
test('a busy recovery leaves confirmation open without starting another task',async()=>{
  await render(paused());recoveryBusy=true;
  await click(button('跳过本批，保留原话'));
  await click(button('确认跳过本批'));
  assert(document.querySelector('#pipeline-recovery-title').closest('dialog').open);
  assert.match(document.body.textContent,/正在运行/);
  assert(!requests.some(item=>item.url.endsWith('/next')));
});
after(async()=>{
  if(root)await act(async()=>root.unmount());
  dom.window.close();globalThis.setInterval=interval;globalThis.clearInterval=clear;
  clear(keepAlive);require('esbuild').stop();
  for(const channel of channels){channel.port1.close();channel.port2.close();}
  globalThis.MessageChannel=nativeChannel;
  const target=resolve(directory),parent=resolve(tmpdir());
  assert(target.startsWith(parent+sep)&&target!==parent);
  await rm(target,{recursive:true,force:true});
});

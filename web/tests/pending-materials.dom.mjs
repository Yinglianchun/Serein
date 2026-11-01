import assert from 'node:assert/strict';
import {test,after} from 'node:test';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
const webRoot=resolve(process.cwd()),require=createRequire(join(webRoot,'package.json'));
const {build,stop}=require('esbuild'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'https://synthetic.invalid'});
for(const key of ['window','document','navigator','HTMLElement','Event','Node'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const channels=[],NativeChannel=globalThis.MessageChannel;
globalThis.MessageChannel=class extends NativeChannel{constructor(){super();channels.push(this);}};
const directory=await mkdtemp(join(tmpdir(),'serein-pending-dom-'));
const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {PendingMaterials} from './src/components/PendingMaterials.jsx';export {act};export function mount(work){const root=createRoot(document.getElementById('root'));root.render(<PendingMaterials work={work}/>);return root;}`,resolveDir:webRoot,loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"'}});
const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
const {act,mount}=await import(pathToFileURL(path));let root,busy=false,skipped=false;const requests=[];
const item={id:17,batch_id:'synthetic',status:'open',kind:'writing',reason:'Synthetic writer failure',summary:'Book club',originals:2,original_chars:100,can_skip:true,
  locator:{source:'synthetic',session_id:'book-chat',first_at:'2025-01-01T00:00:00Z',last_at:'2025-01-01T00:01:00Z',first_raw_id:101,last_raw_id:102,preview:'FIRST_SNIPPET',longest_message:{id:102,role:'assistant',chars:2000}},
  resolved_event_ids:[],last_attempt:{role:'event_writer:0:0',error:'Synthetic error',output_chars:20}};
globalThis.fetch=async(url,options={})=>{
  requests.push({url,body:options.body?JSON.parse(options.body):null});let result;
  if(url.endsWith('/pending/17'))result={id:17,batch_id:'synthetic',messages:[{id:101,role:'user',content:'SYNTHETIC_ORIGINAL'},
    {id:102,role:'assistant',content:'L'.repeat(2000)}],last_attempt:{output_text:'SYNTHETIC_BAD_OUTPUT'}};
  else if(url.endsWith('/resume-pending'))result={status:busy?'busy':'resumed'};
  else if(url.endsWith('/next'))result={status:'queued'};
  else if(url.endsWith('/status'))result={pending_materials:[{...item,status:skipped?'resolved_skipped':'resolved',resolved_event_ids:skipped?[]:['event-synthetic']}]};
  else if(url.endsWith('/recovery-preview'))result={pending_id:17,batch_id:'synthetic',originals:2,original_chars:100,max_input_chars:5000,batches:2,oversized_batches:0,can_rebatch:true};
  else if(url.endsWith('/rebatch'))result={status:'rebatched'};
  else if(url.endsWith('/skip-batch')){skipped=true;result={status:'skipped_batch',skipped_originals:2};}
  else throw new Error('Unexpected request '+url);
  return {ok:true,json:async()=>structuredClone(result)};
};
const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent===text);
async function render(status){if(root)await act(async()=>root.unmount());requests.length=0;skipped=false;await act(async()=>{root=mount({status,pending_materials:[item]});});}
async function click(text){await act(async()=>{button(text).click();});}
after(async()=>{if(root)await act(async()=>root.unmount());dom.window.close();stop();for(const c of channels){c.port1.close();c.port2.close();}globalThis.MessageChannel=NativeChannel;await rm(directory,{recursive:true,force:true});});
test('pending summaries do not fetch originals until explicitly opened',async()=>{
  await render();assert.match(document.body.textContent,/已切分待补写/);assert(!requests.length);
  assert.match(document.body.textContent,/book-chat/);assert.match(document.body.textContent,/raw:101/);assert.match(document.body.textContent,/FIRST_SNIPPET/);
  assert(!document.body.textContent.includes('SYNTHETIC_ORIGINAL'));
  await click('查看原话与最后输出');assert.match(document.body.textContent,/SYNTHETIC_BAD_OUTPUT/);assert.match(document.body.textContent,/SYNTHETIC_ORIGINAL/);
  assert.equal(document.querySelectorAll('.pending-originals article').length,2);
  assert.equal(document.querySelector('.pending-originals article pre').textContent,'SYNTHETIC_ORIGINAL');
});
test('continuing a pending item sends its id and starts work only after resume',async()=>{
  await render();await click('从失败步骤继续');assert.deepEqual(requests[0].body,{pending_id:17,replan:false});assert(requests.some(r=>r.url.endsWith('/next')));assert.match(document.body.textContent,/event-synthetic/);
});
test('a busy pipeline does not start another task',async()=>{
  await render();busy=true;await click('从失败步骤继续');busy=false;assert(!requests.some(r=>r.url.endsWith('/next')));assert.match(document.body.textContent,/整理正在运行/);
});
test('smaller batches require preview and an explicit confirmation',async()=>{
  await render();await click('按更小材料重新整理');assert(!requests.some(r=>r.url.endsWith('/rebatch')));await click('确认拆小并整理');assert.deepEqual(requests.find(r=>r.url.endsWith('/rebatch')).body,{batch_id:'synthetic',max_input_chars:5000,confirm:'REBATCH_PIPELINE_BATCH'});
});

test('locating the longest original opens its complete text and focuses the correct message',async()=>{
  await render();await click('定位这条原话');
  assert.equal(document.activeElement.dataset.rawId,'102');
  const message=document.querySelector('[data-raw-id="102"]');
  assert.equal(message.querySelector('details').open,true);
  assert.equal(message.querySelector('pre').textContent.length,2000);
});

test('skipping is limited to the selected batch and requires a preview plus confirmation',async()=>{
  await render();await click('跳过本批，保留原话');
  assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  await click('取消');assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  await click('跳过本批，保留原话');await click('确认仅跳过本批');
  assert.deepEqual(requests.find(r=>r.url.endsWith('/skip-batch')).body,{batch_id:'synthetic',confirm:'SKIP_PIPELINE_BATCH'});
  assert.match(document.body.textContent,/已手动跳过，原话保留/);
});

test('a running task still permits material inspection while preventing competing recovery actions',async()=>{
  await render('running');
  for(const label of ['从失败步骤继续','重新切分，保留归线','按更小材料重新整理','跳过本批，保留原话'])assert.equal(button(label).disabled,true);
  await click('查看原话与最后输出');assert.equal(requests.length,1);
});

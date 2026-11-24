import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve,sep} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','Event','Node'])
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
let poll,root,historyReads=0;
const interval=globalThis.setInterval,clear=globalThis.clearInterval;
globalThis.setInterval=fn=>{poll=fn;return 1;};globalThis.clearInterval=()=>{};
const job={id:'synthetic-upload',filename:'Synthetic history.json',format:'messages',sessions:1,total:1000,
  status:'ready',processed:0,inserted:0,duplicate:0,failed:0,errors:[],warnings:[],preview:[],event_boundary_active:true};
const requests=[];
globalThis.fetch=async(url,options={})=>{
  let data;
  if(url==='/__serein/imports'){historyReads++;data={items:[job],tagging:{}};}
  else if(url.endsWith('/continue')){
    const args=JSON.parse(options.body);requests.push(args);
    job.task={status:'queued',arguments:args};data=job.task;
  }else if(url.endsWith('/pause')){
    job.task.status='paused';data=job.task;
  }else throw new Error('Unexpected request '+url);
  return {ok:true,json:async()=>structuredClone(data)};
};
const directory=await mkdtemp(join(tmpdir(),'serein-import-dom-'));
try{
  const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {ConversationImport} from './src/components/ConversationImport.jsx';export {act};export function show(root,active){root.render(<ConversationImport active={active}/>);}export function mount(active=true){const root=createRoot(document.getElementById('root'));show(root,active);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"'}});
  const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
  const {act,mount,show}=await import(pathToFileURL(path));
  const wait=()=>new Promise(resolve=>setTimeout(resolve,20));
  const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent===text);
  const toggle=()=>[...document.querySelectorAll('label')].find(node=>node.textContent.includes('大批量导入')).querySelector('input');
  await act(async()=>{root=mount(false);await wait();});
  assert.equal(historyReads,0,'Inactive settings tab does not read import history');
  assert.equal(poll,undefined);
  await act(async()=>{show(root,true);await wait();});
  assert.equal(historyReads,1);
  Object.defineProperty(document,'hidden',{value:true,configurable:true});
  await act(async()=>{await poll();});
  assert.equal(historyReads,1,'Hidden browser tab skips scheduled reads');
  delete document.hidden;
  assert.equal(toggle().checked,false,'Large batches are opt-in');
  await act(async()=>{toggle().click();});
  await act(async()=>{button('开始导入').click();await wait();});
  assert.deepEqual(requests,[{large_batch:true}]);
  assert(toggle().disabled);assert(button('开始导入').disabled);
  job.task={...job.task,status:'running',entries_per_second:25,remaining:925,eta_seconds:37};
  Object.assign(job,{processed:75,inserted:75,status:'paused'});
  await act(async()=>{await poll();});
  assert.match(document.body.textContent,/剩余 925 条.*预计还需/);
  await act(async()=>{button('暂停').click();await wait();});
  assert.equal(toggle().disabled,false);assert.equal(toggle().checked,true);
  assert.doesNotMatch(document.body.textContent,/预计还需/);
  await act(async()=>{root.unmount();root=mount();await wait();});
  assert.equal(toggle().checked,true,'Reload keeps the saved import choice');
  console.log('PASS import DOM: explicit opt-in, request body, running controls, approximate ETA, pause and reload');
  await act(async()=>root.unmount());root=null;
}finally{
  if(root)root.unmount();dom.window.close();
  globalThis.setInterval=interval;globalThis.clearInterval=clear;
  assert(resolve(directory).startsWith(resolve(tmpdir())+sep));
  await rm(directory,{recursive:true,force:true});
}
process.exit(0);

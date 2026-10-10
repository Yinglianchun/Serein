import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<!doctype html><div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','CustomEvent','Node'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const holds=[{scope:'chat-held',batch_id:'route:repair',hold_status:'needs_repair',reason:'frozen range cannot be proved'},
 {scope:'chat-backoff',batch_id:'route:wait',hold_status:'retry_wait',reason:'synthetic network failure',next_retry_at:'2026-10-10T00:45:00Z'}];
const mutations=[];
globalThis.fetch=async(url,options={})=>{
 let data;
 if(url==='/__serein/settings')data={identity:{user_name:'User',ai_name:'AI'},features:{},pipeline:{auto_enabled:true}};
 else if(url==='/__serein/imports')data={items:[],tagging:{}};
 else if(url==='/__serein/pipeline/status')data={status:'blocked',stage:'blocked',blocked_scopes:holds,paused_batches:[],result:{status:'blocked'}};
 else if(url==='/__serein/pipeline/rebuild'){mutations.push(JSON.parse(options.body));data={status:'rebuilt'};}
 else if(url==='/__serein/pipeline/next')data={status:'queued'};
 else throw Error('Unexpected request '+url);
 return {ok:true,json:async()=>data};
};
const directory=await mkdtemp(join(tmpdir(),'serein-pipeline-dom-'));let root;
try{
 const bundle=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {PipelineSettings} from './src/components/PipelineSettings.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<PipelineSettings/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
 const path=join(directory,'bundle.mjs');await writeFile(path,bundle.outputFiles[0].text);
 const {act,mount}=await import(pathToFileURL(path));
 const wait=()=>new Promise(resolve=>setTimeout(resolve,30));
 await act(async()=>{root=mount();await wait();});
 assert.match(document.body.textContent,/chat-held/);assert.match(document.body.textContent,/chat-backoff/);
 assert.match(document.body.textContent,/下次重试/);assert.match(document.body.textContent,/其他聊天可继续/);
 assert.match(document.body.textContent,/当日尚未全部结算/);assert.equal(mutations.length,0);
 assert.match(document.body.textContent,/route:repair/);
 const buttons=[...document.querySelectorAll('button')];
 await act(async()=>{buttons.find(b=>b.textContent==='查看重建确认').click();await wait();});
 assert.equal(document.querySelector('dialog[open] code').textContent,'route:repair');
 assert.equal(mutations.length,0,'opening confirmation never rebuilds');
 await act(async()=>{[...document.querySelectorAll('dialog[open] button')].find(b=>b.textContent.startsWith('确认')).click();await wait();});
 assert.deepEqual(mutations,[{batch_id:'route:repair',confirm:'REBUILD_PIPELINE_BATCH'}]);
 await act(async()=>root.unmount());root=null;
 console.log('PASS scope holds, visible retry time, truthful incomplete-day copy and targeted explicit rebuild confirmation');
}finally{if(root)root.unmount();dom.window.close();await rm(directory,{recursive:true,force:true});}
// Bundled UI imports retain a GSAP ticker, as in the existing standalone DOM suites.
process.exit(0);

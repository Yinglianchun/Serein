import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
const {JSDOM}=await import(process.env.JSDOM_MODULE||'jsdom');
const dom=new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','CustomEvent','Node'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;globalThis.fetch=(...args)=>window.fetch(...args);
const directory=await mkdtemp(join(tmpdir(),'serein-mailbox-dom-'));let root;
try{
 const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {fixture} from './tests/event-mailbox-fixture.js';import {EventMailboxPage} from './src/pages/EventMailboxPage.jsx';export {act,fixture};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<EventMailboxPage onOpenSettings={()=>{}}/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
 const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);const {act,fixture,mount}=await import(pathToFileURL(path));
 const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 async function click(node,delay=200){assert(node,'Element exists');assert(!node.disabled,'Enabled');await act(async()=>{node.click();await wait(delay);});}
 const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent.trim()===text||node.getAttribute('aria-label')===text);
 const tab=text=>[...document.querySelectorAll('.mailbox-tabs button')].find(node=>node.textContent.startsWith(text));
 const row=id=>[...document.querySelectorAll('.mailbox-card-heading')].find(node=>node.textContent.includes(id));
 const checkbox=label=>document.querySelector(`input[aria-label="${label}"]`);
 await act(async()=>{root=mount();await wait(30);});
 assert.equal(fixture.mutations,0);assert(!document.querySelector('textarea'));assert(!document.querySelector('.mailbox-card-content'));assert.match(document.body.textContent,/3 件事/);
 await click(row('event_synthetic'));assert.match(document.querySelector('.mailbox-card-content').textContent,/Last paragraph/);assert(!document.querySelector('.mailbox-evidence-list'));
 await click(document.querySelector('.mailbox-evidence-toggle'));assert.match(document.querySelector('.mailbox-evidence-list').textContent,/Exact original evidence/);assert(!button('升为 Scene'));assert(!button('保存草稿'));
 fixture.badDecisionReplyOnce=true;await click(button('想留下'));assert.match(document.querySelector('[role=alert]').textContent,/尚未确认决定/);assert.equal(fixture.rows.get('event_synthetic').status,'pending');
 const choose=button('想留下');await act(async()=>{choose.click();choose.click();await wait(250);});assert.equal(fixture.selectCalls,1);assert.equal(fixture.rows.get('event_synthetic').status,'approved');assert.equal(fixture.promoteCalls,0);
 await click(tab('已留下'));await click(row('event_synthetic'));assert.match(document.body.textContent,/等 AI 亲手写下/);await click(button('重新决定'));assert.equal(fixture.rows.get('event_synthetic').status,'pending');
 await click(tab('待决定'));await click(button('继续读取'));await click(button('继续读取'));assert.equal(document.querySelectorAll('.mailbox-card').length,3);
 await click(button('批量操作'));await click(checkbox('全选已加载候选'));assert.match(document.querySelector('.mailbox-batch').textContent,/已选 3 件/);
 fixture.failedIds.add('event_second');await click(button('批量留下'),500);assert.equal(fixture.rows.get('event_synthetic').status,'approved');assert.equal(fixture.rows.get('event_third').status,'approved');assert.equal(fixture.rows.get('event_second').status,'pending');assert.equal(fixture.promoteCalls,0);assert.match(document.querySelector('[role=alert]').textContent,/1 件未完成/);assert(checkbox('选择 event_second').checked);
 fixture.failedIds.clear();await click(button('批量不留'));assert.equal(fixture.rows.get('event_second').status,'removed');assert.equal(fixture.rows.get('event_second').event.document.body_md,'Full original Event\n\nLast paragraph');
 await click(tab('未保留'));await click(row('event_second'));await click(button('重新决定'));await click(tab('待决定'));await click(row('event_second'));
 fixture.loseDecisionReplyOnce=true;await click(button('想留下'));assert.match(document.querySelector('[role=alert]').textContent,/response lost/);const first=fixture.requests.at(-1);await click(button('想留下'));assert.equal(fixture.requests.at(-1).operation_id,first.operation_id);assert.equal(fixture.rows.get('event_second').status,'approved');
 await click(tab('已留下'));await click(button('继续读取'));await click(button('继续读取'));fixture.rows.get('event_third').status='completed';fixture.rows.get('event_third').scene_id='scene_fixture';await click(button('刷新信箱'));await click(button('继续读取'));await click(button('继续读取'));await click(row('event_third'));assert.match(document.body.textContent,/已经写成 Scene/);assert(!button('重新决定'));
 fixture.enabled=false;const before=fixture.mutations;await click(button('刷新信箱'));assert.match(document.body.textContent,/尚未开启/);assert(button('批量操作').disabled);assert.equal(fixture.mutations,before);
 console.log('PASS decision-only inbox: collapsed cards, evidence, no editor/promotion, single and batch decisions, partial failure, exact retry, restore, completed and disabled states');
 await act(async()=>root.unmount());root=null;
}finally{if(root)root.unmount();dom.window.close();await rm(directory,{recursive:true,force:true});}
process.exit(0);

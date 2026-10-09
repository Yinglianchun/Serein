// Actual React DOM interaction in an isolated jsdom. Optional JSDOM_MODULE may
// point to an existing official install. No real backend or network is used.
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
const {JSDOM}=await import(process.env.JSDOM_MODULE||'jsdom');
const dom=new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','HTMLInputElement','HTMLTextAreaElement','Event','CustomEvent','Node'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
window.confirm=()=>true;
globalThis.fetch=(...args)=>window.fetch(...args);
const directory=await mkdtemp(join(tmpdir(),'serein-mailbox-dom-'));
let root;
try {
 const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {fixture} from './tests/event-mailbox-fixture.js';import {EventMailboxPage} from './src/pages/EventMailboxPage.jsx';import {EventMailboxSelect} from './src/components/EventMailboxSelect.jsx';export {act,fixture};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<><EventMailboxSelect eventId="event_synthetic"/><EventMailboxPage onOpenMemory={()=>{}} onOpenSettings={()=>{}}/></>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
 const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
 const {act,fixture,mount}=await import(pathToFileURL(path));
 const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 async function flush(){await act(async()=>{await wait(10);});}
 async function click(element){assert(element,'Element exists');assert(!element.disabled,'Action enabled');await act(async()=>{element.click();await wait(20);});}
 const buttons=(scope=document)=>[...scope.querySelectorAll('button')];
 const button=(text,scope=document)=>buttons(scope).find(node=>node.textContent.trim()===text||node.getAttribute('aria-label')===text);
 const row=id=>document.querySelector(`.mailbox-list button[aria-pressed] span`)&&buttons(document.querySelector('.mailbox-list')).find(node=>node.textContent.includes(id));
 const detail=()=>document.querySelector('.mailbox-detail');
 const input=label=>[...detail().querySelectorAll('label')].find(node=>node.firstChild?.textContent===label)?.querySelector('input,textarea');
 async function fill(element,value){assert(element);await act(async()=>{Object.getOwnPropertyDescriptor(element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(element,value);element.dispatchEvent(new Event('input',{bubbles:true}));element.dispatchEvent(new Event('change',{bubbles:true}));});}
 await act(async()=>{root=mount();await wait(20);});
 assert.equal(fixture.mutations,0,'Viewing never selects or writes');
 const select=button('加入信箱');await act(async()=>{select.click();select.click();await wait(200);});assert.equal(fixture.selectCalls,1,'Double selection is locked');
 await click(button('刷新信箱'));await click(button('继续读取'));await click(button('继续读取'));assert.equal(document.querySelectorAll('.mailbox-list li').length,3,'Cursor pagination appends explicit selections');
 await click(row('event_synthetic'));assert.match(detail().textContent,/Exact original evidence, never rewritten/);assert.match(detail().textContent,/Last paragraph/);
 await fill(input('标题'),'Edited title');await fill(input('正文'),'Edited Scene body');await click(button('增加一条',detail()));await fill(detail().querySelector('[aria-label="召回入口 1"]'),'When we talk about rain');
 let confirmations=0;window.confirm=()=>{confirmations++;return false;};await click(row('event_second'));assert.equal(input('标题').value,'Edited title');assert.equal(confirmations,1,'Dirty row switching asks before discarding');
 const navigation=new Event('serein:before-navigation',{cancelable:true});window.dispatchEvent(navigation);assert(navigation.defaultPrevented,'External navigation honors the same dirty guard');
 await click(button('保存草稿',detail()));assert.deepEqual(fixture.rows.get('event_synthetic').draft,{title:'Edited title',body_md:'Edited Scene body',cues:['When we talk about rain']});assert.match(detail().textContent,/草稿已保存/);
 fixture.failPromotion=true;await click(button('升为 Scene',detail()));assert.match(detail().textContent,/Synthetic promotion failed/);assert.equal(fixture.rows.get('event_synthetic').status,'pending');assert.equal(input('标题').value,'Edited title');
 fixture.failPromotion=false;const promote=button('升为 Scene',detail());await act(async()=>{promote.click();promote.click();await wait(30);});assert.equal(fixture.promoteCalls,2,'Failed call plus one successful double-click action');assert.equal(fixture.rows.get('event_synthetic').status,'completed');assert.match(detail().textContent,/已升为 Scene/);
 await click(button('关闭信箱详情',detail()));
 const status=document.querySelector('[aria-label="信箱状态"]');await act(async()=>{status.value='completed';status.dispatchEvent(new Event('change',{bubbles:true}));await wait(20);});await click(row('event_synthetic'));assert.match(detail().textContent,/关联 Scene/);assert(!button('升为 Scene',detail()));
 await click(button('关闭信箱详情',detail()));await act(async()=>{status.value='pending';status.dispatchEvent(new Event('change',{bubbles:true}));await wait(20);});await click(row('event_second'));await fill(input('标题'),'Unsaved retained');
 fixture.conflictDraft=true;await click(button('保存草稿',detail()));assert.match(detail().textContent,/已有新版本/);assert.equal(input('标题').value,'Unsaved retained');assert(button('升为 Scene',detail()).disabled);
 fixture.conflictDraft=false;window.confirm=()=>true;await click(button('重新读取',detail()));await click(button('移出信箱',detail()));assert.equal(fixture.rows.get('event_second').status,'removed');assert.equal(fixture.rows.get('event_second').event.document.body_md,'Full original Event\n\nLast paragraph');
 await click(button('关闭信箱详情',detail()));fixture.rows.get('event_third').draft_stale=true;await click(row('event_third'));assert(button('升为 Scene',detail()).disabled);assert.match(detail().textContent,/草稿仍基于旧版本/);await click(button('保存草稿',detail()));assert.equal(fixture.rows.get('event_third').draft_stale,false);
 // Dirty promotion first saves the exact cleared cues, never resurrecting saved ones.
 await click(button('增加一条',detail()));await fill(detail().querySelector('[aria-label="召回入口 1"]'),'Saved cue');await click(button('保存草稿',detail()));await click(button('删除召回入口 1',detail()));fixture.failPromotion=true;await click(button('升为 Scene',detail()));assert.deepEqual(fixture.rows.get('event_third').draft.cues,[]);assert.equal(fixture.rows.get('event_third').status,'pending');
 // A lost draft-save response retries the same operation instead of colliding with its own new revision.
 fixture.failPromotion=false;fixture.loseDraftReplyOnce=true;await fill(input('标题'),'Retry-safe title');await click(button('升为 Scene',detail()));assert.match(detail().textContent,/response lost after draft commit/);assert.equal(input('标题').value,'Retry-safe title');const firstSave=fixture.requests.at(-1);await click(button('升为 Scene',detail()));const retriedSave=fixture.requests.slice(-2)[0];assert.equal(firstSave.operation_id,retriedSave.operation_id);assert.equal(fixture.rows.get('event_third').status,'completed');
 await click(button('关闭信箱详情',detail()));fixture.rows.get('event_second').status='pending';await click(button('刷新信箱'));await click(row('event_second'));
 fixture.enabled=false;const before=fixture.mutations;await click(button('刷新信箱'));assert.match(document.body.textContent,/尚未开启/);assert(button('升为 Scene',detail()).disabled);assert(button('移出信箱',detail()).disabled);assert(!button('加入信箱'));assert.equal(fixture.mutations,before);
 await flush();console.log('PASS actual React DOM: explicit selection, cursor paging, full Event/evidence, cue editing, dirty navigation, save, failure retention, repeated clicks, completed view, revision conflict, withdrawal, stale drafts, clear-cue promotion, lost-response idempotent retry, disabled opt-in');
 await act(async()=>root.unmount());root=null;
}finally{if(root)root.unmount();dom.window.close();await rm(directory,{recursive:true,force:true});}

// React scheduler may retain a MessagePort in Node after jsdom is closed.
process.exit(0);

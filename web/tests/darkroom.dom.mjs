// Actual room interaction: synthetic snapshots, controlled clock, no backend.
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','Event','CustomEvent','Node'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLElement.prototype.scrollTo=()=>{};
window.localStorage.setItem('serein.diary.entries.v1',JSON.stringify([{id:'diary-vps-1',date:'2026-10-10',time:'09:00',title:'给两年后的你',excerpt:'SECRET mirror',body:['SECRET mirror'],darkroom:false,locked:false,unlockAt:'2020-01-01',revision:99}]));
let now=Date.parse('2026-10-09T09:00:00+08:00'),released=false,allLocked=false,empty=false,requests=0;
const originalNow=Date.now;Date.now=()=>now;
const timers=new Map();let nextTimer=0;
window.setInterval=(fn,ms)=>{const key=++nextTimer;timers.set(key,{fn,ms});return key;};
window.clearInterval=key=>timers.delete(key);
globalThis.fetch=async url=>{
 assert.equal(url,'/__serein/live/diaries');requests++;
 const entries=empty?[]:[
  {id:1,date:'2026-10-10',title:'给两年后的你',entry_type:'darkroom',author:'ai',created_at:'2026-10-10T09:00:00+08:00',unlock_at:'2028-10-09T09:00:00+08:00',locked:true,content:'SECRET capsule body'},
  {id:2,date:'2026-10-08',title:'已经开启的信',entry_type:'darkroom',author:'ai',created_at:'2026-10-08T09:00:00+08:00',unlock_at:allLocked?'2028-10-09T09:00:00+08:00':'2026-10-08T10:00:00+08:00',locked:allLocked,content:'Readable letter body'},
  {id:3,date:'2026-10-09',title:'快到时间的信',entry_type:'darkroom',author:'ai',created_at:'2026-10-09T08:00:00+08:00',unlock_at:'2026-10-09T09:00:01+08:00',locked:!released,content:released?'Freshly opened letter':'SECRET not-yet body'},
 ];
 return {ok:true,json:async()=>({status:'ok',snapshotId:String(requests),entries})};
};
const directory=await mkdtemp(join(tmpdir(),'serein-darkroom-dom-'));let root;
try {
 const bundle=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {DiaryPage} from './src/pages/DiaryPage.jsx';export {act};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<DiaryPage/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
 const path=join(directory,'bundle.mjs');await writeFile(path,bundle.outputFiles[0].text);
 const {act,mount}=await import(pathToFileURL(path));
 const flush=()=>new Promise(resolve=>setTimeout(resolve,15));
 const button=(text,scope=document)=>[...scope.querySelectorAll('button')].find(item=>item.textContent.includes(text));
 const click=async item=>{assert(item);await act(async()=>{item.click();await flush();});};
 await act(async()=>{root=mount();await flush();});
 assert.doesNotMatch(document.body.textContent,/SECRET/,'An older server snapshot still owns the lock over a newer local mirror');
 await click(button('进入暗房'));
 const room=()=>document.querySelector('.darkroom-open-room');
 assert(room(),'Room opens despite a two-year capsule');
 assert.match(room().textContent,/Readable letter body/);
 assert.doesNotMatch(room().textContent,/SECRET/);
 await click(button('给两年后的你',room()));
 assert.match(room().textContent,/2028年/);
 assert.match(room().querySelector('[role="status"]').textContent,/还有 \d+ 天/);
 assert.doesNotMatch(room().textContent,/SECRET|Readable letter body/);
 await click(button('已经开启的信',room()));assert.match(room().textContent,/Readable letter body/);
 await click(button('快到时间的信',room()));
 const beforeExpiry=requests;now+=2000;
 await act(async()=>{for(const timer of [...timers.values()])if(timer.ms===1000)timer.fn();await flush();});
 assert(requests>beforeExpiry,'One deadline refreshes while another letter remains locked');
 assert(room().querySelector('[role="status"]'),'Local expiry must wait for server unlock');
 assert.doesNotMatch(room().textContent,/SECRET|Freshly opened letter/);
 released=true;await act(async()=>{window.dispatchEvent(new Event('focus'));await flush();});
 assert.match(room().textContent,/Freshly opened letter/);assert(!room().querySelector('[role="status"]'));
 assert.match(room().querySelector('aside').textContent,/尚未开启/);
 await click(button('回到日记',room()));allLocked=true;released=false;await click(button('进入暗房'));
 assert(room()&&room().querySelectorAll('aside button').length===3);
 assert.doesNotMatch(room().textContent,/Readable letter body|SECRET/);
 empty=true;await act(async()=>{window.dispatchEvent(new Event('focus'));await flush();});
 assert.match(room().textContent,/门后还没有日记/);assert.equal(room().querySelectorAll('aside button').length,0);
 await act(async()=>root.unmount());root=null;assert.equal(timers.size,0);
 console.log('PASS darkroom DOM: independent locks, two-year capsule, readable selection, expiry refresh, server authority, all-locked and empty room');
} finally {if(root)root.unmount();Date.now=originalNow;dom.window.close();await rm(directory,{recursive:true,force:true});}
// React's scheduler can retain a MessagePort after jsdom closes.
process.exit(0);

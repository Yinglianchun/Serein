import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','CustomEvent','Node']) {
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
}
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const config={identity:{user_name:'User',ai_name:'AI'},features:{resume:true,window_shadows:false},settings_version:1,
  resume:{mode:'mcp',command_enabled:false,mcp_enabled:true,latest_shadow:false,recent_events:false,
    favorite_scenes:false,selected_memories:true,selected_ids:['scene_test'],recent_originals:false,
    recent_event_limit:10,recent_scene_limit:10,recent_original_limit:20,pending_originals:false}};
let fail=true,previews=0,lastSelection;
globalThis.fetch=async(url,options={})=>{
  if(url==='/__serein/settings') {
    assert.equal(options.method,'GET');
    return {ok:true,json:async()=>structuredClone(config)};
  }
  assert.equal(url,'/__serein/resume');
  const {selection}=JSON.parse(options.body);
  lastSelection=selection;
  for(const key of ['mode','command_enabled','mcp_enabled'])assert(!Object.hasOwn(selection,key));
  previews++;
  if(fail)return {ok:false,json:async()=>({detail:'Invalid resume preview selection; use content options only'})};
  return {ok:true,json:async()=>({collection_id:'synthetic',total_items:1,has_more:false,next_cursor:null,
    items:[{id:'scene_test',kind:'scene',section:'selected_memory',title:'Synthetic Scene',
      body_md:'Synthetic continuity body',body_offset:0,body_complete:true}]})};
};
const directory=await mkdtemp(join(tmpdir(),'serein-resume-dom-'));
let root;
try {
  const output=await build({stdin:{contents:`import React,{act} from 'react';
    import {createRoot} from 'react-dom/client';import {ResumePage} from './src/pages/ResumePage.jsx';
    export {act};export function mount(){const root=createRoot(document.getElementById('root'));
    root.render(<ResumePage/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},
    jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,loader:{'.css':'empty'},
    define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
  const path=join(directory,'bundle.mjs');
  await writeFile(path,output.outputFiles[0].text);
  const {act,mount}=await import(pathToFileURL(path));
  await act(async()=>{root=mount();});
  await act(async()=>{await new Promise(resolve=>setTimeout(resolve,250));});
  const panel=document.querySelector('.resume-preview');
  assert.match(panel.querySelector('[role="alert"]').textContent,/Invalid resume preview selection/);
  assert(!panel.textContent.includes('勾选要带走的内容'));
  fail=false;
  const retry=[...panel.querySelectorAll('button')].find(button=>button.textContent==='重新预览');
  assert(!retry.disabled);
  await act(async()=>retry.click());
  assert.equal(panel.querySelector('[role="alert"]'),null);
  assert.match(panel.textContent,/Synthetic continuity body/);
  assert.equal(previews,2);
  for (const [label,limit] of [['最近事件',2],['最近 Scene',11]]) {
    const input=document.querySelector(`input[aria-label="${label}带入条数"]`);
    assert.equal(input.value,'10');
    assert(input.disabled);
    await act(async()=>input.closest('.resume-custom-choice').querySelector('[type="checkbox"]').click());
    await act(async()=>{
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,String(limit));
      input.dispatchEvent(new Event('input',{bubbles:true}));
    });
  }
  await act(async()=>{await new Promise(resolve=>setTimeout(resolve,250));});
  assert.equal(lastSelection.recent_event_limit,2);
  assert.equal(lastSelection.recent_scene_limit,11);
  await act(async()=>root.unmount());root=null;
  console.log('PASS resume preview: error visible in reading pane, retry restores content without saving');
} finally {
  if(root)root.unmount();
  dom.window.close();
  await rm(directory,{recursive:true,force:true});
}
process.exit(0);

import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<div id="root"></div>',{url:'http://synthetic.invalid',pretendToBeVisual:true});
for(const key of ['window','document','navigator','HTMLElement','Event','CustomEvent','Node'])
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLElement.prototype.scrollTo=()=>{};
const directory=await mkdtemp(join(tmpdir(),'serein-scene-restore-dom-'));let root;
try {
  const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {fixture} from './tests/scene-restore-fixture.js';import {MemoryPage} from './src/pages/MemoryPage.jsx';export {act,fixture};export function mount(){const root=createRoot(document.getElementById('root'));root.render(<MemoryPage/>);return root;}`,resolveDir:process.cwd(),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,loader:{'.css':'empty'},define:{'process.env.NODE_ENV':'"development"','import.meta.env.BASE_URL':'"/"'}});
  const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
  const {act,fixture,mount}=await import(pathToFileURL(path));
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent.trim().startsWith(text)||node.getAttribute('aria-label')===text);
  async function click(node){assert(node,'Element exists');assert(!node.disabled,'Enabled');await act(async()=>{node.click();await wait(30);});}
  await act(async()=>{root=mount();await wait(60);});
  assert.equal(document.querySelectorAll('.scene-entry').length,3);assert.equal(fixture.requests.length,0);
  await click(button('已沉底'));await click(button('批量整理'));await click(button('全选当前筛选'));
  fixture.fail=true;await click(button('恢复可浮现'));
  assert.equal(fixture.requests.length,3);assert(fixture.requests.every(row=>row.status==='active'&&row.restoreSurface===true));
  assert(document.body.textContent.includes('Synthetic version conflict'));
  assert.equal(document.querySelectorAll('.scene-entry').length,3);
  assert(fixture.scenes.every(row=>row.storage_status==='archived'));
  fixture.fail=false;fixture.hold=true;
  await click(button('恢复可浮现'));assert(button('恢复可浮现').disabled);assert(button('归档').disabled);
  // Release all pending requests: the fixture exposes one resolver per call below.
  fixture.hold=false;
  await act(async()=>{for(const resolve of fixture.pending) resolve();await wait(60);});
  assert(fixture.scenes.every(row=>row.storage_status==='active'&&row.manual===true));
  assert.equal(document.querySelectorAll('.scene-entry').length,0);
  assert(document.body.textContent.includes('已恢复可浮现'));
  await click(button('全部'));await click(button('全选当前筛选'));await click(button('归档'));
  assert(fixture.requests.slice(-3).every(row=>row.status==='archived'&&!('restoreSurface' in row)));
  assert(fixture.scenes.every(row=>row.manual===true));
  await click(button('已沉底'));await click(button('全选当前筛选'));await click(button('恢复可浮现'));
  assert(fixture.requests.slice(-3).every(row=>row.expectedUpdatedAt.endsWith('-next-next')));
  assert(fixture.scenes.every(row=>row.storage_status==='active'));
  console.log('PASS Scene restore DOM: explicit opt-in for false/null/true, conflict retention, saving controls, archive isolation and refreshed versions');
  await act(async()=>root.unmount());root=null;
} finally {if(root)root.unmount();dom.window.close();await rm(directory,{recursive:true,force:true});}
process.exit(0);

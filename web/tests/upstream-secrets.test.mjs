import {test} from 'node:test';
import assert from 'node:assert/strict';
import {upstreamsForSave} from '../src/upstreamSecrets.js';

function formWith(values) {
  return {
    querySelectorAll() {
      return Object.entries(values).map(([upstreamId,value])=>({dataset:{upstreamId},value}));
    },
  };
}

test('the password field value replaces a configured upstream key even without a state change',()=>{
  const draft=[{id:'provider',name:'Provider',api_key:'',api_key_configured:true,clear_key:false}];
  const [saved]=upstreamsForSave(draft,formWith({provider:'synthetic-new-key'}));
  assert.equal(saved.api_key,'synthetic-new-key');
  assert.equal('api_key_configured' in saved,false);
  assert.equal('clear_key' in saved,false);
  assert.equal(draft[0].api_key,'');
});

test('a non-empty password field wins over a stale clear-key choice',()=>{
  const [saved]=upstreamsForSave(
    [{id:'provider',api_key_configured:true,clear_key:true,api_key_env:'PROVIDER_KEY'}],
    formWith({provider:'synthetic-replacement'}),
  );
  assert.equal(saved.api_key,'synthetic-replacement');
  assert.equal(saved.api_key_env,'PROVIDER_KEY');
});

test('an empty password preserves a configured key unless removal is explicit',()=>{
  const upstream={id:'provider',api_key:'',api_key_configured:true,api_key_env:'PROVIDER_KEY'};
  const [preserved]=upstreamsForSave([{...upstream,clear_key:false}],formWith({provider:''}));
  assert.equal('api_key' in preserved,false);
  assert.equal(preserved.api_key_env,'PROVIDER_KEY');
  const [removed]=upstreamsForSave([{...upstream,clear_key:true}],formWith({provider:''}));
  assert.equal(removed.api_key,'');
  assert.equal(removed.api_key_env,'');
});

test('saving the configuration tab falls back to the current draft without a model form',()=>{
  const [saved]=upstreamsForSave([{id:'provider',api_key:'typed-key'}],null);
  assert.equal(saved.api_key,'typed-key');
});

test('per-model reasoning compatibility survives editing and save without affecting neighboring models',async()=>{
  const {JSDOM}=await import('jsdom');
  const {build,stop}=await import('esbuild');
  const {mkdtemp,writeFile,rm}=await import('node:fs/promises');
  const {tmpdir}=await import('node:os');
  const {join}=await import('node:path');
  const {pathToFileURL,fileURLToPath}=await import('node:url');
  const dom=new JSDOM('<!doctype html><div id="root"></div>',{url:'https://synthetic.invalid'});
  const previous=new Map();
  for(const key of ['window','document','navigator','HTMLElement','HTMLSelectElement','Event','Node']){
    previous.set(key,Object.getOwnPropertyDescriptor(globalThis,key));
    Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
  }
  const nativeChannel=globalThis.MessageChannel;
  const channels=[];
  globalThis.MessageChannel=class extends nativeChannel {constructor(){super();channels.push(this);}};
  globalThis.IS_REACT_ACT_ENVIRONMENT=true;
  const directory=await mkdtemp(join(tmpdir(),'serein-compat-dom-'));
  let root;
  try{
    const bundled=await build({stdin:{contents:`
      import React,{act,useState} from 'react';import {createRoot} from 'react-dom/client';
      import {UpstreamSettings} from './src/components/UpstreamSettings.jsx';
      export {act};export let current;
      const initial=[{id:'s',name:'Synthetic',base_url:'https://synthetic.invalid',models:[
        {id:'a',upstream_model:'opaque-a'},{id:'b',upstream_model:'opaque-b',reasoning_content_compat:'off'}]}];
      function Harness(){const [items,setItems]=useState(initial);current=items;return <UpstreamSettings upstreams={items} onChange={setItems} onImported={setItems}/>;}
      export function mount(){const r=createRoot(document.getElementById('root'));r.render(<Harness/>);return r;}
      `,resolveDir:fileURLToPath(new URL('..',import.meta.url)),loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"'}});
    const path=join(directory,'bundle.mjs');await writeFile(path,bundled.outputFiles[0].text);
    const app=await import(pathToFileURL(path));
    await app.act(async()=>{root=app.mount();});
    const controls=()=>[...document.querySelectorAll('label')].filter(n=>n.textContent.includes('工具历史推理字段兼容')).map(n=>n.querySelector('select'));
    assert.deepEqual(controls().map(n=>n.value),['auto','off']);
    assert(controls().every(n=>!n.disabled)); // omitted protocol defaults to OpenAI

    await app.act(async()=>{controls()[0].value='on';controls()[0].dispatchEvent(new Event('change',{bubbles:true}));});
    assert.deepEqual(controls().map(n=>n.value),['on','off']);
    const saved=upstreamsForSave(app.current,null);
    assert.deepEqual(saved[0].models.map(n=>n.reasoning_content_compat),['on','off']);
    // Repeated selection and closing/reopening the editor must preserve the draft.
    await app.act(async()=>{controls()[0].dispatchEvent(new Event('change',{bubbles:true}));
      const card=document.querySelector('.configured-model');card.open=false;card.open=true;});
    assert.equal(controls()[0].value,'on');
    const protocol=[...document.querySelectorAll('label')].find(n=>n.textContent.startsWith('接口格式')).querySelector('select');
    await app.act(async()=>{protocol.value='anthropic';protocol.dispatchEvent(new Event('change',{bubbles:true}));});
    assert(controls().every(n=>n.disabled));
    assert.deepEqual(app.current[0].models.map(n=>n.reasoning_content_compat),['on','off']);
    await app.act(async()=>{protocol.value='openai';protocol.dispatchEvent(new Event('change',{bubbles:true}));});
    assert(controls().every(n=>!n.disabled));
    assert.deepEqual(controls().map(n=>n.value),['on','off']);
  } finally{
    if(root){const {act}=await import(pathToFileURL(join(directory,'bundle.mjs')));await act(async()=>root.unmount());}
    dom.window.close();delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    for(const channel of channels){channel.port1.close();channel.port2.close();}
    globalThis.MessageChannel=nativeChannel;
    for(const [key,descriptor] of previous){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}
    await rm(directory,{recursive:true,force:true});
    await stop();
  }
});

import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';

let server, searchLinkCandidates, linkCandidateAvailable;
before(async()=>{
  server=await createServer({configFile:false,root:fileURLToPath(new URL('..',import.meta.url)),
    plugins:[react()],server:{middlewareMode:true,hmr:false,watch:null}});
  ({searchLinkCandidates,linkCandidateAvailable}=await server.ssrLoadModule('/src/components/ResumeMemoryPicker.jsx'));
});
after(async()=>{await server?.close();});

const row=(kind='scene',id='scene_a')=>({id,kind,title:'河边散步',date:'2026-09-20',excerpt:'沿着河边走回家'});
const payload=(items=[row()])=>({purpose:'link',items,total:items.length,has_more:false});
const response=(body,status=200)=>new Response(JSON.stringify(body),{status});

for(const kind of ['scene','event','diary','darkroom','upload']) {
  test(`lookup ${kind} sends encoded title keywords to the server`,async()=>{
    const signal=new AbortController().signal;
    let request;
    const item=row(kind,kind==='diary'||kind==='darkroom'?'17':`${kind}_a`);
    const result=await searchLinkCandidates({kind,query:'河边 & 散步',date:'2026-09-20',offset:30,signal},async(url,options)=>{
      request={url:new URL(url,'http://fixture.invalid'),options};return response(payload([item]));
    });
    assert.equal(request.url.pathname,'/__serein/settings/resume-candidates');
    assert.equal(request.url.searchParams.get('purpose'),'link');
    assert.equal(request.url.searchParams.get('kind'),kind);
    assert.equal(request.url.searchParams.get('q'),'河边 & 散步');
    assert.equal(request.url.searchParams.get('date'),'2026-09-20');
    assert.equal(request.url.searchParams.get('offset'),'30');
    assert.equal(request.options.signal,signal);
    assert.equal(request.options.cache,'no-store');
    assert.equal(request.options.method,undefined); // GET only, no model/mutation endpoint.
    assert.deepEqual(result.items,[item]);
  });
}

test('empty result and pagination metadata are preserved',async()=>{
  assert.deepEqual(await searchLinkCandidates({kind:'scene'},async()=>response(payload([]))),payload([]));
  const paged={...payload(),total:80,has_more:true};
  assert.deepEqual(await searchLinkCandidates({kind:'scene'},async()=>response(paged)),paged);
});

test('old backend cannot silently masquerade as a working link search',async()=>{
  await assert.rejects(searchLinkCandidates({kind:'scene'},async()=>response({items:[],total:0})),/更新后端/);
});

test('bad responses do not expose raw error payloads',async()=>{
  for(const status of [401,422,500,502]) {
    await assert.rejects(searchLinkCandidates({kind:'scene'},async()=>response({detail:{token:'secret'}},status)),error=>{
      assert.doesNotMatch(error.message,/secret|token/);return /查找暂不可用/.test(error.message);
    });
  }
  await assert.rejects(searchLinkCandidates({kind:'scene'},async()=>new Response('not json')));
});

test('malformed successful payloads are rejected before selection',async()=>{
  for(const body of [
    {...payload(),items:null}, {...payload(),total:-1}, {...payload(),has_more:'yes'},
    payload([row('event')]), payload([{...row(),id:''}]), payload([{...row(),excerpt:42}]),
    payload([null]), payload([{...row(),date:null}]),
  ]) await assert.rejects(searchLinkCandidates({kind:'scene'},async()=>response(body)),/有效的查找结果/);
});

test('unsupported kind is rejected without making a request',async()=>{
  let called=false;
  await assert.rejects(searchLinkCandidates({kind:'__proto__'},async()=>{called=true;}),/材料类型/);
  assert.equal(called,false);
});

test('already aborted request does not fetch',async()=>{
  const controller=new AbortController();controller.abort();let called=false;
  await assert.rejects(searchLinkCandidates({kind:'scene',signal:controller.signal},async()=>{called=true;}),{name:'AbortError'});
  assert.equal(called,false);
});

test('late response remains discarded even when transport ignores cancellation',async()=>{
  const controller=new AbortController();let finish;
  const pending=searchLinkCandidates({kind:'scene',signal:controller.signal},()=>new Promise(resolve=>{finish=resolve;}));
  controller.abort();finish(response(payload()));
  await assert.rejects(pending,{name:'AbortError'});
});

test('cancellation while JSON is decoding cannot publish old results',async()=>{
  const controller=new AbortController();
  await assert.rejects(searchLinkCandidates({kind:'scene',signal:controller.signal},async()=>({
    ok:true,json:async()=>{controller.abort();return payload();},
  })),{name:'AbortError'});
});

test('selection uses canonical ID, not title, and blocks duplicates or the other Scene',()=>{
  const first=row(),sameTitle=row('scene','scene_b');
  assert.equal(linkCandidateAvailable(first,'scene',['scene_a']),false);
  assert.equal(linkCandidateAvailable(sameTitle,'scene',['scene_a']),true);
  assert.equal(linkCandidateAvailable(row('diary','17'),'diary',[17]),false);
  assert.equal(linkCandidateAvailable(first,'event'),false);
  assert.equal(linkCandidateAvailable(null,'scene'),false);
  assert.equal(linkCandidateAvailable({...first,id:''},'scene'),false);
});

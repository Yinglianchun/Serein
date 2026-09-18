import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { scryptSync } from 'node:crypto';
import { once } from 'node:events';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { runEventBatch } from '../src/utils/eventBatch.js';

async function freePort() {
  const server=http.createServer();server.listen(0,'127.0.0.1');await once(server,'listening');
  const port=server.address().port;await new Promise(resolve=>server.close(resolve));return port;
}

test('gateway separates web auth from API auth, saves settings and streams responses', {timeout:30000}, async()=>{
  const dir=mkdtempSync(join(tmpdir(),'serein-gateway-'));const salt='1234567890abcdef';
  const record=password=>JSON.stringify({username:'test',salt,hash:scryptSync(password,Buffer.from(salt,'hex'),32).toString('hex')});
  writeFileSync(join(dir,'auth.json'),record('password-one'));writeFileSync(join(dir,'token'),'synthetic-api-key');
  const requests=[];
  const core=http.createServer((req,res)=>{
    if(req.url==='/health'){res.setHeader('Content-Type','application/json');res.end('{"status":"ok"}');return;}
    requests.push({path:req.url,auth:req.headers.authorization,method:req.method,forwardedHost:req.headers['x-forwarded-host'],forwardedProto:req.headers['x-forwarded-proto'],protocol:req.headers['mcp-protocol-version']});
    if(req.url.startsWith('/.well-known/oauth-') || ['/authorize','/token','/register'].includes(req.url)){
      res.writeHead(req.url==='/register'?201:200,{'Content-Type':'application/json'});res.end('{"oauth":true}');return;
    }
    if(req.headers.authorization!=='Bearer synthetic-api-key'){res.writeHead(401);res.end('{}');return;}
    if(req.url==='/api/hook/recall'){
      let raw='';req.on('data',chunk=>raw+=chunk);req.on('end',()=>{
        const body=JSON.parse(raw||'{}');requests.push({path:req.url,body});
        res.writeHead(200,{'Content-Type':'application/json'});res.end('{"ok":true,"cards":[],"debug":{}}');
      });return;
    }
    if(req.url.startsWith('/diaries/') && req.method==='DELETE'){
      res.setHeader('Content-Type','application/json');
      if(req.url==='/diaries/999999'){res.statusCode=404;res.end('{"message":"Synthetic diary missing"}');return;}
      res.end('{"status":"deleted","recoverable":true}');return;
    }
    if(['/api/fact-events/status','/api/fact-events/delete','/api/buckets/delete','/v1/tools/call'].includes(req.url)){
      let raw='';req.on('data',chunk=>raw+=chunk);req.on('end',()=>{
        const body=JSON.parse(raw);requests.push({path:req.url,body});
        const payload=req.url==='/api/fact-events/status'
          ? {item:{item_id:body.item_id,item_type:'event',status:body.status}}
          : req.url==='/api/fact-events/delete' ? {deleted:1,item_type:'event',item_ids:[body.item_id]}
          : req.url==='/api/buckets/delete' ? {deleted:body.bucket_ids.length}
          : {result:{status:'updated',updated_at:'synthetic-new-version'}};
        res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(payload));
      });return;
    }
    if(req.url.startsWith('/v1/host/deliveries') || req.url.startsWith('/api/gateway-injections')){
      res.setHeader('Content-Type','application/json');
      res.end(JSON.stringify({status:'ok',items:[{id:1,query:'A current request',payload:{query:'A current request',request_status:'completed',recall_state:'disabled'}}],has_more:false}));return;
    }
    if(req.url.startsWith('/api/narrative-revision-inbox/')){
      res.setHeader('Content-Type','application/json');
      if(req.url.includes('nrev_missing')){res.statusCode=404;res.end('{"status":"not_found"}');return;}
      const query=new URL(req.url,'http://localhost').searchParams;
      res.end(JSON.stringify(query.has('identifier')
        ? {status:'ok',readable:true,document:{title:'Synthetic scene',body_md:'Complete material text'}}
        : {status:'ok',items:[{kind:'scene',id:'scene_test',title:'Synthetic scene',readable:true}],total:51,next_offset:null}));return;
    }
    if(req.url==='/v1/chat/completions'){
      res.writeHead(200,{'Content-Type':'text/event-stream'});res.write('data: {"part":1}\n\n');
      setTimeout(()=>res.end('data: [DONE]\n\n'),400);return;
    }
    if(req.url==='/v1/migration/preview-path'){
      let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
        requests.push({path:req.url,body:JSON.parse(body)});
        res.writeHead(200,{'Content-Type':'application/json'});res.end('{"status":"ok"}');
      });return;
    }
    res.writeHead(200,{'Content-Type':'application/json'});res.end('{"status":"ok"}');
  });
  core.listen(0,'127.0.0.1');await once(core,'listening');
  const port=await freePort(),preview=await freePort();
  const child=spawn(process.execPath,['server/gateway.mjs'],{cwd:new URL('../',import.meta.url),stdio:['ignore','pipe','pipe'],env:{...process.env,
    SEREIN_MEMORY_URL:`http://127.0.0.1:${core.address().port}`,SEREIN_MEMORY_TOKEN_FILE:join(dir,'token'),
    SEREIN_WEB_AUTH_FILE:join(dir,'auth.json'),SEREIN_GATEWAY_PORT:String(port),SEREIN_GATEWAY_BIND:'127.0.0.1',SEREIN_PREVIEW_PORT:String(preview),
    SEREIN_PUBLIC_ORIGIN:'https://memory.example',
    SEREIN_LEGACY_SOURCE_HOST:dir}});
  let output='';child.stdout.on('data',d=>output+=d);child.stderr.on('data',d=>output+=d);
  const base=`http://127.0.0.1:${port}`;
  const basic=password=>'Basic '+Buffer.from('test:'+password).toString('base64');
  try {
    let started=false;
    for(let i=0;i<150;i++){
      try {await fetch(base);started=true;break;}catch{if(child.exitCode!==null)throw new Error(output);await new Promise(r=>setTimeout(r,100));}
    }
    assert.ok(started,output);
    assert.equal((await fetch(base)).status,401);
    assert.equal((await fetch(base+'/ready')).status,200);
    for(const [path,method] of [['/.well-known/oauth-protected-resource','GET'],['/.well-known/oauth-authorization-server','GET'],['/register','POST'],['/authorize','GET'],['/token','POST']]){
      const response=await fetch(base+path,{method,headers:method==='POST'?{'Content-Type':'application/json'}:{},body:method==='POST'?'{}':undefined});
      assert.ok([200,201].includes(response.status));
      assert.ok(requests.some(r=>r.path===path&&r.method===method&&r.forwardedHost==='memory.example'&&r.forwardedProto==='https'));
    }
    const auth={Authorization:basic('password-one')};
    const page=await fetch(base,{headers:auth});assert.equal(page.status,200);
    assert.ok(!(await page.text()).includes('synthetic-api-key'));
    const settings=await fetch(base+'/__serein/settings',{headers:auth});assert.equal(settings.status,200);
    const saved=await fetch(base+'/__serein/settings',{method:'PATCH',headers:{...auth,'Content-Type':'application/json',Origin:base},body:'{"identity":{"user_name":"Example"}}'});
    assert.equal(saved.status,200);
    assert.ok(requests.some(r=>r.path==='/v1/settings'&&r.method==='PATCH'&&r.auth==='Bearer synthetic-api-key'));
    const discovery=await fetch(base+'/__serein/settings/models/discover',{method:'POST',headers:{...auth,'Content-Type':'application/json',Origin:base},body:'{"upstream_id":"test","base_url":"https://provider.example/v1"}'});
    assert.equal(discovery.status,200);
    assert.ok(requests.some(r=>r.path==='/v1/settings/models/discover'&&r.method==='POST'&&r.auth==='Bearer synthetic-api-key'));
    assert.equal((await fetch(base+'/__serein/settings/models/discover',{method:'POST',headers:{...auth,'Content-Type':'application/json',Origin:'https://foreign.invalid'},body:'{}'})).status,403);
    assert.equal((await fetch(base+'/__serein/settings',{method:'PATCH',headers:{...auth,'Content-Type':'application/json',Origin:'https://foreign.invalid'},body:'{}'})).status,403);
    for(const path of ['/__serein/pipeline/status','/__serein/pipeline/attempts/1']){
      assert.equal((await fetch(base+path,{headers:auth})).status,200);
      assert.ok(requests.some(r=>r.path===path.replace('/__serein/','/v1/')&&r.method==='GET'));
    }
    const postHeaders={...auth,'Content-Type':'application/json',Origin:base};
    const thresholdTrial=await fetch(base+'/__serein/gateway/recall',{method:'POST',headers:postHeaders,
      body:JSON.stringify({query:'Synthetic threshold trial',simulation:true,recall_mode:'full',direct_threshold:.6})});
    assert.equal(thresholdTrial.status,200);
    assert.ok(requests.some(r=>r.path==='/api/hook/recall'&&r.body?.direct_threshold===.6&&r.body?.simulation===true));
    // Exercise the actual browser helpers through the authenticated gateway and Vite proxy.
    const bundled=await build({entryPoints:[fileURLToPath(new URL('../src/storage/diaryStore.js',import.meta.url))],
      bundle:true,write:false,format:'esm',define:{'import.meta.env.BASE_URL':'"/"'}});
    const diary=await import('data:text/javascript;base64,'+Buffer.from(bundled.outputFiles[0].text).toString('base64'));
    const nativeFetch=globalThis.fetch;
    try {
      globalThis.fetch=(path,options={})=>nativeFetch(base+path,{...options,headers:{...auth,Origin:base,...options.headers}});
      assert.equal((await diary.deleteDiaryEntry({id:'diary-vps-42'})).status,'deleted');
      assert.equal((await diary.deleteDiaryComment({id:'diary-vps-42'},'diary-comment-vps-7')).status,'deleted');
      await assert.rejects(diary.deleteDiaryEntry({id:'diary-vps-999999'}),/Synthetic diary missing/);
    } finally {globalThis.fetch=nativeFetch;}
    for(const path of ['/diaries/42','/diaries/42/comments/7']){
      assert.ok(requests.some(r=>r.path===path&&r.method==='DELETE'&&r.auth==='Bearer synthetic-api-key'));
    }
    assert.equal((await fetch(base+'/__serein/live/diaries/42',{method:'DELETE',headers:auth})).status,403);
    assert.equal((await fetch(base+'/__serein/live/diaries/42',{method:'DELETE',headers:{...postHeaders,Origin:'https://foreign.invalid'}})).status,403);
    const browserRequest=(path,options)=>fetch(base+path,{...options,headers:{...auth,Origin:base,...options.headers}});
    for(const action of ['archive','restore','delete']){
      const result=await runEventBatch(['event_synthetic'],action,{request:browserRequest});
      assert.equal(result.failures.length,0);
      assert.ok(result.completed.has('event_synthetic'));
    }
    for(const status of ['archived','active']){
      const response=await browserRequest('/__serein/memory/set-scene-status',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({sceneId:'scene_synthetic',expectedUpdatedAt:'synthetic-version',status})});
      assert.equal(response.status,200);
      assert.equal((await response.json()).status,'updated');
      assert.deepEqual(requests.at(-1).body,{name:'set_scene_status',arguments:{
        scene_id:'scene_synthetic',expected_updated_at:'synthetic-version',status}});
    }
    const sceneDelete=await browserRequest('/__serein/memory/delete-scenes',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sceneIds:['scene_synthetic']})});
    assert.equal(sceneDelete.status,200);
    assert.equal((await sceneDelete.json()).deleted,1);
    assert.deepEqual(requests.at(-1).body,{bucket_ids:['scene_synthetic'],confirm:'DELETE'});
    for(const path of ['/__serein/migration','/__serein/export/backup']){
      assert.equal((await fetch(base+path)).status,401);
      const response=await fetch(base+path,{headers:auth});
      assert.equal(response.status,200);
      assert.equal(response.headers.get('cache-control'),'no-store');
      assert.ok(requests.some(r=>r.path===path.replace('/__serein/','/v1/')&&r.auth==='Bearer synthetic-api-key'));
    }
    const migrationPreview=await fetch(base+'/__serein/migration/preview',{method:'POST',headers:postHeaders,body:'{"content":"synthetic"}'});
    assert.equal(migrationPreview.status,200);
    assert.ok(requests.some(r=>r.path==='/v1/migration/preview'&&r.method==='POST'));
    const pathPreview=await fetch(base+'/__serein/migration/preview-path',{method:'POST',headers:postHeaders,body:JSON.stringify({path:join(dir,'buckets')})});
    assert.equal(pathPreview.status,200);
    assert.deepEqual(requests.find(r=>r.path==='/v1/migration/preview-path'&&r.body)?.body,
      {path:'/legacy/input/buckets',display_path:join(dir,'buckets')});
    const outside=await fetch(base+'/__serein/migration/preview-path',{method:'POST',headers:postHeaders,body:JSON.stringify({path:join(tmpdir(),'outside')})});
    assert.equal(outside.status,400);
    for (const action of ['discover-theme','create-line']) {
      const response=await fetch(base+'/__serein/narrative-'+action, {
        method:'POST',headers:postHeaders,body:JSON.stringify({theme:'Synthetic story'}),
      });
      assert.equal(response.status,200);
      assert.deepEqual(await response.json(),{status:'ok'});
      assert.ok(requests.some(r=>r.path==='/api/narrative-rolls/'+action && r.method==='POST'
        && r.auth==='Bearer synthetic-api-key'));
    }
    for(const route of ['/__serein/assistant-bridge/hook-injections','/__serein/gateway/injections']){
      const response=await fetch(base+route,{method:'POST',headers:postHeaders,body:'{"limit":5}'});
      assert.equal(response.status,200);
      const payload=await response.json();
      assert.equal(payload.status,'ok');
      assert.equal(payload.items[0].query,'A current request');
    }
    for (const args of [{proposalId:'nrev_test',offset:50}, {proposalId:'nrev_test',kind:'scene',identifier:'scene_test'}]) {
      const material = await fetch(base+'/__serein/memory/revision-materials', {
        method:'POST',headers:postHeaders,body:JSON.stringify(args),
      });
      assert.equal(material.status,200);
      const payload=await material.json();
      assert.equal(payload.status,'ok');
      assert.equal('payload' in payload,false);
      if(args.identifier) assert.equal(payload.document.body_md,'Complete material text');
      else assert.equal(payload.items[0].title,'Synthetic scene');
      const forwarded=requests.at(-1);
      assert.equal(forwarded.auth,'Bearer synthetic-api-key');
      const path=new URL(forwarded.path,base);
      assert.equal(path.pathname,'/api/narrative-revision-inbox/nrev_test/materials');
      assert.equal(path.searchParams.get('offset'),String(args.offset || 0));
      if(args.identifier) assert.equal(path.searchParams.get('identifier'),args.identifier);
    }
    const missing=await fetch(base+'/__serein/memory/revision-materials',{method:'POST',headers:postHeaders,body:JSON.stringify({proposalId:'nrev_missing'})});
    assert.equal(missing.status,404);
    assert.deepEqual(await missing.json(),{status:'not_found'});
    assert.equal((await fetch(base+'/__serein/pipeline/next',{method:'POST',headers:postHeaders,body:'{"include_recent":true}'})).status,200);
    assert.ok(requests.some(r=>r.path==='/v1/pipeline/next'&&r.method==='POST'));
    assert.equal((await fetch(base+'/__serein/pipeline/next',{method:'POST',headers:{...postHeaders,Origin:'https://foreign.invalid'},body:'{}'})).status,403);
    const rebuildBody={batch_id:'pipeline:synthetic',confirm:'REBUILD_PIPELINE_BATCH'};
    assert.equal((await fetch(base+'/__serein/pipeline/rebuild',{method:'POST',headers:postHeaders,body:JSON.stringify(rebuildBody)})).status,200);
    assert.deepEqual(requests.find(r=>r.path==='/v1/pipeline/rebuild')?.body,rebuildBody);
    assert.equal((await fetch(base+'/__serein/pipeline/rebuild',{method:'POST',headers:{...postHeaders,Origin:'https://foreign.invalid'},body:JSON.stringify(rebuildBody)})).status,403);
    assert.equal((await fetch(base+'/__serein/pipeline/rebuild',{headers:auth})).status,405);
    const upload='upload%3A'+'a'.repeat(64);
    for(const action of ['continue','pause'])assert.equal((await fetch(base+'/__serein/imports/'+upload+'/'+action,{method:'POST',headers:postHeaders,body:'{}'})).status,200);
    assert.equal((await fetch(base+'/v1/models',{headers:auth})).status,401);
    assert.equal((await fetch(base+'/v1/models',{headers:{Authorization:'Bearer wrong'}})).status,401);
    for(const [path,method] of [['/api/hook/recall','POST'],['/v1/host/deliveries','GET'],['/v1/host/deliveries','POST']]) {
      const body=method==='POST'?'{}':undefined;
      assert.equal((await fetch(base+path,{method,headers:{...auth,'Content-Type':'application/json'},body})).status,401);
      assert.equal((await fetch(base+path,{method,headers:{Authorization:'Bearer wrong','Content-Type':'application/json'},body})).status,401);
      const response=await fetch(base+path,{method,headers:{Authorization:'Bearer synthetic-api-key','Content-Type':'application/json'},body});
      assert.equal(response.status,200);
      assert.ok(requests.some(r=>r.path===path&&r.method===method&&r.auth==='Bearer synthetic-api-key'));
    }
    for (const path of ['/serein/mcp', '/serein/mcp/', '/mcp', '/mcp/']) {
      assert.equal((await fetch(base+path,{headers:auth})).status,401);
      assert.equal((await fetch(base+path,{headers:{Authorization:'Bearer wrong'}})).status,401);
      for (const method of ['POST', 'GET', 'DELETE']) {
        const headers={Authorization:'Bearer synthetic-api-key','Content-Type':'application/json','Mcp-Protocol-Version':'2025-03-26',Origin:base};
        assert.equal((await fetch(base+path,{method,headers,body:method==='POST'?'{}':undefined})).status,200);
        assert.ok(requests.some(r=>r.path===path&&r.method===method&&r.auth==='Bearer synthetic-api-key'&&r.forwardedHost==='memory.example'&&r.forwardedProto==='https'&&r.protocol==='2025-03-26'));
      }
      assert.equal((await fetch(base+path,{method:'POST',headers:{Authorization:'Bearer synthetic-api-key',Origin:'https://foreign.invalid'},body:'{}'})).status,403);
    }
    const stream=await fetch(base+'/v1/chat/completions',{method:'POST',headers:{Authorization:'Bearer synthetic-api-key','Content-Type':'application/json'},body:'{"stream":true}'});
    const reader=stream.body.getReader();const first=new TextDecoder().decode((await reader.read()).value);
    assert.ok(first.includes('part')&&!first.includes('[DONE]'));
    const second=new TextDecoder().decode((await reader.read()).value);assert.ok(second.includes('[DONE]'));
    writeFileSync(join(dir,'auth.json'),record('password-two'));
    assert.equal((await fetch(base,{headers:auth})).status,401);
    assert.equal((await fetch(base,{headers:{Authorization:basic('password-two')}})).status,200);
  } finally {
    child.kill();if(child.exitCode===null)await once(child,'exit');
    core.closeAllConnections();await new Promise(resolve=>core.close(resolve));rmSync(dir,{recursive:true,force:true});
  }
});

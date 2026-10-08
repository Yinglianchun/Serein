import test from 'node:test';
import assert from 'node:assert/strict';
import {indexSafetySummary,resumeIndex} from '../src/memoryPreparation.js';
import {Readable} from 'node:stream';
import config from '../vite.config.mjs';

test('paused status explains attempts, pending work and recovery',()=>{
  const message=indexSafetySummary({status:'paused',attempts:3,max_attempts:3,pending:12,reason:'transient_provider_error'});
  assert.match(message,/已暂停/);
  assert.match(message,/3 \/ 3/);
  assert.match(message,/待处理 12/);
  assert.match(message,/已有向量/);
});

test('recovery posts explicit confirmation and observed state token',async()=>{
  let request;
  await resumeIndex({recovery_token:'observed-token'},{fetchImpl:async(path,options)=>{
    request={path,...options};return {ok:true,json:async()=>({status:'ready'})};
  }});
  assert.equal(request.path,'/__serein/settings/resume-index');
  assert.equal(request.method,'POST');
  assert.deepEqual(JSON.parse(request.body),{confirm:'RESUME_INDEX_EMBEDDING',recovery_token:'observed-token'});
});

test('stale recovery does not claim success',async()=>{
  await assert.rejects(resumeIndex({recovery_token:'old'},{fetchImpl:async()=>({ok:false})}),/恢复未生效/);
});

test('web recovery uses server credential proxy, same-origin JSON and state token',async(t)=>{
  const previous={url:process.env.SEREIN_MEMORY_URL,token:process.env.SEREIN_MEMORY_TOKEN};
  process.env.SEREIN_MEMORY_URL='http://synthetic.invalid';
  process.env.SEREIN_MEMORY_TOKEN='synthetic-server-only';
  try {
    const handlers=new Map();
    config.plugins.flat().find(plugin=>plugin.name==='serein-memory-bridge').configureServer({middlewares:{use:(path,handler)=>handlers.set(path,handler)}});
    const requests=[];
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      requests.push({url,options});
      return {ok:true,status:200,json:async()=>({status:'ready'})};
    });
    const invoke=async(method,url,origin)=>{
      const request=Readable.from([Buffer.from(JSON.stringify({confirm:'RESUME_INDEX_EMBEDDING',recovery_token:'observed'}))]);
      Object.assign(request,{method,url,headers:{'content-type':'application/json',host:'localhost',...(origin?{origin}:{})}});
      const response={setHeader(){},end(text){this.payload=JSON.parse(text);}};
      await handlers.get('/__serein/settings')(request,response);
      return response;
    };
    assert.equal((await invoke('POST','/resume-index','http://other.invalid')).statusCode,403);
    assert.equal((await invoke('GET','/resume-index')).statusCode,405);
    assert.equal(requests.length,0);
    assert.equal((await invoke('POST','/resume-index','http://localhost')).statusCode,200);
    assert.equal(requests[0].url,'http://synthetic.invalid/v1/settings/resume-index');
    assert.equal(requests[0].options.headers.Authorization,'Bearer synthetic-server-only');
    assert.deepEqual(JSON.parse(requests[0].options.body),{confirm:'RESUME_INDEX_EMBEDDING',recovery_token:'observed'});
  } finally {
    for(const [key,value] of [['SEREIN_MEMORY_URL',previous.url],['SEREIN_MEMORY_TOKEN',previous.token]]) {
      if(value===undefined)delete process.env[key];else process.env[key]=value;
    }
  }
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {mailboxDraft,cleanMailboxDraft,validMailboxDraft,mailboxWrite,mailboxPromotion,mailboxRequest} from '../src/utils/eventMailbox.js';
import {eventMailboxBridge} from '../server/eventMailboxBridge.mjs';
const item={event_id:'event_1',queue_revision:2,event_revision:4,draft:{title:'Title',body_md:'Body',cues:['cue']}};
test('mailbox write keeps Event and queue revisions, explicit selection and authored cues',()=>{
 assert.deepEqual(mailboxWrite(item,'select',undefined,'op'),{operation_id:'op',action:'select',expected_revision:4,expected_queue_revision:2});
 assert.deepEqual(mailboxPromotion(item,{title:' T ',body_md:' B ',cues:[' cue ','']},'op'),{operation_id:'op',event_id:'event_1',expected_revision:4,expected_queue_revision:2,title:'T',body_md:'B',cues:['cue']});
 assert.equal('cues' in mailboxPromotion(item,{title:'T',body_md:'B',cues:[]},'op'),false);
 assert.deepEqual(mailboxWrite(item,'draft',{title:'T',body_md:'B',cues:[]},'op').cues,[]);
});
test('draft never auto-fills cues from tags, clone avoids mutating loaded record',()=>{
 const draft=mailboxDraft({...item,event:{document:{metadata:{tags:['not a cue']}}}});draft.cues.push('new');assert.deepEqual(item.draft.cues,['cue']);
 assert.deepEqual(mailboxDraft({event:{document:{title:'T',body_md:'B',metadata:{tags:['x']}}}}).cues,[]);
 assert.equal(validMailboxDraft({title:'T',body_md:'B',cues:[]}),true);
 assert.equal(validMailboxDraft({title:'T',body_md:'B',cues:['a'.repeat(81)]}),false);
 assert.equal(validMailboxDraft({title:'T',body_md:'B',cues:Array(9).fill('x')}),false);
 assert.deepEqual(cleanMailboxDraft({title:' T ',body_md:' B ',cues:[' ',' a ']}),{title:'T',body_md:'B',cues:['a']});
});
test('conflict and network errors reject without modifying original draft',async()=>{
 await assert.rejects(()=>mailboxRequest('/event_1',{},async()=>new Response(JSON.stringify({detail:'stale'}),{status:409})),error=>error.conflict===true);
 await assert.rejects(()=>mailboxRequest('/event_1',{},async()=>new Response(JSON.stringify({status:'error',message:'failed'}))),/failed/);
 assert.deepEqual(item.draft.cues,['cue']);
});
test('server proxy forwards only canonical routes, filters queries, passes write revisions',async()=>{
 const handlers=new Map(),calls=[];
 eventMailboxBridge({middlewares:{use:(path,handler)=>handlers.set(path,handler)}},async(path,options)=>{calls.push({path,options});return{status:409,payload:{detail:'conflict'}};},async request=>request.body);
 const response={setHeader(){},end(value){this.body=JSON.parse(value);}};
 await handlers.get('/__serein/event-mailbox')({url:'/?status=pending&limit=20&cursor=next%3D&secret=no',method:'GET'},response);
 assert.equal(calls[0].path,'/api/event-mailbox?status=pending&limit=20&cursor=next%3D');assert.equal(response.statusCode,409);
 await handlers.get('/__serein/event-mailbox')({url:'/event_1',method:'POST',body:mailboxWrite(item,'remove',undefined,'op')},response);
 assert.equal(calls[1].options.body.action,'remove');assert.equal(calls[1].options.body.expected_queue_revision,2);
 await handlers.get('/__serein/event-mailbox-promote')({url:'/',method:'POST',body:mailboxPromotion(item,item.draft,'op')},response);
 assert.equal(calls[2].path,'/v1/extensions/promote_event_to_scene');
 await handlers.get('/__serein/event-mailbox')({url:'/../../bad',method:'POST',body:{}},response);assert.equal(response.statusCode,400);
});
test('standalone preview is isolated, exercises mutation and failure receipts without backend access',async()=>{
 const originalWindow=globalThis.window;globalThis.window={location:{origin:'http://synthetic.invalid'}};
 try {
  const {fixture}=await import('./event-mailbox-fixture.js');
  const request=async(path,body)=>{const response=await window.fetch('/__serein/'+path,body?{body:JSON.stringify(body)}:{});return{status:response.status,payload:await response.json()};};
  assert.equal((await request('unrecognized')).status,404);
  const item=(await request('event-mailbox/event_synthetic')).payload;
  const select=(await request('event-mailbox/event_synthetic',mailboxWrite(item,'select',undefined,'op'))).payload;
  assert.equal(select.status,'approved');assert.equal(fixture.selectCalls,1);
  const saved=(await request('event-mailbox/event_synthetic',mailboxWrite(select,'draft',{title:'New',body_md:'Full body',cues:['rain']},'draft'))).payload;
  assert.deepEqual(saved.draft.cues,['rain']);
  fixture.failPromotion=true;
  assert.equal((await request('event-mailbox-promote',mailboxPromotion(saved,saved.draft,'promote'))).status,502);
  assert.equal(fixture.rows.get(item.event_id).status,'approved');
  fixture.failPromotion=false;
  assert.equal((await request('event-mailbox-promote',mailboxPromotion(saved,saved.draft,'promote'))).payload.queue_status,'completed');
  fixture.enabled=false;
  const second=(await request('event-mailbox/event_second')).payload;
  assert.equal((await request('event-mailbox/event_second',mailboxWrite(second,'remove',undefined,'remove'))).status,403);
  assert.equal(fixture.rows.get('event_second').status,'pending');
 }finally{globalThis.window=originalWindow;}
});

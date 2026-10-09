// Fully isolated synthetic preview. No fetch request can reach a backend.
const makeRow=(id,status='pending')=>({event_id:id,title:id,lifecycle:'active',created_at:'2026-01-01T00:00:00Z',event_revision:3,queue_revision:status==='not_selected'?0:1,status,processable:true,updated_at:'2026-01-01T00:00:00Z',draft_stale:false,draft:{title:id,body_md:'Full original Event\n\nLast paragraph',cues:[]},event:{readable:true,document:{title:id,body_md:'Full original Event\n\nLast paragraph'},evidence:[{binding_id:1,source_key:'synthetic-original',content:'Exact original evidence, never rewritten.',metadata:{role:'user'}}]}});
export const fixture={receipts:new Map(),requests:[],loseDraftReplyOnce:false,enabled:true,failPromotion:false,conflictDraft:false,badDecisionReplyOnce:false,failedIds:new Set(),loseDecisionReplyOnce:false,selectCalls:0,promoteCalls:0,mutations:0,rows:new Map([
 ['event_synthetic',{...makeRow('event_synthetic'),queue_revision:0,body_preview:'Full original Event',topic:'日常',evidence_count:1,source_started_at:'2026-01-01'}],['event_second',makeRow('event_second')],['event_third',makeRow('event_third')]
])};
window.mailboxFixture=fixture;
window.fetch=async(input,options={})=>{
 const url=new URL(String(input),window.location.origin),body=options.body?JSON.parse(options.body):null;
 if(body){fixture.requests.push(structuredClone(body));if(fixture.badDecisionReplyOnce&&['select','remove','restore'].includes(body.action)){fixture.badDecisionReplyOnce=false;return new Response(JSON.stringify({status:'pending'}),{status:200});}if(fixture.receipts.has(body.operation_id))return new Response(JSON.stringify(fixture.receipts.get(body.operation_id)),{status:200});}
 let payload={},status=200;
 if(url.pathname==='/__serein/settings')payload={identity:{user_name:'User',ai_name:'AI'},features:{event_to_scene:fixture.enabled}};
 else if(url.pathname==='/__serein/event-mailbox-promote'){
  fixture.promoteCalls++;fixture.mutations++;const item=fixture.rows.get(body.event_id);
  if(!fixture.enabled){status=403;payload={message:'Feature disabled'};}
  else if(fixture.failPromotion){status=502;payload={message:'Synthetic promotion failed'};}
  else{item.status='completed';item.queue_revision++;item.scene_id='scene_synthetic';payload={id:item.scene_id,status:'saved',queue_status:'completed'};}
 }else if(url.pathname==='/__serein/event-mailbox'){
  const filter=url.searchParams.get('status');
  const all=[...fixture.rows.values()].filter(item=>filter==='retained'?['approved','completed'].includes(item.status):item.status===filter);
  const counts={pending:0,approved:0,completed:0,removed:0};for(const item of fixture.rows.values())counts[item.status]=(counts[item.status]||0)+1;counts.retained=counts.approved+counts.completed;
  const offset=Number(url.searchParams.get('cursor')||url.searchParams.get('offset')||0);
  payload={counts,items:all.slice(offset,offset+1).map(item=>({...item,event:undefined,draft:undefined})),has_more:offset+1<all.length,next_offset:offset+1,next_cursor:String(offset+1)};
 }else if(url.pathname.startsWith('/__serein/event-mailbox/')){
  const item=fixture.rows.get(decodeURIComponent(url.pathname.split('/').at(-1)));
  if(!item)return new Response(JSON.stringify({message:'Synthetic Event missing'}),{status:404});
  if(body){fixture.mutations++;
   if(!fixture.enabled){status=403;payload={message:'Feature disabled'};}
   else if(fixture.failedIds.has(item.event_id)){status=502;payload={message:'Synthetic decision failed'};}
   else if(body.expected_revision!==item.event_revision||body.expected_queue_revision!==item.queue_revision){status=409;payload={detail:'conflict'};}
   else if(body.action==='select'){fixture.selectCalls++;await new Promise(resolve=>setTimeout(resolve,150));item.status='approved';item.queue_revision++;}
   else if(body.action==='restore'){item.status='pending';item.queue_revision++;}
   else if(body.action==='draft'){if(fixture.conflictDraft){status=409;payload={detail:'conflict'};}else{item.draft={title:body.title,body_md:body.body_md,cues:body.cues};item.queue_revision++;item.draft_stale=false;}}
   else if(body.action==='remove'){item.status='removed';item.queue_revision++;}
  }
  if(status===200)payload=structuredClone(item);
 }else return new Response(JSON.stringify({message:'Synthetic preview blocks unknown requests'}),{status:404});
 if(body&&status===200){fixture.receipts.set(body.operation_id,structuredClone(payload));if(['select','remove','restore'].includes(body.action)&&fixture.loseDecisionReplyOnce){fixture.loseDecisionReplyOnce=false;return new Response(JSON.stringify({message:'Synthetic decision response lost'}),{status:502});}if(body.action==='draft'&&fixture.loseDraftReplyOnce){fixture.loseDraftReplyOnce=false;return new Response(JSON.stringify({message:'Synthetic response lost after draft commit'}),{status:502});}}
 return new Response(JSON.stringify(payload),{status,headers:{'Content-Type':'application/json'}});
};

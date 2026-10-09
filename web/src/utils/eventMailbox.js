import {createUuid} from './createUuid.js';

export function mailboxDraft(item) {
  return {title:item?.draft?.title ?? item?.event?.document?.title ?? '',
    body_md:item?.draft?.body_md ?? item?.event?.document?.body_md ?? '',
    cues:Array.isArray(item?.draft?.cues)?[...item.draft.cues]:[]};
}
export function cleanMailboxDraft(draft) {
  return {title:draft.title.trim(),body_md:draft.body_md.trim(),cues:draft.cues.map(cue=>cue.trim()).filter(Boolean)};
}
export function validMailboxDraft(draft) {
  const clean=cleanMailboxDraft(draft);
  return !!clean.title&&!!clean.body_md&&clean.cues.length<=8&&clean.cues.every(cue=>Array.from(cue).length<=80);
}
export function mailboxWrite(item, action, draft, operationId=createUuid()) {
  return {operation_id:operationId,action,expected_revision:item.event_revision,expected_queue_revision:item.queue_revision,
    ...(draft?cleanMailboxDraft(draft):{})};
}
export function mailboxPromotion(item, draft, operationId=createUuid()) {
  const {cues,...prose}=cleanMailboxDraft(draft);
  return {operation_id:operationId,event_id:item.event_id,expected_revision:item.event_revision,
    expected_queue_revision:item.queue_revision,...prose,...(cues.length?{cues}:{})};
}
export async function mailboxRequest(path='', body, request=fetch) {
  const response=await request(`/__serein/event-mailbox${path}`,{method:body?'POST':'GET',cache:'no-store',
    ...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
  const payload=await response.json().catch(()=>({}));
  if(!response.ok||['conflict','error','invalid','disabled'].includes(payload?.status)) {
    const detail=typeof payload.detail==='string'?payload.detail:payload.message||payload.reason||payload.error;
    const error=new Error(response.status===409||payload.status==='conflict'?'这条 Event 或草稿已有新版本。你的输入仍保留，请重新读取后核对。':detail||'操作未完成，请重试。');
    error.conflict=response.status===409||payload.status==='conflict';throw error;
  }
  return payload;
}

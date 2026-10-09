import {useEffect,useRef,useState} from 'react';
import {ArrowClockwise,EnvelopeSimple,Plus,Trash,X} from '@phosphor-icons/react';
import {instanceSettings,identityName} from '../storage/instanceStore.js';
import {mailboxRequest,mailboxDraft,mailboxWrite,mailboxPromotion,validMailboxDraft,cleanMailboxDraft} from '../utils/eventMailbox.js';
import {MarkdownProjection} from '../components/MarkdownProjection.jsx';
import './event-mailbox-page.css';

function eventRange(item){const start=(item.source_started_at||item.created_at||'').slice(0,10),end=(item.source_ended_at||'').slice(0,10);return end&&end!==start?`${start} — ${end}`:start;}

function MailboxDetail({eventId,enabled,onClose,onChanged}) {
  const [item,setItem]=useState(null),[draft,setDraft]=useState({title:'',body_md:'',cues:[]});
  const [busy,setBusy]=useState('loading'),[error,setError]=useState(''),[notice,setNotice]=useState(''),[conflict,setConflict]=useState(false);
  const epoch=useRef(0),lock=useRef(false),pending=useRef(null),preSave=useRef(null);
  const dirty=item&&JSON.stringify(cleanMailboxDraft(draft))!==JSON.stringify(cleanMailboxDraft(mailboxDraft(item)));
  useEffect(()=>{
    function guard(event){if(dirty&&!window.confirm('尚有未保存的输入，离开详情吗？'))event.preventDefault();}
    function unload(event){if(dirty){event.preventDefault();event.returnValue='';}}
    window.addEventListener('serein:before-navigation',guard);window.addEventListener('beforeunload',unload);
    return()=>{window.removeEventListener('serein:before-navigation',guard);window.removeEventListener('beforeunload',unload);};
  },[dirty]);
  async function read(ask=true) {
    if(lock.current)return;
    if(ask&&dirty&&!window.confirm('重新读取会替换尚未保存的输入，继续吗？'))return;
    const current=++epoch.current;setBusy('loading');setError('');
    try {const value=await mailboxRequest('/'+encodeURIComponent(eventId));if(current!==epoch.current)return;
      setItem(value);setDraft(mailboxDraft(value));setConflict(false);setNotice('');pending.current=null;preSave.current=null;
    } catch(err){if(current===epoch.current)setError(err.message);}
    finally{if(current===epoch.current)setBusy('');}
  }
  useEffect(()=>{read(false);return()=>{epoch.current++;};},[eventId]);
  function change(next){setDraft(next);setNotice('');pending.current=null;preSave.current=null;}
  async function act(action) {
    if(lock.current||!enabled||!item||conflict)return;
    lock.current=true;setBusy(action);setError('');setNotice('');const current=epoch.current;
    try {
      // Reuse a failed request's operation ID when its exact payload is retried.
      let live=item;
      if(action==='promote'&&dirty){
        const saving=mailboxWrite(item,'draft',draft);
        const saveFingerprint=JSON.stringify({...saving,operation_id:undefined});
        if(preSave.current?.fingerprint===saveFingerprint)saving.operation_id=preSave.current.operationId;
        else preSave.current={fingerprint:saveFingerprint,operationId:saving.operation_id};
        const saved=await mailboxRequest('/'+encodeURIComponent(item.event_id),saving);
        preSave.current=null;
        live={...item,...saved};
        if(current!==epoch.current)return;setItem(live);setDraft(mailboxDraft(live));
      }
      const body=action==='promote'?mailboxPromotion(live,draft):mailboxWrite(item,action,action==='draft'?draft:undefined);
      const fingerprint=JSON.stringify({...body,operation_id:undefined});
      if(pending.current?.fingerprint===fingerprint)body.operation_id=pending.current.operationId;
      else pending.current={fingerprint,operationId:body.operation_id};
      const value=await mailboxRequest(action==='promote'?'-promote':'/'+encodeURIComponent(item.event_id),body);
      if(current!==epoch.current)return;
      if(action==='promote') {
        // Only a persisted completed queue receipt closes this editor.
        const completed=await mailboxRequest('/'+encodeURIComponent(item.event_id));
        if(current!==epoch.current)return;
        if(completed.status!=='completed')throw new Error('尚未确认升为 Scene，请重新读取信箱确认。草稿仍保留。');
        setItem(completed);setDraft(mailboxDraft(completed));setNotice('已升为 Scene，并从待处理信箱移出。');
      } else {
        setItem({...item,...value});setDraft(mailboxDraft({...item,...value}));
        setNotice(action==='remove'?'已移出信箱，Event 与已保存草稿仍保留。':'草稿已保存。');
      }
      pending.current=null;onChanged();
    } catch(err){if(current===epoch.current){setError(err.message);setConflict(!!err.conflict);}}
    finally{lock.current=false;if(current===epoch.current)setBusy('');}
  }
  const writable=enabled&&item?.status==='pending'&&item?.processable&&!conflict;
  const document=item?.event?.document;
  return <section className="mailbox-detail" aria-label="信箱详情" aria-busy={!!busy}>
    <header><h2>{item?.title||'Event 详情'}</h2><button type="button" aria-label="关闭信箱详情" disabled={!!busy} onClick={()=>{if(!dirty||window.confirm('尚有未保存的输入，离开详情吗？'))onClose();}}><X size={20}/></button></header>
    {busy==='loading'&&<p role="status">正在读取完整 Event 与原话…</p>}
    {error&&<p role="alert" className="mailbox-error">{error}</p>}
    {notice&&<p role="status">{notice}</p>}
    {(!item||conflict||error)&&<button type="button" disabled={!!busy} onClick={()=>read()}>重新读取</button>}
    {item&&<>
      <div className="mailbox-state">{eventRange(item)} · {item.status==='completed'?'已完成':item.status==='removed'?'未保留':'等待决定'}{item.topic?` · ${item.topic}`:''}{!item.processable&&item.status==='pending'?' · 当前 Event 不可升为 Scene，请核对其状态。':''}</div>
      {item.draft_stale&&<p role="alert" className="mailbox-error">Event 已更新，草稿仍基于旧版本。请核对完整 Event 和原话，再保存草稿确认。</p>}
      {item.scene_id&&<p className="mailbox-note">关联 Scene：{item.scene_id}</p>}
      <section className="mailbox-original"><h3>完整 Event</h3>{document?<><h4>{document.title}</h4><MarkdownProjection content={document.body_md||''}/></>:<p>这条 Event 当前不可读取。</p>}</section>
      {item.event?.evidence?.[0]&&<blockquote className="mailbox-quote">{item.event.evidence[0].content}</blockquote>}
      <details className="mailbox-evidence"><summary>查看原文 · {item.event?.evidence?.length||0} 条</summary>
        {(item.event?.evidence||[]).map((source,index)=><article key={source.binding_id||index}><small>{source.metadata?.role?identityName(source.metadata.role):'原始证据'}{source.metadata?.timestamp?` · ${source.metadata.timestamp}`:''}</small><p>{source.content}</p></article>)}
        {!item.event?.evidence?.length&&<p className="mailbox-note">没有已绑定的原话。</p>}
      </details>
      <form className="mailbox-editor" onSubmit={event=>{event.preventDefault();act('draft');}}>
        <h3>升为 Scene 的草稿</h3><p className="mailbox-note">手动编辑标题、正文与召回入口。保存草稿后仍需点“升为 Scene”。</p>
        <fieldset disabled={!writable||!!busy}>
          <label>标题<input value={draft.title} onChange={event=>change({...draft,title:event.target.value})}/></label>
          <label>正文<textarea rows={10} value={draft.body_md} onChange={event=>change({...draft,body_md:event.target.value})}/></label>
          <div className="scene-cues__editor"><h4>召回入口</h4><p className="mailbox-note">以后提到什么，希望这段记忆回来？可留空，最多 8 条，每条 80 字。</p>
            <div className="scene-cues__rows">{draft.cues.map((cue,index)=><label key={index}><span>{index+1}</span><input aria-label={`召回入口 ${index+1}`} value={cue} onChange={event=>change({...draft,cues:draft.cues.map((text,i)=>i===index?event.target.value:text)})}/><button type="button" aria-label={`删除召回入口 ${index+1}`} onClick={()=>change({...draft,cues:draft.cues.filter((_,i)=>i!==index)})}><Trash size={14}/></button></label>)}</div>
            {draft.cues.length<8&&<button type="button" onClick={()=>change({...draft,cues:[...draft.cues,'']})}><Plus size={14}/>增加一条</button>}
          </div>
        </fieldset>
        {item.status==='pending'&&<div className="mailbox-actions">
          <button type="submit" disabled={!writable||!!busy||(!dirty&&!item.draft_stale)||!validMailboxDraft(draft)}>{busy==='draft'?'正在保存…':'保存草稿'}</button>
          <button type="button" className="is-primary" disabled={!writable||!!busy||item.draft_stale||!validMailboxDraft(draft)} onClick={()=>act('promote')}>{busy==='promote'?'正在保存 Scene…':'升为 Scene'}</button>
          <button type="button" disabled={!enabled||!!busy||conflict} onClick={()=>{if(!dirty||window.confirm('尚未保存的输入不会保留。仍要移出信箱吗？'))act('remove');}}>移出信箱</button>
        </div>}
        {item.status==='pending'&&!validMailboxDraft(draft)&&<p className="mailbox-note">请填写标题和正文；召回入口最多 8 条，每条不超过 80 字。</p>}
      </form>
    </>}
  </section>;
}

export function EventMailboxPage({onOpenSettings,onOpenMemory}) {
  const [config,setConfig]=useState(null),[rows,setRows]=useState([]),[status,setStatus]=useState('pending'),[selected,setSelected]=useState(null);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[next,setNext]=useState(null);
  const generation=useRef(0);
  async function load(position=0) {
    const more=position!==0;
    const pagination=typeof position==='string'?`cursor=${encodeURIComponent(position)}`:`offset=${position}`;
    const current=++generation.current;setBusy(true);setError('');
    try {
      const [settings,result]=await Promise.all([instanceSettings(),mailboxRequest(`?status=${status}&limit=20&${pagination}`)]);
      if(current!==generation.current)return;setConfig(settings);setRows(old=>more?[...new Map([...old,...result.items].map(row=>[row.event_id,row])).values()]:result.items);setNext(result.has_more?(result.next_cursor??result.next_offset):null);
    } catch(err){if(current===generation.current)setError(err.message);}
    finally{if(current===generation.current)setBusy(false);}
  }
  useEffect(()=>{setRows([]);setSelected(null);load();return()=>{generation.current++;};},[status]);
  function navigate(action){if(window.dispatchEvent(new Event('serein:before-navigation',{cancelable:true})))action();}
  return <div className="mailbox-layout">
    <header className="mailbox-header"><p>一起经历，再亲手记住</p><h1>信箱</h1><span>新 Event 会直接来到这里，读过之后再决定怎样留下。</span></header>
    {config&&!config.features.event_to_scene&&<div className="mailbox-disabled"><p>“Event 升为 Scene”尚未开启。已有信箱和草稿会保留。</p><button type="button" onClick={onOpenSettings}>打开功能设置</button></div>}
    <div className={`mailbox-columns${selected?' has-detail':''}`}>
      <section className="mailbox-list" aria-label="信箱列表">
        <div className="mailbox-toolbar"><label>查看<select aria-label="信箱状态" value={status} onChange={event=>navigate(()=>setStatus(event.target.value))}><option value="pending">待处理</option><option value="completed">已完成</option><option value="removed">未保留</option></select></label><button type="button" aria-label="刷新信箱" disabled={busy} onClick={()=>load()}><ArrowClockwise size={17}/></button></div>
        {error&&<p role="alert" className="mailbox-error">{error}<button type="button" disabled={busy} onClick={()=>load()}>重试</button></p>}
        {busy&&<p role="status">正在读取信箱…</p>}
        {!busy&&!error&&!rows.length&&<div className="mailbox-empty"><EnvelopeSimple size={30} weight="light"/><p>{status==='pending'?'信箱现在是空的。':'这里还没有记录。'}</p><span>生成的 Event 会自动出现在信箱里。</span><button type="button" onClick={onOpenMemory}>去看 Event</button></div>}
        <ol>{rows.map(row=><li key={row.event_id}><button type="button" aria-pressed={selected===row.event_id} onClick={()=>{if(selected!==row.event_id)navigate(()=>setSelected(row.event_id));}}><small>{[row.topic,row.evidence_count?`${row.evidence_count} 条原文`:''].filter(Boolean).join(' · ')}</small><span>{row.title||'未命名 Event'}</span>{row.body_preview&&<p className="mailbox-body-preview">{row.body_preview}</p>}<small>{eventRange(row)}{!row.processable&&status==='pending'?' · 需核对状态':''}</small></button></li>)}</ol>
        {next!=null&&<button type="button" disabled={busy} onClick={()=>load(next)}>继续读取</button>}
      </section>
      {selected?<MailboxDetail key={selected} eventId={selected} enabled={!!config?.features.event_to_scene} onClose={()=>setSelected(null)} onChanged={()=>load()}/>:<div className="mailbox-placeholder"><p>选一封，读过原话再写。</p><span>草稿、收藏和换窗选择各自保存。</span></div>}
    </div>
  </div>;
}

import {useEffect,useRef,useState} from 'react';
import {ArrowClockwise,CheckSquare} from '@phosphor-icons/react';
import {instanceSettings,identityName} from '../storage/instanceStore.js';
import {mailboxRequest,mailboxWrite} from '../utils/eventMailbox.js';
import {formatSourceTime} from '../utils/sourceTime.js';
import {MarkdownProjection} from '../components/MarkdownProjection.jsx';
import './event-mailbox-page.css';

const dateText=value=>value?formatSourceTime(value,{includeYear:true}):'';
function sourceRange(item){const start=dateText(item.source_started_at),end=dateText(item.source_ended_at);return end&&end!==start?`${start} — ${end}`:start;}
function CandidateContent({row,enabled,busy,onDecide}){
  const [item,setItem]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(true),[evidenceOpen,setEvidenceOpen]=useState(false);
  const generation=useRef(0);
  async function read(){
    const current=++generation.current;setLoading(true);setError('');
    try{const value=await mailboxRequest('/'+encodeURIComponent(row.event_id));if(current===generation.current)setItem(value);}
    catch(err){if(current===generation.current)setError(err.message);}
    finally{if(current===generation.current)setLoading(false);}
  }
  useEffect(()=>{read();return()=>{generation.current++;};},[row.event_id,row.event_revision,row.queue_revision]);
  const evidence=item?.event?.evidence||[],document=item?.event?.document;
  const decidable=enabled&&!busy&&!loading&&item&&!error&&item.lifecycle==='active'&&!item.scene_id;
  return <section className="mailbox-card-content" aria-label="沉淀详情" aria-busy={loading}>
    {loading&&<p role="status">正在翻原文…</p>}
    {error&&<p role="alert" className="mailbox-error">{error} <button type="button" onClick={read}>重新读取</button></p>}
    {item&&<>
      <p className="mailbox-intention">选入后，可交给 {identityName('assistant')} 根据原文写成 Scene。跳过不会删除 Event 或原文。</p>
      {sourceRange(item)&&<p className="mailbox-source-range">{sourceRange(item)}</p>}
      {document?<div className="mailbox-original"><MarkdownProjection content={document.body_md||''}/></div>:<p>这条 Event 当前不可读取。</p>}
      {evidence[0]&&<blockquote className="mailbox-quote">{evidence[0].content}</blockquote>}
      <div className="mailbox-evidence">
        <button type="button" className="mailbox-evidence-toggle" aria-expanded={evidenceOpen} onClick={()=>setEvidenceOpen(value=>!value)}>{evidenceOpen?'收起原文':`查看原文 · ${evidence.length} 条`}</button>
        {evidenceOpen&&<div className="mailbox-evidence-list">{evidence.map((source,index)=><article key={source.binding_id||index}><header><span>{source.metadata?.role?identityName(source.metadata.role):'原文'}</span><time>{dateText(source.metadata?.timestamp||source.metadata?.created_at||source.metadata?.occurred_at)}</time></header><p>{source.content}</p></article>)}{!evidence.length&&<p>没有保留的原文。</p>}</div>}
      </div>
      <footer className="mailbox-decision-actions">
        {item.status==='pending'?<><button type="button" disabled={!decidable} onClick={()=>onDecide(item,'remove')}>跳过</button><button type="button" className="is-primary" disabled={!decidable} onClick={()=>onDecide(item,'select')}>选入</button></>:item.status==='completed'?<p>已经写成 Scene</p>:<><button type="button" disabled={!decidable} onClick={()=>onDecide(item,'restore')}>重新选择</button>{item.status==='approved'&&<p>已选入，等待 {identityName('assistant')} 书写。</p>}</>}
      </footer>
    </>}
  </section>;
}
export function EventMailboxPage({onOpenSettings}){
  const [config,setConfig]=useState(null),[rows,setRows]=useState([]),[counts,setCounts]=useState({}),[status,setStatus]=useState('pending');
  const [expanded,setExpanded]=useState(null),[selected,setSelected]=useState(new Set()),[batch,setBatch]=useState(false);
  const [loading,setLoading]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[next,setNext]=useState(null);
  const generation=useRef(0),lock=useRef(false),requests=useRef(new Map());
  async function load(position=0,limit=20){
    const current=++generation.current,more=position!==0;setLoading(true);setError('');
    try{
      const pagination=typeof position==='string'?`cursor=${encodeURIComponent(position)}`:`offset=${position}`;
      const [settings,result]=await Promise.all([instanceSettings(),mailboxRequest(`?status=${status}&limit=${limit}&${pagination}`)]);
      if(current!==generation.current)return;
      setConfig(settings);setCounts(result.counts||{});setRows(old=>more?[...new Map([...old,...result.items].map(row=>[row.event_id,row])).values()]:result.items);setNext(result.has_more?(result.next_cursor??result.next_offset):null);
    }catch(err){if(current===generation.current)setError(err.message);}
    finally{if(current===generation.current)setLoading(false);}
  }
  useEffect(()=>{setRows([]);setSelected(new Set());setExpanded(null);setBatch(false);setNotice('');requests.current.clear();load();return()=>{generation.current++;};},[status]);
  const enabled=!!config?.features.event_to_scene;
  async function decide(items,action){
    if(lock.current||!enabled||!items.length)return;
    lock.current=true;setBusy(true);setError('');setNotice('');const failures=[],succeeded=[];
    try{
      for(const item of items){
        const key=`${item.event_id}:${action}:${item.event_revision}:${item.queue_revision}`;
        const request=requests.current.get(key)||mailboxWrite(item,action);requests.current.set(key,request);
        try{const saved=await mailboxRequest('/'+encodeURIComponent(item.event_id),request);if(saved.status!==({select:'approved',remove:'removed',restore:'pending'})[action])throw new Error('尚未确认决定已保存，请重新读取或重试。');requests.current.delete(key);succeeded.push(item.event_id);}
        catch(err){failures.push({item,error:err});if(err.conflict)requests.current.delete(key);}
      }
      setSelected(old=>new Set([...old].filter(id=>!succeeded.includes(id))));
      await load(0,Math.min(100,Math.max(20,rows.length)));
      setRows(current=>[...new Map([...current,...failures.filter(({error})=>!error.conflict).map(({item})=>item)].map(item=>[item.event_id,item])).values()]);
      setNotice(succeeded.length?`${succeeded.length} 件已${action==='select'?`选入，等待 ${identityName('assistant')} 书写`:action==='remove'?'跳过，Event 仍保留':'放回待选'}。`:'');
      if(failures.length)setError(`${failures.length} 件未完成：${failures.map(({item,error})=>`${item.title}：${error.message}`).join('；')}`);
    }finally{lock.current=false;setBusy(false);}
  }
  const selectable=rows.filter(row=>row.lifecycle==='active'&&!row.scene_id);
  const chosen=selectable.filter(row=>selected.has(row.event_id));
  const selectedRow=rows.find(row=>row.event_id===expanded);
  function toggle(id){setSelected(old=>{const value=new Set(old);value.has(id)?value.delete(id):value.add(id);return value;});}
  return <div className="mailbox-layout">
    <header className="mailbox-header"><p>把 Event 升为 Scene</p><h1>沉淀</h1></header>
    {!enabled&&config&&<div className="mailbox-disabled"><p>“Event 升为 Scene”尚未开启。已有决定会保留。</p><button type="button" onClick={onOpenSettings}>打开功能设置</button></div>}
    <nav className="mailbox-tabs" aria-label="沉淀状态">{[['pending','待选'],['retained','已选'],['removed','已跳过']].map(([value,label])=><button type="button" key={value} aria-pressed={status===value} disabled={busy} onClick={()=>setStatus(value)}>{label}<span>{counts[value]||0}</span></button>)}</nav>
    <div className="mailbox-workbench">
    <section className="mailbox-list-panel" aria-label="候选列表视窗">
    <div className="mailbox-toolbar"><button type="button" aria-label="刷新沉淀" disabled={busy||loading} onClick={()=>load()}><ArrowClockwise size={17}/></button><button type="button" disabled={busy||!enabled} onClick={()=>{setBatch(value=>!value);setSelected(new Set());}}>{batch?'取消批量':'批量操作'}</button></div>
    {batch&&<div className="mailbox-batch" aria-label="批量决定"><label><input type="checkbox" aria-label="全选已加载候选" disabled={busy||!selectable.length} checked={!!selectable.length&&chosen.length===selectable.length} onChange={event=>setSelected(event.target.checked?new Set(selectable.map(row=>row.event_id)):new Set())}/>全选已加载候选</label><span>已选 {chosen.length} 件</span>{status==='pending'?<><button type="button" disabled={busy||!chosen.length||!enabled} onClick={()=>decide(chosen,'remove')}>批量跳过</button><button type="button" className="is-primary" disabled={busy||!chosen.length||!enabled} onClick={()=>decide(chosen,'select')}>批量选入</button></>:<button type="button" disabled={busy||!chosen.length||!enabled} onClick={()=>decide(chosen,'restore')}>批量重选</button>}</div>}
    {error&&<p role="alert" className="mailbox-error">{error}<button type="button" disabled={busy||loading} onClick={()=>load()}>重新读取</button></p>}
    {notice&&<p role="status" className="mailbox-notice">{notice}</p>}
    {loading&&<p role="status">正在读取候选…</p>}
    {!loading&&!rows.length&&!error&&<div className="mailbox-empty"><CheckSquare size={28} weight="light"/><p>{status==='pending'?'没有待选的 Event。':'这里还没有记录。'}</p></div>}
    <ol className="mailbox-candidates" aria-label="沉淀列表">{rows.map(row=><li className="mailbox-card" key={row.event_id}>
      {batch&&row.lifecycle==='active'&&!row.scene_id&&<label className="mailbox-card-selection"><input type="checkbox" aria-label={`选择 ${row.title}`} checked={selected.has(row.event_id)} disabled={busy} onChange={()=>toggle(row.event_id)}/>选择</label>}
      <button type="button" className="mailbox-card-heading" aria-expanded={expanded===row.event_id} disabled={busy} onClick={()=>setExpanded(value=>value===row.event_id?null:row.event_id)}><strong>{row.title||'未命名 Event'}</strong><span><time>{dateText(row.created_at)}</time><small>{row.status==='completed'?'已写成 Scene':'Event'}</small></span></button>
    </li>)}</ol>
    {next!=null&&<button type="button" className="mailbox-more" disabled={loading||busy} onClick={()=>load(next)}>继续读取</button>}
    </section>
    <section className="mailbox-detail-panel" aria-label="候选阅读视窗">
      {selectedRow?<><header className="mailbox-reader-heading"><h2>{selectedRow.title}</h2><button type="button" disabled={busy} onClick={()=>setExpanded(null)}>收起</button></header><p className="mailbox-reader-date">{dateText(selectedRow.created_at)} · {selectedRow.status==='completed'?'已写成 Scene':'Event'}</p><CandidateContent key={selectedRow.event_id} row={selectedRow} enabled={enabled} busy={busy} onDecide={(item,action)=>decide([item],action)}/></>:<p className="mailbox-reader-empty">选一件，听听那时说过的话。</p>}
    </section>
    </div>
  </div>;
}

import {useEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {X} from '@phosphor-icons/react';

export function ResumeMemoryPicker({ids,disabled,onChange}) {
  const dialog=useRef(null);
  const [open,setOpen]=useState(false),[draft,setDraft]=useState([]),[kind,setKind]=useState('event');
  const [query,setQuery]=useState(''),[date,setDate]=useState(''),[offset,setOffset]=useState(0);
  const [page,setPage]=useState({items:[],total:0}),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [selectedOnly,setSelectedOnly]=useState(false),[retry,setRetry]=useState(0);
  const selectedKey=selectedOnly?JSON.stringify(draft):'';
  useEffect(()=>{
    if(!open)return;
    const abort=new AbortController();setBusy(true);setError('');
    const timer=setTimeout(async()=>{
      try {
        if(selectedOnly&&!draft.length){setPage({items:[],total:0});return;}
        const params=new URLSearchParams({kind:selectedOnly?'':kind,q:query,date,offset:String(offset)});
        if(selectedOnly)draft.forEach(id=>params.append('ids',id));
        const response=await fetch('/__serein/settings/resume-candidates?'+params,{signal:abort.signal});
        if(!response.ok)throw new Error('暂时没有读到记忆，请重试。');
        const result=await response.json();if(!abort.signal.aborted)setPage(result);
      } catch(error) {if(!abort.signal.aborted)setError(error.message);}
      finally{if(!abort.signal.aborted)setBusy(false);}
    },180);
    return()=>{clearTimeout(timer);abort.abort();};
  },[open,kind,query,date,offset,selectedOnly,selectedKey,retry]);
  function begin(){setDraft([...ids]);setKind('event');setSelectedOnly(false);setQuery('');setDate('');setOffset(0);setOpen(true);dialog.current.showModal();}
  function close(){dialog.current.close();setOpen(false);}
  function toggle(id){setDraft(current=>current.includes(id)?current.filter(key=>key!==id):current.length<200?[...current,id]:current);}
  return <>
    <button className="resume-picker-trigger" type="button" disabled={disabled} onClick={begin}>选择内容 · {ids.length} 条</button>
    {createPortal(<dialog ref={dialog} className="resume-picker" aria-labelledby="resume-picker-title" onCancel={()=>setOpen(false)}>
      <header><div><span>RESUME</span><h3 id="resume-picker-title">自选事件 / Scene</h3></div><button type="button" aria-label="关闭选择器" onClick={close}><X size={22}/></button></header>
      <p>选好要带进新窗口的内容，确认后保存功能设置。</p>
      <div className="resume-picker-tabs" role="tablist" aria-label="续接内容类型">
        {[['event','事件'],['scene','Scene'],['selected','已选内容']].map(([key,label])=><button type="button" role="tab" key={key} aria-selected={key==='selected'?selectedOnly:!selectedOnly&&kind===key}
          onClick={()=>{setSelectedOnly(key==='selected');if(key!=='selected')setKind(key);setOffset(0);}}>{label}</button>)}
      </div>
      <div className="resume-picker-filters"><input type="search" aria-label="搜索续接记忆" placeholder="搜索标题或正文" value={query} onChange={event=>{setQuery(event.target.value);setOffset(0);}}/>
        <input type="date" aria-label="筛选记录日期" value={date} onChange={event=>{setDate(event.target.value);setOffset(0);}}/></div>
      <div className="resume-picker-actions"><span>已选 {draft.length} / 200 条</span><button type="button" disabled={busy||!!error} onClick={()=>setDraft(current=>[...new Set([...current,...page.items.map(item=>item.id)])].slice(0,200))}>选中本页</button><button type="button" onClick={()=>setDraft([])}>清空选择</button></div>
      <div className="resume-picker-list" aria-busy={busy}>
        {error?<p role="alert">{error}<button type="button" onClick={()=>setRetry(value=>value+1)}>重试</button></p>:busy?<p role="status">读取中…</p>:page.items.length?page.items.map(item=><article key={item.id}>
          <label><input type="checkbox" checked={draft.includes(item.id)} disabled={!draft.includes(item.id)&&draft.length>=200} onChange={()=>toggle(item.id)}/><span><small>{item.kind==='event'?'事件':'Scene'} · {item.date}</small><strong>{item.title||'未命名'}</strong><span className="resume-picker-preview">{item.body_md.slice(0,220)}</span></span></label>
          <details><summary>读全文</summary><p>{item.body_md}</p></details>
        </article>):<p>{selectedOnly?'没有符合筛选的已选内容。已删除或归档的内容不会续接，可清空后重新选择。':'没有符合筛选的内容。'}</p>}
      </div>
      <footer><div><button type="button" disabled={busy||offset===0} onClick={()=>setOffset(value=>Math.max(0,value-30))}>上一页</button><span>{Math.floor(offset/30)+1} / {Math.max(1,Math.ceil(page.total/30))}</span><button type="button" disabled={busy||!page.has_more} onClick={()=>setOffset(value=>value+30)}>下一页</button></div><button type="button" onClick={()=>{onChange(draft);close();}}>确认选择 · {draft.length} 条</button></footer>
    </dialog>,document.body)}
  </>;
}

// The shared authenticated picker endpoint has an explicit link purpose. The
// original resume selection above keeps its existing kinds and save semantics.
export const linkKindLabels = {event:'Event',scene:'Scene',diary:'日记',darkroom:'暗房',upload:'已上传文件'};

export async function searchLinkCandidates({kind,query='',date='',offset=0,signal},fetchImpl=fetch) {
  if (!Object.hasOwn(linkKindLabels,kind)) throw new Error('请选择一种材料类型。');
  const params=new URLSearchParams({purpose:'link',kind,q:query,date,offset:String(offset),limit:'30'});
  signal?.throwIfAborted();
  const response=await fetchImpl('/__serein/settings/resume-candidates?'+params,{signal,cache:'no-store'});
  const result=await response.json().catch(()=>null);
  signal?.throwIfAborted();
  if (!response.ok) throw new Error('查找暂不可用，请确认后端已更新后重试。');
  if (result?.purpose!=='link') throw new Error('后端尚不支持材料查找，请更新后端后重试。');
  if (!Array.isArray(result.items)||!Number.isSafeInteger(result.total)||result.total<0||
      typeof result.has_more!=='boolean'||result.items.some(item=>!item||item.kind!==kind||
        typeof item.id!=='string'||!item.id||typeof item.title!=='string'||typeof item.excerpt!=='string'||typeof item.date!=='string')) {
    throw new Error('没有读到有效的查找结果，请重试。');
  }
  return result;
}

export function linkCandidateAvailable(item,kind,blockedIds=[]) {
  return !!item&&item.kind===kind&&typeof item.id==='string'&&!!item.id&&
    !blockedIds.some(id=>String(id)===item.id);
}

function LinkCandidateDialog({kind,label,blockedIds,blockedLabel,onSelect,onClose}) {
  const dialog=useRef(null);
  const [query,setQuery]=useState(''),[date,setDate]=useState(''),[offset,setOffset]=useState(0);
  const [picked,setPicked]=useState(null),[retry,setRetry]=useState(0);
  const [state,setState]=useState({key:'',busy:true,items:[],total:0,error:''});
  const key=JSON.stringify([kind,query,date,offset,retry]);
  const current=state.key===key;
  const busy=!current||state.busy;
  const available=linkCandidateAvailable(picked,kind,blockedIds);
  useEffect(()=>{
    const trigger=document.activeElement;
    const node=dialog.current;
    node.showModal();
    return()=>{node.close();if(trigger?.isConnected)trigger.focus({preventScroll:true});};
  },[]);
  useEffect(()=>{
    const abort=new AbortController();
    setState({key,busy:true,items:[],total:0,error:''});
    const timer=setTimeout(async()=>{
      try {
        const result=await searchLinkCandidates({kind,query,date,offset,signal:abort.signal});
        if(!abort.signal.aborted)setState({...result,key,busy:false,error:''});
      }catch(error){
        if(!abort.signal.aborted)setState({key,busy:false,items:[],total:0,error:error.message});
      }
    },200);
    return()=>{clearTimeout(timer);abort.abort();};
  },[kind,query,date,offset,retry]);
  function changePage(next){setOffset(next);setPicked(null);}
  return createPortal(<dialog ref={dialog} className="resume-picker" aria-label={label}
    onCancel={event=>{event.preventDefault();onClose();}}
    onKeyDown={event=>{if(event.key==='Escape')event.stopPropagation();}}>
    <header><div><span>查找 · {linkKindLabels[kind]}</span><h3>{label}</h3></div>
      <button type="button" aria-label="关闭材料查找" onClick={onClose}><X size={22}/></button></header>
    <p>输入部分标题或正文关键词，多个关键词用空格分开。选中后再确认，不会自动关联或调用模型。</p>
    <div className="resume-picker-filters">
      <input type="search" autoFocus maxLength={200} aria-label="标题或关键词" placeholder="标题 / 正文关键词 / ID"
        value={query} onChange={event=>{setQuery(event.target.value);changePage(0);}}/>
      <input type="date" aria-label="材料日期" value={date} onChange={event=>{setDate(event.target.value);changePage(0);}}/>
    </div>
    <div className="resume-picker-list" aria-busy={busy}>
      {busy?<p role="status">正在查找…</p>:state.error?<p role="alert">{state.error} <button type="button"
        onClick={()=>{setPicked(null);setRetry(value=>value+1);}}>重试</button></p>:state.items.length?state.items.map(item=>{
        const blocked=!linkCandidateAvailable(item,kind,blockedIds);
        return <article key={`${item.kind}:${item.id}`}>
          <label><input type="radio" style={{width:16,height:16,flexShrink:0}} name="link-candidate" disabled={blocked} checked={picked?.id===item.id}
            onChange={()=>setPicked(item)}/><span><small>{linkKindLabels[item.kind]} · {item.date||'日期未记录'}{blocked?` · ${blockedLabel}`:''}</small>
            <strong>{item.title}</strong><span className="resume-picker-preview">{item.excerpt||'（正文为空）'}</span>
            <code style={{overflowWrap:'anywhere'}}>ID：{item.id}</code></span></label>
        </article>;
      }):<p>没有找到匹配的可用材料。可尝试更短的关键词；已归档、删除或尚未解锁的内容不会列出。</p>}
    </div>
    <footer><div><button type="button" disabled={busy||offset===0} onClick={()=>changePage(Math.max(0,offset-30))}>上一页</button>
      <span>{busy?'查找中':`${Math.floor(offset/30)+1} / ${Math.max(1,Math.ceil(state.total/30))} · ${state.total} 条`}</span>
      <button type="button" disabled={busy||!!state.error||!state.has_more} onClick={()=>changePage(offset+30)}>下一页</button></div>
      <button type="button" disabled={busy||!!state.error||!available} onClick={()=>{
        if(!busy&&!state.error&&available){onSelect(picked);onClose();}
      }}>确认选择</button></footer>
  </dialog>,document.body);
}

export function LinkCandidatePicker({kind,label='查找材料',disabled=false,blockedIds=[],blockedLabel='已在拟绑定中',onSelect}) {
  const [open,setOpen]=useState(false);
  useEffect(()=>{if(disabled)setOpen(false);},[disabled]);
  return <>
    <button className="resume-picker-trigger" type="button" disabled={disabled} aria-haspopup="dialog" aria-label={label}
      onClick={()=>setOpen(true)}>按标题 / 关键词查找</button>
    {open&&!disabled&&<LinkCandidateDialog key={kind} kind={kind} label={label} blockedIds={blockedIds}
      blockedLabel={blockedLabel} onSelect={onSelect} onClose={()=>setOpen(false)}/>}
  </>;
}

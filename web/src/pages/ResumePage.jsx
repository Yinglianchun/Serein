import {useEffect,useRef,useState} from 'react';
import {Copy} from '@phosphor-icons/react';
import {MarkdownProjection} from '../components/MarkdownProjection.jsx';
import {ResumeSelection} from '../components/ResumeSelection.jsx';
import {instanceSettings} from '../storage/instanceStore.js';
import {appendResumePage,resumeMaterials,resumePagesText} from '../utils/resumePages.js';
import './resume-page.css';

const labels={latest_shadow:'窗影',favorite:'收藏 Scene',selected_memory:'自选记忆',recent_event:'最近事件',recent_original:'最近原话',pending_original:'待整理原话'};

export function ResumePage({onOpenSettings}) {
  const [config,setConfig]=useState(null),[selection,setSelection]=useState(null),[pages,setPages]=useState([]);
  const [busy,setBusy]=useState('loading'),[error,setError]=useState(''),[status,setStatus]=useState('');
  const [previewing,setPreviewing]=useState(false),[previewError,setPreviewError]=useState('');
  const version=useRef(0),previewVersion=useRef(0),request=useRef(null);
  const previewPanel=useRef(null),previewBody=useRef(null);
  const enabled=!!config?.features.resume;
  const valid=!!selection&&Number.isInteger(selection.recent_original_limit)&&selection.recent_original_limit>=1&&selection.recent_original_limit<=50;
  const dirty=!!config&&JSON.stringify(selection)!==JSON.stringify(config.resume);
  const last=pages.at(-1),items=resumeMaterials(pages);
  function cancelPreview(){previewVersion.current++;request.current?.abort();}
  async function reload() {
    const current=++version.current;cancelPreview();setPages([]);setPreviewing(false);setBusy('loading');setError('');
    try {const value=await instanceSettings();if(current===version.current){setConfig(value);setSelection(value.resume);setPages([]);setStatus('');}}
    catch(err){if(current===version.current)setError(err.message);}
    finally{if(current===version.current)setBusy('');}
  }
  useEffect(()=>{reload();return()=>{version.current++;cancelPreview();};},[]);
  useEffect(()=>{
    setPages([]);setPreviewError('');setPreviewing(enabled&&valid);
    if(!enabled||!valid)return;
    const timer=setTimeout(()=>preview(),180);
    return()=>{clearTimeout(timer);cancelPreview();};
  },[selection,enabled]);
  useEffect(()=>{
    const panel=previewPanel.current,body=previewBody.current;
    if(!panel||!body)return;
    function wheel(event){
      if(event.ctrlKey||!event.deltaY)return;
      event.preventDefault();
      body.scrollTop+=event.deltaY*(event.deltaMode===1?16:event.deltaMode===2?body.clientHeight:1);
    }
    panel.addEventListener('wheel',wheel,{passive:false});
    return()=>panel.removeEventListener('wheel',wheel);
  },[enabled]);
  function change(value){cancelPreview();setSelection(value);setPages([]);setStatus('');}
  async function save(event) {
    event.preventDefault();const current=++version.current;setBusy('saving');setError('');
    const {mode,command_enabled,mcp_enabled,...contentSelection}=selection;
    try {const value=await instanceSettings({expected_version:config.settings_version,resume:contentSelection});
      if(current===version.current){setConfig(value);setSelection(value.resume);setStatus('已保存，下次续接会使用这份选择。');}}
    catch(err){if(current===version.current)setError(err.message);}
    finally{if(current===version.current)setBusy('');}
  }
  async function preview(cursor='') {
    cancelPreview();const current=previewVersion.current;
    request.current=new AbortController();setPreviewing(true);setPreviewError('');
    const {mode,command_enabled,mcp_enabled,...contentSelection}=selection;
    try {
      const response=await fetch('/__serein/resume',{method:'POST',cache:'no-store',signal:request.current.signal,
        headers:{'Content-Type':'application/json'},body:JSON.stringify({window_id:'main',cursor,selection:contentSelection})});
      const value=await response.json();
      if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'续接资料暂时不可用，请重新读取。');
      const next=appendResumePage(pages,value,cursor);if(current===previewVersion.current)setPages(next);
    } catch(err){if(current===previewVersion.current&&err.name!=='AbortError'){setPages([]);setPreviewError(err.message);}}
    finally{if(current===previewVersion.current)setPreviewing(false);}
  }
  async function copy() {
    try{await navigator.clipboard.writeText(resumePagesText(pages));setStatus('完整续接资料已复制。');}
    catch(err){setError(err.message||'复制未完成，请允许剪贴板访问后重试。');}
  }
  return <div className="resume-layout">
    <header className="resume-header"><p>接着上一窗</p><h1>换窗</h1><span>把还想留在身边的，带去下一次见面。</span></header>
    {!config?<div role={error?'alert':'status'}>{error||'正在读取换窗设置…'}{error&&<button type="button" onClick={reload}>重新读取</button>}</div>:
      !enabled?<div className="resume-disabled"><h2>还没有开启续接</h2><p>在功能设置开启“开窗续接”，再分别开启网关 /resume 指令或 MCP 工具，也可同时开启。已有选择会保留。</p><button type="button" onClick={onOpenSettings}>打开功能设置</button><button type="button" onClick={reload}>重新读取</button></div>:
      <div className="resume-columns"><form className="resume-options" onSubmit={save}>
        <ResumeSelection selection={selection} onChange={change} disabled={!!busy} windowShadows={config.features.window_shadows}/>
        <p className="resume-note">{config.resume.command_enabled?(config.resume.mcp_enabled?'网关 /resume 指令与 MCP resume 工具均已开启。':'网关 /resume 指令已开启，MCP resume 工具已关闭。'):(config.resume.mcp_enabled?'MCP resume 工具已开启，网关 /resume 指令已关闭。':'两个续接入口均已关闭，仍可预览与复制资料。')}<button type="button" className="resume-settings-link" onClick={onOpenSettings}>修改续接方式</button></p>
        <div className="resume-actions"><button type="submit" disabled={!!busy||!dirty||!valid}>{busy==='saving'?'正在保存…':'保存选择'}</button><button type="button" disabled={!!busy} onClick={reload}>重新读取</button></div>
      </form><section ref={previewPanel} className="resume-preview" aria-labelledby="resume-preview-title">
        <header><div><p>当前选择会读到</p><h2 id="resume-preview-title">续接资料</h2></div><button type="button" disabled={!!busy||previewing||!valid} onClick={()=>preview()}>{previewing?'正在读取…':'重新预览'}</button></header>
        {!valid&&<p className="resume-note">原话条数需填写 1–50 的整数，填好后会自动预览。</p>}
        {(dirty||!!pages.length)&&<p className="resume-note">{dirty&&'当前选择尚未保存。 '}{!!pages.length&&<>已读 {items.filter(item=>item.body_complete).length} / {last.total_items} 条 · {pages.length} 页{last.has_more?' · 还有资料':' · 已完整读取'}</>}</p>}
        <div ref={previewBody} className="resume-preview-scroll" tabIndex={0} role="region" aria-label="续接资料正文">
        {!pages.length?<div className="resume-empty"><p>一窗结束，另一窗接起。</p><span>{previewing?'正在读取当前选择…':'勾选要带走的内容，预览会自动更新。'}</span></div>:<>
          <div className="resume-materials">{items.length?items.map(item=><article key={item.id}>
            <p>{labels[item.section]||item.section}</p><h3>{item.title||'未命名'}</h3><small>{item.id}{item.created_at&&<> · <time dateTime={item.created_at}>{item.created_at.slice(0,10)}</time></>}</small>
            {item.kind==='raw'&&<p className="resume-note">{item.role==='user'?'用户':'助手'}</p>}
            <MarkdownProjection content={item.body_md}/>{!item.body_complete&&<p className="resume-note">这段文字还有下一页。</p>}
          </article>):<p className="resume-note">当前选择没有可读资料。</p>}</div>
          {pages[0].handoff&&<article className="resume-handoff"><h3>续接便笺</h3><MarkdownProjection content={pages[0].handoff.body}/></article>}
        </>}
        </div>
        {!!pages.length&&<div className="resume-actions">{last.has_more&&<button type="button" disabled={!!busy||previewing} onClick={()=>preview(last.next_cursor)}>继续读取</button>}
            <button type="button" disabled={!!busy||previewing||last.has_more} onClick={copy}><Copy size={16}/>复制完整资料</button></div>
        }
      </section></div>}
    {(error||previewError)&&config&&<p role="alert" className="resume-feedback">{error||previewError}</p>}{status&&<p role="status" className="resume-feedback">{status}</p>}
  </div>;
}

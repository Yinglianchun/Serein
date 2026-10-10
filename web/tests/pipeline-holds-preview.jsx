import React from 'react';
import {createRoot} from 'react-dom/client';
import {PipelineSettings} from '../src/components/PipelineSettings.jsx';
import '../src/styles.css';
// This preview intercepts every request; it never reaches a backend or model.
const holds=[{scope:'synthetic-chat-A',batch_id:'route:synthetic-repair',hold_status:'needs_repair',reason:'历史 producer frame 跨过 03:00，无法证明冻结范围'},
 {scope:'synthetic-chat-B',batch_id:'route:synthetic-backoff',hold_status:'retry_wait',reason:'合成网络错误（累计 2 次）',next_retry_at:'2026-10-10T00:45:00Z'}];
window.fetch=async(url,options={})=>{
 let data;
 if(url==='/__serein/settings')data={identity:{user_name:'User',ai_name:'AI'},features:{},pipeline:{auto_enabled:true}};
 else if(url==='/__serein/imports')data={items:[],tagging:{}};
 else if(url==='/__serein/pipeline/status')data={status:'blocked',stage:'blocked',blocked_scopes:holds,paused_batches:[],result:{status:'blocked'}};
 else if(url==='/__serein/pipeline/next')data={status:'queued'};
 else if(url==='/__serein/pipeline/rebuild')data={status:'rebuilt'};
 else throw Error('Unexpected synthetic request '+url);
 return {ok:true,json:async()=>data};
};
createRoot(document.getElementById('root')).render(<main className="settings-page" style={{maxWidth:900,margin:'48px auto',padding:24}}><h1>归线与调度 · 合成验收</h1><PipelineSettings/></main>);

import {useEffect,useState} from 'react';

async function call(path='',body){
  const response=await fetch('/__serein/migration'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result=await response.json();
  if(!response.ok)throw new Error(result.detail || '旧库操作未完成');
  return result;
}
const stages={queued:'等待处理',starting:'准备',history:'梦境、窗影、日记与暗房',companion:'心绪与备忘',bodies:'导入正文',tagging:'打标、实体与 cues',edges:'转换旧边',vectors:'准备向量',cue_bindings:'绑定线索',completed:'已完成'};
const states={queued:'等待处理',running:'处理中',completed:'已完成',paused:'已暂停',interrupted:'已中断',failed:'未完成',idle:'待确认'};

export function LegacyMigration(){
  const [items,setItems]=useState([]),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[dragging,setDragging]=useState(false);
  const [sourcePath,setSourcePath]=useState(''),[installation,setInstallation]=useState(null);
  const [names,setNames]=useState({user_name:'',ai_name:'',aliases:''}),[confirmed,setConfirmed]=useState(false);
  const [generateCues,setGenerateCues]=useState(true),[cueChoices,setCueChoices]=useState({});
  const [backupBusy,setBackupBusy]=useState(false),[backupMessage,setBackupMessage]=useState('');
  async function downloadBackup(){
    if(backupBusy)return;
    setBackupBusy(true);setBackupMessage('正在准备并接收数据库备份，请保持页面打开…');
    try{
      // Fetch inside the signed-in page: mobile download managers may lose Basic auth.
      const response=await fetch('/__serein/export/backup',{credentials:'same-origin',cache:'no-store'});
      if(!response.ok){
        const error=await response.json().catch(()=>null);
        throw new Error(response.status===401?'登录已失效，请刷新页面重新登录后下载。':error?.detail||'数据库备份未完成，请稍后重试。');
      }
      const blob=await response.blob();
      if(!blob.size)throw new Error('收到的备份为空，请重试。');
      const url=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=url;link.download='serein-backup.db';document.body.appendChild(link);
      link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60_000);
      setBackupMessage('备份已接收，已请求浏览器保存 serein-backup.db。');
    }catch(error){setBackupMessage(error instanceof TypeError?'备份连接中断，请保持网络稳定后重试。':error.message||'备份下载未完成，请重试。');}
    finally{setBackupBusy(false);}
  }
  useEffect(()=>{
    let disposed=false,pending=false;
    async function refresh(){if(document.hidden||pending)return;pending=true;try{const data=await call();if(!disposed)setItems(data.items);}catch(error){if(!disposed)setMessage(error.message);}finally{pending=false;}}
    refresh();const timer=setInterval(refresh,3000);return()=>{disposed=true;clearInterval(timer);};
  },[]);
  useEffect(()=>{fetch('/__serein/install-location').then(response=>response.ok?response.json():null)
    .then(value=>setInstallation(value)).catch(()=>{});},[]);
  async function previewPath(){
    const path=sourcePath.trim();if(!path || busy)return;
    setBusy(true);setMessage('正在读取旧库并扫描…');
    try{
      const result=await call('/preview-path',{path});
      setItems((await call()).items);
      setMessage(result.errors.length?'扫描发现错误，请修正旧库后重新预览。':'路径预览已生成；确认后才会开始迁移。');
    }catch(error){setMessage(error.message||'路径预览未完成');}finally{setBusy(false);}
  }
  async function upload(file){
    if(!file)return;
    if(file.size>64*1024*1024){setMessage('网页支持 64 MiB 以内的 tar / tar.gz；较大备份请使用安装脚本。');return;}
    setBusy(true);setMessage('正在上传和扫描…');
    try{
      const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      const result=await call('/preview',{content:encoded});
      setItems((await call()).items);setMessage(result.errors.length?'扫描发现错误，请修正备份后重新上传。':'预览已生成，确认后才会导入。');
    }catch(error){setMessage(error.message || '上传未完成');}finally{setBusy(false);}
  }
  async function action(id,operation){
    setBusy(true);
    try{
      const saved=items.find(item=>item.id===id)?.options;
      const body=operation==='continue'?{...(saved || {...names,aliases:names.aliases.split(/[,，]/).map(s=>s.trim()).filter(Boolean)}),
        generate_cues:saved?(cueChoices[id]??saved.generate_cues??true):generateCues,confirmed}:{};
      await call('/'+id+'/'+operation,body);setItems((await call()).items);
      setCueChoices(current=>{const next={...current};delete next[id];return next;});
      setMessage(operation==='pause'?'将在当前条目或处理阶段结束后暂停。':'已加入后台任务，可以离开页面；续跑沿用原名字和别名，cues 选项只影响尚未成功打标的条目。');
    }catch(error){setMessage(error.message);}finally{setBusy(false);}
  }
  return <section className="settings-group" aria-label="旧记忆库迁移">
    <div className="settings-group__heading"><h3>旧记忆库迁移</h3><p>输入旧库位置或上传备份，先预览再转换。支持 JSONL 旧边及 SQLite 关系表。</p></div>
    <p>保留清理后的正文；reflection、affect_anchor 整段删除，其他三级标题删除但保留内容。feel / whisper 转为日记，日印象不导入。旧“自我锚点”正文直接丢弃，不恢复独立“自我”区；持续更新的窗影承接现在的自我。旧心绪状态与备忘照旧迁入，已有新状态不覆盖。</p>
    <p>旧公开版的梦境与暗房 JSONL 一并迁入，暗房按房间保留当前正文、历史修订及锁定状态，已归档或撤回的内容不会自动公开。无需 diary.db；如备份另含正式日记库或窗影库，也会保留导入。早期已迁过的实例可在安装菜单 10 单独补漏，不需要重新打标或生成向量。</p>
    <p>旧边由程序转换，不调用模型。updates 转为 continues 并调整方向；无法确定的旧边保留原记录并报告。主域和实体打标、向量准备会调用“配置”页选定的模型；是否同时生成 cues 可选，打标失败不自动重试。</p>
    <p>旧原文库一并归档，可搜索并手动绑定到 Scene，不会自动重新整理或调用模型；不凭正文猜测旧绑定。记忆日期优先用旧 date，没有有效 date 时取 created 或 created_at。旧记忆 comments／年轮作为独立注脚迁入，保留内容、作者和时间。已迁入的实例可用菜单 10 补旧原文、缺失日期和记忆注脚，不重复导入正文。</p>
    <label className="settings-field"><span>旧库位置（运行 Serein 的机器上的绝对路径）</span>
      <input value={sourcePath} disabled={busy} placeholder="/srv/old-memory 或 /home/user/Ombre-Brain/buckets"
        onChange={event=>setSourcePath(event.target.value)} onKeyDown={event=>{if(event.key==='Enter')previewPath();}} /></label>
    <div className="settings-actions"><button type="button" disabled={busy||!sourcePath.trim()} onClick={previewPath}>读取路径并预览</button></div>
    {installation?.legacySourceRoot&&<p className="import-help">Docker 当前只读来源：<code>{installation.legacySourceRoot}</code>。{installation.legacySourceConfigured?'可输入该目录或其子路径。':'其他目录先在安装菜单 7 授权；也可把备份放到此目录。'}</p>}
    <p className="import-help">如果 Serein 运行在 Docker 中，先从安装菜单 7 授权旧库目录，服务会短暂重建；网页不能直接读取浏览器设备上的路径。原库停止写入后，再确认迁移。</p>
    <p className="import-help">或者上传 tar / tar.gz 备份：</p>
    <label className={'import-upload'+(dragging?' is-dragging':'')} onDragOver={event=>{event.preventDefault();if(!busy)setDragging(true);}}
      onDragLeave={()=>setDragging(false)} onDrop={event=>{event.preventDefault();setDragging(false);if(!busy)upload(event.dataTransfer.files?.[0]);}}>
      <strong>选择文件，或拖到这里</strong><span>Ombre 旧库 tar / tar.gz 备份 · 最大 64 MiB</span>
      <input type="file" aria-label="导入 Ombre 旧记忆库备份" accept=".tar,.gz,.tgz" disabled={busy} onChange={event=>{upload(event.target.files?.[0]);event.target.value='';}} />
    </label>
    {['user_name','ai_name','aliases'].map(key=><label className="settings-field" key={key}><span>{{user_name:'旧记忆里的用户名字',ai_name:'旧记忆里的 AI 名字',aliases:'旧称／别名（逗号分隔，可留空）'}[key]}</span><input value={names[key]} maxLength={key==='aliases'?1000:64} onChange={event=>setNames({...names,[key]:event.target.value})}/></label>)}
    <label className="settings-toggle"><span><strong>新迁移：打标时生成召回线索 cues</strong><small>默认开启。关闭后仅打主域和实体标签；没有 cues 仍可按正文检索，向量准备照常进行。</small></span><input type="checkbox" role="switch" checked={generateCues} disabled={busy} onChange={event=>setGenerateCues(event.target.checked)}/></label>
    <label className="settings-toggle"><span><strong>确认转换规则与模型费用</strong><small>使用停止写入后的完整备份；转换期间避免编辑本批导入的内容。导入前自动备份当前数据库。</small></span><input type="checkbox" role="switch" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)}/></label>
    {items.map(item=>{const task=item.task||{},active=['queued','running'].includes(task.status);return <section className="legacy-migration-task" key={item.id}>
      <p><strong>{item.source_path?'路径':'备份'} {item.id.slice(0,8)}</strong> · Scene {item.summary.scenes} · 日记 {item.summary.diaries} · 旧边 {item.summary.old_edges}{item.summary.self_anchors_discarded?` · 丢弃旧自我锚点 ${item.summary.self_anchors_discarded}`:''}</p>
      {item.summary.history?<><p>历史数据：梦境 {item.summary.history.dreams || 0} · 窗影 {item.summary.history.shadows || 0} · 正式日记 {item.summary.history.diaries || 0} · 暗房 {item.summary.history.darkroom || 0} · 日记评论 {item.summary.history.comments || 0} · 修订 {item.summary.history.revisions || 0}</p>{item.summary.history.warnings?.map((warning,index)=><p key={index}>{warning}</p>)}</>:<p>这是旧版本预览，请重新预览同一备份以检查梦境、日记和暗房。</p>}
      {item.summary.memory_comments?<p>记忆注脚／旧年轮：{item.summary.memory_comments.comments} 条 · 涉及 {item.summary.memory_comments.memories} 条旧记忆</p>:<p>请重新预览同一备份，以检查记忆注脚／旧年轮。</p>}
      {item.summary.originals?<><p>旧聊天原文：{item.summary.originals.messages} 条{item.summary.originals.path?` · ${item.summary.originals.path}`:''}</p>{item.summary.originals.warnings?.map((warning,index)=><p key={index}>{warning}</p>)}</>:<p>请重新预览同一备份，以检查旧原文库和日期。</p>}
      {item.source_path&&<p className="import-help">只读来源：<code>{item.source_path}</code> · <a className="settings-link" href={'/__serein/migration/'+item.id+'/export'} download>导出旧库 ZIP</a></p>}
      <p>{item.summary.edge_sources?.map(source=>`${source.path}：${source.count} 条`).join('；') || item.summary.edge_warning || '没有找到旧关系边文件，请核对备份是否带有旧服务实际使用的 state 目录。缺少旧边不影响单独导入正文。'}</p>
      {item.options&&<p>续跑沿用：{item.options.user_name} / {item.options.ai_name}{item.options.aliases.length?' · '+item.options.aliases.join('、'):''}</p>}
      {item.options&&<label className="settings-toggle"><span><strong>本批迁移：打标时生成 cues</strong><small>暂停或失败后可修改，仅影响尚未成功打标的条目；保留已保存的 cues，不重跑成功条目。</small></span><input type="checkbox" role="switch" checked={cueChoices[item.id]??item.options.generate_cues??true} disabled={busy||active||task.status==='completed'} onChange={event=>setCueChoices({...cueChoices,[item.id]:event.target.checked})}/></label>}
      {item.errors.map((error,index)=><p key={index}>{error.path}：{error.error}</p>)}
      <p role="status">{states[task.status]||'待确认'}{task.stage && stages[task.stage]?' · '+(task.stage==='tagging'&&item.options?.generate_cues===false?'打标与实体':stages[task.stage]):''}{task.total?` · ${task.completed}/${task.total}`:''}{task.error?' · '+task.error:''}</p>
      <div className="settings-actions">{active?<button disabled={busy} onClick={()=>action(item.id,'pause')}>暂停</button>:task.status!=='completed'&&<button disabled={busy||!!item.errors.length||!confirmed||(!item.options&&(!names.user_name.trim()||!names.ai_name.trim()))} onClick={()=>action(item.id,'continue')}>确认并开始／继续</button>}</div>
      {!active&&task.status!=='completed'&&<p className="import-help">{item.errors.length?'请先修正上面的扫描错误并重新预览。':!item.options&&(!names.user_name.trim()||!names.ai_name.trim())?'请先填写上方旧记忆里的用户名字和 AI 名字。':!confirmed?'请先打开上方“确认转换规则与模型费用”开关。':'可以开始；旧边为 0 不会禁用此按钮。'}</p>}
    </section>;})}
    <p role="status">{message}</p>
    <div className="settings-actions"><button type="button" disabled={backupBusy} onClick={downloadBackup}>{backupBusy?'正在下载备份…':'下载当前数据库备份'}</button></div>
    <p role="status">{backupMessage}</p>
    <p>旧库 ZIP 包含所选路径中的全部文件，可能含配置和密钥；数据库备份也包含记忆与模型密钥，请妥善保存。图片、向量索引和安装账号文件需随安装目录另行备份；数据库备份不是一键恢复整站的安装包。</p>
  </section>;
}

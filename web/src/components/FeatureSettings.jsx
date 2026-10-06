import {useEffect,useState} from 'react';
import {instanceSettings} from '../storage/instanceStore.js';

const features = {
  image_transcription_async:['异步图片转录','原图照常交给能识图的主模型；回复完成后在后台转录，并把结果写回同一条原始消息。不阻塞当前回复，也不把转录注入当前聊天。'],
  image_eyes:['眼睛（主模型不能识图时）','先由图片转录模型看图，把转录注入当前聊天，再移除发往主模型的原图；原始消息仍保存原图和转录。与“异步图片转录”只能开启一个。'],
  current_time:['当前日期时间','每个新用户轮次向聊天主模型注入所选时区的日期和时间；工具续轮沿用该轮时间。只保证当轮请求能看到时间，Serein 无法修改客户端已落盘的消息记录。'],
  memos:['备忘','留给未来的话。到期时带入聊天；关闭后不注册备忘工具。'],
  persona:['心绪','记录并延续对话状态。请在“配置”页选择“心绪/防撤退”使用的模型。'],
  anti_retreat:['防撤退','使用“心绪/防撤退”模型，回复后异步判断、下一轮提示。同一窗口冷却 6 轮且至少 10 分钟。'],
  window_shadows:['窗影','由 agent 主动写下窗口侧影。关闭后不注册窗影工具。'],
  originals:['原话查阅','按文字、日期、角色找原话，默认最多返回 10 条；按 ID 读全文和同会话前后文。关闭后不注册这两个工具。'],
  event_to_scene:['Event 升为 Scene','让主模型读过 Event 和原话后，自己编辑并保存为 Scene。默认关闭；关闭后不再提供工具，已有记忆保留。'],
  favorites:['收藏工具','让模型读取、收藏或取消收藏 Event 和 Scene。读取默认每页 10 条；写入和状态工具可传 favorite。关闭不影响页面收藏。'],
  narrative_tools:['主模型读写叙事卷','注册叙事卷读写工具，让聊天主模型自己阅读、起草和保存。同时关闭自动 Narrative Writer；关闭此开关不会自动重启 Writer。'],
  narrative_nightly_organize:['夜间整理叙事卷','每天凌晨四点后，用“叙事卷找材料”模型把新增 Event、Scene 和可读日记接入旧 Arc，或建立空白 collecting Arc。没有新增材料不调用模型，也不会自动写正文。'],
  association:['联想','沿已确认的 Scene 关系，最多补一条记忆参与召回筛选。关闭后仅直接召回，已有关系保留。'],
  write_context:['写入时找前情','新建 Scene 后，至多提示一条可能相关的旧 Scene，以及它可能所属的 Arc。只返回候选，不建关系或加入 Arc；没有可靠线索就不提示。'],
  relations_auto_accept:['关系提案自动通过','新提案写完后自动通过；仍需通过当前记忆与证据校验。'],
  resume:['开窗续接（resume）','按保存的选择读取续接资料。发送 /resume 和通过 MCP 读取只能选择一个。通过网关聊天时，建议使用 /resume 指令，不建议开启此 MCP 工具；此工具主要供官方客户端使用。'],
};

const fallbackTimeZones = ['Asia/Shanghai','UTC','Asia/Tokyo','Asia/Singapore','Europe/London','Europe/Berlin','America/New_York','America/Chicago','America/Denver','America/Los_Angeles','Australia/Sydney'];
const timeZones = [...new Set([...fallbackTimeZones,...(Intl.supportedValuesOf?.('timeZone')||[])])];

export function FeatureSettings({onOpenSummary,onOpenEventGuide}) {
  const [values,setValues]=useState(null),[resumeMode,setResumeMode]=useState('command'),[settingsVersion,setSettingsVersion]=useState(null),[clock,setClock]=useState({timezone:'Asia/Shanghai'}),[status,setStatus]=useState(''),[busy,setBusy]=useState(false),[autoEnabled,setAutoEnabled]=useState(false);
  useEffect(()=>{let active=true;instanceSettings().then(value=>{if(active){setValues(value.features);setResumeMode(value.resume.mode);setSettingsVersion(value.settings_version);setClock(value.clock);setAutoEnabled(value.pipeline.auto_enabled!==false);}})
    .catch(error=>{if(active)setStatus(error.message);});return()=>{active=false;};},[]);
  async function save(event) {
    event.preventDefault();setBusy(true);
    try {const result=await instanceSettings({expected_version:settingsVersion,features:values,resume:{mode:resumeMode},clock,pipeline:{auto_enabled:autoEnabled}});setValues(result.features);setResumeMode(result.resume.mode);setSettingsVersion(result.settings_version);setClock(result.clock);setStatus('已保存并生效。已有内容会保留。');}
    catch(error){setStatus(error.message);}finally{setBusy(false);}
  }
  return <section className="settings-group"><div className="settings-group__heading"><h3>可选功能</h3><p>按需开启，保存后生效。</p></div>
    {values&&<form onSubmit={save}>
      <div className="settings-toggle"><span><strong id="automatic-summary-label">自动摘要</strong><small>自动把新聊天归线、整理为 Event，会按待处理材料调用模型；每次从关闭改为开启时，从保存后的新原话开始，不补跑此前积压。</small>
        <button type="button" className="settings-link" onClick={onOpenSummary}>自动摘要配置</button>{' · '}
        <button type="button" className="settings-link" onClick={onOpenEventGuide}>了解模型调用与费用</button></span>
        <input type="checkbox" role="switch" aria-labelledby="automatic-summary-label" disabled={busy} checked={autoEnabled} onChange={event=>setAutoEnabled(event.target.checked)}/></div>
      {Object.entries(features).map(([key,[label,help]])=><label className="settings-toggle" key={key}>
      <span><strong>{label}</strong><small>{help}</small></span><input type="checkbox" role="switch" aria-label={label} disabled={busy} checked={!!values[key]}
        onChange={event=>setValues(current=>({...current,[key]:event.target.checked,
          ...(event.target.checked&&key==='image_transcription_async'?{image_eyes:false}:event.target.checked&&key==='image_eyes'?{image_transcription_async:false}:{})}))}/></label>)}
      {values.current_time&&<label className="settings-field time-context-zone"><span>时间戳时区</span><select disabled={busy} value={clock.timezone}
        onChange={event=>setClock({timezone:event.target.value})}>{timeZones.map(zone=><option value={zone} key={zone}>{zone}</option>)}</select>
        <small>默认 Asia/Shanghai（东八区）；注入内容也会写明当时的 UTC 偏移。</small></label>}
      {values.resume&&<label className="settings-field"><span>续接方式</span><select aria-label="续接方式" disabled={busy} value={resumeMode} onChange={event=>setResumeMode(event.target.value)}>
        <option value="command">发送 /resume 指令</option><option value="mcp">通过 MCP 读取续接资料</option></select>
        <small>两种方式只能选择一个。MCP 模式停用聊天中的 /resume 指令。内容选择、预览与复制在侧栏“换窗”页。</small></label>}
      <div className="settings-actions"><button disabled={busy} type="submit">保存功能设置</button></div></form>}
    <p role="status">{status}</p></section>;
}

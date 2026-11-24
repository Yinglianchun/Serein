import { useCallback, useEffect, useRef, useState } from "react";
import { PersonaCardStack } from "../components/PersonaCardStack.jsx";
import { ArrowClockwise } from "@phosphor-icons/react";
import { memoDateParts, changeMemoDate } from "../utils/memoDate.js";
import "./companion-pages.css";

async function request(path, method = "GET", body, signal) {
  const response = await fetch(`/__serein/companion/${path}`, {
    method, signal, ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "没有保存，请检查填写内容后重试。");
  return result;
}

function FeatureHeader({ title, eyebrow, description, feature, setEnabled }) {
  useEffect(() => {
    const update = event => setEnabled(event.detail[feature]);
    window.addEventListener("serein:features", update);
    return () => window.removeEventListener("serein:features", update);
  }, [feature, setEnabled]);
  return <header className="companion-heading">
    <div><span className="companion-eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>
  </header>;
}

const names = { affinity: "亲近", dominance: "主动", defensiveness: "防备", trust: "信任",
  valence: "愉悦", arousal: "情绪能量", tenderness: "温柔", possessiveness: "占有欲", longing: "想念",
  security: "安全感", protective_drive: "保护欲", libido: "欲望", session_defensiveness: "当前防备" };

const affectKeys = ["valence", "arousal", "tenderness", "possessiveness", "longing", "security", "protective_drive", "libido"];
const eventNames = { praise: "赞许", affection: "亲近", comfort: "安慰", criticism: "分歧", stress: "压力", neutral: "日常", request: "请求", conflict: "冲突", playful: "玩笑" };
function formatTime(value) {
  if (!value) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function StateMeters({ values, keys }) {
  return <div className="persona-meters">{keys.map(key => {
    const value = values?.[key], available = typeof value === "number", percent = available ? Math.round(value * 100) : 0;
    return <div className="persona-meter" key={key}><span>{names[key]}</span>
      <div className="persona-meter-track" role={available ? "meter" : undefined} aria-label={names[key]} aria-valuemin={available ? 0 : undefined} aria-valuemax={available ? 100 : undefined} aria-valuenow={available ? percent : undefined}>
        <i style={{ width: `${percent}%` }} /></div><span>{available ? percent : "—"}</span></div>;
  })}</div>;
}
export function PersonaPage() {
  const [data, setData] = useState(null), [, setEnabled] = useState(null);
  const [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  const [selectedSession, setSelectedSession] = useState("");
  const [visible, setVisible] = useState(!document.hidden);
  const requestRef = useRef(null);
  const load = useCallback(async (quiet = false) => {
    requestRef.current?.abort();
    const controller = new AbortController(); requestRef.current = controller;
    if (!quiet) setBusy(true);
    try {
      const result = await request(`persona?session_id=${encodeURIComponent(selectedSession)}`, "GET", undefined, controller.signal);
      if (controller.signal.aborted) return;
      setData(result); setEnabled(result.enabled); setMessage("");
    } catch (error) { if (error.name !== "AbortError") setMessage(error.message); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }, [selectedSession]);
  useEffect(() => {
    if (!document.hidden) load();
    const onVisibility = () => {
      setVisible(!document.hidden);
      if (!document.hidden) load(true);
    };
    document.addEventListener("visibilitychange", onVisibility);
    const timer = window.setInterval(() => { if (!document.hidden) load(true); }, 15000);
    return () => { requestRef.current?.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, [load]);
  const state = data?.session;
  const events = data ? [...data.events].sort((a, b) => b.id - a.id) : [];
  return <div className="companion-content persona-content">
    <FeatureHeader title="心绪" eyebrow="THE INNER WEATHER" description="对话之后，心里留下的天气。" feature="persona" setEnabled={setEnabled} />
    {!data && <button type="button" disabled={busy} onClick={() => load()}>{busy ? "读取中…" : "刷新"}</button>}
    {message && <p role="status">{message}</p>}
    {data && <>
    <section className="persona-history persona-history--direct" aria-label="心情卡片">
      <div className="persona-toolbar">
          <div className="companion-actions">{data.sessions.length > 0 && <select aria-label="心绪记录范围" disabled={busy} value={selectedSession} onChange={event => { setData(null); setSelectedSession(event.target.value); }}>
            <option value="">全部心绪</option>
            {data.sessions.filter(session => session.session_id !== "gateway-shared").map(session => <option key={session.session_id} value={session.session_id}>{session.session_id}{session.mood_label ? ` · ${session.mood_label}` : ""}</option>)}
          </select>}<button type="button" aria-label="刷新心绪" title="刷新" disabled={busy} onClick={() => load()}><ArrowClockwise size={17} aria-hidden="true" /></button></div>
      </div>
      {!events.length && <p className="companion-empty">还没有变化记录。下一场对话之后，再来看一看。{!data.model_configured && <><br /><a href="#settings">配置心绪 ↗</a></>}</p>}
      <PersonaCardStack key={selectedSession || "latest"} events={events} scope={`${window.location.pathname}:${selectedSession || "latest"}`} visible={visible}>
      {events.map((item, index) => <article className={`persona-trace${index === 0 ? " is-latest" : ""}`} key={item.id} data-event-id={item.id}>
        <div className="persona-trace-heading"><span>{index === 0 && <em>最新</em>}{eventNames[item.event_type] || item.event_type}</span><time dateTime={item.created_at}>{formatTime(item.created_at)}</time></div>
        <p className="persona-trace-thought">{item.inner_thought || item.residue || item.perceived_intent || "这一次，没有留下独白。"}</p>
        {(item.surface_trigger || item.perceived_intent) && <p className="companion-note">{item.surface_trigger || item.perceived_intent}</p>}
        {item.residue && item.residue !== item.inner_thought && <p className="persona-residue">{item.residue}</p>}
        {Object.values({ ...item.relationship_delta, ...item.affect_delta }).some(value => Math.abs(value) >= .0001) && <details className="persona-card-details"><summary>这一刻的变化</summary><div className="persona-deltas">{Object.entries({ ...item.relationship_delta, ...item.affect_delta }).filter(([, value]) => Math.abs(value) >= .0001).map(([key, value]) => <span key={key}>{names[key] || key} {value > 0 ? "+" : ""}{Number(value).toFixed(3)}</span>)}</div></details>}
        {item.error && <p className="companion-note">这次状态更新未完成。</p>}
      </article>)}
      </PersonaCardStack>
    </section>
      <details className="persona-state-details"><summary><span>情绪与关系基调</span><span className="persona-state-toggle">展开细看</span></summary>
      <div className="persona-readings"><section><div className="persona-reading-heading"><span className="companion-eyebrow">AFFECT</span><h2>情绪流动</h2></div>
        <StateMeters values={state} keys={affectKeys} />
        </section><section>
        <div className="persona-reading-heading persona-relationship-heading"><span className="companion-eyebrow">RELATIONSHIP</span><h2>关系基调</h2><p>由共同经历慢慢沉淀，所有窗口共用。</p></div>
        <StateMeters values={data.relationship} keys={["affinity", "dominance", "defensiveness", "trust"]} />
      </section></div></details>
    </>}
  </div>;
}

function MemoDateField({ label, value, onChange }) {
  const parts = memoDateParts(value);
  return <fieldset className="memo-date-field"><legend>{label}</legend>
    <div className="memo-date-pair"><label><span>日期</span><input type="date" aria-label={`${label}日期`} value={parts.date} onChange={event => onChange(changeMemoDate(value, "date", event.target.value))} /></label>
      <label><span>时间 · 可选</span><input type="time" aria-label={`${label}时间`} disabled={!parts.date} value={parts.time} onChange={event => onChange(changeMemoDate(value, "time", event.target.value))} /></label></div>
    <div className="memo-date-hint"><span>{parts.date ? parts.time ? "北京时间" : label === "结束" ? "这一天结束时到期" : "这一天开始时生效" : label === "结束" ? "不设到期日期" : "保存后即可生效"}</span>
      {value && <button type="button" aria-label={`清除${label}`} onClick={() => onChange("")}>清除</button>}</div>
  </fieldset>;
}

const blankMemo = () => ({ title: "", content: "", repeat_rule: "every_n_rounds", interval_rounds: 6, daily_limit: "",
  start_at: "", end_at: "", next_due_at: "", cooldown_minutes: 0, max_injections: 0, channel: "global", session_id: "" });
const statusNames = { active: "进行中", done: "已完成", archived: "已归档" };
const repeatNames = { every_n_rounds: "每隔几轮", daily: "每天", morning_evening: "早晚", once: "只提醒一次", none: "不重复" };
const numberFields = [["interval_rounds", "轮次间隔", 1, 10000], ["daily_limit", "每日上限", 0, 10000], ["max_injections", "总次数上限", 0, 100000], ["cooldown_minutes", "间隔分钟", 0, 525600]];
export function MemosPage() {
  const [items, setItems] = useState([]), [enabled, setEnabled] = useState(null);
  const [filter, setFilter] = useState("active"), [draft, setDraft] = useState(null), [editing, setEditing] = useState("");
  const [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  async function load() {
    const result = await request("memos"); setItems(result.items); setEnabled(result.enabled);
  }
  async function refresh() { setBusy(true); try { await load(); setMessage(""); } catch (error) { setMessage(error.message); } finally { setBusy(false); } }
  useEffect(() => { refresh(); }, []);
  async function mutate(path, method, body, notice = "备忘已保存。") {
    setBusy(true);
    try { await request(path, method, body); await load(); setMessage(notice); return true; }
    catch (error) { setMessage(error.message); return false; }
    finally { setBusy(false); }
  }
  function edit(item) {
    setEditing(item?.id || "");
    setDraft(item ? { ...Object.fromEntries(Object.entries(blankMemo()).map(([key, fallback]) => [key, item[key] ?? fallback])), interval_rounds: item.interval_rounds || 6 } : blankMemo());
    setMessage("");
  }
  function field(key, value) { setDraft(d => ({ ...d, [key]: value })); }
  async function save(event) {
    event.preventDefault();
    const body = { ...draft, daily_limit: draft.daily_limit === "" ? null : Number(draft.daily_limit) };
    if (await mutate(editing ? `memos/${editing}` : "memos", editing ? "PUT" : "POST", body)) { setDraft(null); setEditing(""); }
  }
  const visible = items.filter(item => filter === "all" || item.status === filter);
  return <div className="companion-content memo-content">
    <FeatureHeader title="备忘" eyebrow="NOTES FOR LATER" description="留给未来的话。" feature="memos" setEnabled={setEnabled} />
    <p className="companion-note">{enabled ? "按约定的时间，在下一次聊天里轻轻提起。" : "未启用时仍可整理备忘；不会带入聊天，也不注册 AI 的备忘工具。"}</p>
    <div className="memo-toolbar"><div className="companion-filters" role="group" aria-label="备忘状态">{Object.entries({ ...statusNames, all: "全部" }).map(([key, label]) => <button type="button" aria-pressed={filter === key} onClick={() => setFilter(key)} key={key}>{label}</button>)}</div>
      <div className="companion-actions"><button className="companion-primary" disabled={busy} type="button" onClick={() => edit()}>＋ 新增备忘</button><button type="button" disabled={busy} onClick={refresh}>刷新</button></div></div>
    {message && <p role="status">{message}</p>}
    {draft && <form className="memo-editor" onSubmit={save}><div className="persona-reading-heading"><span className="companion-eyebrow">{editing ? "EDIT NOTE" : "A NEW NOTE"}</span><h2>{editing ? "编辑备忘" : "写一条备忘"}</h2></div>
      <div className="memo-editor-grid"><label className="companion-field memo-wide">标题<input autoFocus required maxLength={200} placeholder="想在之后记起什么？" value={draft.title} onChange={event => field("title", event.target.value)} /></label>
        <label className="companion-field memo-wide">正文<textarea required rows={4} maxLength={12000} value={draft.content} onChange={event => field("content", event.target.value)} /></label>
        {[['start_at', '开始'], ['end_at', '结束']].map(([key, label]) => <MemoDateField key={key} label={label} value={draft[key]} onChange={value => field(key, value)} />)}
        <label className="companion-field">重复<select value={draft.repeat_rule} onChange={event => field("repeat_rule", event.target.value)}>{Object.entries(repeatNames).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
      </div><p className="companion-note">日期和时间按北京时间安排。只选日期也可以；到时随聊天提起。</p>
      <details className="memo-options"><summary>提醒次数与范围</summary><div className="memo-editor-grid">
        {numberFields.filter(([key]) => key !== "interval_rounds" || draft.repeat_rule === "every_n_rounds").map(([key, label, min, max]) => <label className="companion-field" key={key}>{label}<input type="number" min={min} max={max} required={key !== "daily_limit"} placeholder={key === "daily_limit" ? "自动" : "0 表示不限"} value={draft[key]} onChange={event => field(key, event.target.value === "" && key === "daily_limit" ? "" : Number(event.target.value))} /></label>)}
        <label className="companion-field">通道<input required value={draft.channel} onChange={event => field("channel", event.target.value)} /></label>
      </div><p className="companion-note">每日上限留空时，新备忘默认每天一次，早晚为两次；编辑时保留原上限。次数填 0 表示不限。通道 global 用于所有聊天通道，gateway 用于聊天代理。</p></details>
      <div className="companion-actions"><button className="companion-primary" disabled={busy} type="submit">{editing ? "保存修改" : "保存备忘"}</button><button type="button" disabled={busy} onClick={() => setDraft(null)}>取消</button></div>
    </form>}
    <div className="memo-list-heading"><span>{statusNames[filter] || "全部备忘"}</span><span>{visible.length} 条</span></div>
    {visible.length === 0 && <div className="memo-quiet"><span aria-hidden="true">✧</span><h2>{filter === "active" ? "还没有待提醒的事" : "这里暂时没有备忘"}</h2><p>想留给未来的话，可以慢慢写在这里。</p></div>}
    {visible.map(item => <article className={`memo-entry memo-entry--${item.status}`} key={item.id}>
      <div className="memo-title"><h2>{item.title}</h2><span>{statusNames[item.status]}</span></div>
      <p className="memo-body">{item.content}</p>
      <div className="memo-meta"><span>{item.start_at ? `开始 ${formatTime(item.start_at)}` : "现在可用"}</span>
        {item.end_at && <span>到期 {formatTime(item.end_at)}</span>}
        <span>{item.repeat_rule === "every_n_rounds" ? `每 ${item.interval_rounds} 轮` : repeatNames[item.repeat_rule]}</span><span>{item.daily_limit ? `每天最多 ${item.daily_limit} 次` : "不限每日次数"}</span>
        {item.max_injections > 0 && <span>总共最多 {item.max_injections} 次</span>}{item.cooldown_minutes > 0 && <span>至少间隔 {item.cooldown_minutes} 分钟</span>}<span>已提醒 {item.reminder_count} 次</span>
        {item.channel !== "global" && <span>通道 {item.channel}</span>}</div>
      <div className="companion-actions"><button type="button" disabled={busy} onClick={() => edit(item)}>编辑</button>
        {item.status === "active" ? <><button type="button" disabled={busy} onClick={() => mutate(`memos/${item.id}`, "PATCH", { status: "done" }, "已完成。")}>标完成</button>
          <button type="button" disabled={busy} onClick={() => mutate(`memos/${item.id}`, "PATCH", { status: "archived" }, "已归档。")}>归档</button></>
          : <button type="button" disabled={busy} onClick={() => mutate(`memos/${item.id}`, "PATCH", { status: "active" }, "已重新打开，原有次数和有效期保留。")}>重新打开</button>}
      </div></article>)}
    <p className="companion-note memo-footer">最多显示 200 条。重新打开会保留已提醒次数和有效期；一次性备忘已提醒过时，可新增一条。</p>
  </div>;
}

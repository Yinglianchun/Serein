import { ImageSquare, ArrowLeft } from "@phosphor-icons/react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { people } from "../data/awake.js";
import { ModelSettings } from "./ModelSettings.jsx";
import { FeatureSettings } from "./FeatureSettings.jsx";
import { PipelineSettings } from "./PipelineSettings.jsx";
import { LegacyMigration } from "./LegacyMigration.jsx";
import { togetherCaption } from "../storage/togetherDate.js";

const tabs = [["appearance", "外观"], ["features", "功能"], ["models", "模型"], ["configuration", "配置"], ["summary", "自动摘要管理"], ["imports", "对话导入"], ["migration", "旧库迁移"]];

export function SettingsPanel({
  open,
  onClose,
  onOpenEventGuide,
  coverSettings,
  onCoverSetting,
  identityNames,
  onNameChange,
  onSaveIdentity,
  identityStatus,
  identityBusy,
  anniversary,
  images,
  imageBusy,
  imageStatus,
  onReplace,
  onEditComposition,
  onResetComposition,
}) {
  const [tab, setTab] = useState("appearance");
  const [recallThreshold,setRecallThreshold]=useState(null);
  const [candidateThresholdDraft,setCandidateThresholdDraft]=useState({});
  const [passageDraft,setPassageDraft]=useState({});
  const pages = useRef(null);
  const snapTimer = useRef(null);
  const targetPage = useRef(null);
  function showTab(key) {
    const index = tabs.findIndex(([name]) => name === key);
    if (index < 0) return;
    setTab(key);
    const element = pages.current;
    if (!element || !open) return;
    window.clearTimeout(snapTimer.current);
    targetPage.current = index;
    const left = element.clientWidth * index;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    element.style.scrollSnapType = "none";
    element.scrollTo({ left, behavior: reduced ? "instant" : "smooth" });
    snapTimer.current = window.setTimeout(() => {
      element.scrollTo({ left, behavior: "instant" });
      element.style.scrollSnapType = "";
      setTab(key);
      targetPage.current = null;
    }, reduced ? 0 : 480);
  }
  useLayoutEffect(() => {
    if (open && pages.current) pages.current.scrollTo({ left: pages.current.clientWidth * tabs.findIndex(([key]) => key === tab), behavior: "instant" });
  }, [open]);
  useEffect(() => () => window.clearTimeout(snapTimer.current), []);
  useEffect(() => {
    const openTab = (event) => {
      if (tabs.some(([key]) => key === event.detail)) showTab(event.detail);
    };
    window.addEventListener("serein:open-settings-tab", openTab);
    return () => window.removeEventListener("serein:open-settings-tab", openTab);
  }, [open]);
  function openSummary() {
    showTab("summary");
  }
  function navigateTabs(event, index) {
    const moves = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = tabs[moves[event.key]][0];
    showTab(next);
    document.getElementById(`settings-tab-${next}`).focus();
  }
  return (
    <>
      <section
        className="settings-panel settings-page"
        aria-labelledby="settings-title"
        hidden={!open}
        onInvalidCapture={event => {
          // Native validation must be able to focus fields in closed menus.
          for (let node = event.target.parentElement; node; node = node.parentElement) {
            if (node.tagName === "DETAILS") node.open = true;
          }
        }}
      >
        <header className="settings-panel__header">
          <button type="button" aria-label="返回上一页" onClick={onClose}>
            <ArrowLeft size={19} weight="light" aria-hidden="true" />
          </button>
          <div>
            <span>SEREIN</span>
            <h2 id="settings-title">设置</h2>
          </div>
        </header>

        <div className="settings-tabs" role="tablist" aria-label="设置分类">
          {tabs.map(([key, label], index) => <button key={key} type="button" role="tab"
            id={`settings-tab-${key}`} aria-controls={`settings-content-${key}`} aria-selected={tab === key}
            tabIndex={tab === key ? 0 : -1} onClick={() => showTab(key)} onKeyDown={event => navigateTabs(event, index)}>
            {label}
          </button>)}
        </div>

        <div className="settings-panel__body settings-panel__body--slides" ref={pages} onScroll={event => {
          if (targetPage.current !== null || !open) return;
          const element = event.currentTarget;
          const index = Math.min(tabs.length - 1, Math.max(0, Math.round(element.scrollLeft / Math.max(1, element.clientWidth))));
          setTab(tabs[index][0]);
        }}>
          <div role="tabpanel" id="settings-content-appearance" aria-labelledby="settings-tab-appearance" aria-hidden={tab !== "appearance"} inert={tab !== "appearance"}>
          <section className="settings-group" aria-labelledby="settings-cover-title">
            <div className="settings-group__heading">
              <h3 id="settings-cover-title">封面</h3>
              <p>照片、名字与留在下面的那句话。</p>
            </div>

            <label className="settings-upload settings-upload--cover">
              <img src={images.hero} alt="当前封面" />
              <span><ImageSquare size={17} weight="light" aria-hidden="true" />更换封面</span>
              <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={imageBusy} onChange={(event) => { onReplace("hero", event.target.files?.[0]); event.target.value = ''; }} />
            </label>

            <p>头像和背景图选好后会自动保存到当前实例。</p>
            <p role="status">{imageStatus}</p>

            <div className="settings-people">
              {people.map((person) => (
                <div className="settings-person" key={person.key}>
                  <label className="settings-upload settings-upload--avatar">
                    <img src={images[person.key]} alt={`${identityNames[person.key]}的头像`} />
                    <span>更换头像</span>
                    <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={imageBusy} onChange={(event) => { onReplace(person.key, event.target.files?.[0]); event.target.value = ''; }} />
                  </label>
                  <label className="settings-field">
                    <span>{person.key === "user" ? "你的名字" : "AI 的名字"}</span>
                    <input
                      type="text"
                      value={identityNames[person.key]}
                      maxLength={64}
                      onChange={(event) => onNameChange(person.key, event.target.value)}
                    />
                  </label>
                </div>
              ))}
            </div>

            <div className="settings-actions"><button type="button" disabled={identityBusy} onClick={onSaveIdentity}>保存名字</button></div>
            <p role="status">{identityStatus}</p>

            <label className="settings-field">
              <span id="meeting-date-label">相遇日期</span>
              <input
                type="date"
                aria-labelledby="meeting-date-label"
                aria-describedby="meeting-date-help"
                max={anniversary.today}
                value={anniversary.draft}
                disabled={anniversary.busy}
                onChange={(event) => anniversary.onChange(event.target.value)}
              />
              <small id="meeting-date-help" className="anniversary-help">{togetherCaption(anniversary.draft, anniversary.today)} · 相遇当天算第 1 天，按当前设备日期计算。</small>
            </label>
            <div className="settings-actions"><button type="button" disabled={anniversary.busy} onClick={anniversary.onSave}>{anniversary.busy ? "保存中…" : "保存日期"}</button></div>
            <p role="status">{anniversary.status}</p>

            <label className="settings-field">
              <span>写在我们下面的话</span>
              <textarea value={coverSettings.tagline} rows={3} maxLength={80}
                onChange={(event) => onCoverSetting("tagline", event.target.value)} />
            </label>

            <details className="settings-menu">
            <summary><span>封面细节<small>白色过渡、头像柔光</small></span></summary>
            <label className="settings-range">
              <span>
                <strong>白色过渡位置</strong>
                <output>{coverSettings.fadeStart}%</output>
              </span>
              <input
                type="range"
                min="48"
                max="82"
                step="1"
                value={coverSettings.fadeStart}
                onInput={(event) => onCoverSetting("fadeStart", Number(event.currentTarget.value))}
              />
              <small>越往左，白色越早出现。</small>
            </label>

            <label className="settings-toggle settings-toggle--cover">
              <span>
                <strong>头像柔光</strong>
                <small>在两个人周围叠一层很淡的白色径向磨砂。</small>
              </span>
              <input
                type="checkbox" role="switch"
                checked={coverSettings.portraitHazeEnabled}
                onChange={(event) => onCoverSetting("portraitHazeEnabled", event.target.checked)}
              />
            </label>

            </details>
          </section>

          <details className="settings-menu" aria-labelledby="settings-composition-title">
            <summary><span id="settings-composition-title">黑白构成<small>{coverSettings.compositionEnabled ? "已开启 · 编辑装饰的位置与大小" : "未开启 · 封面上的装饰元素"}</small></span></summary>
            <p className="model-connection-help">装饰会随封面一起收缩，并在画像退场前先淡去。</p>
            <label className="settings-toggle">
              <span>
                <strong>显示构成元素</strong>
                <small>随时可以关掉，不会删除已经摆好的位置。</small>
              </span>
              <input
                type="checkbox" role="switch"
                checked={coverSettings.compositionEnabled}
                onChange={(event) => onCoverSetting("compositionEnabled", event.target.checked)}
              />
            </label>
            <div className="settings-actions">
              <button type="button" onClick={onEditComposition}>编辑位置与大小</button>
              <button type="button" onClick={onResetComposition}>恢复默认构图</button>
            </div>
          </details>
          </div>
          <div role="tabpanel" id="settings-content-features" aria-labelledby="settings-tab-features" aria-hidden={tab !== "features"} inert={tab !== "features"}>
            {open && <FeatureSettings onOpenSummary={openSummary} />}
          </div>
          {open && <ModelSettings page={tab} onOpenEventGuide={onOpenEventGuide} recallThreshold={recallThreshold} setRecallThreshold={setRecallThreshold}
            candidateThresholdDraft={candidateThresholdDraft} setCandidateThresholdDraft={setCandidateThresholdDraft}
            passageDraft={passageDraft} setPassageDraft={setPassageDraft} onOpenPipeline={()=>showTab("imports")} onOpenCatalog={()=>showTab("models")} onOpenAssignments={()=>showTab("configuration")} />}
          <div role="tabpanel" id="settings-content-imports" aria-labelledby="settings-tab-imports" aria-hidden={tab !== "imports"} inert={tab !== "imports"}>
            {open && <PipelineSettings onOpenSummary={openSummary} />}
          </div>
          <div role="tabpanel" id="settings-content-migration" aria-labelledby="settings-tab-migration" aria-hidden={tab !== "migration"} inert={tab !== "migration"}>
            {open && tab === "migration" && <LegacyMigration />}
          </div>
        </div>
      </section>
    </>
  );
}

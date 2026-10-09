import { EventMailboxPage } from "./pages/EventMailboxPage.jsx";
import { useCallback, useEffect, useRef, useState } from "react";
import { UsageGuide } from "./components/UsageGuide.jsx";
import { Sidebar } from "./components/Sidebar.jsx";
import { AwakePage } from "./pages/AwakePage.jsx";
import { MemoryPage } from "./pages/MemoryPage.jsx";
import { NarrativePage } from "./pages/NarrativePage.jsx";
import { DiaryPage } from "./pages/DiaryPage.jsx";
import { BasementPage } from "./pages/BasementPage.jsx";
import { GardenPage } from "./pages/GardenPage.jsx";
import "./garden-page.css";
import { PersonaPage, MemosPage } from "./pages/CompanionPages.jsx";
import { ResumePage } from "./pages/ResumePage.jsx";

const availableAreas = new Set(["沉淀", "醒来", "记忆", "叙事卷", "日记", "地下室", "花园", "设置", "心绪", "备忘", "使用说明", "换窗"]);
const areaHashes = {
  沉淀: "#mailbox",
  心绪: "#persona",
  备忘: "#memos",
  换窗: "#resume",
  醒来: "#awake",
  设置: "#settings",
  使用说明: "#help",
  记忆: "#memory",
  叙事卷: "#narrative",
  日记: "#diary",
  地下室: "#basement",
  花园: "#garden",
};

const readAreaFromHash = () => {
  if (window.location.hash === "#mailbox") return "沉淀";
  if (window.location.hash === "#resume") return "换窗";
  if (window.location.hash === "#persona") return "心绪";
  if (window.location.hash === "#memos") return "备忘";
  if (window.location.hash === "#help") return "使用说明";
  if (window.location.hash === "#settings") return "设置";
  if (window.location.hash === "#memory") return "记忆";
  if (window.location.hash === "#narrative") return "叙事卷";
  if (window.location.hash === "#diary") return "日记";
  if (window.location.hash === "#basement") return "地下室";
  if (["#garden", "#universe"].includes(window.location.hash)) return "花园";
  return "醒来";
};

export function App() {
  const root = useRef(null);
  const [activeArea, setActiveArea] = useState(readAreaFromHash);
  const [helpPage, setHelpPage] = useState(null);
  const [, refreshIdentity] = useState(0);
  useEffect(() => {
    const update = () => refreshIdentity(value => value + 1);
    window.addEventListener("serein:preference-change", update);
    return () => window.removeEventListener("serein:preference-change", update);
  }, []);
  const [notice, setNotice] = useState("");
  const [awakeEntry, setAwakeEntry] = useState(() => window.location.hash === "#awake" ? "content" : "cover");
  const settingsOrigin = useRef("醒来");
  const [gardenTransition, setGardenTransition] = useState(() => readAreaFromHash() === "花园" ? "loading" : "idle");
  const pendingNavigation = useRef(null);
  const gardenOrigin = useRef({ area: "记忆", scroll: 0 });

  const commitNavigation = useCallback(({ area, scroll = 0, entry = "content" }) => {
    if (area === "醒来") setAwakeEntry(entry);
    setActiveArea(area);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${area === "醒来" && entry === "cover" ? "#cover" : areaHashes[area]}`);
    requestAnimationFrame(() => window.scrollTo({ top: scroll, behavior: "auto" }));
  }, []);

  useEffect(() => {
    const syncAreaFromHash = () => navigateTo(readAreaFromHash(), 0, window.location.hash === "#awake" ? "content" : "cover");
    window.addEventListener("hashchange", syncAreaFromHash);
    return () => window.removeEventListener("hashchange", syncAreaFromHash);
  }, [activeArea, gardenTransition]);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let timer;
    if (gardenTransition === "covering") {
      timer = window.setTimeout(() => {
        const destination = pendingNavigation.current;
        commitNavigation(destination);
        setGardenTransition(destination.area === "花园" ? "loading" : "revealing");
      }, reduced ? 0 : 450);
    } else if (gardenTransition === "revealing") {
      timer = window.setTimeout(() => setGardenTransition("idle"), reduced ? 0 : 900);
    }
    return () => window.clearTimeout(timer);
  }, [gardenTransition, commitNavigation]);

  const gardenReady = useCallback(() => {
    setGardenTransition(phase => phase === "loading" ? "revealing" : phase);
  }, []);

  const showUnavailable = (label) => {
    setNotice(`${label}还没有展开。我们先把这一页做好。`);
    window.setTimeout(() => setNotice(""), 2400);
  };

  const navigateTo = (label, scroll = 0, entry = "content") => {
    if (activeArea === "沉淀" && label !== activeArea && !window.dispatchEvent(new Event("serein:before-navigation", {cancelable:true}))) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#mailbox`);
      return;
    }
    if (!availableAreas.has(label)) {
      showUnavailable(label);
      return;
    }

    if (label === "设置" && activeArea !== "设置") settingsOrigin.current = activeArea;
    if (label === "使用说明") setHelpPage(null);
    if (label === activeArea && label !== "醒来") return;
    if (label === "花园" && activeArea !== "花园") {
      gardenOrigin.current = { area: activeArea, scroll: window.scrollY };
    }
    if (label === "花园" || activeArea === "花园" || gardenTransition !== "idle") {
      pendingNavigation.current = { area: label, scroll, entry };
      setGardenTransition("covering");
    } else {
      commitNavigation({ area: label, scroll, entry });
    }
  };

  const setSettingsOpen = (open) => navigateTo(open ? "设置" : settingsOrigin.current);

  return (
    <main
      ref={root}
      className={`app-shell app-shell--${
        activeArea === "醒来"
          ? "awake"
          : activeArea === "记忆"
            ? "memory"
            : activeArea === "叙事卷"
              ? "narrative"
              : activeArea === "日记"
                ? "diary"
                : activeArea === "地下室"
                  ? "basement"
                  : activeArea === "沉淀" ? "mailbox" : activeArea === "换窗" ? "resume" : ["设置", "使用说明"].includes(activeArea) ? "settings" : ["心绪", "备忘"].includes(activeArea) ? "companion" : "garden"
      }`}
    >
      <AwakePage
        activeArea={activeArea}
        scopeRef={root}
        onNavigate={navigateTo}
        onUnavailable={showUnavailable}
        settingsOpen={activeArea === "设置"}
        awakeEntry={awakeEntry}
        onShowCover={() => navigateTo("醒来", 0, "cover")}
        onSettingsOpenChange={setSettingsOpen}
        onOpenEventGuide={() => { navigateTo("使用说明"); setHelpPage("events"); }}
      />

      {activeArea === "沉淀" && <section className="mailbox-page" aria-label="沉淀">
        <EventMailboxPage onOpenMemory={() => navigateTo("记忆")} onOpenSettings={() => {window.dispatchEvent(new CustomEvent("serein:open-settings-tab", {detail:"features"}));navigateTo("设置");}} />
        <Sidebar activeArea={activeArea} onNavigate={navigateTo} onOpenSettings={() => setSettingsOpen(true)} />
      </section>}

      {activeArea === "换窗" && <section className="resume-page" aria-label="换窗">
        <ResumePage onOpenSettings={() => { window.dispatchEvent(new CustomEvent("serein:open-settings-tab", {detail:"features"})); navigateTo("设置"); }}/>
        <Sidebar activeArea={activeArea} onNavigate={navigateTo} onOpenSettings={() => setSettingsOpen(true)}/>
      </section>}

      {activeArea === "使用说明" && <section className="help-page" aria-label="使用说明">
        <div className="settings-panel settings-page">
          <header className="settings-panel__header"><div><span>SEREIN</span><h2>使用说明</h2></div></header>
          <div className="settings-panel__body"><UsageGuide initialPage={helpPage} onOpenSettingsTab={(tab) => {
            window.dispatchEvent(new CustomEvent("serein:open-settings-tab", { detail: tab }));
            navigateTo("设置");
          }} /></div>
        </div>
        <Sidebar activeArea={activeArea} onNavigate={navigateTo} onOpenSettings={() => setSettingsOpen(true)} />
      </section>}

      <section className="memory-page" aria-label="记忆" hidden={activeArea !== "记忆"}>
        {activeArea === "记忆" ? <MemoryPage /> : null}
        <Sidebar
          activeArea={activeArea}
          onNavigate={navigateTo}
          onUnavailable={showUnavailable}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      </section>

      <section className="narrative-page" aria-label="叙事卷" hidden={activeArea !== "叙事卷"}>
        <NarrativePage />
        <Sidebar
          activeArea={activeArea}
          onNavigate={navigateTo}
          onUnavailable={showUnavailable}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      </section>

      <section className="diary-page" aria-label="日记" hidden={activeArea !== "日记"}>
        <DiaryPage />
        <Sidebar
          activeArea={activeArea}
          onNavigate={navigateTo}
          onUnavailable={showUnavailable}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      </section>

      <section className="basement-page" aria-label="地下室" hidden={activeArea !== "地下室"}>
        <BasementPage />
        <Sidebar
          activeArea={activeArea}
          onNavigate={navigateTo}
          onUnavailable={showUnavailable}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      </section>

      {["心绪", "备忘"].includes(activeArea) && <section className="companion-page" aria-label={activeArea}>
        {activeArea === "心绪" ? <PersonaPage /> : <MemosPage />}
        <Sidebar activeArea={activeArea} onNavigate={navigateTo} onOpenSettings={() => setSettingsOpen(true)} />
      </section>}

      {activeArea === "花园" && <section className="garden-page" aria-label="花园">
        <GardenPage onReady={gardenReady} />
      </section>}

      <div className={`garden-blackout garden-blackout--${gardenTransition}`} aria-hidden={gardenTransition !== "loading"}>
        {gardenTransition === "loading" && <p role="status">雨正在落下…</p>}
      </div>
      {activeArea === "花园" && <button className="garden-return" type="button"
        aria-label={`返回${gardenOrigin.current.area}`}
        onClick={() => navigateTo(gardenOrigin.current.area, gardenOrigin.current.scroll)}>
        <span aria-hidden="true">←</span> 返回
      </button>}

      <div className={`notice${notice ? " is-visible" : ""}`} role="status" aria-live="polite">{notice}</div>
    </main>
  );
}

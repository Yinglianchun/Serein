import { PortraitIntro } from "../components/PortraitIntro.jsx";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { MoonStars, PencilSimple } from "@phosphor-icons/react";
import { Avatar } from "../components/Avatar.jsx";
import { SettingsPanel } from "../components/SettingsPanel.jsx";
import { Sidebar } from "../components/Sidebar.jsx";
import { DreamReader } from "../components/DreamReader.jsx";
import { WindowShadowReader } from "../components/WindowShadowReader.jsx";
import {
  compositionPresets,
  coverSettingStorageKeys,
  defaultCompositionItems,
  defaultCoverSettings,
  people,
} from "../data/awake.js";
import { defaultWindowShadows } from "../data/windowShadows.js";
import { useReplaceableImages } from "../hooks/useReplaceableImages.js";
import {
  readCompositionItems,
  readLocalPreference,
  storeCompositionItems,
  storeLocalPreference,
} from "../storage/awakeStore.js";
import {
  loadWindowShadows,
  readFallbackWindowShadows,
  windowShadowPortraits,
} from "../storage/windowShadowStore.js";
import { dreamExcerpt, loadDreams } from "../storage/dreamStore.js";
import { instanceSettings, identityName } from "../storage/instanceStore.js";
import { localCalendarDate, togetherDays, togetherCaption } from "../storage/togetherDate.js";

gsap.registerPlugin(ScrollTrigger);

export function AwakePage({
  activeArea,
  scopeRef,
  onNavigate,
  onUnavailable,
  settingsOpen,
  awakeEntry,
  onShowCover,
  onSettingsOpenChange,
  onOpenEventGuide,
}) {
  const compositionRef = useRef(null);
  const shadowTriggerRef = useRef(null);
  const dreamTriggerRef = useRef(null);
  const activeDrag = useRef(null);
  const activeResize = useRef(null);
  const { images, replace, busy: imageBusy, status: imageStatus } = useReplaceableImages();
  const [compositionEditing, setCompositionEditing] = useState(false);
  const [compositionElements, setCompositionElements] = useState(readCompositionItems);
  const [selectedCompositionId, setSelectedCompositionId] = useState(null);
  const [newCompositionKind, setNewCompositionKind] = useState("black-block");
  const [shadowReaderOpen, setShadowReaderOpen] = useState(false);
  const [shadowsEnabled, setShadowsEnabled] = useState(false);
  const [shadowsStatus, setShadowsStatus] = useState("loading");
  const [dreamReaderOpen, setDreamReaderOpen] = useState(false);
  const [windowShadows, setWindowShadows] = useState(readFallbackWindowShadows);
  const [selectedShadowId, setSelectedShadowId] = useState(defaultWindowShadows[0]?.id ?? null);
  const [dreams, setDreams] = useState([]);
  const [dreamsStatus, setDreamsStatus] = useState("loading");
  const [selectedDreamId, setSelectedDreamId] = useState(null);
  const [meetingDate, setMeetingDate] = useState(() => readLocalPreference("serein.awake.meetingDate", ""));
  const [meetingDateDraft, setMeetingDateDraft] = useState(meetingDate);
  const [meetingDateBusy, setMeetingDateBusy] = useState(false);
  const [meetingDateStatus, setMeetingDateStatus] = useState("");
  const [today, setToday] = useState(localCalendarDate);
  useEffect(() => {
    let timer;
    const refreshDate = () => {
      window.clearTimeout(timer);
      const now = new Date();
      setToday(localCalendarDate(now));
      const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
      timer = window.setTimeout(refreshDate, midnight - now + 100);
    };
    refreshDate();
    window.addEventListener("focus", refreshDate);
    document.addEventListener("visibilitychange", refreshDate);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("focus", refreshDate);
      document.removeEventListener("visibilitychange", refreshDate);
    };
  }, []);
  const saveMeetingDate = async () => {
    if (meetingDateDraft && togetherDays(meetingDateDraft) === null) {
      setMeetingDateStatus("请选择有效的相遇日期，不能晚于今天。"); return;
    }
    setMeetingDateBusy(true);
    try {
      const { identity } = await instanceSettings({ identity: { meeting_date: meetingDateDraft } });
      setMeetingDate(identity.meeting_date ?? "");
      setMeetingDateDraft(identity.meeting_date ?? "");
      setMeetingDateStatus(identity.meeting_date ? "相遇日期已保存，天数会每天自动更新。" : "相遇日期已清空。");
    } catch (error) { setMeetingDateStatus(error.message); }
    finally { setMeetingDateBusy(false); }
  };
  const [coverSettings, setCoverSettings] = useState(() => ({
    tagline: readLocalPreference("serein.awake.tagline", defaultCoverSettings.tagline),
    fadeStart: Math.min(82, Math.max(48, Number(
      readLocalPreference("serein.awake.fadeStart", defaultCoverSettings.fadeStart),
    ) || defaultCoverSettings.fadeStart)),
    portraitHazeEnabled:
      readLocalPreference("serein.awake.portraitHaze", defaultCoverSettings.portraitHazeEnabled ? "on" : "off") !== "off",
    compositionEnabled:
      readLocalPreference("serein.awake.composition", defaultCoverSettings.compositionEnabled ? "on" : "off") !== "off",
  }));
  const [identityNames, setIdentityNames] = useState(() => ({
    user: readLocalPreference("serein.awake.name.user", people[0].name),
    assistant: readLocalPreference("serein.awake.name.assistant", people[1].name),
  }));
  const [identityStatus, setIdentityStatus] = useState("");
  const [identityBusy, setIdentityBusy] = useState(false);
  useEffect(() => {
    let active = true;
    const featuresChanged = event => {setShadowsEnabled(!!event.detail?.window_shadows);if(!event.detail?.window_shadows)setShadowReaderOpen(false);};
    window.addEventListener('serein:features',featuresChanged);
    instanceSettings().then(({ identity,features }) => {
      if (active) setIdentityNames({ user: identity.user_name, assistant: identity.ai_name });
      if (active) { setMeetingDate(identity.meeting_date ?? ""); setMeetingDateDraft(identity.meeting_date ?? ""); }
      if (active) setShadowsEnabled(!!features?.window_shadows);
    }).catch(error => { if (active) setIdentityStatus(error.message); });
    return () => { active = false;window.removeEventListener('serein:features',featuresChanged); };
  }, []);
  const saveIdentity = async () => {
    if (!identityNames.user.trim() || !identityNames.assistant.trim()) {
      setIdentityStatus("请填写双方的名字。"); return;
    }
    setIdentityBusy(true);
    try {
      const { identity } = await instanceSettings({ identity: { user_name: identityNames.user, ai_name: identityNames.assistant } });
      setIdentityNames({ user: identity.user_name, assistant: identity.ai_name });
      setIdentityStatus("名字已保存，将用于页面显示和后续任务提示词。");
    } catch (error) { setIdentityStatus(error.message); }
    finally { setIdentityBusy(false); }
  };
  const latestShadow = windowShadows[0];
  const portraits = windowShadowPortraits(shadowsEnabled && shadowsStatus === "ready" ? windowShadows : []);
  const portraitPlaceholder = !shadowsEnabled ? "窗影尚未开启。" : shadowsStatus === "loading" ? "正在读回上一窗。" : shadowsStatus === "error" ? "窗影暂时没有接通。" : latestShadow ? "这一窗还没有写下这部分。" : "还没有窗影。";
  const resolvedPeople = people.map((person) => ({ ...person, name: identityName(person.key), detail: portraits[person.key].text || portraitPlaceholder, source: portraits[person.key].source }));
  const latestDream = dreams.find((dream) => dream.hasBody) ?? dreams[0] ?? null;

  useEffect(() => {
    if (!shadowsEnabled || activeArea !== "醒来") return;
    let active = true;
    let request = 0;
    const refresh = () => {
      if (document.hidden) return;
      const currentRequest = ++request;
      loadWindowShadows().then((snapshotShadows) => {
        if (!active || currentRequest !== request) return;
        setShadowsStatus(snapshotShadows === null ? "error" : "ready");
        setWindowShadows(snapshotShadows ?? []);
        setSelectedShadowId(previous => snapshotShadows?.some(shadow => shadow.id === previous) ? previous : snapshotShadows?.[0]?.id ?? null);
      });
    };
    setShadowsStatus("loading");
    refresh();
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      active = false;
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [shadowsEnabled, activeArea]);

  useEffect(() => {
    let active = true;
    loadDreams()
      .then((records) => {
        if (!active) return;
        setDreams(records);
        setSelectedDreamId(records.find((dream) => dream.hasBody)?.id ?? records[0]?.id ?? null);
        setDreamsStatus("ready");
      })
      .catch(() => {
        if (active) setDreamsStatus("error");
      });
    return () => {
      active = false;
    };
  }, []);

  useLayoutEffect(() => {
    if (activeArea !== "醒来") return undefined;

    const mm = gsap.matchMedia();
    const context = gsap.context(() => {
      mm.add(
        {
          motion: "(prefers-reduced-motion: no-preference)",
          reduced: "(prefers-reduced-motion: reduce)",
        },
        ({ conditions }) => {
          if (conditions.reduced || awakeEntry === "content") {
            gsap.set(".cover", { autoAlpha: 0 });
            gsap.set([".white-veil", ".content-view", ".content-view .reveal", ".awake-stage .sidebar"], { autoAlpha: 1, x: 0, y: 0 });
            return;
          }

          const timeline = gsap.timeline({
            defaults: { ease: "none" },
            scrollTrigger: {
              trigger: ".awake-experience",
              start: "top top",
              end: "+=165%",
              scrub: 0.65,
              pin: ".awake-stage",
              anticipatePin: 1,
            },
          });

          timeline
            .to(".cover__composition", { autoAlpha: 0, duration: 0.18 }, 0)
            .to(".cover__portrait-haze", { autoAlpha: 0, duration: 0.34 }, 0.06)
            .to(".cover__image", { scale: 1.045, autoAlpha: 0.12, duration: 0.46 }, 0)
            .to(".cover__identity", { yPercent: -7, autoAlpha: 0, duration: 0.34 }, 0.14)
            .to(".white-veil", { autoAlpha: 1, duration: 0.48 }, 0.08)
            .fromTo(
              ".content-view",
              { autoAlpha: 0, y: 28 },
              { autoAlpha: 1, y: 0, duration: 0.42 },
              0.46,
            )
            .fromTo(
              ".content-view .reveal",
              { autoAlpha: 0, y: 18 },
              { autoAlpha: 1, y: 0, stagger: 0.035, duration: 0.28 },
              0.52,
            )
            .fromTo(
              ".awake-stage .sidebar",
              { autoAlpha: 0, x: -10 },
              { autoAlpha: 1, x: 0, duration: 0.25, ease: "power2.out" },
              0.66,
            );
        },
      );
    }, scopeRef.current);

    document.fonts?.ready.then(() => ScrollTrigger.refresh());

    return () => {
      context.revert();
      mm.revert();
    };
  }, [activeArea, awakeEntry, scopeRef]);

  const navigateFromAwake = (label) => {
    setShadowReaderOpen(false);
    setDreamReaderOpen(false);
    setCompositionEditing(false);
    setSelectedCompositionId(null);
    onNavigate(label);
  };

  const closeShadowReader = useCallback(() => {
    setShadowReaderOpen(false);
    window.setTimeout(() => shadowTriggerRef.current?.focus(), 0);
  }, []);

  const closeDreamReader = useCallback(() => {
    setDreamReaderOpen(false);
    window.setTimeout(() => dreamTriggerRef.current?.focus(), 0);
  }, []);

  const storeLoadedDream = useCallback((loadedDream) => {
    setDreams((current) => current.map((dream) => dream.id === loadedDream.id ? loadedDream : dream));
  }, []);

  const beginCompositionDrag = (event, id) => {
    if (!compositionEditing || !compositionRef.current) return;
    event.preventDefault();
    setSelectedCompositionId(id);
    event.currentTarget.setPointerCapture(event.pointerId);
    const item = compositionElements.find((element) => element.id === id);
    activeDrag.current = { id, x: item.x, y: item.y };
  };

  const moveCompositionItem = (event, id) => {
    if (!compositionEditing || activeDrag.current?.id !== id || !compositionRef.current) return;
    const rect = compositionRef.current.getBoundingClientRect();
    const x = Math.min(98, Math.max(2, ((event.clientX - rect.left) / rect.width) * 100));
    const y = Math.min(96, Math.max(4, ((event.clientY - rect.top) / rect.height) * 100));
    activeDrag.current = { id, x, y };
    event.currentTarget.style.left = `${x}%`;
    event.currentTarget.style.top = `${y}%`;
  };

  const finishCompositionDrag = (event, id) => {
    if (activeDrag.current?.id !== id) return;
    const { x, y } = activeDrag.current;
    activeDrag.current = null;
    setCompositionElements((current) => {
      const next = current.map((item) => item.id === id ? { ...item, x, y } : item);
      storeCompositionItems(next);
      return next;
    });
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const beginCompositionResize = (event, item) => {
    if (!compositionEditing) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    activeResize.current = {
      id: item.id,
      startX: event.clientX,
      startY: event.clientY,
      width: item.width,
      height: item.height,
      scale: compositionRef.current ? compositionRef.current.clientWidth / 1100 : 1,
    };
  };

  const resizeCompositionItem = (event, id) => {
    if (!compositionEditing || activeResize.current?.id !== id) return;
    event.preventDefault();
    event.stopPropagation();
    const scale = Math.max(0.1, activeResize.current.scale);
    const width = Math.min(360, Math.max(8, activeResize.current.width + (event.clientX - activeResize.current.startX) / scale));
    const height = Math.min(240, Math.max(6, activeResize.current.height + (event.clientY - activeResize.current.startY) / scale));
    activeResize.current = { ...activeResize.current, nextWidth: width, nextHeight: height };
    const shape = event.currentTarget.parentElement;
    shape.style.width = `${(width / 1100) * 100}%`;
    shape.style.height = "auto";
    shape.style.aspectRatio = `${width} / ${height}`;
  };

  const finishCompositionResize = (event, id) => {
    if (activeResize.current?.id !== id) return;
    event.preventDefault();
    event.stopPropagation();
    const width = activeResize.current.nextWidth ?? activeResize.current.width;
    const height = activeResize.current.nextHeight ?? activeResize.current.height;
    activeResize.current = null;
    setCompositionElements((current) => {
      const next = current.map((item) => item.id === id ? { ...item, width, height } : item);
      storeCompositionItems(next);
      return next;
    });
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const nudgeCompositionItem = (event, id) => {
    if (compositionEditing && ["Delete", "Backspace"].includes(event.key)) {
      event.preventDefault();
      deleteCompositionItem(id);
      return;
    }
    if (!compositionEditing || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();

    if (event.altKey) {
      const delta = event.shiftKey ? 10 : 2;
      setCompositionElements((items) => {
        const next = items.map((item) => {
          if (item.id !== id) return item;
          const horizontal = event.key === "ArrowLeft" ? -delta : event.key === "ArrowRight" ? delta : 0;
          const vertical = event.key === "ArrowUp" ? -delta : event.key === "ArrowDown" ? delta : 0;
          return {
            ...item,
            width: Math.min(360, Math.max(8, item.width + horizontal)),
            height: Math.min(240, Math.max(6, item.height + vertical)),
          };
        });
        storeCompositionItems(next);
        return next;
      });
      return;
    }

    const delta = event.shiftKey ? 1 : 0.25;
    setCompositionElements((items) => {
      const next = items.map((item) => item.id === id ? {
        ...item,
        x: Math.min(98, Math.max(2, item.x + (event.key === "ArrowRight" ? delta : event.key === "ArrowLeft" ? -delta : 0))),
        y: Math.min(96, Math.max(4, item.y + (event.key === "ArrowDown" ? delta : event.key === "ArrowUp" ? -delta : 0))),
      } : item);
      storeCompositionItems(next);
      return next;
    });
  };

  const resetComposition = () => {
    setCompositionElements(defaultCompositionItems);
    setSelectedCompositionId(null);
    storeCompositionItems(defaultCompositionItems);
  };

  const addCompositionItem = () => {
    const preset = compositionPresets[newCompositionKind];
    const id = `custom-${Date.now()}`;
    const item = {
      id,
      label: `新增${preset.label}`,
      kind: newCompositionKind,
      layer: "front",
      x: 50,
      y: 50,
      width: preset.width,
      height: preset.height,
    };
    setCompositionElements((current) => {
      const next = [...current, item];
      storeCompositionItems(next);
      return next;
    });
    setSelectedCompositionId(id);
  };

  const deleteCompositionItem = (id = selectedCompositionId) => {
    if (!id) return;
    setCompositionElements((current) => {
      const next = current.filter((item) => item.id !== id);
      storeCompositionItems(next);
      return next;
    });
    setSelectedCompositionId((current) => current === id ? null : current);
  };

  const setCompositionLayer = (layer) => {
    if (!selectedCompositionId) return;
    setCompositionElements((current) => {
      const next = current.map((item) => item.id === selectedCompositionId ? { ...item, layer } : item);
      storeCompositionItems(next);
      return next;
    });
  };

  const updateCoverSetting = (key, value) => {
    setCoverSettings((current) => ({ ...current, [key]: value }));
    const storesAsToggle = key === "compositionEnabled" || key === "portraitHazeEnabled";
    storeLocalPreference(
      coverSettingStorageKeys[key],
      storesAsToggle ? (value ? "on" : "off") : value,
    );
  };

  const updateIdentityName = (key, value) => {
    setIdentityNames((current) => ({ ...current, [key]: value }));
    setIdentityStatus("名字尚未保存。");
  };

  const startCompositionEditing = () => {
    if (!coverSettings.compositionEnabled) updateCoverSetting("compositionEnabled", true);
    onShowCover();
    setSelectedCompositionId(null);
    setCompositionEditing(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  return (
    <>
      <section
        className={`awake-experience${awakeEntry === "content" ? " awake-experience--content" : ""}`}
        aria-label="醒来"
        aria-hidden={shadowReaderOpen || dreamReaderOpen ? "true" : undefined}
        hidden={activeArea !== "醒来"}
      >
        <div className="awake-stage">
          <div
            className={`cover${coverSettings.compositionEnabled ? "" : " composition-is-off"}${compositionEditing ? " composition-is-editing" : ""}`}
          >
            <img
              className="cover__image"
              src={images.hero}
              alt="雨后窗外的树影"
              style={{ "--cover-fade-start": `${coverSettings.fadeStart}%` }}
            />
            <div
              className={`cover__portrait-haze-shell${coverSettings.portraitHazeEnabled ? "" : " is-off"}`}
              aria-hidden="true"
            >
              <span className="cover__portrait-haze" />
            </div>
            <div
              ref={compositionRef}
              className="cover__composition"
              aria-label={compositionEditing ? "黑白构成层调整画板" : undefined}
              aria-hidden={compositionEditing ? undefined : "true"}
            >
              {compositionElements.map((item) => (
                <span
                  className={`composition-shape composition-shape--${item.kind} is-${item.layer}${selectedCompositionId === item.id ? " is-selected" : ""}`}
                  key={item.id}
                  role={compositionEditing ? "button" : undefined}
                  tabIndex={compositionEditing ? 0 : -1}
                  aria-pressed={compositionEditing ? selectedCompositionId === item.id : undefined}
                  aria-label={compositionEditing ? `移动${item.label}` : undefined}
                  style={{
                    left: `${item.x}%`,
                    top: `${item.y}%`,
                    width: `${(item.width / 1100) * 100}%`,
                    height: "auto",
                    aspectRatio: `${item.width} / ${item.height}`,
                  }}
                  onPointerDown={(event) => beginCompositionDrag(event, item.id)}
                  onPointerMove={(event) => moveCompositionItem(event, item.id)}
                  onPointerUp={(event) => finishCompositionDrag(event, item.id)}
                  onPointerCancel={(event) => finishCompositionDrag(event, item.id)}
                  onKeyDown={(event) => nudgeCompositionItem(event, item.id)}
                >
                  {compositionEditing && selectedCompositionId === item.id ? (
                    <i
                      className="composition-resize-handle"
                      aria-hidden="true"
                      onPointerDown={(event) => beginCompositionResize(event, item)}
                      onPointerMove={(event) => resizeCompositionItem(event, item.id)}
                      onPointerUp={(event) => finishCompositionResize(event, item.id)}
                      onPointerCancel={(event) => finishCompositionResize(event, item.id)}
                    />
                  ) : null}
                </span>
              ))}
            </div>
            {compositionEditing ? (
              <div className="composition-editor" aria-label="构成层工具">
                <label>
                  <span>新增元素</span>
                  <select value={newCompositionKind} onChange={(event) => setNewCompositionKind(event.target.value)}>
                    {Object.entries(compositionPresets).map(([value, preset]) => (
                      <option value={value} key={value}>{preset.label}</option>
                    ))}
                  </select>
                </label>
                <button type="button" onClick={addCompositionItem}>增加</button>
                <button type="button" disabled={!selectedCompositionId} onClick={() => deleteCompositionItem()}>删除</button>
                <button type="button" disabled={!selectedCompositionId} onClick={() => setCompositionLayer("behind")}>头像下方</button>
                <button type="button" disabled={!selectedCompositionId} onClick={() => setCompositionLayer("front")}>置顶</button>
                <button type="button" onClick={resetComposition}>重置</button>
                <button
                  type="button"
                  onClick={() => {
                    setCompositionEditing(false);
                    setSelectedCompositionId(null);
                  }}
                >
                  <PencilSimple size={14} weight="light" aria-hidden="true" />
                  完成
                </button>
              </div>
            ) : null}
            <div className="cover__identity" aria-label={`${identityName("user")}和${identityName("assistant")}`}>
              <div className="cover-person">
                <Avatar person={resolvedPeople[0]} src={images.user} onReplace={(file) => replace("user", file)} />
                <h1>{resolvedPeople[0].name}</h1>
              </div>

              <div className="together-mark">
                <span className="together-mark__line" aria-hidden="true" />
                <MoonStars size={24} weight="light" aria-hidden="true" />
                <span className="together-mark__line" aria-hidden="true" />
                <strong>{togetherCaption(meetingDate, today)}</strong>
              </div>

              <div className="cover-person">
                <Avatar person={resolvedPeople[1]} src={images.assistant} onReplace={(file) => replace("assistant", file)} />
                <h1>{resolvedPeople[1].name}</h1>
              </div>

              <div className="cover__tagline">
                <p>{coverSettings.tagline}</p>
              </div>
            </div>
          </div>

          <div className="white-veil" aria-hidden="true" />

          <div className="content-view">
            <header className="content-header reveal">
              <div>
                <span className="content-header__date">Serein</span>
                <h2>醒来</h2>
              </div>
              <p>做了什么梦呢</p>
            </header>

            <section className="portrait-section" aria-label="个人介绍">
              {imageStatus && <p role="status">{imageStatus}</p>}
              <div className="portrait-pair">
                {resolvedPeople.map((person) => (
                  <article className="portrait reveal" key={person.key}>
                    <Avatar
                      person={person}
                      src={images[person.key]}
                      size="small"
                      editable
                      busy={imageBusy}
                      onReplace={(file) => replace(person.key, file)}
                    />
                    <div className="portrait__copy">
                      <div className="portrait__name-row">
                        <h4>{person.name}</h4>
                      </div>
                      <PortraitIntro name={person.name} text={person.detail} source={person.source} />
                    </div>
                  </article>
                ))}
              </div>
            </section>

            <div className="lower-grid">
              <section className="window-shadow reveal" aria-labelledby="shadow-title">
                <div className="section-heading">
                  <h3 id="shadow-title">上一窗影</h3>
                  <span>{shadowsEnabled ? latestShadow?.relativeLabel || "" : ""}</span>
                </div>
                <p>{!shadowsEnabled ? "窗影尚未开启，可以在设置中启用。" : shadowsStatus === "loading" ? "正在读回上一窗。" : shadowsStatus === "error" ? "窗影暂时没有接通。" : latestShadow?.summary || "还没有窗影。"}</p>
                {!shadowsEnabled ? <button type="button" onClick={() => onSettingsOpenChange(true)}>设置窗影</button> : latestShadow && <button
                  ref={shadowTriggerRef}
                  type="button"
                  onClick={() => {
                    if (!latestShadow) return;
                    setSelectedShadowId(latestShadow.id);
                    setShadowReaderOpen(true);
                  }}
                >
                  读那一窗
                </button>}
              </section>

              <section className="dream-preview reveal" aria-labelledby="dream-preview-title">
                <div className="section-heading">
                  <h3 id="dream-preview-title">梦境</h3>
                  <span>{latestDream ? latestDream.dateLabel : ""}</span>
                </div>
                <p>{dreamsStatus === "loading"
                  ? "梦还在雾里。"
                  : dreamsStatus === "error"
                    ? "梦境暂时没有接通。"
                    : dreamExcerpt(latestDream)}</p>
                {latestDream ? (
                  <button
                    ref={dreamTriggerRef}
                    type="button"
                    onClick={() => {
                      setSelectedDreamId(latestDream.id);
                      setDreamReaderOpen(true);
                    }}
                  >
                    翻开梦境
                  </button>
                ) : null}
              </section>
            </div>
          </div>

          <Sidebar
            activeArea={activeArea}
            onNavigate={navigateFromAwake}
            onUnavailable={onUnavailable}
            onOpenSettings={() => {
              setCompositionEditing(false);
              setSelectedCompositionId(null);
              onSettingsOpenChange(true);
            }}
          />
        </div>
      </section>

      {settingsOpen && <Sidebar activeArea="设置" onNavigate={navigateFromAwake} onOpenSettings={() => {}} />}
      <SettingsPanel
        open={settingsOpen}
        onOpenEventGuide={onOpenEventGuide}
        onClose={() => onSettingsOpenChange(false)}
        coverSettings={coverSettings}
        onCoverSetting={updateCoverSetting}
        identityNames={identityNames}
        onNameChange={updateIdentityName}
        onSaveIdentity={saveIdentity}
        identityStatus={identityStatus}
        identityBusy={identityBusy}
        anniversary={{ draft: meetingDateDraft, today, busy: meetingDateBusy, status: meetingDateStatus, onChange: value => { setMeetingDateDraft(value); setMeetingDateStatus(""); }, onSave: saveMeetingDate }}
        images={images}
        imageBusy={imageBusy}
        imageStatus={imageStatus}
        onReplace={replace}
        onEditComposition={startCompositionEditing}
        onResetComposition={resetComposition}
      />

      <WindowShadowReader
        open={shadowsEnabled && shadowReaderOpen}
        shadows={windowShadows}
        selectedShadowId={selectedShadowId}
        onSelect={setSelectedShadowId}
        onClose={closeShadowReader}
      />

      <DreamReader
        open={dreamReaderOpen}
        dreams={dreams}
        selectedDreamId={selectedDreamId}
        onSelect={setSelectedDreamId}
        onDreamLoaded={storeLoadedDream}
        onClose={closeDreamReader}
      />
    </>
  );
}

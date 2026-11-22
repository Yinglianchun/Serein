import { defaultWindowShadows } from "../data/windowShadows.js";

export function windowShadowViews(shadow) {
  const sections = shadow?.sections;
  if (sections && typeof sections === "object" && ("user_view" in sections || "self_view" in sections)) {
    return {
      user: typeof sections.user_view === "string" ? sections.user_view.trim() : "",
      assistant: typeof sections.self_view === "string" ? sections.self_view.trim() : "",
    };
  }
  // Older retained shadows may have only the authored Markdown sections.
  const views = { user: "", assistant: "" };
  let current = null;
  let level = 0;
  let fence = null;
  for (const line of String(shadow?.text ?? "").split(/\r?\n/)) {
    const marker = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null;
    }
    const heading = !fence && !marker && line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const key = { "我眼中的你": "user", "我眼中的自己": "assistant" }[heading[2]];
      if (key) { current = key; level = heading[1].length; continue; }
      if (heading[1].length <= level) current = null;
    }
    if (current) views[current] += `${line}\n`;
  }
  return { user: views.user.trim(), assistant: views.assistant.trim() };
}

export function windowShadowPortraits(shadows) {
  const views = shadows?.[0]?.portraitViews;
  if (!views || typeof views !== "object") {
    const latest = windowShadowViews(shadows?.[0]);
    return { user: { text: latest.user, source: "" }, assistant: { text: latest.assistant, source: "" } };
  }
  const portrait = (key) => {
    const view = views[key];
    const text = typeof view?.text === "string" ? view.text.trim() : "";
    const source = text && Number(view?.position) > 1
      ? `沿用倒数第 ${view.position} 篇窗影 · ${view.title || "旧窗影"}` : "";
    return { text, source };
  };
  return { user: portrait("user_view"), assistant: portrait("self_view") };
}

function normalizeShadow(shadow) {
  if (
    !shadow
    || typeof shadow.id !== "string"
    || typeof shadow.closedAt !== "string"
    || typeof shadow.dateLabel !== "string"
    || typeof shadow.timeLabel !== "string"
    || typeof shadow.title !== "string"
    || typeof shadow.summary !== "string"
    || typeof shadow.text !== "string"
  ) return null;

  return {
    ...shadow,
    relativeLabel: typeof shadow.relativeLabel === "string"
      ? shadow.relativeLabel
      : `${shadow.dateLabel} ${shadow.timeLabel}`,
    scenes: Array.isArray(shadow.scenes)
      ? shadow.scenes.filter((scene) => (
        scene
        && typeof scene.id === "string"
        && typeof scene.title === "string"
      ))
      : [],
    sourceLabel: typeof shadow.sourceLabel === "string" ? shadow.sourceLabel : "",
    statusLabel: typeof shadow.statusLabel === "string" ? shadow.statusLabel : "",
    documentOwnsTitle: shadow.documentOwnsTitle === true,
  };
}

async function readWindowShadowResponse(response) {
  if (!response.ok) return null;
  const payload = await response.json();
  if (
    !payload
    || typeof payload.snapshotId !== "string"
    || !Array.isArray(payload.shadows)
  ) return null;

  const shadows = payload.shadows.map(normalizeShadow).filter(Boolean);
  if (shadows.length && payload.portraitViews) shadows[0].portraitViews = payload.portraitViews;
  return shadows;
}

export async function loadWindowShadows() {
  try {
    const liveResponse = await fetch("/__serein/live/window-shadows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const liveShadows = await readWindowShadowResponse(liveResponse);
    if (liveShadows) return liveShadows;
  } catch {}

  return null;
}

export function readFallbackWindowShadows() {
  return defaultWindowShadows;
}

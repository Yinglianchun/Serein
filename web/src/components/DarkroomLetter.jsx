import { useLayoutEffect, useRef, useState } from "react";
import { MarkdownProjection } from "./MarkdownProjection.jsx";

const readKey = "serein.darkroom.read.v1";
const readLetters = new Set();

function hasRead(id) {
  try {
    const saved = JSON.parse(window.localStorage.getItem(readKey));
    if (Array.isArray(saved)) saved.forEach((key) => readLetters.add(key));
  } catch { /* Viewing still works without local storage. */ }
  return readLetters.has(id);
}

function remember(id) {
  readLetters.add(id);
  try { window.localStorage.setItem(readKey, JSON.stringify([...readLetters])); } catch { /* Session fallback. */ }
}

// Reveal rendered text instead of slicing Markdown, so links, lists and emoji stay intact.
export function DarkroomLetter({ id, content }) {
  const bodyRef = useRef(null);
  const finishRef = useRef(() => {});
  const [typing, setTyping] = useState(false);
  const source = Array.isArray(content) ? content.join("\n\n") : String(content || "");

  useLayoutEffect(() => {
    const motion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (hasRead(id) || motion?.matches) {
      setTyping(false);
      remember(id);
      return undefined;
    }
    const body = bodyRef.current.querySelector(".markdown-projection");
    const walker = document.createTreeWalker(body, window.NodeFilter.SHOW_TEXT);
    const segmenter = typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;
    const texts = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const text = node.data;
      if (!text) continue;
      const letters = segmenter ? [...segmenter.segment(text)].map((item) => item.segment) : Array.from(text);
      texts.push({ node, text, letters });
    }
    if (!texts.length) { remember(id); return undefined; }
    const cursor = document.createElement("span");
    cursor.className = "darkroom-typewriter__cursor";
    cursor.setAttribute("aria-hidden", "true");
    texts.forEach(({ node }) => { node.data = ""; });
    texts[0].node.after(cursor);
    let index = 0, count = 0, timer, finished = false;
    const restore = () => {
      window.clearInterval(timer);
      texts.forEach(({ node, text }) => { node.data = text; });
      cursor.remove();
    };
    const finish = () => {
      if (finished) return;
      finished = true;
      motion?.removeEventListener?.("change", onMotion);
      restore(); remember(id); setTyping(false);
    };
    finishRef.current = finish;
    setTyping(true);
    timer = window.setInterval(() => {
      const current = texts[index];
      current.node.data += current.letters[count++];
      current.node.after(cursor);
      if (count >= current.letters.length) { index++; count = 0; }
      if (index >= texts.length) finish();
    }, 28);
    const onMotion = (event) => { if (event.matches) finish(); };
    motion?.addEventListener?.("change", onMotion);
    return () => {
      restore(); finishRef.current = () => {};
      motion?.removeEventListener?.("change", onMotion);
    };
  }, [id, source]);

  return <div ref={bodyRef} className={`darkroom-typewriter${typing ? " is-typing" : ""}`}>
    {typing ? <button className="darkroom-typewriter__skip" type="button" onClick={() => finishRef.current()}>显示全文</button> : null}
    <div onClick={(event) => { if (!event.target.closest("a")) finishRef.current(); }}>
      <MarkdownProjection className="darkroom-open-room__content" content={source} />
    </div>
  </div>;
}

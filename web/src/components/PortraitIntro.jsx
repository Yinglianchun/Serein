import { useEffect, useId, useRef, useState } from "react";
import { ReadingLayer } from "./ReadingLayer.jsx";

export function PortraitIntro({ name, text, source = "" }) {
  const article = useRef(null);
  const trigger = useRef(null);
  const closeButton = useRef(null);
  const titleId = useId();
  const [reading, setReading] = useState(false);
  const characters = Array.from(text.replace(/\s+/gu, " ").trim());
  const truncated = characters.length > 180;
  useEffect(() => {
    if (!reading) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    article.current?.focus({ preventScroll: true });
    const onKeyDown = event => {
      if (event.key === 'Escape') setReading(false);
      if (event.key === 'Tab') {
        event.preventDefault();
        (document.activeElement === closeButton.current ? article.current : closeButton.current)?.focus();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', onKeyDown);
      trigger.current?.focus({ preventScroll: true });
    };
  }, [reading]);
  return <>
    <p className="portrait__preview">{characters.slice(0, 180).join("")}{truncated ? "…" : ""}</p>
    {source && <small className="portrait__source">{source}</small>}
    {truncated && <button ref={trigger} className="portrait__read" type="button" aria-label={`展开${name}的介绍全文`} onClick={() => setReading(true)}>展开全文</button>}
    {reading && <ReadingLayer title={`${name}的介绍`} titleId={titleId} label="PORTRAIT" closeLabel="关闭介绍全文" closeButtonRef={closeButton} onClose={() => setReading(false)} className="portrait-layer" readerClassName="portrait-reader is-mobile-detail-open">
      <article ref={article} className="window-shadow-reading portrait-reader__body" tabIndex={0}>
        <div className="window-shadow-reading__text portrait-reader__text">{text}</div>
      </article>
    </ReadingLayer>}
  </>;
}

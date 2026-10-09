import {
  BookOpenText,
  Books,
  GearFine,
  Question,
  House,
  FlowerLotus,
  SlidersHorizontal,
  Sparkle,
  NotePencil,
  Browsers,
  CheckSquare,
} from "@phosphor-icons/react";

function ThoughtCloud({ size = 20, weight, ...props }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.25} strokeLinecap="round" strokeLinejoin="round" focusable="false" {...props}>
      <path
        d="M7.5 16.5a4.5 4.5 0 0 1-1.8-8.62 5.5 5.5 0 0 1 10.49-1.91A5.25 5.25 0 0 1 17 16.5Z"
        fill="currentColor"
        fillOpacity={weight === "fill" ? 0.16 : 0}
      />
      <circle cx="6.5" cy="19" r="1.25" />
      <circle cx="3.5" cy="21.5" r="0.65" fill="currentColor" stroke="none" />
    </svg>
  );
}

const navItems = [
  { label: "醒来", icon: House },
  { label: "记忆", icon: Sparkle },
  { label: "沉淀", icon: CheckSquare },
  { label: "叙事卷", icon: Books },
  { label: "日记", icon: BookOpenText },
  { label: "心绪", icon: ThoughtCloud },
  { label: "备忘", icon: NotePencil },
  { label: "换窗", icon: Browsers },
  { label: "地下室", icon: SlidersHorizontal },
];

export function Sidebar({ activeArea, onNavigate, onOpenSettings }) {
  return (
    <aside className="sidebar" aria-label="主要导航">
      <div className="sidebar__brand" aria-label="Serein">
        <img className="sidebar__mark" src={`${import.meta.env.BASE_URL}assets/serein-mark.svg`} width="28" height="28" alt="" />
        <span className="sidebar__name">Serein</span>
      </div>
      <nav className="sidebar__nav">
        {navItems.map(({ label, icon: Icon }) => {
          const active = label === activeArea;
          return (
            <button
              className={`nav-item${active ? " is-active" : ""}`}
              key={label}
              type="button"
              aria-label={label}
              aria-current={active ? "page" : undefined}
              onClick={() => onNavigate(label)}
            >
              <Icon size={20} weight={active ? "fill" : "light"} aria-hidden="true" />
              <span>{label}</span>
            </button>
          );
        })}
      </nav>
      <div className="sidebar__footer">
        <button className={`nav-item${activeArea === "使用说明" ? " is-active" : ""}`} type="button"
          aria-label="使用说明" aria-current={activeArea === "使用说明" ? "page" : undefined} onClick={() => onNavigate("使用说明")}>
          <Question size={20} weight="light" aria-hidden="true" /><span>使用说明</span>
        </button>
        <button
          className={`nav-item sidebar__garden${activeArea === "花园" ? " is-active" : ""}`}
          type="button"
          aria-label="花园"
          aria-current={activeArea === "花园" ? "page" : undefined}
          onClick={() => activeArea !== "花园" && onNavigate("花园")}
        >
          <FlowerLotus size={20} weight={activeArea === "花园" ? "fill" : "light"} aria-hidden="true" />
          <span>花园</span>
        </button>
        <button className={`nav-item${activeArea === "设置" ? " is-active" : ""}`} aria-current={activeArea === "设置" ? "page" : undefined} type="button" aria-label="设置" onClick={onOpenSettings}>
          <GearFine size={20} weight="light" aria-hidden="true" />
          <span>设置</span>
        </button>
      </div>
    </aside>
  );
}

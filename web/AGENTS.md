# Public frontend

Event list body previews show at most three lines before opening; the detail reader preserves the full body (Xiaoyu, 2026-10-09).

Use the approved S-and-raindrop SVG for the tab favicon and sidebar mark. Show the full serif Serein wordmark only when the sidebar is expanded; preserve the existing compact mobile navigation.
Preserve the existing layout and interaction. All default data is empty or synthetic.
Browser credentials must never contain backend bearer tokens. Node proxies use an explicit SEREIN_MEMORY_URL and SEREIN_MEMORY_TOKEN.
Favorites, annotations and comments persist in the canonical database. localStorage is a mirror.
Writer update uses old body plus new materials; rewrite uses all bound materials. Preview is not publication.
No separate self category; favorites remain explicit. Garden rendering, diary locks and deletion confirmations retain their contracts.

## 2026-09-08 navigation decision
Clicking 醒来 in navigation opens its white second page (#awake). Initial cover and composition editing remain available. Settings is a separate #settings page with normal navigation, not a modal. Preserve saved appearance preferences.

Public defaults hide composition elements and omit the Facts tab. Instance names are canonical settings, used by UI and authoring prompts; never rewrite original evidence on rename. Models are configured once, then selected per task. Keep upstream secrets server-side. Changing the embedding profile requires its own prepared index before memory injection can be enabled.

Chat exposes every configured upstream model, using the gateway.upstreams grouping and alias mapping. Do not restrict chat to a single task selector. Other task selectors use the same aggregated catalog.

## 2026-09-09 narrative writing

Domain management lives in Basement's 主域边界, with native dialogs for adding/editing labels and short descriptions. Settings has appearance, features, models and imports tabs. Save the full catalog to canonical instance settings; tagging reads it on each new request. Existing tagged memories are not automatically rewritten.
New collecting lines may save only materials. The explicit 书写 action generates and saves the first body, then updates that book immediately from the save receipt. Existing-body update/rewrite remains preview then confirmed save. Material-only saves must never erase an existing body.
When the public 夜间整理叙事卷 feature switch is enabled, the scheduled pass after 04:00 routes newly available Events, Scenes and readable Diaries into an existing Arc or a new blank collecting Arc. If the switch is off or there are no new unbound materials, it makes no model call. It reuses the configured 叙事卷找材料 model and has no separate model assignment. It never invokes Narrative Writer or authors body prose.

## 2026-09-09 Persona and memos
Persona is read-only: show runtime mood, inner thought, affect/relationship meters and event history. Relationship state is not human input. Keep Persona and memo navigation visible while disabled. Memos follow the existing reminder workflow: time window, repetition/limits, channel scope, done, archive and reopen. Use styled date/time pickers and the existing editorial fonts. No memo session field, next-due field/list label or snooze action in the UI. Preserve internal next_due_at when editing other fields. The reference is the local old Ombre repository, with these user-requested simplifications.

Persona checks for new records every 15 seconds while visible, and on return to the browser tab. Persona is the only large page title: show inner thought without a second mood-label heading. Keep expandable affect/relationship details. History defaults to a casually rotated compact deck, newest on top; clicking an exposed card expands it. Wheel or arrow controls change the active card, with newer cards stacked above and older cards below. Long card text scrolls before turning. Expanded deck wheel gestures never scroll the outer page, including at either end; move the pointer outside the deck to scroll the page. New records preserve the card currently being read and offer a jump to the new page. Card entry animation remembers seen event IDs per browser site/path and selected window; this is a local viewing preference, not canonical Persona data. First visits establish a baseline; later unseen successful evaluations coalesce into one entry animation on the newest successful card. Only acknowledge after its heading is visible and animation finished; leaving early keeps it pending. Do not force scrolling or animate failures. Respect reduced motion. The synthetic preview under web/tests must never call a backend.

## Persona handnotes motion
Use a centered narrow warm-white deck, with hidden rear body text and small date edges. Both navigation directions share one continuous GSAP position, which also determines stacking order and ink opacity. Wheel distance advances the target without a fixed lock; retarget an in-flight animation instead of queuing animations. Keep outside-page scrolling independent. Pagination stays quiet, arrows appear on hover/focus or touch devices. Preserve reduced motion and unseen-record acknowledgment.

## 2026-09-10 Settings switches and Persona header
Enable/disable actions for Persona and memos live only in Settings. Settings binary options use accessible sliding switches, not check marks; retain existing save semantics. Persona has no enabled/paused toolbar: place a quiet window selector and icon refresh alongside the current-thought eyebrow. Preserve the approved handnote cards unchanged.

Persona reading order: page heading, quiet window selector and refresh, handnote cards, then collapsed affect/relationship details. Do not repeat the current thought or add a separate history title above the deck.

## 2026-09-10 心绪 naming
User-facing Persona labels are 心绪 (page, navigation, feature switch and model selector). Keep internal persona keys, API paths and #persona links compatible. Preserve this optional feature and its current design for existing users; personal dislike of the presentation is not a request to remove or redesign it.

Awake keeps 上一窗影 beside 梦境, with a settings link while disabled; never enable its tools merely by opening the page. Remove the 画像 section heading and use 做了什么梦呢 as the page subtitle. Both portraits are read-only projections of the latest shadow's user_view / self_view, independent of instance user_description / ai_description. Keep long-text reading, show empty sections without retaining an older portrait, and refresh on entering Awake or returning to the browser tab. Never edit or save portrait prose from this page.

Appearance uses a saved 相遇日期 (identity.meeting_date) instead of editable anniversary wording. The cover shows 在一起的 XX 天, counting the meeting day as day 1 using the device's local calendar date; update at midnight and on return to the tab. Empty dates show 从这里开始. Persist the date in instance settings; localStorage is only its mirror.

Resume content checkboxes appear only while its feature switch is on. Keep favorite Scenes separate from the explicit Event/Scene picker; the picker selection persists in instance resume.selected_ids, not favorites. Model configuration has its own settings tab.

## 2026-09-12 Settings configuration
Upstream credentials and model catalogs live in 模型. Functional model assignments, automatic Event execution/limits and Agent setup guides live in the separate 配置 tab. 上游与模型 and the configured upstream list are immediately visible; each upstream card starts collapsed, hiding its models and editor until expanded. The automatic-summary switch lives in 功能 and keeps its save semantics. Settings navigation uses pale gray dashed-underlined text. Hide the outer page scrollbar while retaining wheel/touch scrolling; mobile tabs stay on one line and scroll horizontally.

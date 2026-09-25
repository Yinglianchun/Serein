"""Bounded proxy helpers adapted from verified gateway ad60c5a. See docs/proxy-provenance.md."""

from __future__ import annotations

import re, json, hashlib, time, logging

from copy import deepcopy

from typing import Any

def count_tokens_approx(text: str) -> int:
    if not text:
        return 0
    return int(len(re.findall(r"[\u4e00-\u9fff]", text)) * 1.5
               + len(re.findall(r"[a-zA-Z]+", text)) * 1.3 + len(text) * 0.05)

logger = logging.getLogger(__name__)

EXTERNAL_CONTEXT_ATTACHMENT_RE = re.compile(
    r"<attachment\b[^>]*>[\s\S]*?</attachment>",
    re.IGNORECASE,
)

SELF_CLOSING_ATTACHMENT_RE = re.compile(
    r"<attachment\b[^>]*/>",
    re.IGNORECASE,
)

WORKSPACE_ATTACHMENT_RE = re.compile(
    r"<workspace_attachment>[\s\S]*?</workspace_attachment>",
    re.IGNORECASE,
)

WORLDBOOK_RE = re.compile(
    r"<worldbook\b[^>]*>[\s\S]*?</worldbook\s*>",
    re.IGNORECASE,
)

LEADING_PROXY_SENDER_RE = re.compile(
    r"^\s*<proxy_sender\b[^>]*/>\s*",
    re.IGNORECASE,
)

LEADING_SYSTEM_PROMPT_RE = re.compile(
    r"^\s*【\s*系统提示[^】]*】\s*",
)

EXTERNAL_CONTEXT_BLOCK_TITLES = {
    "当前时间",
    "当前电量",
    "当前天气",
    "当前位置",
    "当前任务",
    "当前页面",
    "当前文件",
    "当前状态",
    "当前人设",
    "当前角色设定",
    "当前项目状态",
    "当前屏幕应用",
    "应用使用时长",
    "最近通知",
    "最近上下文",
    "近期上下文",
    "相关记忆",
    "工作区",
    "工作区结构",
    "工具结果",
    "工具返回",
    "关系天气",
    "照顾备忘",
    "照顾提醒",
    "屏幕文本",
    "Persona",
    "Recent Context",
    "Relationship Weather",
    "Care Memo",
    "Care Reminder",
}

OPERIT_STABLE_CONTEXT_TITLES = {
    "角色卡",
    "角色设定",
    "固定规则",
    "长期规则",
    "记忆规则",
    "长期偏好",
    "固定偏好",
    "系统提示",
    "工具说明",
    "工具列表",
    "工具栏",
    "使用说明",
    "System Prompt",
}

OPERIT_STABLE_CONTEXT_KEYWORDS = (
    "角色卡",
    "固定规则",
    "长期规则",
    "记忆规则",
    "固定偏好",
    "长期偏好",
    "工具说明",
    "工具列表",
    "system prompt",
)

class ClientContext:
    def __init__(self, *, operit=True):
        self.operit_context_rewrite_enabled = operit
        self.gateway_cfg = {}
        self.pending_turn_injections = {}
        self.pending_tool_reasoning = {}
        self.turn_injection_snapshot_ttl_seconds = 3600.0
        self.turn_injection_snapshot_max_per_session = 4


    @staticmethod
    def _assistant_message_has_output(assistant_message: dict[str, Any] | None) -> bool:
        if not isinstance(assistant_message, dict):
            return False
        content = assistant_message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and content:
            return True
        reasoning = assistant_message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return True
        tool_calls = assistant_message.get("tool_calls")
        return isinstance(tool_calls, list) and bool(tool_calls)

    @staticmethod
    def _reasoning_text_from_details(details: Any) -> str:
        if not isinstance(details, list):
            return ""
        return "\n".join(
            str(detail.get("text") or detail.get("summary") or "")
            for detail in details
            if isinstance(detail, dict)
            and str(detail.get("type") or "") in {"reasoning.text", "reasoning.summary"}
            and str(detail.get("text") or detail.get("summary") or "")
        )

    def _apply_explicit_anthropic_cache_control(
        self,
        payload: dict[str, Any],
        cache_control: dict[str, str],
        model: str = "",
    ) -> None:
        self._attach_cache_control_to_anthropic_content(payload, "system", cache_control)
        self._attach_cache_control_to_anthropic_tools(payload, cache_control)
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            return

        breakpoint_index = self._find_cache_breakpoint(messages, model=model)
        if breakpoint_index is None:
            return
        message = messages[breakpoint_index]
        if isinstance(message, dict):
            self._attach_cache_control_to_anthropic_content(message, "content", cache_control)

    @staticmethod
    def _cache_min_tokens_for_model(model: str) -> int:
        lowered = str(model or "").lower()
        if "sonnet" in lowered:
            return 2048
        return 4096

    @staticmethod
    def _cache_tail_tokens_for_model(model: str) -> int:
        return 4000

    def _find_cache_breakpoint(self, messages: list[Any], *, model: str = "") -> int | None:
        if not isinstance(messages, list) or len(messages) < 3:
            return None
        min_tokens = self._cache_min_tokens_for_model(model)
        tail_target = self._cache_tail_tokens_for_model(model)
        estimates = [
            self._anthropic_message_token_estimate(message)
            if isinstance(message, dict)
            else count_tokens_approx(str(message or ""))
            for message in messages
        ]
        prefix_tokens = sum(estimates)
        tail_tokens = 0
        for index in range(len(messages) - 2, -1, -1):
            tail_tokens += estimates[index + 1]
            prefix_tokens -= estimates[index + 1]
            message = messages[index]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            if prefix_tokens >= min_tokens and tail_tokens >= tail_target:
                return index
        return None

    def _anthropic_message_token_estimate(self, message: dict[str, Any]) -> int:
        if not isinstance(message, dict):
            return 0
        return count_tokens_approx(
            " ".join(
                part
                for part in (
                    str(message.get("role") or ""),
                    self._anthropic_content_text(message.get("content")),
                )
                if part
            )
        )

    def _anthropic_content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text")
                    if text is not None:
                        parts.append(str(text))
                    else:
                        parts.append(json.dumps(block, ensure_ascii=False, sort_keys=True, default=str))
            return "\n".join(parts)
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)

    def _attach_cache_control_to_anthropic_tools(
        self,
        payload: dict[str, Any],
        cache_control: dict[str, str],
    ) -> bool:
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return False
        for tool in reversed(tools):
            if not isinstance(tool, dict):
                continue
            if tool.get("cache_control"):
                return True
            tool["cache_control"] = deepcopy(cache_control)
            return True
        return False

    def _attach_cache_control_to_anthropic_content(
        self,
        container: dict[str, Any],
        field: str,
        cache_control: dict[str, str],
    ) -> bool:
        content = container.get(field)
        if isinstance(content, str):
            if not content.strip():
                return False
            container[field] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": deepcopy(cache_control),
                }
            ]
            return True
        if not isinstance(content, list):
            return False
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("cache_control"):
                return True
            if block.get("type") in {"text", "image", "document", "tool_result"}:
                block["cache_control"] = deepcopy(cache_control)
                return True
        return False

    def _extract_current_turn_user_query(self, messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "system":
                continue
            if role != "user":
                return ""
            content = self._coerce_message_text(message.get("content"))
            # A worldbook can contain many entries. Exclude the whole envelope
            # from recall while preserving the original message for forwarding.
            content = WORLDBOOK_RE.sub("\n", content)
            cleaned = self._strip_external_context_from_user_text(content)
            if cleaned:
                return cleaned
            continue
        return ""

    def _coerce_message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type in {"text", "input_text"}:
                    text = item.get("text") or item.get("input_text") or ""
                    if text:
                        chunks.append(str(text))
            return "\n".join(chunks)
        return ""

    def _strip_external_context_from_user_text(self, text: str) -> str:
        cleaned = WORKSPACE_ATTACHMENT_RE.sub("", str(text or ""))
        cleaned = EXTERNAL_CONTEXT_ATTACHMENT_RE.sub("", cleaned)
        cleaned = SELF_CLOSING_ATTACHMENT_RE.sub("", cleaned)
        cleaned = self._strip_leading_auto_context_markers(cleaned)
        return self._strip_external_context_blocks(cleaned)

    def _strip_leading_auto_context_markers(self, text: str) -> str:
        cleaned = str(text or "")
        while True:
            previous = cleaned
            cleaned = LEADING_PROXY_SENDER_RE.sub("", cleaned, count=1)
            cleaned = LEADING_SYSTEM_PROMPT_RE.sub("", cleaned, count=1)
            if cleaned == previous:
                return cleaned

    def _strip_external_context_blocks(self, text: str, protected_markers: tuple[str, ...] = ()) -> str:
        kept: list[str] = []
        skipping = False
        for line in str(text or "").splitlines():
            stripped = line.strip()
            title = ""
            if stripped.startswith("【") and "】" in stripped:
                title = stripped[1 : stripped.index("】")].strip()
            if title:
                skipping = title in EXTERNAL_CONTEXT_BLOCK_TITLES
                if skipping:
                    kept.extend(marker for marker in protected_markers if marker in line)
                    continue
            if skipping and protected_markers:
                kept.extend(marker for marker in protected_markers if marker in line)
                continue
            if not skipping:
                kept.append(line)
        return "\n".join(kept).strip()

    def _inject_context_messages(
        self,
        messages: list[dict],
        stable_context: str,
        dynamic_context: str,
        trailing_context: str = "",
    ) -> list[dict]:
        new_messages = deepcopy(messages)
        if stable_context.strip():
            stable_message = {"role": "system", "content": stable_context}
            if new_messages and isinstance(new_messages[0], dict) and new_messages[0].get("role") == "system":
                new_messages.insert(1, stable_message)
            else:
                new_messages.insert(0, stable_message)
        if dynamic_context.strip():
            current_user_index = self._current_turn_user_index(new_messages)
            if current_user_index is not None:
                new_messages[current_user_index] = self._prepend_dynamic_context_to_user_message(
                    new_messages[current_user_index],
                    dynamic_context,
                )
            else:
                dynamic_message = {"role": "system", "content": dynamic_context}
                insert_at = self._after_leading_system_index(new_messages)
                new_messages.insert(insert_at, dynamic_message)
        if trailing_context.strip():
            current_user_index = self._current_turn_user_index(new_messages)
            if current_user_index is not None:
                new_messages[current_user_index] = self._append_dynamic_context_to_user_message(
                    new_messages[current_user_index],
                    trailing_context,
                )
            else:
                trailing_message = {"role": "system", "content": trailing_context}
                insert_at = self._after_leading_system_index(new_messages)
                new_messages.insert(insert_at, trailing_message)
        return new_messages

    def _remember_turn_injection_snapshot(
        self,
        session_id: str,
        source_messages: list[dict],
        prepared_payload: dict[str, Any],
        *,
        stable_context: str,
        dynamic_context: str,
        retain_unchanged: bool = False,
    ) -> str:
        prepared_messages = prepared_payload.get("messages")
        if not isinstance(source_messages, list) or not isinstance(prepared_messages, list):
            return ""
        if prepared_messages == source_messages and not retain_unchanged:
            return ""

        now = time.monotonic()
        self._prune_turn_injection_snapshots(now)
        source_digest = self._turn_injection_messages_digest(source_messages)
        contract_digest = self._turn_injection_contract_digest(prepared_payload)
        snapshot_key = f"{source_digest}:{contract_digest}"
        session_cache = self.pending_turn_injections.setdefault(session_id, {})
        session_cache[snapshot_key] = {
            "source_message_count": len(source_messages),
            "source_digest": source_digest,
            "contract_digest": contract_digest,
            "prepared_messages": deepcopy(prepared_messages),
            "stable_context": str(stable_context or ""),
            "dynamic_context": str(dynamic_context or ""),
            "created_at": now,
            "last_used_at": now,
        }
        while len(session_cache) > self.turn_injection_snapshot_max_per_session:
            oldest_key = min(
                session_cache,
                key=lambda key: float(session_cache[key].get("last_used_at", 0.0)),
            )
            session_cache.pop(oldest_key, None)
        logger.info(
            "Gateway cached turn injection snapshot | session=%s snapshot=%s messages=%s",
            session_id,
            snapshot_key[:12],
            len(source_messages),
        )
        return snapshot_key

    def _find_turn_injection_snapshot(
        self,
        session_id: str,
        incoming_messages: list[dict],
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        now = time.monotonic()
        self._prune_turn_injection_snapshots(now)
        session_cache = self.pending_turn_injections.get(session_id)
        if not session_cache:
            return "", None

        contract_digest = self._turn_injection_contract_digest(payload)
        candidates = sorted(
            session_cache.items(),
            key=lambda item: int(item[1].get("source_message_count", 0)),
            reverse=True,
        )
        prefix_digests: dict[int, str] = {}
        for snapshot_key, snapshot in candidates:
            if snapshot.get("contract_digest") != contract_digest:
                continue
            source_message_count = int(snapshot.get("source_message_count", 0))
            if source_message_count <= 0 or len(incoming_messages) < source_message_count:
                continue
            if source_message_count not in prefix_digests:
                prefix_digests[source_message_count] = self._turn_injection_messages_digest(
                    incoming_messages[:source_message_count]
                )
            if prefix_digests[source_message_count] != snapshot.get("source_digest"):
                continue
            snapshot["last_used_at"] = now
            logger.info(
                "Gateway reused turn injection snapshot | session=%s snapshot=%s messages=%s",
                session_id,
                snapshot_key[:12],
                source_message_count,
            )
            return snapshot_key, snapshot
        return "", None

    def _update_turn_injection_snapshot_after_assistant(
        self,
        session_id: str,
        injection_debug: dict[str, Any] | None,
        assistant_message: dict[str, Any] | None,
    ) -> None:
        if not self._assistant_message_has_output(assistant_message):
            return
        if self._tool_call_signature(assistant_message):
            return
        snapshot_debug = (
            injection_debug.get("turn_injection_snapshot")
            if isinstance(injection_debug, dict)
            else None
        )
        snapshot_key = str(
            (snapshot_debug.get("snapshot_key") or "")
            if isinstance(snapshot_debug, dict)
            else ""
        ).strip()
        if not snapshot_key:
            return
        session_cache = self.pending_turn_injections.get(session_id)
        if not session_cache:
            return
        removed = session_cache.pop(snapshot_key, None)
        if not session_cache:
            self.pending_turn_injections.pop(session_id, None)
        if removed is not None:
            snapshot_debug["cleared_after_response"] = True
            logger.info(
                "Gateway cleared turn injection snapshot | session=%s snapshot=%s",
                session_id,
                snapshot_key[:12],
            )

    def _prune_turn_injection_snapshots(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self.turn_injection_snapshot_ttl_seconds
        for session_id, session_cache in list(self.pending_turn_injections.items()):
            for snapshot_key, snapshot in list(session_cache.items()):
                if float(snapshot.get("last_used_at", 0.0)) < cutoff:
                    session_cache.pop(snapshot_key, None)
            if not session_cache:
                self.pending_turn_injections.pop(session_id, None)

    @staticmethod
    def _turn_injection_messages_digest(messages: list[dict]) -> str:
        serialized = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _turn_injection_contract_digest(self, payload: dict[str, Any]) -> str:
        contract = {
            key: deepcopy(payload.get(key))
            for key in (
                "model",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
            )
            if key in payload
        }
        return self._turn_injection_messages_digest([contract])

    def _current_turn_user_index(self, messages: list[dict]) -> int | None:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "system":
                continue
            if role == "user":
                content = self._coerce_message_text(message.get("content"))
                if self._strip_external_context_from_user_text(content):
                    return index
                continue
            return None
        return None

    def _after_leading_system_index(self, messages: list[dict]) -> int:
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "system":
                return index
        return len(messages)

    def _prepend_dynamic_context_to_user_message(
        self,
        message: dict[str, Any],
        dynamic_context: str,
    ) -> dict[str, Any]:
        updated = deepcopy(message)
        prefix = (
            "<serein_live_context>\n"
            f"{dynamic_context}\n"
            "</serein_live_context>\n\n"
            "Current user message:\n"
        )
        content = updated.get("content")
        if isinstance(content, str):
            updated["content"] = prefix + content
        elif isinstance(content, list):
            updated["content"] = [{"type": "text", "text": prefix}, *deepcopy(content)]
        else:
            updated["content"] = prefix
        return updated

    def _append_dynamic_context_to_user_message(
        self,
        message: dict[str, Any],
        dynamic_context: str,
    ) -> dict[str, Any]:
        updated = deepcopy(message)
        suffix = (
            "\n\n<serein_current_time>\n"
            f"{dynamic_context}\n"
            "</serein_current_time>"
        )
        content = updated.get("content")
        if isinstance(content, str):
            updated["content"] = content + suffix
        elif isinstance(content, list):
            updated["content"] = [*deepcopy(content), {"type": "text", "text": suffix.lstrip()}]
        else:
            updated["content"] = suffix.lstrip()
        return updated

    def _operit_context_rewrite_debug_base(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self, "operit_context_rewrite_enabled", False)),
            "applied": False,
            "skip_reason": "disabled" if not getattr(self, "operit_context_rewrite_enabled", False) else "",
            "stable_chars": 0,
            "activity_chars": 0,
            "cleaned_message_count": 0,
            "dropped_message_count": 0,
            "incoming_roles": [],
            "incoming_system_chars": 0,
            "incoming_system_count": 0,
            "incoming_operit_titles": [],
            "incoming_system_titles": [],
            "incoming_user_titles": [],
            "incoming_system_outlines": [],
            "operit_stable_titles": [],
            "operit_activity_titles": [],
        }

    def _rewrite_operit_context_for_forward(
        self,
        messages: list[dict],
    ) -> tuple[list[dict], str, str, dict[str, Any]]:
        debug = self._operit_context_rewrite_debug_base()
        if not self.operit_context_rewrite_enabled:
            return messages, "", "", debug
        debug["skip_reason"] = ""
        if not isinstance(messages, list) or not messages:
            debug["skip_reason"] = "invalid_messages"
            return messages, "", "", debug
        debug.update(self._operit_incoming_debug(messages))
        current_user_index = self._current_turn_user_index(messages)
        if self._messages_are_tool_continuation(messages, current_user_index):
            debug["skip_reason"] = "tool_protocol"
            return messages, "", "", debug
        if self._messages_contain_non_text_content(messages):
            debug["skip_reason"] = "non_text_content"
            return messages, "", "", debug
        if not any(self._message_contains_operit_context(message) for message in messages):
            debug["skip_reason"] = "no_operit_context"
            return messages, "", "", debug

        stable_parts: list[str] = []
        activity_parts: list[str] = []
        rewritten: list[dict] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                rewritten.append(deepcopy(message))
                continue
            content = message.get("content")
            if not isinstance(content, str):
                rewritten.append(deepcopy(message))
                continue
            cleaned, stable, activity, found = self._split_operit_context_from_user_text(content)
            if not found:
                rewritten.append(deepcopy(message))
                continue
            debug["cleaned_message_count"] += 1
            stable_parts.extend(stable)
            if current_user_index is not None and index >= current_user_index:
                activity_parts.extend(activity)
            if cleaned:
                updated = deepcopy(message)
                updated["content"] = cleaned
                rewritten.append(updated)
            else:
                debug["dropped_message_count"] += 1

        stable_context = self._format_operit_context_block(
            "Client-provided stable Operit context extracted from attachments. Treat it as private app context, not user speech.",
            stable_parts,
            max_chars=1800,
        )
        activity_context = self._format_operit_context_block(
            "Client-provided current Operit activity context extracted from attachments. Use only if it helps the current reply.",
            activity_parts,
            max_chars=1800,
        )
        if debug["cleaned_message_count"] <= 0:
            debug["skip_reason"] = "no_rewriteable_context"
            return messages, "", "", debug
        debug["applied"] = True
        debug["stable_chars"] = len(stable_context)
        debug["activity_chars"] = len(activity_context)
        debug["operit_stable_titles"] = self._operit_part_titles(stable_parts)
        debug["operit_activity_titles"] = self._operit_part_titles(activity_parts)
        return rewritten, stable_context, activity_context, debug

    def _operit_incoming_debug(self, messages: list[dict]) -> dict[str, Any]:
        roles: list[str] = []
        titles: list[str] = []
        system_titles: list[str] = []
        user_titles: list[str] = []
        system_outlines: list[dict[str, Any]] = []
        system_chars = 0
        system_count = 0
        for index, message in enumerate(messages or []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            roles.append(role)
            text = self._coerce_message_text(message.get("content"))
            message_titles = self._operit_titles_from_text(text)
            if role == "system":
                system_count += 1
                system_chars += len(text)
                system_titles.extend(message_titles)
                system_outlines.append(self._system_debug_outline(index, text, message_titles))
            elif role == "user":
                user_titles.extend(message_titles)
            titles.extend(message_titles)
        return {
            "incoming_roles": roles[:80],
            "incoming_system_chars": system_chars,
            "incoming_system_count": system_count,
            "incoming_operit_titles": self._unique_strings(titles),
            "incoming_system_titles": self._unique_strings(system_titles),
            "incoming_user_titles": self._unique_strings(user_titles),
            "incoming_system_outlines": system_outlines[:5],
        }

    def _system_debug_outline(
        self,
        index: int,
        text: str,
        titles: list[str] | None = None,
    ) -> dict[str, Any]:
        raw = str(text or "")
        return {
            "index": index,
            "chars": len(raw),
            "sha256_12": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12] if raw else "",
            "titles": self._unique_strings(titles or self._operit_titles_from_text(raw))[:30],
            "preview_lines": self._debug_preview_lines(raw, max_lines=12, max_chars=160),
        }

    @staticmethod
    def _debug_preview_lines(text: str, *, max_lines: int, max_chars: int) -> list[str]:
        lines: list[str] = []
        secret_re = re.compile(r"(?i)(api[_-]?key|secret|token|bearer|password|authorization|cookie)")
        for raw_line in str(text or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            if secret_re.search(line):
                line = "[redacted potential secret line]"
            elif len(line) > max_chars:
                line = line[: max(0, max_chars - 3)].rstrip() + "..."
            lines.append(line)
            if len(lines) >= max_lines:
                break
        return lines

    @staticmethod
    def _unique_strings(values: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = str(value or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique.append(cleaned)
        return unique

    def _operit_part_titles(self, parts: list[str]) -> list[str]:
        titles: list[str] = []
        for part in parts or []:
            part_text = str(part or "").strip()
            titles.extend(self._operit_titles_from_text(part_text))
        return self._unique_strings(titles)

    def _messages_are_tool_continuation(
        self,
        messages: list[dict],
        current_user_index: int | None,
    ) -> bool:
        if current_user_index is not None:
            return False
        for message in reversed(messages or []):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "system":
                continue
            return self._message_has_tool_protocol(message)
        return False

    @staticmethod
    def _message_has_tool_protocol(message: dict[str, Any]) -> bool:
        if message.get("role") == "tool" or message.get("tool_call_id"):
            return True
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return True
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"tool_result", "tool_use"}:
                    return True
        return False

    def _messages_contain_non_text_content(self, messages: list[dict]) -> bool:
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    return True
                item_type = item.get("type")
                if item_type not in {"text", "input_text"}:
                    return True
        return False

    def _message_contains_operit_context(self, message: dict[str, Any]) -> bool:
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if isinstance(content, str):
            return self._text_contains_operit_context(content)
        if isinstance(content, list):
            return any(
                isinstance(item, dict)
                and self._text_contains_operit_context(str(item.get("text") or item.get("input_text") or ""))
                for item in content
            )
        return False

    @staticmethod
    def _text_contains_operit_context(text: str) -> bool:
        lowered = str(text or "").lower()
        return (
            "message_insert_extra_bundle" in lowered
            or "<workspace_attachment" in lowered
            or ('filename="time:' in lowered and "<attachment" in lowered)
        )

    def _operit_titles_from_text(self, text: str) -> list[str]:
        raw = str(text or "")
        titles = [
            match.group(1).strip()
            for match in re.finditer(r"【([^】\n]{1,80})】", raw)
            if match.group(1).strip()
        ]
        if "<workspace_attachment" in raw.lower():
            titles.append("工作区")
        if self._text_contains_operit_activity_marker(raw):
            titles.append("照顾备忘")
        return self._unique_strings(titles)

    @staticmethod
    def _text_contains_operit_activity_marker(text: str) -> bool:
        raw = str(text or "")
        return bool(
            re.search(r"(?m)^\s*照顾备忘[：:].*只在合适时轻轻带一句", raw)
            or re.search(r"(?m)^\s*-\s*\[reminder_id:[^\]\n]+\]", raw)
            or re.search(r"(?m)^\s*=+\s*照顾备忘\s*=+\s*$", raw)
        )

    def _split_operit_context_from_user_text(
        self,
        text: str,
    ) -> tuple[str, list[str], list[str], bool]:
        raw = str(text or "")
        found = self._text_contains_operit_context(raw) or self._has_external_context_title(raw)
        if not found:
            return raw, [], [], False

        stable_parts: list[str] = []
        activity_parts: list[str] = []
        ordinary_attachments: list[tuple[str, str]] = []

        def collect_from_attachment(match: re.Match) -> str:
            block = match.group(0)
            if not self._text_contains_operit_context(block.split(">", 1)[0]):
                marker = f"\x00serein_ordinary_attachment_{len(ordinary_attachments)}\x00"
                ordinary_attachments.append((marker, block))
                return marker
            stable, activity = self._operit_context_sections_from_text(
                self._inner_text_from_tag_block(block, "attachment"),
            )
            stable_parts.extend(stable)
            activity_parts.extend(activity)
            return ""

        def collect_from_workspace(match: re.Match) -> str:
            text_value = self._inner_text_from_tag_block(match.group(0), "workspace_attachment")
            if text_value.strip():
                activity_parts.append(f"【工作区】\n{text_value.strip()}")
            return ""

        without_workspace = WORKSPACE_ATTACHMENT_RE.sub(collect_from_workspace, raw)
        without_attachments = EXTERNAL_CONTEXT_ATTACHMENT_RE.sub(collect_from_attachment, without_workspace)
        without_attachments = SELF_CLOSING_ATTACHMENT_RE.sub(
            lambda match: "" if self._text_contains_operit_context(match.group(0)) else match.group(0),
            without_attachments,
        )
        section_text = without_attachments
        for marker, _ in ordinary_attachments:
            section_text = section_text.replace(marker, "")
        stable, activity = self._operit_context_sections_from_text(section_text)
        stable_parts.extend(stable)
        activity_parts.extend(activity)
        cleaned = self._strip_external_context_blocks(
            self._strip_leading_auto_context_markers(without_attachments),
            tuple(marker for marker, _ in ordinary_attachments),
        )
        for marker, block in ordinary_attachments:
            cleaned = cleaned.replace(marker, block)
        return cleaned, stable_parts, activity_parts, True

    @staticmethod
    def _inner_text_from_tag_block(block: str, tag_name: str) -> str:
        text = re.sub(
            rf"^\s*<{tag_name}\b[^>]*>",
            "",
            str(block or ""),
            flags=re.IGNORECASE,
        )
        text = re.sub(rf"</{tag_name}>\s*$", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _has_external_context_title(text: str) -> bool:
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("【") or "】" not in stripped:
                continue
            title = stripped[1 : stripped.index("】")].strip()
            if title in EXTERNAL_CONTEXT_BLOCK_TITLES or title in OPERIT_STABLE_CONTEXT_TITLES:
                return True
        return False

    def _operit_context_sections_from_text(self, text: str) -> tuple[list[str], list[str]]:
        stable_parts: list[str] = []
        activity_parts: list[str] = []
        sections: list[tuple[str, list[str]]] = []
        current_title = ""
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_title, current_lines
            body = "\n".join(line for line in current_lines).strip()
            if current_title or body:
                sections.append((current_title, current_lines[:]))
            current_title = ""
            current_lines = []

        for line in str(text or "").splitlines():
            stripped = line.strip()
            match = re.match(r"^【([^】]+)】\s*(.*)$", stripped)
            if match:
                flush()
                current_title = match.group(1).strip()
                rest = match.group(2).strip()
                current_lines = [rest] if rest else []
                continue
            current_lines.append(line)
        flush()

        for title, lines in sections:
            body = "\n".join(line for line in lines).strip()
            if not title and not body:
                continue
            if title:
                part = f"【{title}】" + (f"\n{body}" if body else "")
            else:
                part = body
            if not part.strip():
                continue
            if self._operit_section_is_stable(title, body):
                stable_parts.append(part)
            elif (
                title in EXTERNAL_CONTEXT_BLOCK_TITLES
                or title
                or self._text_contains_operit_context(body)
                or self._text_contains_operit_activity_marker(body)
            ):
                activity_parts.append(part)
        return stable_parts, activity_parts

    @staticmethod
    def _operit_section_is_stable(title: str, body: str) -> bool:
        title_text = str(title or "").strip()
        if title_text in EXTERNAL_CONTEXT_BLOCK_TITLES:
            return False
        if title_text in OPERIT_STABLE_CONTEXT_TITLES:
            return True
        haystack = f"{title_text}\n{body}".lower()
        return any(keyword.lower() in haystack for keyword in OPERIT_STABLE_CONTEXT_KEYWORDS)

    def _format_operit_context_block(
        self,
        intro: str,
        parts: list[str],
        *,
        max_chars: int,
    ) -> str:
        unique_parts: list[str] = []
        seen: set[str] = set()
        for part in parts:
            cleaned = str(part or "").strip()
            if not cleaned:
                continue
            key = re.sub(r"\s+", " ", cleaned)
            if key in seen:
                continue
            seen.add(key)
            unique_parts.append(cleaned)
        if not unique_parts:
            return ""
        return self._trim_text("\n\n".join([intro, *unique_parts]), max_chars)

    def _restore_cached_reasoning_content(self, session_id: str, messages: Any) -> None:
        if not isinstance(messages, list) or not any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in messages
        ):
            return

        cache = self.pending_tool_reasoning.get(session_id)
        if not cache:
            return

        restored = 0
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            signature = self._tool_call_signature(message)
            if not signature:
                continue
            cached_message = cache.get(signature)
            if not cached_message:
                continue
            restored_fields = []
            if not message.get("reasoning_content") and cached_message.get("reasoning_content"):
                message["reasoning_content"] = cached_message["reasoning_content"]
                restored_fields.append("reasoning_content")
            if not message.get("reasoning_details") and cached_message.get("reasoning_details"):
                message["reasoning_details"] = deepcopy(cached_message["reasoning_details"])
                reasoning_text = self._reasoning_text_from_details(message["reasoning_details"])
                if reasoning_text and not message.get("reasoning"):
                    message["reasoning"] = reasoning_text
                restored_fields.append("reasoning_details")
            if restored_fields:
                restored += 1

        if restored:
            logger.info(
                "Gateway restored reasoning context for %s assistant tool-call message(s) | session=%s",
                restored,
                session_id,
            )

    def _update_reasoning_cache(self, session_id: str, assistant_message: dict[str, Any]) -> None:
        signature = self._tool_call_signature(assistant_message)
        reasoning_content = assistant_message.get("reasoning_content")
        reasoning_details = assistant_message.get("reasoning_details")
        if not isinstance(reasoning_details, list):
            reasoning_details = []
        if signature and (reasoning_content or reasoning_details):
            cache = self.pending_tool_reasoning.setdefault(session_id, {})
            cache[signature] = {
                "reasoning_content": reasoning_content,
                "reasoning_details": deepcopy(reasoning_details),
                "tool_calls": deepcopy(assistant_message.get("tool_calls", [])),
            }
            logger.info(
                "Gateway cached reasoning context for tool continuation | session=%s tool_calls=%s",
                session_id,
                list(signature),
            )
            return

        if not signature:
            self.pending_tool_reasoning.pop(session_id, None)

    def _tool_call_signature(self, assistant_message: Any) -> tuple[str, ...]:
        if not isinstance(assistant_message, dict):
            return ()
        tool_calls = assistant_message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            return ()

        signature = []
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function", {})
            if isinstance(function, dict) and function.get("name"):
                signature.append(
                    f"idx:{index}:{function.get('name', '')}:{self._normalize_tool_arguments(function.get('arguments', ''))}"
                )
                continue
            tool_id = tool_call.get("id")
            if tool_id:
                signature.append(f"id:{tool_id}")
        return tuple(signature)

    def _normalize_tool_arguments(self, arguments: Any) -> str:
        if isinstance(arguments, (dict, list)):
            return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if isinstance(arguments, str):
            raw = arguments.strip()
            if not raw:
                return ""
            try:
                parsed = json.loads(raw)
            except ValueError:
                return " ".join(raw.split())
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return str(arguments)

    def _trim_text(self, text: str, budget_tokens: int) -> str:
        if budget_tokens <= 0:
            return ""
        if count_tokens_approx(text) <= budget_tokens:
            return text
        trimmed = text
        while trimmed and count_tokens_approx(trimmed) > budget_tokens:
            cut = max(1, int(len(trimmed) * 0.85))
            trimmed = trimmed[:cut].rstrip()
        return trimmed

    @staticmethod
    def _usage_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _openai_message_to_anthropic_content(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        native_content = message.get("_serein_anthropic_content")
        if isinstance(native_content, list) and all(isinstance(block, dict) for block in native_content):
            return deepcopy(native_content)

        content_blocks: list[dict[str, Any]] = []
        content_blocks.extend(self._reasoning_details_to_anthropic_blocks(message.get("reasoning_details")))
        text = self._coerce_message_text(message.get("content"))
        if text:
            content_blocks.append({"type": "text", "text": text})

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "")
                if not name:
                    continue
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or f"call_{len(content_blocks)}"),
                        "name": name,
                        "input": self._parse_tool_arguments(function.get("arguments")),
                    }
                )
        return content_blocks

    @staticmethod
    def _anthropic_thinking_block_to_reasoning_detail(
        block: dict[str, Any],
        *,
        index: int,
    ) -> dict[str, Any] | None:
        block_type = str(block.get("type") or "").strip()
        common = {
            "id": block.get("id"),
            "format": "anthropic-claude-v1",
            "index": index,
        }
        if block_type == "thinking":
            return {
                "type": "reasoning.text",
                "text": str(block.get("thinking") or ""),
                "signature": block.get("signature"),
                **common,
            }
        if block_type == "redacted_thinking":
            return {
                "type": "reasoning.encrypted",
                "data": str(block.get("data") or ""),
                **common,
            }
        return None

    @staticmethod
    def _reasoning_details_to_anthropic_blocks(details: Any) -> list[dict[str, Any]]:
        if not isinstance(details, list):
            return []

        blocks: list[dict[str, Any]] = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            detail_type = str(detail.get("type") or "").strip()
            detail_format = str(detail.get("format") or "").strip()
            if detail_format and detail_format != "anthropic-claude-v1":
                continue
            if detail_type == "reasoning.text":
                signature = detail.get("signature")
                if not isinstance(signature, str) or not signature:
                    continue
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": str(detail.get("text") or ""),
                        "signature": signature,
                    }
                )
                continue
            if detail_type == "reasoning.encrypted":
                data = detail.get("data")
                if isinstance(data, str) and data:
                    blocks.append({"type": "redacted_thinking", "data": data})
        return blocks

    def _parse_tool_arguments(self, raw_arguments: Any) -> Any:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if raw_arguments in (None, ""):
            return {}
        if not isinstance(raw_arguments, str):
            return {}
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _anthropic_payload_for_upstream(
        self,
        payload: dict[str, Any],
        route: dict[str, Any],
    ) -> dict[str, Any]:
        upstream = route["upstream"]
        upstream_payload: dict[str, Any] = {
            "model": route["upstream_model"],
            "messages": [],
            "max_tokens": self._anthropic_max_tokens(payload),
        }

        system_parts: list[str] = []
        for message in payload.get("messages", []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            if role == "system":
                system_text = self._coerce_message_text(message.get("content")).strip()
                if system_text:
                    system_parts.append(system_text)
                continue
            if role == "tool":
                tool_use_id = str(message.get("tool_call_id") or message.get("tool_use_id") or "").strip()
                if tool_use_id:
                    upstream_payload["messages"].append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use_id,
                                    "content": self._coerce_message_text(message.get("content")),
                                }
                            ],
                        }
                    )
                continue
            if role not in {"user", "assistant"}:
                continue
            content = (
                self._openai_message_to_anthropic_content(message)
                if role == "assistant"
                else self._openai_content_to_anthropic_blocks(message.get("content"))
            )
            upstream_payload["messages"].append({"role": role, "content": content or ""})

        if system_parts:
            upstream_payload["system"] = "\n\n".join(system_parts)

        for field in ("temperature", "top_p", "stream"):
            if field in payload:
                upstream_payload[field] = payload[field]
        thinking = payload.get("_serein_anthropic_thinking")
        if isinstance(thinking, dict):
            upstream_payload["thinking"] = deepcopy(thinking)
        if "stop" in payload:
            upstream_payload["stop_sequences"] = payload["stop"]

        tools = self._openai_tools_to_anthropic(payload.get("tools"))
        if tools:
            upstream_payload["tools"] = tools
        tool_choice = self._openai_tool_choice_to_anthropic(payload.get("tool_choice"))
        if tool_choice is not None:
            upstream_payload["tool_choice"] = tool_choice

        self._apply_anthropic_prompt_cache(upstream_payload, upstream)
        return upstream_payload

    def _anthropic_max_tokens(self, payload: dict[str, Any]) -> int:
        try:
            return max(1, int(payload.get("max_tokens") or payload.get("max_completion_tokens") or self.gateway_cfg.get("anthropic_max_tokens") or 1024))
        except (TypeError, ValueError):
            return 1024

    def _apply_anthropic_prompt_cache(
        self,
        payload: dict[str, Any],
        upstream: dict[str, Any],
    ) -> None:
        strategy = str(upstream.get("prompt_cache") or "").strip().lower()
        if strategy not in {"anthropic", "anthropic_explicit", "anthropic-explicit", "anthropic_block", "anthropic-block"}:
            return
        cache_control = self._anthropic_cache_control(upstream)
        if strategy == "anthropic":
            if payload.get("cache_control"):
                return
            payload["cache_control"] = cache_control
            return

        self._apply_explicit_anthropic_cache_control(
            payload,
            cache_control,
            model=str(payload.get("model") or ""),
        )

    def _anthropic_cache_control(self, upstream: dict[str, Any]) -> dict[str, str]:
        cache_control: dict[str, str] = {"type": "ephemeral"}
        retention = str(
            upstream.get("prompt_cache_ttl")
            or upstream.get("prompt_cache_retention")
            or ""
        ).strip()
        if retention == "1h":
            cache_control["ttl"] = "1h"
        return cache_control

    def _openai_content_to_anthropic_blocks(self, content: Any) -> str | list[dict[str, Any]]:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)

        blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
                continue
            if not isinstance(item, dict):
                continue
            block_type = item.get("type")
            if block_type == "text":
                blocks.append({"type": "text", "text": str(item.get("text") or "")})
                continue
            if block_type == "image_url":
                image_url = item.get("image_url")
                url = str(image_url.get("url") if isinstance(image_url, dict) else image_url or "").strip()
                if not url:
                    continue
                blocks.append(self._openai_image_url_to_anthropic_block(url))
                continue
        return blocks

    def _openai_image_url_to_anthropic_block(self, url: str) -> dict[str, Any]:
        if url.startswith("data:") and ";base64," in url:
            header, data = url.split(";base64,", 1)
            media_type = header.replace("data:", "", 1) or "image/png"
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
        return {"type": "image", "source": {"type": "url", "url": url}}

    def _openai_tools_to_anthropic(self, tools: Any) -> list[dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") if tool.get("type") == "function" else tool
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            converted_tool = {
                "name": name,
                "input_schema": function.get("parameters") or function.get("input_schema") or {"type": "object"},
            }
            description = str(function.get("description") or "").strip()
            if description:
                converted_tool["description"] = description
            converted.append(converted_tool)
        return converted

    def _openai_tool_choice_to_anthropic(self, tool_choice: Any) -> Any:
        if tool_choice is None:
            return None
        if isinstance(tool_choice, str):
            return {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}.get(
                tool_choice,
                None,
            )
        if not isinstance(tool_choice, dict):
            return None
        if tool_choice.get("type") == "function":
            function = tool_choice.get("function")
            name = str(function.get("name") if isinstance(function, dict) else "").strip()
            if name:
                return {"type": "tool", "name": name}
        return None

    def _anthropic_response_body_to_openai_message(self, body: Any) -> dict[str, Any] | None:
        if not isinstance(body, dict):
            return None
        content = body.get("content")
        if not isinstance(content, list):
            return None
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        reasoning_details: list[dict[str, Any]] = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    text_parts.append(text)
                continue
            if block_type in {"thinking", "redacted_thinking"}:
                detail = self._anthropic_thinking_block_to_reasoning_detail(
                    block,
                    index=len(reasoning_details),
                )
                if detail:
                    reasoning_details.append(detail)
                continue
            if block_type == "tool_use":
                name = str(block.get("name") or "")
                if not name:
                    continue
                tool_calls.append(
                    {
                        "id": str(block.get("id") or f"call_{index}"),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                block.get("input") if isinstance(block.get("input"), dict) else {},
                                ensure_ascii=False,
                            ),
                        },
                    }
                )

        if not text_parts and not tool_calls and not reasoning_details:
            return None
        message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
        if reasoning_details:
            message["reasoning_details"] = reasoning_details
            reasoning_text = self._reasoning_text_from_details(reasoning_details)
            if reasoning_text:
                message["reasoning"] = reasoning_text
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    def _merge_stream_message_delta(self, stream_state: dict[str, Any], delta: dict[str, Any]) -> None:
        message = stream_state["message"]
        if delta.get("role"):
            message["role"] = delta["role"]
        if isinstance(delta.get("content"), str):
            message["content"] += delta["content"]
        if isinstance(delta.get("reasoning_content"), str):
            message["reasoning_content"] += delta["reasoning_content"]
        if isinstance(delta.get("reasoning"), str):
            message["reasoning_content"] += delta["reasoning"]
        reasoning_details = delta.get("reasoning_details")
        if isinstance(reasoning_details, list):
            for detail in reasoning_details:
                if isinstance(detail, dict):
                    self._merge_reasoning_detail_delta(stream_state, detail)

        tool_calls = delta.get("tool_calls")
        if not isinstance(tool_calls, list):
            return
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            index = int(tool_call.get("index", 0))
            target = stream_state["tool_calls_by_index"].setdefault(
                index,
                {"type": "function", "function": {"name": "", "arguments": ""}},
            )
            if tool_call.get("id"):
                target["id"] = tool_call["id"]
            if tool_call.get("type"):
                target["type"] = tool_call["type"]
            function = tool_call.get("function")
            if isinstance(function, dict):
                target_function = target.setdefault("function", {"name": "", "arguments": ""})
                if isinstance(function.get("name"), str):
                    target_function["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    target_function["arguments"] += function["arguments"]

    def _build_stream_assistant_message(self, stream_state: dict[str, Any]) -> dict[str, Any] | None:
        message = deepcopy(stream_state.get("message", {}))
        tool_calls_by_index = stream_state.get("tool_calls_by_index", {})
        tool_calls = [
            deepcopy(tool_calls_by_index[index])
            for index in sorted(tool_calls_by_index)
            if isinstance(tool_calls_by_index[index], dict)
        ]
        reasoning_details_by_index = stream_state.get("reasoning_details_by_index", {})
        reasoning_details = [
            deepcopy(reasoning_details_by_index[index])
            for index in sorted(reasoning_details_by_index)
            if isinstance(reasoning_details_by_index[index], dict)
        ]

        content = message.get("content", "")
        reasoning_content = message.get("reasoning_content", "")
        if not (tool_calls or content or reasoning_content or reasoning_details):
            return None

        assistant_message: dict[str, Any] = {"role": message.get("role", "assistant")}
        assistant_message["content"] = content if content else None
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        if reasoning_details:
            assistant_message["reasoning_details"] = reasoning_details
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        return assistant_message

    def _merge_reasoning_detail_delta(
        self,
        stream_state: dict[str, Any],
        detail: dict[str, Any],
    ) -> None:
        details_by_index = stream_state.setdefault("reasoning_details_by_index", {})
        index = self._usage_int(detail.get("index"))
        detail_type = str(detail.get("type") or "").strip()
        if detail_type not in {"reasoning.text", "reasoning.summary", "reasoning.encrypted"}:
            return
        target = details_by_index.setdefault(
            index,
            {
                "type": detail_type,
                "id": detail.get("id"),
                "format": detail.get("format") or "anthropic-claude-v1",
                "index": index,
            },
        )
        if detail.get("id") is not None:
            target["id"] = detail.get("id")
        if detail.get("format"):
            target["format"] = detail.get("format")
        if detail_type == "reasoning.text":
            target["type"] = detail_type
            target["text"] = str(target.get("text") or "") + str(detail.get("text") or "")
            if detail.get("signature") is not None:
                target["signature"] = detail.get("signature")
            else:
                target.setdefault("signature", None)
            return
        if detail_type == "reasoning.summary":
            target["type"] = detail_type
            target["summary"] = str(target.get("summary") or "") + str(detail.get("summary") or "")
            return
        target["type"] = detail_type
        target["data"] = str(target.get("data") or "") + str(detail.get("data") or "")

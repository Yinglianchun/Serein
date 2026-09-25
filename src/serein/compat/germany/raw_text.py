from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
logger = logging.getLogger('serein_brain.raw_events')
ALLOWED_RAW_ROLES = {'user', 'assistant'}
RAW_EVENT_DEFAULT_SOURCE = 'raw'
INJECTION_SECTION_RE = re.compile('(?im)^\\s*(?:Core Memory|Recalled Memory|Recent Context|Just Now Chat Context|Related Memory|Dream Context|Additional private memory detail|Long-term State Summary)\\s*:?\\s*$')
CLIENT_ATTACHMENT_RE = re.compile('<attachment\\b[^>]*>[\\s\\S]*?</attachment>', re.IGNORECASE)
SELF_CLOSING_ATTACHMENT_RE = re.compile('<attachment\\b[^>]*/>', re.IGNORECASE)
WORKSPACE_ATTACHMENT_RE = re.compile('<workspace_attachment>[\\s\\S]*?</workspace_attachment>', re.IGNORECASE)
CLIENT_CONTEXT_BLOCK_TITLES = {'当前时间', '当前电量', '当前天气', '当前位置', '当前屏幕应用', '应用使用时长', '最近通知', '相关记忆', '屏幕文本'}

# Match the complete Operit envelope, not individual entries or arbitrary XML.
WORLDBOOK_RE = re.compile(r"<worldbook\b[^>]*>[\s\S]*?</worldbook\s*>", re.IGNORECASE)


def strip_worldbook_context(text: str) -> str:
    """Project speech for recall/archive; never mutate the upstream message."""
    return WORLDBOOK_RE.sub("\n", str(text or ""))


def strip_raw_client_context(text: str) -> str:
    cleaned = strip_worldbook_context(text)
    cleaned = WORKSPACE_ATTACHMENT_RE.sub('', cleaned)
    cleaned = CLIENT_ATTACHMENT_RE.sub('', cleaned)
    cleaned = SELF_CLOSING_ATTACHMENT_RE.sub('', cleaned)
    cleaned = _strip_client_context_blocks(cleaned)
    cleaned = re.sub('[ \\t]{2,}', ' ', cleaned)
    cleaned = re.sub('\\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

def _strip_client_context_blocks(text: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in str(text or '').splitlines():
        stripped = line.strip()
        title = ''
        if stripped.startswith('【') and '】' in stripped:
            title = stripped[1:stripped.index('】')].strip()
        if title:
            skipping = title in CLIENT_CONTEXT_BLOCK_TITLES
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return '\n'.join(kept)

def raw_event_text_looks_injected(text: str, raw: dict[str, Any] | None=None) -> bool:
    raw = raw or {}
    metadata = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
    flags = {str(raw.get('kind') or '').lower(), str(raw.get('source_type') or '').lower(), str(metadata.get('kind') or '').lower(), str(metadata.get('source_type') or '').lower()}
    if flags & {'injection', 'memory_injection', 'tool', 'tool_result', 'system', 'developer'}:
        return True
    stripped = str(text or '').strip()
    if stripped.startswith('Live private context for the current turn'):
        return True
    if INJECTION_SECTION_RE.search(stripped):
        return True
    return '[bucket_id:' in stripped and any((marker in stripped for marker in ('Recalled Memory', 'Related Memory', 'Recent Context', 'Core Memory')))

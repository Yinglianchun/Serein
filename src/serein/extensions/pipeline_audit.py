"""Structural receipts for public Event decisions.

These checks verify references and exact quotes, not the truth of a paraphrase.
"""
from __future__ import annotations

import re
from typing import Any


CLAIM_TYPES = {'trigger', 'fact', 'subjective_claim', 'subjective_comparison',
               'self_description', 'reason', 'speech_act', 'result', 'landing'}
OWNERS = {'我', '她', '双方', '外部', '混合'}
RENDER_MODES = {'direct', 'attribution_once', 'speech_act'}
FOCUS_ROLES = {'core', 'supporting'}


def canonicalize_claim_group_ids(result: dict) -> None:
    """Renumber unambiguous model labels without changing their references."""
    groups, sentences = result.get('claim_groups'), result.get('sentence_evidence')
    if not isinstance(groups, list) or not groups or not isinstance(sentences, list):
        return
    old = [str(group.get('claim_group_id') or '').strip() if isinstance(group, dict) else ''
           for group in groups]
    if any(not value for value in old) or len(old) != len(set(old)):
        return
    mapping = {value: f'g{index + 1}' for index, value in enumerate(old)}
    if any(not isinstance(row, dict) or not isinstance(row.get('claim_group_ids'), list)
           or any(not isinstance(value, str) or value.strip() not in mapping
                  for value in row['claim_group_ids']) for row in sentences):
        return
    for group, value in zip(groups, old):
        group['claim_group_id'] = mapping[value]
    for row in sentences:
        row['claim_group_ids'] = [mapping[value.strip()] for value in row['claim_group_ids']]


def _source_span(span: Any, sources: dict[int, dict] | None, label: str, errors: list[str]) -> tuple[int, str] | None:
    if not isinstance(span, dict) or set(span) != {'source_message_id', 'quote'}:
        errors.append(f'{label} 必须包含 source_message_id 和 quote')
        return None
    source_id, quote = span['source_message_id'], span['quote']
    if type(source_id) is not int or not isinstance(quote, str) or not quote.strip():
        errors.append(f'{label} 缺少有效来源或逐字引文')
        return None
    if sources is not None:
        source = sources.get(source_id)
        materials = ([str(source.get('content') or ''),
                      *[str(value or '') for value in source.get('evidence_texts') or []]]
                     if source is not None else [])
        if not source or not any(quote in material for material in materials):
            errors.append(f'{label} 不是 owned 原文或绑定图片转录的逐字片段')
            return None
    return source_id, quote


def _sentences_match_body(sentences: list[str], body: object) -> bool:
    if not isinstance(body, str):
        return False
    position = 0
    for sentence in sentences:
        while position < len(body) and body[position].isspace():
            position += 1
        text = sentence.strip()
        if not body.startswith(text, position):
            return False
        position += len(text)
    return not body[position:].strip()


def _quote_attributed_to_user(prefix: str, user_name: str | None = None) -> bool:
    clause = re.split(r'[。！？!?；;\n]', prefix)[-1]
    if re.search(r'(?:她|用户)', clause):
        return True
    if not user_name:
        return False
    name = re.escape(user_name)
    if user_name.isascii():
        name = rf'(?<![A-Za-z0-9]){name}(?![A-Za-z0-9])'
    return bool(re.search(name, clause))


def _direct_quote_errors(text: str, source_id: int, quote: str, label: str,
                         user_name: str | None = None) -> list[str]:
    errors: list[str] = []
    start = text.find(quote)
    while start >= 0:
        prefix = text[:start].rstrip()
        if prefix.endswith(('“', '"', '‘', "'")):
            before_open = prefix[:-1].rstrip()
            attributed = _quote_attributed_to_user(before_open, user_name)
            if not attributed:
                errors.append(f'{label} 直接引用来源 {source_id} 却没有在引语所在句标明她／用户')
            end = start + len(quote)
            tail = text[end:]
            dialogue_quote = before_open.endswith(('：', ':')) or not attributed
            if dialogue_quote and tail.startswith(('”', '"', '’', "'")):
                after = tail[1:].lstrip()
                quote_has_ending = bool(re.search(r'[。！？!?；;，,：:…]$', quote))
                separated = bool(after and re.match(r'[。！？!?；;，,：:]', after))
                if after and not quote_has_ending and not separated:
                    errors.append(f'{label} 直接引用来源 {source_id} 后切回叙述时缺少标点')
        start = text.find(quote, start + len(quote))
    return list(dict.fromkeys(errors))


def writer_receipt_errors(result: dict, owned_sources: list[dict] | None,
                          user_name: str | None = None) -> list[str]:
    sources = ({item['id']: item for item in owned_sources if type(item.get('id')) is int}
               if owned_sources is not None else None)
    groups, sentences = result.get('claim_groups'), result.get('sentence_evidence')
    if result.get('evidence_sufficient') is False:
        return [] if groups == sentences == [] else ['证据不足时 claim_groups 和 sentence_evidence 必须为空数组']
    errors: list[str] = []
    if not isinstance(groups, list) or not groups:
        errors.append('claim_groups 缺失或为空')
        groups = []
    if not isinstance(sentences, list) or not sentences:
        errors.append('sentence_evidence 缺失或为空')
        sentences = []
    by_id: dict[str, list[tuple[int, str]]] = {}
    for index, group in enumerate(groups):
        label = f'claim_groups[{index}]'
        if not isinstance(group, dict):
            errors.append(f'{label} 不是对象')
            continue
        group_id = group.get('claim_group_id')
        if group_id != f'g{index + 1}':
            errors.append(f'{label} ID 必须连续')
        if group_id in by_id:
            errors.append(f'{label} ID 重复')
        if group.get('claim_type') not in CLAIM_TYPES or group.get('owner') not in OWNERS:
            errors.append(f'{label} 类型或归属无效')
        if group.get('render_mode') not in RENDER_MODES or group.get('focus_role') not in FOCUS_ROLES:
            errors.append(f'{label} 表达或主次角色无效')
        if not isinstance(group.get('summary'), str) or not group['summary'].strip():
            errors.append(f'{label} 缺少命题摘要')
        kind, mode = group.get('claim_type'), group.get('render_mode')
        if (kind == 'speech_act') != (mode == 'speech_act'):
            errors.append(f'{label} speech_act 类型与表达不匹配')
        if kind in {'subjective_claim', 'subjective_comparison', 'self_description'} and mode != 'attribution_once':
            errors.append(f'{label} 主观命题必须保留一次归属')
        sides = group.get('comparison_sides')
        if kind == 'subjective_comparison':
            if (not isinstance(sides, list) or len(sides) != 2 or
                any(not isinstance(side, dict) or not isinstance(side.get('subject'), str) or
                    not side['subject'].strip() or not isinstance(side.get('claim'), str) or
                    not side['claim'].strip() for side in sides)):
                errors.append(f'{label} 比较必须有两个主体及各自命题')
            elif sides[0]['subject'] == sides[1]['subject']:
                errors.append(f'{label} 比较两端主体相同')
        elif sides is not None:
            errors.append(f'{label} 非比较命题不得带 comparison_sides')
        spans = group.get('source_spans')
        if not isinstance(spans, list) or not spans:
            errors.append(f'{label} 缺少来源片段')
            spans = []
        valid_spans = []
        for span in spans:
            valid = _source_span(span, sources, label, errors)
            if valid:
                valid_spans.append(valid)
        if isinstance(group_id, str):
            by_id[group_id] = valid_spans
    used: set[str] = set()
    covered: dict[str, list[tuple[int, str]]] = {}
    text_parts: list[str] = []
    user_quotes_in_body: list[tuple[int, str]] = []
    for index, entry in enumerate(sentences):
        label = f'sentence_evidence[{index}]'
        if not isinstance(entry, dict):
            errors.append(f'{label} 不是对象')
            continue
        if type(entry.get('sentence_index')) is not int or entry['sentence_index'] != index:
            errors.append(f'{label} 索引不连续')
        sentence = entry.get('sentence')
        if not isinstance(sentence, str) or not sentence.strip():
            errors.append(f'{label} 句子为空')
            continue
        text_parts.append(sentence)
        ids = entry.get('claim_group_ids')
        if not isinstance(ids, list) or not ids or len(ids) != len(set(map(str, ids))):
            errors.append(f'{label} 命题组引用缺失或重复')
            ids = []
        for group_id in ids:
            if group_id not in by_id:
                errors.append(f'{label} 引用了不存在的命题组')
            else:
                used.add(group_id)
        spans = entry.get('source_spans')
        if not isinstance(spans, list) or not spans:
            errors.append(f'{label} 缺少来源片段')
            spans = []
        quotes, valid_spans = [], []
        for span in spans:
            valid = _source_span(span, sources, label, errors)
            if valid:
                valid_spans.append(valid)
                quotes.append(valid[1])
                source = sources.get(valid[0]) if sources is not None else None
                if (source and source.get('role') == 'user' and
                        valid[1] in str(source.get('content') or '')):
                    user_quotes_in_body.append(valid)
                    errors.extend(_direct_quote_errors(sentence, valid[0], valid[1], label, user_name))
        for group_id in ids:
            covered.setdefault(group_id, []).extend(valid_spans)
    if not _sentences_match_body(text_parts, result.get('event_draft')):
        errors.append('event_draft 与 sentence_evidence 逐句拼接不一致')
    body = str(result.get('event_draft') or '')
    for source_id, quote in dict.fromkeys(user_quotes_in_body):
        errors.extend(error for error in _direct_quote_errors(body, source_id, quote, 'event_draft', user_name)
                      if '缺少标点' in error)
    for group_id, spans in by_id.items():
        if group_id not in used:
            errors.append(f'{group_id} 未被正文引用')
        elif spans and not any(sentence_id == source_id and quote in sentence_quote
                               for source_id, quote in spans
                               for sentence_id, sentence_quote in covered.get(group_id, [])):
            errors.append(f'{group_id} 的来源未被引用它的句子覆盖')
    return errors



def canonicalize_curator_review(review: Any) -> Any:
    """Repair only schema details whose intended meaning is deterministic."""
    if not isinstance(review, dict):
        return review
    normalized = dict(review)
    dispositions = review.get('dispositions')
    if isinstance(dispositions, list):
        rows = []
        for row in dispositions:
            if not isinstance(row, dict):
                rows.append(row)
                continue
            item = dict(row)
            if 'disposition' not in item and item.get('status') in {'skip', 'defer'}:
                item['disposition'] = item.pop('status')
            if item.get('disposition') == 'skip' and 'parked_source_message_ids' not in item:
                item['parked_source_message_ids'] = []
            rows.append(item)
        normalized['dispositions'] = rows
    return normalized

def curator_receipt_errors(review: Any, plan: dict, component: dict) -> list[str]:
    errors: list[str] = []
    required = {'events', 'boundaries', 'dispositions'}
    allowed = required | {'bridge_exclusions'} | ({'continuations'} if component.get('continuity_pairs') else set())
    if not isinstance(review, dict) or not required.issubset(review) or set(review) - allowed or any(not isinstance(value, list) for value in review.values()):
        return ['decision_review 必须包含 events、boundaries、dispositions 数组']
    events = plan['events']
    event_indexes = set(range(len(events)))
    seen_events = set()
    for row in review['events']:
        if not isinstance(row, dict) or not {'event_index', 'reason'}.issubset(row) or set(row) - {'event_index', 'reason', 'materials', 'admission'} or type(row['event_index']) is not int or row['event_index'] not in event_indexes or row['event_index'] in seen_events or not isinstance(row['reason'], str) or not row['reason'].strip():
            errors.append('decision_review.events 存在无效索引或理由')
            continue
        seen_events.add(row['event_index'])
    if seen_events != event_indexes:
        errors.append('decision_review.events 未覆盖所有拟议 Event')
    by_track: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        by_track.setdefault(event['primary_track_id'], []).append(index)
    pairs = {(left, right) for indexes in by_track.values()
             for left, right in zip(indexes, indexes[1:])}
    if component.get('continuity_pairs'):
        pairs.update((left, left + 1) for left in range(len(events) - 1))
    transcriptions: dict[int, list[str]] = {}
    for item in component.get('curator_image_transcriptions') or []:
        if item.get('evidence_role') in (None, 'owned') and type(item.get('source_message_id')) is int:
            transcriptions.setdefault(item['source_message_id'], []).append(str(item.get('text') or ''))
    messages = {item['id']: {**item, 'evidence_texts': transcriptions.get(item['id'], [])}
                for item in component.get('context_messages') or []}
    seen_pairs = set()
    for row in review['boundaries']:
        if not isinstance(row, dict) or set(row) != {'left_event_index', 'right_event_index', 'reason', 'evidence'}:
            errors.append('decision_review.boundaries 格式无效')
            continue
        pair = row['left_event_index'], row['right_event_index']
        if type(pair[0]) is not int or type(pair[1]) is not int or pair not in pairs or pair in seen_pairs or not isinstance(row['reason'], str) or not row['reason'].strip():
            errors.append('decision_review.boundaries 不是相邻 Event 或缺少理由')
            continue
        seen_pairs.add(pair)
        owners = [{binding['source_message_id'] for binding in events[index]['source_bindings']} for index in pair]
        witnessed = set()
        if not isinstance(row['evidence'], list):
            errors.append('decision_review.boundaries 缺少双方证据')
            continue
        for span in row['evidence']:
            valid = _source_span(span, messages, 'boundary', errors)
            if valid:
                sides = [side for side, ids in enumerate(owners) if valid[0] in ids]
                if len(sides) == 1:
                    witnessed.add(sides[0])
                else:
                    errors.append('boundary 引文须来自一侧独占的 owned 原文')
        if witnessed != {0, 1}:
            errors.append('decision_review.boundaries 缺少双方独占原文')
    if seen_pairs != pairs:
        errors.append('decision_review.boundaries 未覆盖全部相邻边界')
    memberships = {item['unit_root_message_id']: set(item['source_message_ids']) for item in component.get('memberships') or []}
    parked = set(component.get('parked_context_source_ids') or [])
    covered = {'skip': set(), 'defer': set()}
    for row in review['dispositions']:
        if not isinstance(row, dict) or set(row) != {'disposition', 'unit_roots', 'reason', 'parked_source_message_ids'}:
            errors.append('decision_review.dispositions 格式无效')
            continue
        kind, roots, cited = row['disposition'], row['unit_roots'], row['parked_source_message_ids']
        if kind not in covered or not isinstance(roots, list) or not roots or not isinstance(row['reason'], str) or not row['reason'].strip() or not isinstance(cited, list):
            errors.append('decision_review.dispositions 缺少处置依据')
            continue
        if (kind == 'defer' and (not cited or any(type(value) is not int or value not in parked for value in cited))) or (kind == 'skip' and cited):
            errors.append('defer 必须引用真实 parked source ID，skip 不得引用')
        for root in roots:
            if type(root) is not int or root not in memberships:
                errors.append('decision_review.dispositions 引用无效 unit')
            elif covered[kind].intersection(memberships[root]):
                errors.append('decision_review.dispositions 重复覆盖 unit')
            else:
                covered[kind].update(memberships[root])
    for kind in covered:
        if covered[kind] != set(plan[f'{kind}_source_message_ids']):
            errors.append(f'decision_review.dispositions 未完整覆盖 {kind}')
    return errors

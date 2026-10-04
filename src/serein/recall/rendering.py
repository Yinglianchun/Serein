"""Germany-compatible cards and context. Rendering never records delivery."""

from .germany.gateway import GatewayService
from .germany.utils import bucket_text_for_embedding
from .germany.arc_materials import build_arc_materials, displayed_arc_materials, arc_materials_fingerprint
from .dates import memory_dates, date_lines


def _arcs(reader, hits, arc_key=None):
    owners = {hit['id'] for hit in hits}
    by_owner, menus = {}, {}
    if not owners and not arc_key:
        return by_owner, menus
    rows = reader.store.conn.execute("SELECT id FROM documents WHERE kind='narrative' AND lifecycle='active' ORDER BY id")
    for row in rows:
        obj = reader.read(row['id'], with_evidence=False)
        doc = obj['document']
        meta = doc['metadata'].get('legacy_registry', doc['metadata'])
        key = meta.get('arc_key')
        if not key:
            continue
        members, offset = [], 0
        while True:
            page = reader.materials(row['id'], offset=offset, limit=100)
            members.extend(page['items'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        linked = {m['id'] for m in members if m['selection'] == 'selected' and m['object']['readable']}
        if not linked & owners and key != arc_key:
            continue
        card = {'arc_key': key, 'title': doc['title']}
        for owner in linked & owners:
            by_owner.setdefault(owner, []).append(card)
        materials = []
        for member in members:
            item = member.get('object')
            if member['selection'] != 'selected' or not item['readable']:
                continue
            if member['kind'] == 'event' and not item['surface_state']['can_surface']:
                continue
            if member['kind'] not in {'event', 'scene', 'diary', 'upload'}:
                continue
            meta = item['document']['metadata']
            day = (meta.get('local_end_date') or meta.get('local_date') or meta.get('source_ended_at')) if member['kind'] == 'event' else (meta.get('date') or meta.get('event_date') or meta.get('created'))
            materials.append({'id': member['id'], 'kind': member['kind'],
                              'title': item['document']['title'], 'date': str(day or '')[:10]})
        all_items = build_arc_materials({'kind': 'narrative', 'id': row['id'], 'title': doc['title'], 'date': ''}, materials)
        visible = displayed_arc_materials(all_items)
        menus[key] = {**card, 'materials': visible, 'all_materials': all_items, 'material_count': len(all_items),
                      'menu_truncated': len(visible) < len(all_items), 'menu_fingerprint': arc_materials_fingerprint(all_items)}
    return by_owner, menus


def read_arc_picks(reader, arc_key, picks, *, with_evidence=False):
    if not picks or len(picks) > 5 or any(type(p) is not int or p < 0 for p in picks):
        raise ValueError('Choose one to five nonnegative material indexes')
    _, menus = _arcs(reader, [], arc_key=arc_key)
    if arc_key not in menus:
        return {'status': 'not_found', 'arc_key': arc_key, 'items': []}
    menu = menus[arc_key]
    rows = {row['index']: row for row in menu['all_materials']}
    if any(p not in rows for p in picks):
        raise ValueError('Material index is not in the current Arc menu')
    return {'status': 'ok', 'arc_key': arc_key, 'menu_fingerprint': menu['menu_fingerprint'],
            'items': [{**rows[p], 'object': reader.read(rows[p]['id'], kind=rows[p]['kind'], with_evidence=with_evidence)}
                      for p in dict.fromkeys(picks)]}


def render(hits, *, reader=None, body_char_limit=1200, scope_arc_key='', delivered_menu_keys=()):
    by_owner, menus = _arcs(reader, hits) if reader else ({}, {})
    cards, parts, used = [], [], {}
    for hit in hits:
        doc = hit['object']['document']
        source = doc['body_md']
        if hit['kind'] == 'scene':
            source = bucket_text_for_embedding({'content': source, 'metadata': {'memory_value_source': 'authored_scene', **doc['metadata']}})
        excerpt = hit.get('body_excerpt')
        if excerpt:
            if not excerpt['text'].strip():
                continue
            from .passages import regions
            spans = excerpt.get('source_spans') or [
                {'start_offset':excerpt['start_offset'],'end_offset':excerpt['end_offset']}]
            allowed = regions(doc)
            previous_end = 0
            pieces = []
            for span in spans:
                start,end = span['start_offset'],span['end_offset']
                if (type(start) is not int or type(end) is not int or start < previous_end or
                        not any(a <= start < end <= b for a,b in allowed)):
                    raise ValueError('Recall excerpt must match a canonical evidence region')
                pieces.append(doc['body_md'][start:end])
                previous_end = end
            if ''.join(pieces) != excerpt['text']:
                raise ValueError('Recall excerpt must match canonical evidence text')
            source = excerpt['text']
        body = GatewayService._clip_text(source.strip(), max(160, min(2400, int(body_char_limit or 1200))))
        if not body:
            continue
        ref = f"{hit['kind']}:{hit['id']}"
        title = str(doc['title'] or hit['id']).strip()
        card = {'id': ref, 'source': 'serein', 'source_kind': hit['kind'], 'title': title,
                'text': body, 'score': round(float(hit.get('score') or 0), 4), 'render_shape': 'typed_memory',
                **memory_dates(doc, hit['kind'])}
        if excerpt:
            card['source_spans'] = spans
            card['reranker_input_tokens'] = excerpt.get('input_tokens')
        cards.append(card)
        lines = [f'[typed_memory ref={ref}]', f'title: {title}']
        lines.extend(date_lines(card))
        arcs = by_owner.get(hit['id'], [])
        arc = next((c for c in arcs if c['arc_key'] == scope_arc_key), None) if scope_arc_key else (arcs[0] if len(arcs) == 1 else None)
        if arc:
            key = arc['arc_key']
            used[key] = menus[key]
            lines.append(f"Arc: {arc['title'] or key} (key={key}) [可按需读取]")
        else:
            lines.append('可能是你的相关记忆，若无关可忽略')
        lines.extend(['text: |', *(f'  {line}' for line in body.splitlines()), '[/typed_memory]'])
        parts.append('\n'.join(lines))
    included, suppressed, fingerprints = [], [], {}
    for key, menu in used.items():
        if key in delivered_menu_keys:
            suppressed.append(key)
            continue
        lines = [f'[arc_materials key={key}]', f"Arc: {menu['title'] or key} [可按需读取]", f"material_count: {menu['material_count']}"]
        for item in menu['materials']:
            day = f" {item['date']}" if item['date'] else ''
            lines.append(f"[{item['index']}] {item['kind']}: {item['title']}{day} (id={item['id']})")
        if menu['menu_truncated']:
            lines.append('menu_truncated: true')
        lines.extend([f'使用 read_arc_materials(arc_key="{key}", picks=[编号]) 一次读取最多5项。', '[/arc_materials]'])
        parts.append('\n'.join(lines))
        included.append(key)
        fingerprints[key] = menu['menu_fingerprint']
    if cards:
        parts.append('需要绑定原文时，用 read_memory(identifier=Event或Scene记忆ID, with_evidence=True) 一次读取正文及全部当前有效证据。')
    context = '\n\n'.join(parts)
    return {'cards': cards, 'context': context,
            'additional_context': GatewayService._render_hook_recall_additional_context(cards),
            'full_additional_context': GatewayService._render_hook_recall_full_additional_context(context),
            'menus_included': included, 'menus_suppressed': suppressed, 'menu_fingerprints': fingerprints,
            'injected': False}

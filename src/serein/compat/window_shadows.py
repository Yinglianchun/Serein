"""Explicit window writing and latest-window continuity; no model is called here."""
import json
import re

from ..core.store import Store, Conflict, encode, digest, now

WRITING_RULE = ('准备换窗时，由正在聊天的主模型写这一窗：我眼中的你、我眼中的自己、这一窗发生的事。'
    '写具体的新认识、变化、选择和未完事项；没有变化可留空，不把暂时情绪写成固定人格。'
    '最近的事可引用已有 Scene，不自动把窗影拆成 Scene。换窗读取最新正文，空白画像沿用各自最近一次填写的内容。')


def latest_shadow(store):
    return store.conn.execute("SELECT * FROM historical_works WHERE kind='shadow' "
        "AND id NOT IN (SELECT document_id FROM deletions) "
        "ORDER BY COALESCE(julianday(json_extract(metadata_json,'$.created_at')),0) DESC,id DESC LIMIT 1").fetchone()


VIEW_LABELS = {'user_view': '我眼中的你', 'self_view': '我眼中的自己'}


def _shadow_views(row):
    raw = json.loads(row['metadata_json']).get('sections_json')
    try:
        sections = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        sections = None
    if isinstance(sections, dict) and sections:
        return {key: value.strip() for key in VIEW_LABELS
                if isinstance(value := sections.get(key), str) and value.strip()}

    # Some imports only stored the authored Markdown. Ignore headings inside code.
    paragraphs = {key: [] for key in VIEW_LABELS}
    labels = {label: key for key, label in VIEW_LABELS.items()}
    current, depth, fence = None, 0, ''
    for line in row['body_md'].splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if not fence:
                fence = token
            elif token.startswith(fence) and not line[marker.end():].strip():
                fence = ''
        heading = not marker and not fence and re.match(r'^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$', line)
        if heading:
            label = heading[2]
            if label in labels:
                current, depth = labels[label], len(heading[1])
                continue
            if label == '这一窗发生的事' or len(heading[1]) <= depth:
                current = None
        if current:
            paragraphs[current].append(line)
    return {key: text for key, lines in paragraphs.items() if (text := '\n'.join(lines).strip())}


def continuity_shadow(store, row):
    """Resolve missing views for the latest shadow; its archived body stays verbatim."""
    views = {}
    def collect(source, position):
        for key, text in _shadow_views(source).items():
            views.setdefault(key, {'text': text, 'position': position, 'title': source['title']})

    collect(row, 1)
    if len(views) < len(VIEW_LABELS):
        older = store.conn.execute("SELECT title,body_md,metadata_json FROM historical_works "
            "WHERE kind='shadow' AND id NOT IN (SELECT document_id FROM deletions) AND id<>? "
            "ORDER BY COALESCE(julianday(json_extract(metadata_json,'$.created_at')),0) DESC,id DESC", (row['id'],))
        for position, source in enumerate(older, 2):
            collect(source, position)
            if len(views) == len(VIEW_LABELS):
                break
    parts = [row['body_md']]
    for key, label in VIEW_LABELS.items():
        view = views.get(key)
        if view and view['position'] > 1:
            parts.append(f"## {label}（沿用倒数第 {view['position']} 篇窗影）\n\n{view['text']}")
    return '\n\n'.join(parts), views


def sync_introductions(store, sections):
    from ..deployment import read_from_store
    state = read_from_store(store)
    for field, section in [('user_description','user_view'),('ai_description','self_view')]:
        if sections.get(section):
            state['identity'][field] = sections[section]
    store.conn.execute("INSERT INTO background_state(name,value_json) VALUES ('deployment_settings',?) "
        "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json", (encode(state),))


class WindowShadows:
    def __init__(self, database):
        self.database = database

    def write(self, window_id: str, title: str, content: str = '', scene_ids: list[str] | None = None,
              expected_revision: int = 0, user_view: str | None = None,
              self_view: str | None = None, recent_events: str | None = None):
        """Write before handoff: user_view=我眼中的你, self_view=我眼中的自己, recent_events=这一窗发生的事.
        The latest saved window updates editable introductions. Empty views leave introductions unchanged.
        Reuse window_id/revision on retries. This never generates Scenes or a separate portrait.
        """
        if not window_id.strip() or len(window_id)>200 or not title.strip():
            raise ValueError('Window ID and title are required')
        sections = {'user_view':user_view, 'self_view':self_view, 'recent_events':recent_events}
        structured = any(value is not None for value in sections.values())
        sections = {key:(value or '').strip() for key,value in sections.items()}
        if any(len(sections[key])>2000 for key in ('user_view','self_view')) or len(sections['recent_events'])>20000:
            raise ValueError('Views allow 2000 characters each; recent events allow 20000')
        if structured:
            content = '\n\n'.join('## '+label+'\n\n'+sections[key] for key,label in
                [('user_view','我眼中的你'),('self_view','我眼中的自己'),('recent_events','这一窗发生的事')]
                if sections[key])
        if not content.strip():
            raise ValueError('Write at least one window section')
        key = 'shadow:'+window_id
        scene_ids = list(dict.fromkeys(scene_ids or []))
        with Store(self.database) as store, store.transaction(immediate=True):
            for scene in scene_ids:
                if not store.conn.execute("SELECT 1 FROM documents WHERE id=? AND kind='scene' AND lifecycle='active'",(scene,)).fetchone():
                    raise ValueError('Window shadow references an unavailable Scene')
            old = store.conn.execute("SELECT * FROM historical_works WHERE id=?",(key,)).fetchone()
            if store.conn.execute('SELECT 1 FROM deletions WHERE document_id=?',(key,)).fetchone():
                raise Conflict('A deleted window shadow cannot be overwritten')
            meta = json.loads(old['metadata_json']) if old else {}
            if old and old['title']==title and old['body_md']==content and meta.get('moment_bucket_ids_json')==encode(scene_ids) and meta.get('sections_json')==encode(sections if structured else {}):
                return {'id':key,'window_id':window_id,'revision':old['revision'],'status':'unchanged','ordinary_recall':False}
            if (old['revision'] if old else 0)!=expected_revision:
                raise Conflict('Read the current window shadow revision before editing')
            revision=expected_revision+1
            meta.update(window_id=window_id,revision_root_id=window_id,revision=revision,title=title,
                created_at=meta.get('created_at') or now(),updated_at=now(),
                sections_json=encode(sections if structured else {}),moment_bucket_ids_json=encode(scene_ids))
            store.conn.execute("INSERT INTO historical_works (id,kind,revision,title,body_md,body_sha256,metadata_json,origin,source_path) "
                "VALUES (?,'shadow',?,?,?,?,?,'authored','') ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,"
                "title=excluded.title,body_md=excluded.body_md,body_sha256=excluded.body_sha256,metadata_json=excluded.metadata_json",
                (key,revision,title,content,digest(content),encode(meta)))
            if structured and latest_shadow(store)['id']==key:
                sync_introductions(store,sections)
        return {'id':key,'window_id':window_id,'revision':revision,'status':'saved','ordinary_recall':False}

    def read(self, window_id: str = ''):
        """Read the shadow's full content once, with section headings preserved. Omit window_id for the latest; explicit IDs are for history/editing."""
        with Store(self.database,read_only=True) as store:
            store.conn.execute("BEGIN")
            row = store.conn.execute("SELECT * FROM historical_works WHERE kind='shadow' AND id IN (?,?) "
                "AND id NOT IN (SELECT document_id FROM deletions)",(window_id,'shadow:'+window_id)).fetchone() if window_id else latest_shadow(store)
            if row is None:return {'status':'not_found'}
            meta=json.loads(row['metadata_json'])
            return {'status':'ok','id':row['id'],'window_id':meta.get('window_id'), 'title':row['title'],
                'content':row['body_md'] if window_id else continuity_shadow(store,row)[0],'revision':row['revision'],
                'scene_ids':json.loads(meta.get('moment_bucket_ids_json') or '[]'),'ordinary_recall':False,
                'writing_rule':WRITING_RULE}

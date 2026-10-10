"""Authored MCP tools with the same calling contract as the self-use tools."""
from contextlib import contextmanager
from datetime import date as Date
import json
import logging
from uuid import uuid4

from pydantic import StrictBool

from ..compat.diaries import Diaries, project_diaries
from ..compat.scenes import Scenes, sources
from ..core.personal import Personal
from ..core.store import Store, now
from ..deployment import read_settings
from .read_text import diary_text
from .diary_read import diary_directory, validate_read


def tools_for(services, settings):
    scenes = Scenes(settings.database)
    personal = Personal(settings.database)

    def notebook(method, *args, write=False, **kwargs):
        # Read, validate, modify and project inside one transaction. The caller
        # does not need to manage the notebook's internal numeric revisions.
        class TransactionDiaries(Diaries):
            @contextmanager
            def _connection(self):
                yield store.conn

        if write and not settings.writable:
            raise ValueError('This deployment is read-only')
        with Store(settings.database, read_only=True) as store:
            canonical = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diaries'").fetchone()
        if not canonical:
            from .core_notebook_tools import invoke
            return invoke(settings.database, method, *args, **kwargs)
        with Store(settings.database, read_only=not write) as store:
            def run():
                result = getattr(TransactionDiaries(settings.database), method)(*args, **kwargs)
                if write:
                    project_diaries(store.conn)
                return result
            if write:
                with store.transaction(immediate=True):
                    return run()
            return run()

    def read_diary(diary_id: int | None = None, date: str = '', limit: int = 5,
                   query: str = '', offset: int = 0) -> str:
        """Find diaries by literal title/body query and/or exact date. Without diary_id, return a directory (default 5, limit 1..20) with excerpts of at most 150 characters; title matches rank first, then newest date/ID. Keep filters and limit when using next_offset. Omit filters for recent entries. Read one complete body and comments with diary_id; do not combine it with query/offset. Locked/deleted bodies stay hidden."""
        validate_read(diary_id, query, limit, offset)
        if diary_id is None:
            return diary_directory(settings.database, query=query, date=date, limit=limit, offset=offset, include_archived=True)
        return diary_text(notebook('read', diary_id=diary_id, date=date, limit=1))

    tools = {'read_diary': read_diary}
    if not settings.writable:
        return tools

    def sync_scene(result):
        key = result.get('scene_id')
        if key and result.get('status') == 'updated':
            try:
                services.sync_index(document_ids=[key])
            except Exception:
                logging.getLogger(__name__).warning('Scene saved; index refresh remains pending', exc_info=True)
        return result

    def write_scene(content: str, cues: list[str], title: str = '', date: str = '', domain: str = '',
                    evidence_refs: list[dict] | None = None, favorite: StrictBool | None = None):
        """Save an authored Scene without summarization. Only content and 1..8 short cues are required. Title/date/domain are optional; IDs are generated internally. Omit evidence_refs by default and do not search for original-message IDs just to save a Scene; attach exact evidence only when explicitly requested. Optional favorite requires the favorites feature. Use edit_scene for an existing Scene. If a response is lost, read/check before repeating: independent calls create independent Scenes."""
        cues = Scenes._cues(cues)
        if date:
            Date.fromisoformat(date)
        metadata = {'object_kind': 'scene', 'memory_value_source': 'authored_scene',
                    'write_contract': 'write-scene-v1', 'scene_cues': cues, 'date': date,
                    'canonical_domain': domain or 'general', 'domain': [domain or 'general'],
                    'scene_status': 'active', 'active': True, 'created': now()}
        request = {'kind': 'scene', 'title': title or content[:40], 'body_md': content,
                   'metadata': metadata, 'sources': sources(evidence_refs or [])}
        if favorite is not None:
            request['favorite'] = favorite
        result = services.write('scene_create:' + uuid4().hex, 'save', request, scene_only=True)
        reply = (f"已保存。\n[scene_id:{result['id']}]\n"
                 f"[evidence_status:{'bound' if evidence_refs else 'unbound'}]\n"
                 f"[evidence_bound_count:{len(evidence_refs or [])}]")
        # Keep the optional post-write context hint, without changing the saved
        # document or requiring callers to understand a different save result.
        hints = {k: v for k, v in result.items() if k not in {'id', 'kind', 'revision', 'status', 'index'}}
        if hints:
            reply += '\n' + json.dumps(hints, ensure_ascii=False)
        return reply

    def edit_scene(scene_id: str, expected_updated_at: str, title: str | None = None,
                   content: str | None = None, cues: list[str] | None = None):
        """Edit a Scene after reading its current updated_at. Omitted fields preserve their values and evidence. Stale timestamps never overwrite newer changes."""
        return sync_scene(scenes.edit(scene_id, expected_updated_at, title=title, content=content, cues=cues))

    def set_scene_status(scene_id: str, expected_updated_at: str, status: str, restore_surface: StrictBool = False):
        """Set a Scene active, archived or deleted using its current updated_at. Lifecycle changes preserve the manual surfacing switch. Explicit restore_surface=true with active also enables surfacing in the same transaction. Deleted is a retained soft deletion. Events cannot be changed through this tool."""
        return sync_scene(scenes.edit(scene_id, expected_updated_at, status=status, restore_surface=restore_surface))

    def write_diary(content: str, date: str = '', title: str = '', author: str = 'ai', unlock_at: str = ''):
        """Write an authored diary. Date defaults to today (UTC+8), author to ai. A future unlock_at creates a sealed entry. IDs are generated internally; no kind or revision is needed. Use revise_diary for edits. Check existing entries before retrying a lost response."""
        return notebook('create', write=True, content=content, date=date, title=title, author=author, unlock_at=unlock_at)

    def revise_diary(diary_id: int, content: str, title: str | None = None, date: str | None = None):
        """Revise a readable diary while preserving authorship and previous versions. Omitted title/date stay unchanged. Locked or deleted entries cannot be edited."""
        return notebook('revise', diary_id, write=True, content=content, title=title, date=date)

    def comment_diary(diary_id: int, content: str, author: str = 'ai'):
        """Persist a comment on a readable diary, with author defaulting to ai. This is not a general memory annotation."""
        return notebook('comment', diary_id, write=True, content=content, author=author)

    def delete_diary(diary_id: int):
        """Soft-delete the explicitly selected readable diary, preserving its history. Locked entries cannot be deleted."""
        return notebook('delete', diary_id, write=True)

    def annotate(memory_id: str, content: str, author: str = '', role: str = 'assistant', annotation_id: str = ''):
        """Append a separate annotation without rewriting the memory or evidence. Omitted author uses this instance's configured AI name. Reuse annotation_id for an identical retry."""
        return personal.annotate(memory_id, content, author or read_settings(settings.database)['identity']['ai_name'],
                                 role, annotation_id)

    tools.update({fn.__name__: fn for fn in (write_scene, edit_scene, set_scene_status, write_diary,
                                           revise_diary, comment_diary, delete_diary, annotate)})
    return tools

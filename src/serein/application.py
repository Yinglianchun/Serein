"""Shared application assembly for CLI and future MCP/HTTP hosts.

Private deployment code can supply lazy extension factories here. Core storage
and retrieval never import deployment settings or extension implementations.
"""

from .config import Settings
from .core.reader import Reader
from .recall.index import Search
from .extensions import Contributions
from .core.store import Store
from .core.writer import Writer
from .recall.index import refresh_index
from .recall.service import Recall
import json
import logging
import sqlite3


class Services:
    def __init__(self, settings: Settings):
        self._settings = settings
        from .adapters.raw import RawMessages
        self.source = RawMessages(settings.database)
        if settings.source:
            from .adapters.bridge import BridgeMessages
            self.source = BridgeMessages(settings.source["database"], settings.source["session_ids"], settings.source["system"])

    def read(self, identifier, **options):
        with Reader(self._settings.database) as reader:
            return reader.read(identifier, **options)

    def read_with_menus(self, identifier, **options):
        with Reader(self._settings.database) as reader:
            result = reader.read(identifier, **options)
            result["narrative_menus"] = []
            if result.get("readable") and result.get("kind") in {"event", "scene"}:
                from .recall.rendering import _arcs
                by_owner, menus = _arcs(reader, [{"id": result["id"]}])
                result["narrative_menus"] = [menus[item["arc_key"]]
                                               for item in by_owner.get(result["id"], [])]
            return result

    def materials(self, identifier, **options):
        with Reader(self._settings.database) as reader:
            return reader.materials(identifier, **options)

    def arc_picks(self, arc_key, picks, *, with_evidence=False):
        from .recall.rendering import read_arc_picks
        with Reader(self._settings.database) as reader:
            return read_arc_picks(reader, arc_key, picks, with_evidence=with_evidence)

    def search(self, query, **options):
        if self._settings.index is None:
            raise ValueError("Search requires a configured index")
        with Search(self._settings.database, self._settings.index) as search:
            return search.search(query, **options)

    def recall(self, query, *, threshold_override=None, manual_fallback=False, **options):
        from .configured_models import effective_settings
        from dataclasses import replace
        settings = effective_settings(self._settings)
        # Only an explicit automatic-method sentinel opts in; other callers that
        # omit method retain Recall.run's lexical default. Resolve against the
        # same effective snapshot used to construct the engine, including UI saves.
        if "method" in options and options["method"] is None:
            options["method"] = "semantic" if settings.embedding else "lexical"
        if threshold_override is not None:
            settings = replace(settings, recall={**settings.recall, 'direct_threshold':threshold_override})
        engine = Recall(settings)
        result = engine.run(query, **options)
        if manual_fallback:
            from .recall.manual import fallback_lookup
            result = fallback_lookup(engine, query, result, options)
        return {**result, 'direct_threshold':engine.policy.direct_threshold}

    def find_arc(self, query, limit=5):
        from .configured_models import effective_settings
        return Recall(effective_settings(self._settings)).find_arc(query, limit)

    def write(self, operation_id, action, request, *, scene_only=False):
        if not self._settings.writable:
            raise ValueError("This deployment is read-only")
        def prepare(value):
            if scene_only:
                # Runs under Writer's transaction, after the retry receipt check.
                draft = value
                if action == 'review':
                    if value.get('decision') != 'accept':
                        return value
                    row = writer.store.conn.execute('SELECT request_json FROM memory_candidates WHERE id=?', (value['candidate_id'],)).fetchone()
                    if row is None:
                        raise ValueError('Scene proposal not found')
                    draft = json.loads(row['request_json'])
                if draft.get('kind') != 'scene':
                    raise ValueError('Main-model memory authoring only accepts Scene; use the Event pipeline or narrative_volume')
                target = writer.store.read(draft['document_id']) if draft.get('document_id') else None
                if draft.get('document_id') and target is None:
                    raise ValueError('Scene to edit was not found')
                if target and (target['kind'] != 'scene' or target['metadata'].get('source_record_immutable')):
                    raise ValueError('Only editable Scenes can be changed here')
                if action in {'save', 'propose'}:
                    metadata = dict(target['metadata']) if target else {**value.get('metadata', {}), 'object_kind':'scene', 'memory_value_source':'authored_scene'}
                    if 'cues' in value:
                        metadata['scene_cues'] = value['cues']
                    if 'date' in value:
                        metadata['date'] = value['date']
                    previous = {'title':target['title'], 'body_md':target['body_md']} if target else {}
                    return {**previous, **value, 'metadata':metadata}
            if action in {"save", "propose"} and value.get("source_message_ids"):
                if self.source is None:
                    raise ValueError("No message source configured")
                return {**value, "sources": [*value.get("sources", []), *self.source.read(value["source_message_ids"])]}
            return value
        from .deployment import read_from_store
        from .compat.diaries import write_in_store as write_diary
        with Writer(self._settings.database, favorite_policy=lambda store: read_from_store(store)['features']['favorites'],
                    promotion_policy=lambda store: read_from_store(store)['features']['event_to_scene'],
                    diary_writer=write_diary) as writer, writer.store.transaction(immediate=True):
            result = writer.execute(operation_id, action, request, prepare=prepare)
            if scene_only and request.get('sources') and writer.store.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scene_evidence_ids'").fetchone():
                from .compat.scenes import map_evidence_ids
                map_evidence_ids(writer.store)
        # A failed cache update must not hide a successful canonical commit.
        try:
            published = result.get('document') or result
            index_status = self.sync_index(document_ids=[published['id']] if published.get('id') else [])
        except (OSError, ValueError, sqlite3.Error):
            index_status = {"status": "pending", "note": "Canonical write succeeded; index update remains pending."}
        response = {**result, "index": index_status}
        if action == 'save' and result.get('kind') == 'scene' and not request.get('document_id'):
            # A hint is never part of the canonical write or its retry receipt.
            # In particular, an unavailable search service must not turn a
            # committed Scene into a reported write failure.
            try:
                from .deployment import feature_enabled
                if feature_enabled(self._settings.database, 'write_context'):
                    from .recall.write_context import find_write_context
                    response.update(find_write_context(self._settings, result['id']))
            except Exception:
                logging.getLogger(__name__).warning('Post-write context lookup failed; Scene remains saved', exc_info=True)
        return response

    def sync_index(self, *, document_ids=None):
        if not self._settings.writable:
            raise ValueError("This deployment is read-only")
        with Writer(self._settings.database) as writer:
            rows = (writer.store.conn.execute("SELECT * FROM index_outbox ORDER BY sequence").fetchall() if document_ids is None else
                    [row for key in set(document_ids) for row in writer.store.conn.execute('SELECT * FROM index_outbox WHERE document_id=? ORDER BY sequence',(key,))])
            if not rows:
                return {"status": "current", "updated": 0}
            from .recall.passage_layouts import prepare_layouts
            layouts = prepare_layouts(self._settings, [row['document_id'] for row in rows])
            if self._settings.index is None:
                return {"status": "pending", "note": "No index configured"}
            refresh_index(self._settings.database, self._settings.index, [row["document_id"] for row in rows])
            if (layouts['status'] != 'disabled' or self._settings.embedding or
                    writer.store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='event_projection_pending' AND type='table'").fetchone()):
                return {"status":"queued","note":"Lexical view updated; persistent worker owns vectors and queue acknowledgement"}
            writer.store.conn.executemany("DELETE FROM index_outbox WHERE sequence=?", [(row['sequence'],) for row in rows])
            return {"status": "current", "updated": len({row["document_id"] for row in rows})}

    def candidates(self, *, status="pending", limit=20):
        if status not in {"pending", "accepted", "dismissed"} or not 1 <= limit <= 100:
            raise ValueError("Invalid candidate status or limit")
        with Store(self._settings.database, read_only=True) as store:
            if store.conn.execute("PRAGMA user_version").fetchone()[0] < 7:
                return {"status": "schema_upgrade_required", "items": []}
            rows = store.conn.execute("SELECT * FROM memory_candidates WHERE status=? ORDER BY created_at,id LIMIT ?", (status, limit))
            return {"status": status, "items": [{"id": row["id"], "request": json.loads(row["request_json"]) if status == "pending" else None,
                                                  "result": json.loads(row["result_json"]) if row["result_json"] else None,
                                                  "created_at": row["created_at"], "status": row["status"]} for row in rows]}


class Application:
    def __init__(self, settings: Settings, *, extension_factories=None):
        self.settings = settings
        self.services = Services(settings)
        self.contributions = Contributions(tools={
            "memory_read": self.services.read,
            "memory_materials": self.services.materials,
            "memory_search": self.services.search,
        })
        if settings.writable:
            self.contributions.tools.update(memory_write=self.services.write, memory_candidates=self.services.candidates,
                                            memory_recall=self.services.recall)
            from .extensions.pipeline import tools_for
            self.contributions.tools.update(tools_for(settings))
        if self.services.source is not None:
            self.contributions.tools.update(source_messages=self.services.source.list, source_read=self.services.source.read)
        if settings.writable and settings.index:
            from .lifecycle import index_job
            self.contributions.jobs['index'] = lambda: index_job(settings)
        from .extensions.builtin import FACTORIES
        factories = {**FACTORIES, **(extension_factories or {})}
        self.enabled_extensions = [name for name, options in settings.extensions.items() if options.get("enabled", False)]
        unavailable = set(self.enabled_extensions) - factories.keys()
        if unavailable:
            raise ValueError(f"Enabled extensions are not installed: {', '.join(sorted(unavailable))}")
        for name in self.enabled_extensions:
            added = factories[name](self.services, settings.extensions[name])
            for category in ("tools", "prompt_hooks", "jobs"):
                target, incoming = getattr(self.contributions, category), getattr(added, category)
                duplicate = target.keys() & incoming.keys()
                if duplicate:
                    raise ValueError(f"Extension {name} duplicates {category}: {', '.join(sorted(duplicate))}")
                target.update(incoming)

        self._optional_names = set()
        self.refresh_optional()

    def refresh_optional(self):
        from .extensions.optional import tools_for
        for name in self._optional_names:
            self.contributions.tools.pop(name, None)
        self.contributions.tools.pop("resume", None)
        self.contributions.prompt_hooks.pop("new_window", None)
        added = tools_for(self.settings)
        self.contributions.tools.update(added)
        self._optional_names = set(added)

    def capabilities(self):
        self.refresh_optional()
        return {"extensions": self.enabled_extensions,
                **{key: sorted(getattr(self.contributions, key)) for key in ("tools", "prompt_hooks", "jobs")}}


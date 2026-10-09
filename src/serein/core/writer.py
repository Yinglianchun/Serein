"""Explicit transactional writes with retry receipts and optimistic revisions."""

import json
from datetime import date, datetime, timezone
from uuid import uuid4

from .store import Store, Conflict, digest, encode, now, promoted_scene_id


class Writer:
    def __init__(self, database, *, favorite_policy=None, promotion_policy=None, diary_writer=None):
        # Writing tools cannot initialize a typo path or implicitly migrate a DB.
        with Store(database, read_only=True) as existing:
            if existing.conn.execute("PRAGMA user_version").fetchone()[0] not in (8,9):
                raise ValueError("Initialize or migrate the runtime database to schema v8 or v9 first")
        self.store = Store(database)
        self.favorite_policy = favorite_policy
        self.promotion_policy = promotion_policy
        self.diary_writer = diary_writer

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.store.close()

    def execute(self, operation_id, action, request, *, prepare=None):
        if not operation_id or not operation_id.strip():
            raise ValueError("A stable operation_id is required for retry safety")
        # A live transplant uses the original Event/Diary writer contracts.
        # Their documents/notebook rows are atomic projections, never a second
        # entry point for modifying those canonical objects.
        production_diary = action.startswith('diary_') and self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='diaries' AND type='table'").fetchone()
        if production_diary and self.diary_writer is None:
            raise ValueError('Use the production Diary writer for this database')
        target=self.store.read(request.get('document_id','')) if request.get('document_id') else None
        favorite_only = action == 'state' and request.get('favorite') is not None and set(request) <= {'document_id','expected_revision','favorite'}
        if not favorite_only and (request.get('kind')=='event' or target and target['kind']=='event') and self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='fact_events' AND type='table'").fetchone():
            raise ValueError('Use the production Event writer for this database')
        stamp = digest(encode({"action": action, "request": request}))
        with self.store.transaction():
            # Obtain the write lock before checking receipt/revision state.
            self.store.conn.execute("UPDATE write_receipts SET operation_id=operation_id WHERE 0")
            if action in {"promote_event", "mailbox_select", "mailbox_draft", "mailbox_remove"} and (self.promotion_policy is None or not self.promotion_policy(self.store)):
                raise ValueError("Event to Scene promotion is disabled")
            old = self.store.conn.execute("SELECT * FROM write_receipts WHERE operation_id=?", (operation_id,)).fetchone()
            if old:
                if old["request_sha256"] != stamp:
                    raise Conflict("operation_id was already used with different arguments")
                return json.loads(old["result_json"])
            from .event_mailbox import mutate
            methods = {"mailbox_select": lambda r: mutate(self.store,r,"select"),
                       "mailbox_draft": lambda r: mutate(self.store,r,"draft"),
                       "mailbox_remove": lambda r: mutate(self.store,r,"remove"),
                       "save": self._save, "promote_event": self._promote_event,
                       "state": self._state, "evidence": self._evidence,
                       "propose": self._propose, "review": self._review,
                       "diary_save": self._diary_save, "diary_comment": self._diary_comment, "diary_delete": self._diary_delete}
            if action not in methods:
                raise ValueError("Unknown write action")
            prepared = prepare(request) if prepare is not None else request
            result = self.diary_writer(self.store, action, prepared) if production_diary else methods[action](prepared)
            self.store.conn.execute("INSERT INTO write_receipts VALUES (?,?,?,?)", (operation_id, stamp, encode(result), now()))
            return result

    def _current(self, document_id, revision):
        doc = self.store.read(document_id)
        if doc is None or doc["revision"] != revision:
            raise Conflict("Object is missing or its revision changed")
        if doc["lifecycle"] == "deleted":
            raise Conflict("Deleted objects require a separate explicit restore operation")
        return doc

    def _dirty(self, document_id):
        self.store.conn.execute("INSERT INTO index_outbox(document_id) VALUES (?)", (document_id,))

    def _check_favorite(self, kind, value):
        if type(value) is not bool or kind not in {'event','scene'}:
            raise ValueError('Favorite requires an Event/Scene and a boolean')
        if self.favorite_policy is None or not self.favorite_policy(self.store):
            raise ValueError('Favorite tools are disabled')

    def _favorite(self, document_id, value):
        from .personal import Personal
        doc = self.store.read(document_id)
        self._check_favorite(doc['kind'], value)
        if doc['lifecycle'] not in {'active','archived'}:
            raise ValueError('Only active or archived memories can change favorites')
        row = self.store.conn.execute("SELECT revision FROM personal_records WHERE scope='favorite' AND key=?", (document_id,)).fetchone()
        record = Personal.save_in_store(self.store, 'favorite', document_id, {'favorite':value},
                                       document_id=document_id, expected_revision=row['revision'] if row else 0)
        return {'favorite':value, 'favorite_revision':record['revision']}

    def _save(self, request):
        kind, title, body = request["kind"], request["title"], request["body_md"]
        if kind not in {"scene", "event", "narrative"} or not title.strip() or not body.strip():
            raise ValueError("Saving requires a Scene/Event/Narrative kind and nonempty title/body")
        document_id = request.get("document_id")
        if document_id:
            old = self._current(document_id, request.get("expected_revision"))
            if old["kind"] != kind:
                raise Conflict("Cannot change object kind")
            doc = self.store.revise(document_id, expected_revision=old["revision"], title=title,
                                    body_md=body, metadata=request.get("metadata"))
        else:
            document_id = kind + "_" + uuid4().hex
            doc = self.store.create(document_id, kind, title, body, metadata=request.get("metadata"),
                                    manual_surface=False if kind == "narrative" else True)
        # Omission preserves evidence; explicit binding changes use the evidence tool.
        for source in request.get("sources", []):
            source_id = self.store.add_source(source["source_key"], source["content"], metadata=source.get("metadata"))
            self.store.bind(document_id, source_id, actor=request.get("actor", "assistant"))
        if kind == "event" and not self.store.conn.execute(
                "SELECT 1 FROM evidence_bindings WHERE document_id=? AND active=1", (document_id,)).fetchone():
            raise ValueError("Event requires at least one original evidence snapshot")
        if kind == "narrative":
            previous = doc["revision"] - 1
            if request.get("materials") is None and previous:
                self.store.conn.execute(
                    "INSERT INTO narrative_materials SELECT document_id,?,locator,kind,target_id,disposition,metadata_json "
                    "FROM narrative_materials WHERE document_id=? AND revision=?", (doc["revision"], document_id, previous))
            else:
                for i, material in enumerate(request.get("materials") or []):
                    self.store.conn.execute("INSERT INTO narrative_materials VALUES (?,?,?,?,?,?,?)",
                                            (document_id, doc["revision"], f"authored:{i}", material["kind"], material["id"],
                                             material.get("disposition", "linked"), encode(material)))
        replacements = request.get("replaces", [])
        if replacements and kind != "event":
            raise ValueError("Only an Event can replace an Event")
        for previous in replacements:
            old = self._current(previous["id"], previous["revision"])
            if old["kind"] != "event" or old["id"] == document_id:
                raise Conflict("Replacement requires a different Event")
            # New successor creation only prevents cycles and ambiguous replacement rewrites.
            if request.get("document_id"):
                raise Conflict("Replacement must create a new successor Event")
            self.store.conn.execute("INSERT INTO event_replacements VALUES (?,?,?,?)",
                                    (old["id"], document_id, "serein", encode({"actor": request.get("actor", "assistant")})))
            self._state({"document_id": old["id"], "expected_revision": old["revision"], "lifecycle": "superseded"})
        mark = self._favorite(document_id, request['favorite']) if request.get('favorite') is not None else {}
        self._dirty(document_id)
        return {"id": document_id, "kind": kind, "revision": doc["revision"], "status": "saved", **mark}

    def _promote_event(self, request):
        from .event_mailbox import check_promotion, complete
        from ..compat.scenes import Scenes
        mailbox_row = check_promotion(self.store, request)
        saved_cues = json.loads(mailbox_row["payload_json"]).get("draft", {}).get("cues", []) if mailbox_row else []
        cues = Scenes._cues(request["cues"]) if "cues" in request else (Scenes._cues(saved_cues) if saved_cues else [])
        event = self._current(request["event_id"], request["expected_revision"])
        if event["kind"] != "event" or event["lifecycle"] != "active":
            raise Conflict("Only an active Event can become a Scene")
        title, body = request["title"].strip(), request["body_md"].strip()
        if not title or not body:
            raise ValueError("Scene title and edited body are required")
        bindings = self.store.conn.execute(
            "SELECT source_id,metadata_json FROM evidence_bindings WHERE document_id=? AND active=1 ORDER BY id",
            (event["id"],)).fetchall()
        if not bindings:
            raise ValueError("Event has no active original evidence to carry into the Scene")
        scene_id = promoted_scene_id(event["id"])
        if self.store.read(scene_id):
            raise Conflict("This Event already has a promoted Scene; edit that Scene instead")
        domain = event["metadata"].get("canonical_domain") or "general"
        scene = self.store.create(scene_id, "scene", title, body,
                                  metadata={"object_kind": "scene", "memory_value_source": "authored_scene",
                                            "write_contract": "event-to-scene-v1", "scene_cues": cues,
                                            "canonical_domain": domain, "domain": [domain],
                                            "date": event["metadata"].get("local_date") or "",
                                            "created": now(), "promoted_from_event": {"id": event["id"],
                                              "revision": event["revision"], "body_sha256": event["body_sha256"]}})
        for binding in bindings:
            self.store.bind(scene_id, binding["source_id"],
                            metadata=json.loads(binding["metadata_json"]), actor="assistant")
        if self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='scene_evidence_ids'").fetchone():
            self.store.conn.execute("INSERT OR IGNORE INTO scene_evidence_ids(binding_id) "
                                    "SELECT id FROM evidence_bindings WHERE document_id=? AND active=1", (scene_id,))
        # Revision-box hints are derived routing data. The authored Scene now
        # owns the material; retire stale hints without losing authored drafts.
        if self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='narrative_proposals'").fetchone():
            for row in self.store.conn.execute("SELECT id,metadata_json FROM narrative_proposals WHERE status='pending'").fetchall():
                hint = json.loads(row["metadata_json"])
                if (event["id"] in hint.get("source_event_ids", []) or
                        hint.get("latest_material_type") == "event" and hint.get("latest_material_id") == event["id"] or
                        hint.get("source_type") == "event" and hint.get("source_id") == event["id"]):
                    hint.update(status="dismissed", resolution="event_promoted_to_scene", updated_at=now())
                    self.store.conn.execute("UPDATE narrative_proposals SET status='dismissed',metadata_json=? WHERE id=?",
                                            (encode(hint), row["id"]))
        self._dirty(scene_id)
        return {"id": scene_id, "kind": "scene", "revision": scene["revision"], "status": "saved",
                "source_event_id": event["id"], "event_surface": self.store.surface_state(event["id"]),
                **complete(self.store, mailbox_row, scene_id)}

    def _state(self, request):
        doc = self._current(request["document_id"], request["expected_revision"])
        if "lifecycle" in request:
            if request["lifecycle"] not in {"active", "archived", "superseded", "deleted"}:
                raise ValueError("Unsupported lifecycle")
            self.store.set_lifecycle(doc["id"], request["lifecycle"])
        if "can_surface" in request:
            self.store.set_manual_surface(doc["id"], request["can_surface"])
        mark = self._favorite(doc['id'], request['favorite']) if request.get('favorite') is not None else {}
        if mark and not {'lifecycle','can_surface'} & request.keys():
            return {'id':doc['id'], 'revision':doc['revision'], 'status':'updated', **mark}
        # Advance revision even for state-only edits so concurrent review cannot be lost.
        self.store._add_revision(doc["id"], doc["revision"] + 1, doc["title"], doc["body_md"], doc["metadata"], now())
        self.store.conn.execute("UPDATE documents SET revision=?,updated_at=? WHERE id=?", (doc["revision"] + 1, now(), doc["id"]))
        if doc["kind"] == "narrative":
            self.store.conn.execute("INSERT INTO narrative_materials SELECT document_id,?,locator,kind,target_id,disposition,metadata_json "
                                    "FROM narrative_materials WHERE document_id=? AND revision=?", (doc["revision"] + 1, doc["id"], doc["revision"]))
        self._dirty(doc["id"])
        return {"id": doc["id"], "revision": doc["revision"] + 1, "status": "updated", **mark}

    def _evidence(self, request):
        doc = self._current(request["document_id"], request["expected_revision"])
        if doc["kind"] not in {"scene", "event"}:
            raise ValueError("Evidence bindings belong to Scene/Event")
        for source in request.get("bind", []):
            source_id = self.store.add_source(source["source_key"], source["content"], metadata=source.get("metadata"))
            self.store.bind(doc["id"], source_id, actor=request.get("actor", "assistant"))
        for binding_id in request.get("unbind", []):
            binding = self.store.conn.execute("SELECT document_id FROM evidence_bindings WHERE id=?", (binding_id,)).fetchone()
            if not binding or binding[0] != doc["id"]:
                raise Conflict("Binding does not belong to this object")
            self.store.unbind(binding_id, actor=request.get("actor", "assistant"))
        if doc["kind"] == "event" and not self.store.conn.execute(
                "SELECT 1 FROM evidence_bindings WHERE document_id=? AND active=1", (doc["id"],)).fetchone():
            raise ValueError("Event must retain original evidence")
        return self._state({"document_id": doc["id"], "expected_revision": doc["revision"]})

    def _propose(self, request):
        if request.get('favorite') is not None:
            self._check_favorite(request.get('kind'), request['favorite'])
        candidate_id = "candidate_" + uuid4().hex
        self.store.conn.execute("INSERT INTO memory_candidates VALUES (?,?, 'pending',NULL,?,NULL)",
                                (candidate_id, encode(request), now()))
        return {"id": candidate_id, "status": "pending"}

    def _review(self, request):
        row = self.store.conn.execute("SELECT * FROM memory_candidates WHERE id=?", (request["candidate_id"],)).fetchone()
        if not row or row["status"] != "pending":
            raise Conflict("Candidate is missing or already reviewed")
        if request["decision"] not in {"accept", "dismiss"}:
            raise ValueError("Review decision must be accept or dismiss")
        result = self._save(json.loads(row["request_json"])) if request["decision"] == "accept" else None
        status = "accepted" if result else "dismissed"
        self.store.conn.execute("UPDATE memory_candidates SET status=?,result_json=?,reviewed_at=? WHERE id=?",
                                (status, encode(result), now(), row["id"]))
        return {"id": row["id"], "status": status, "document": result}

    def _notebook(self, entry_id, revision=None, *, readable=True):
        from .notebook import resolve_entry
        row = self.store.conn.execute("SELECT * FROM diary_entries WHERE id=?", (entry_id,)).fetchone()
        if not row or (revision is not None and row["revision"] != revision):
            raise Conflict("Notebook entry is missing or its revision changed")
        if row["deleted_at"] or row["visibility"] == "deleted":
            raise Conflict("Notebook entry was deleted")
        if readable and resolve_entry(self.store, entry_id)["resolution"] != "active":
            raise Conflict("Notebook entry is not currently readable")
        return dict(row)

    def _notebook_history(self, row):
        self.store.conn.execute("INSERT INTO diary_history(entry_id,revision,body_md,metadata_json) VALUES (?,?,?,?)",
                                (row["id"], row["revision"], row["body_md"], row["metadata_json"]))

    def _diary_save(self, request):
        kind, author, day, body = request["kind"], request["author"], request["day"], request["body_md"]
        if kind not in {"diary", "darkroom"} or author not in {"ai", "user"} or not body.strip():
            raise ValueError("Notebook requires diary/darkroom, explicit ai/user author, and nonempty body")
        date.fromisoformat(day)
        entry_id, unlock = request.get("entry_id"), request.get("unlock_at") or ""
        timestamp = now()
        if unlock:
            parsed = datetime.fromisoformat(unlock)
            if parsed.tzinfo is None:
                raise ValueError("unlock_at must include a timezone")
        if not entry_id and kind == "darkroom" and (not unlock or datetime.fromisoformat(unlock) <= datetime.now(timezone.utc)):
            raise ValueError("A new darkroom entry requires a future unlock time")
        old = self._notebook(entry_id, request.get("expected_revision")) if entry_id else None
        if old and (request.get("expected_revision") is None or old["kind"] != kind or old["author"] != author):
            raise Conflict("Editing requires the current revision and preserves kind and author")
        if old and request.get("unlock_at") is None:
            unlock = old["unlock_at"]
        meta = json.loads(old["metadata_json"]) if old else {}
        if old:
            self._notebook_history(old)
        entry_id = entry_id or self.store.conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM diary_entries").fetchone()[0]
        meta.update(id=entry_id, entry_type=kind, author=author, date=day, content=body, title=request.get("title"),
                    revision=old["revision"] + 1 if old else 1, visibility="active", unlock_at=unlock,
                    deleted_at="", created_at=old["created_at"] if old else timestamp, updated_at=timestamp,
                    source_id=old["source_id"] if old else "", metadata=meta.get("metadata", "{}"),
                    emotion_tags=meta.get("emotion_tags", "[]"))
        self.store.conn.execute(
            "INSERT INTO diary_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "revision=excluded.revision,day=excluded.day,title=excluded.title,body_md=excluded.body_md,"
            "unlock_at=excluded.unlock_at,updated_at=excluded.updated_at,metadata_json=excluded.metadata_json",
            (entry_id, kind, meta["revision"], author, day, meta["title"], body, "active", unlock, "", meta["created_at"],
             timestamp, meta["source_id"], encode(meta)))
        return {"id": str(entry_id), "kind": kind, "revision": meta["revision"], "status": "saved"}

    def _diary_comment(self, request):
        row = self._notebook(request["entry_id"])
        if request["author"] not in {"ai", "user"} or not request["body_md"].strip():
            raise ValueError("Comment requires an explicit ai/user author and nonempty body")
        comment_id = self.store.conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM diary_comments").fetchone()[0]
        meta = {"id": comment_id, "diary_id": row["id"], "author": request["author"],
                "content": request["body_md"], "created_at": now()}
        self.store.conn.execute("INSERT INTO diary_comments VALUES (?,?,?,?,?,?)",
                                (comment_id, row["id"], meta["author"], meta["content"], meta["created_at"], encode(meta)))
        return {"id": comment_id, "entry_id": row["id"], "status": "saved"}

    def _diary_delete(self, request):
        row = self._notebook(request["entry_id"], request["expected_revision"], readable=False)
        self._notebook_history(row)
        meta = json.loads(row["metadata_json"])
        meta.update(visibility="deleted", deleted_at=now(), updated_at=now(), revision=row["revision"] + 1)
        self.store.conn.execute("UPDATE diary_entries SET visibility='deleted',deleted_at=?,updated_at=?,revision=?,metadata_json=? WHERE id=?",
                                (meta["deleted_at"], meta["updated_at"], meta["revision"], encode(meta), row["id"]))
        return {"id": str(row["id"]), "kind": row["kind"], "revision": meta["revision"], "status": "deleted"}

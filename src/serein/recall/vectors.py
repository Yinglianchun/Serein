"""Resumable whole-body vector coverage in the disposable index only."""

from collections import Counter
import json
from pathlib import Path
import sqlite3

from ..adapters.embedding import EmbeddingClient
from ..core.reader import Reader
from ..core.store import encode
from .index import Search, content_stamp, refresh_index, unit_vector
from .scene import evidence_text


def coverage(database, index):
    counts = {kind: Counter() for kind in ("event", "scene")}
    with Search(database, index) as search:
        for row in search.reader.store.conn.execute("SELECT id,kind FROM documents WHERE kind IN ('event','scene') AND lifecycle!='deleted'"):
            obj = search.reader.read(row["id"], with_evidence=False)
            if not obj["readable"]:
                continue
            count = counts[row["kind"]]
            count["readable"] += 1
            count["surfaceable"] += obj["surface_state"]["can_surface"]
            cached = search.conn.execute("SELECT d.stamp,v.id FROM documents d LEFT JOIN vectors v ON d.id=v.id WHERE d.id=?", (row["id"],)).fetchone()
            valid = bool(cached and cached["id"] and cached["stamp"] == content_stamp(obj["document"]))
            count["covered"] += valid
            count["missing"] += not valid
            count["surfaceable_covered"] += valid and obj["surface_state"]["can_surface"]
    return {kind: dict(count) for kind, count in counts.items()}


def fill_vectors(settings, *, batch_size=16, limit=None, client=None, progress=None, document_ids=None):
    """Reuse current vectors, commit completed batches, and reject changed bodies.

    Canonical access is read-only. A failed provider request leaves earlier
    batches available; rerunning requests only the remaining missing vectors.
    """
    if not 1 <= batch_size <= 64 or (limit is not None and limit < 1):
        raise ValueError("batch_size must be 1..64 and limit must be positive")
    client = client or EmbeddingClient(settings.database, settings.index, **settings.embedding)
    if document_ids is None:
        with Search(settings.database, settings.index) as search:
            document_ids = [row[0] for row in search.reader.store.conn.execute(
                "SELECT id FROM documents WHERE kind IN ('event','scene')")]
    document_ids = set(document_ids)
    refresh_index(settings.database, settings.index, document_ids)
    pending, skipped = [], Counter()
    with Search(settings.database, settings.index) as search:
        stored = dict(search.conn.execute("SELECT key,value FROM settings WHERE key LIKE 'embedding_%'"))
        profile, dimension = json.loads(stored["embedding_profile"]), json.loads(stored["embedding_dimension"])
        if client.profile != profile or client.dimension != dimension:
            raise ValueError("Document provider must match the index embedding profile and dimension")
        rows = (row for key in sorted(document_ids) for row in search.conn.execute(
            "SELECT d.* FROM documents d LEFT JOIN vectors v ON d.id=v.id WHERE d.id=? AND v.id IS NULL AND d.kind IN ('event','scene')",(key,)))
        for row in rows:
            obj = search.reader.read(row["id"], with_evidence=False)
            if not obj["readable"]:
                continue
            document = obj["document"]
            body = evidence_text(document) if row["kind"] == "scene" else document["body_md"]
            if not body.strip():
                skipped["empty_body"] += 1
                continue
            # Do not label truncated whole-body inputs as complete coverage.
            prefix = f"Instruct: {profile['document_instruction']}\nDocument: " if profile["document_instruction"] else ""
            if len(prefix) + len(body) > profile["max_chars"]:
                skipped["requires_passage_index"] += 1
                continue
            if getattr(client, 'window', None) and not client.window.fits(prefix + body):
                skipped['requires_token_passages'] += 1
                continue
            pending.append({"id": row["id"], "kind": row["kind"], "stamp": content_stamp(document), "body": body})
    if limit is not None:
        pending = pending[:limit]
    result = {"planned": len(pending), "embedded": 0, "requests": 0, "skipped": dict(skipped), "canonical_writes": 0}
    if progress:
        progress(dict(result))
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        vectors = client.documents([item["body"] for item in batch])
        if len(vectors) != len(batch):
            raise ValueError("Provider returned an unexpected number of document vectors")
        vectors = [unit_vector(vector, dimension) for vector in vectors]
        result["requests"] += 1
        conn = sqlite3.connect(Path(settings.index).resolve().as_uri() + "?mode=rw", uri=True)
        try:
            with conn, Reader(settings.database) as reader:
                conn.execute("BEGIN IMMEDIATE")
                current_profile = dict(conn.execute("SELECT key,value FROM settings WHERE key LIKE 'embedding_%'"))
                if current_profile != stored:
                    raise ValueError("Index embedding configuration changed during backfill")
                for item, vector in zip(batch, vectors):
                    obj = reader.read(item["id"], with_evidence=False)
                    indexed = conn.execute("SELECT stamp FROM documents WHERE id=?", (item["id"],)).fetchone()
                    if not obj["readable"] or content_stamp(obj["document"]) != item["stamp"] or not indexed or indexed[0] != item["stamp"]:
                        skipped["changed_during_embedding"] += 1
                        continue
                    cursor = conn.execute("INSERT OR IGNORE INTO vectors VALUES (?,?,?)", (item["id"], encode(vector), dimension))
                    result["embedded"] += cursor.rowcount
        finally:
            conn.close()
        result["skipped"] = dict(skipped)
        if progress:
            progress(dict(result))
    return {**result, "coverage": coverage(settings.database, settings.index)}

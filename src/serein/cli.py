import argparse
import json
from pathlib import Path
from dataclasses import replace

from serein.core import Store
from serein.ingest.legacy_scene import import_snapshot
from serein.ingest.scene_snapshot import export_snapshot, write_snapshot
from serein.ingest.narrative_snapshot import export_narratives
from serein.ingest.legacy_narrative import import_narratives
from serein.ingest.event_snapshot import export_events
from serein.ingest.legacy_event import import_events
from serein.ingest.diary_snapshot import export_diaries
from serein.ingest.legacy_diary import import_diaries
from serein.ingest.archive_snapshot import export_archives
from serein.ingest.legacy_archive import import_archives
from serein.recall.index import build_index
from serein.config import Settings, load_settings
from serein.application import Application


def main():
    parser = argparse.ArgumentParser(description="Serein local storage and offline memory import")
    parser.add_argument("--config", type=Path, help="Explicit deployment TOML for read/materials/search/capabilities")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser('index-status', help='Read durable indexing safety status without provider calls')
    recovery = sub.add_parser('resume-index', help='Authorize a new finite indexing budget')
    recovery.add_argument('--recovery-token', required=True)
    recovery.add_argument('--confirm', choices=['RESUME_INDEX_EMBEDDING'], required=True)
    sub.add_parser('setup', help='Initialize an empty standalone deployment from explicit --config')
    semantic = sub.add_parser('prepare-routes', help='Configure the index profile and embed authored route examples')
    semantic.add_argument('--profile',type=Path,required=True)
    semantic.add_argument('--examples',type=Path,required=True)
    chat = sub.add_parser('import-chat', help='Import full original messages into the same continuing Event pipeline')
    chat.add_argument('input',type=Path)
    initialize = sub.add_parser("init")
    initialize.add_argument("database", type=Path)
    load = sub.add_parser("import-scenes")
    load.add_argument("snapshot", type=Path)
    load.add_argument("database", type=Path)
    load.add_argument("--report", type=Path, required=True)
    export = sub.add_parser("export-scenes")
    export.add_argument("--buckets", type=Path, required=True)
    export.add_argument("--state", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    narrative_export = sub.add_parser("export-narratives")
    narrative_export.add_argument("--state", type=Path, required=True)
    narrative_export.add_argument("--output", type=Path, required=True)
    narrative_load = sub.add_parser("import-narratives")
    narrative_load.add_argument("snapshot", type=Path)
    narrative_load.add_argument("database", type=Path)
    narrative_load.add_argument("--report", type=Path, required=True)
    event_export = sub.add_parser("export-events")
    event_export.add_argument("--database", type=Path, required=True)
    event_export.add_argument("--output", type=Path, required=True)
    event_load = sub.add_parser("import-events")
    event_load.add_argument("snapshot", type=Path)
    event_load.add_argument("database", type=Path)
    event_load.add_argument("--report", type=Path, required=True)
    diary_export = sub.add_parser("export-diaries")
    diary_export.add_argument("--state", type=Path, required=True)
    diary_export.add_argument("--output", type=Path, required=True)
    diary_load = sub.add_parser("import-diaries")
    diary_load.add_argument("snapshot", type=Path)
    diary_load.add_argument("database", type=Path)
    diary_load.add_argument("--report", type=Path, required=True)
    read = sub.add_parser("read", help="Explicitly read one object from an existing database")
    read.add_argument("database", type=Path, nargs="?")
    read.add_argument("identifier")
    read.add_argument("--kind", choices=["scene", "event", "narrative", "diary", "darkroom", "upload", "shadow", "dream"])
    read.add_argument("--revision", type=int)
    read.add_argument("--without-evidence", action="store_true")
    read.add_argument("--include-blob", action="store_true")
    read.add_argument("--output", type=Path)
    materials = sub.add_parser("materials", help="Read a Narrative's declared materials and reference states")
    materials.add_argument("database", type=Path, nargs="?")
    materials.add_argument("identifier")
    materials.add_argument("--revision", type=int)
    materials.add_argument("--include-mentions", action="store_true")
    materials.add_argument("--with-evidence", action="store_true")
    materials.add_argument("--offset", type=int, default=0)
    materials.add_argument("--limit", type=int, default=20)
    materials.add_argument("--output", type=Path)
    archive_export = sub.add_parser("export-archives")
    archive_export.add_argument("--state", type=Path, required=True)
    archive_export.add_argument("--output", type=Path, required=True)
    archive_load = sub.add_parser("import-archives")
    archive_load.add_argument("snapshot", type=Path)
    archive_load.add_argument("database", type=Path)
    archive_load.add_argument("--report", type=Path, required=True)
    indexing = sub.add_parser("build-index", help="Build a separate disposable search index")
    indexing.add_argument("database", type=Path)
    indexing.add_argument("index", type=Path)
    indexing.add_argument("--event-cache", type=Path)
    search = sub.add_parser("search", help="Search current canonical objects through a local index")
    search.add_argument("database", type=Path, nargs="?")
    search.add_argument("index", type=Path, nargs="?")
    search.add_argument("query")
    search.add_argument("--kind", choices=["scene", "event", "narrative"])
    search.add_argument("--mode", choices=["surface", "lookup"], default="surface")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--with-evidence", action="store_true")
    search.add_argument("--query-embedding", type=Path)
    search.add_argument("--min-cosine", type=float)
    search.add_argument("--output", type=Path)
    sub.add_parser("capabilities", help="List assembled tools/hooks/jobs without executing them")
    sub.add_parser("mcp", help="Run the local stdio MCP server using an explicit profile")
    sub.add_parser("mcp-live", help="Run the private Germany tool catalog over the live database")
    http = sub.add_parser('http', help='Run the authenticated read-only HTTP host')
    http.add_argument('--host', default='127.0.0.1')
    http.add_argument('--port', type=int, default=8011)
    http.add_argument('--token-env', default='SEREIN_HTTP_TOKEN')
    http.add_argument('--live', action='store_true', help='Enable writable production client adapters')
    sub.add_parser("vector-coverage", help="Report current Event/Scene whole-body vector coverage")
    filling = sub.add_parser("fill-vectors", help="Fill missing Event/Scene vectors using the configured provider")
    filling.add_argument("--batch-size", type=int, default=16)
    filling.add_argument("--limit", type=int)
    sub.add_parser('prepare-passages', help='Prepare exact passage slices without calling a model')
    sub.add_parser('passage-coverage', help='Report prepared passage vector coverage')
    passage_fill=sub.add_parser('fill-passages', help='Generate missing passage vectors; does not enable passage recall')
    passage_fill.add_argument('--batch-size',type=int,default=16)
    sub.add_parser('rebuild-entities', help='Recheck entity vocabulary against current bound original snapshots')
    args = parser.parse_args()
    configured_commands = {"index-status", "resume-index", "import-chat", "prepare-routes", "setup", "read", "materials", "search", "capabilities", "mcp", "mcp-live", "http", "vector-coverage", "fill-vectors", 'prepare-passages','passage-coverage','fill-passages','rebuild-entities'}
    if args.config and args.command not in configured_commands:
        parser.error("--config is for read/materials/search/capabilities; imports and writes require explicit paths")
    if args.command in configured_commands:
        if args.config:
            settings = load_settings(args.config)
            if getattr(args, "database", None) is not None:
                settings = replace(settings, database=args.database)
            if getattr(args, "index", None) is not None:
                settings = replace(settings, index=args.index)
        else:
            if not getattr(args, "database", None):
                parser.error("Provide a database path or --config")
            settings = Settings(args.database, getattr(args, "index", None))
        if args.command in ('index-status', 'resume-index'):
            from .recall.index_safety import index_status, resume_index
            if args.command == 'resume-index' and not settings.writable:
                raise ValueError('This deployment is read-only')
            result = index_status(settings.database) if args.command == 'index-status' else resume_index(settings.database, args.recovery_token)
            print(json.dumps(result))
            return
        if args.command == 'setup':
            from .bootstrap import initialize
            print(json.dumps(initialize(settings)))
            return
        if args.command == 'prepare-routes':
            from .semantic_setup import prepare
            print(json.dumps(prepare(settings,args.profile,args.examples)))
            return
        if args.command == 'import-chat':
            if not settings.writable:
                raise ValueError('Chat import requires writable storage')
            from .compat.raw_archive import raw_archive
            from .compat.germany.raw_ingest import _raw_ingest_events_from_body
            body=json.loads(args.input.read_text('utf-8'))
            events=_raw_ingest_events_from_body(body)
            if not events:
                raise ValueError('Import requires nonempty original events')
            archive=raw_archive(settings)
            result={'ok':True,'inserted':0,'duplicate':0,'rejected':0,'items':[]}
            for start in range(0,len(events),archive.max_ingest_batch):
                page=archive.ingest(events[start:start+archive.max_ingest_batch],source=body.get('source','raw'))
                for key in ('inserted','duplicate','rejected'):
                    result[key]+=page[key]
                result['items'].extend(page['items'])
            print(json.dumps(result))
            return
        app = Application(settings)
        if args.command == "http":
            import os
            import uvicorn
            from serein.api.http import create_app
            uvicorn.run(create_app(settings, token=os.environ.get(args.token_env, ''), live=args.live),
                        host=args.host, port=args.port, access_log=False)
            return
        if args.command in ("mcp", "mcp-live"):
            from serein.api.mcp import create_server
            create_server(app,private=args.command=='mcp-live').run(transport="stdio")
            return
        if args.command == "capabilities":
            print(json.dumps(app.capabilities(), ensure_ascii=False))
            return
        if args.command in {"vector-coverage", "fill-vectors"}:
            from serein.recall.vectors import coverage, fill_vectors
            result = coverage(settings.database, settings.index) if args.command == "vector-coverage" else fill_vectors(
                settings, batch_size=args.batch_size, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False))
            return
        if args.command in {'prepare-passages','passage-coverage','fill-passages','rebuild-entities'}:
            from serein.recall.passages import prepare_passages,passage_coverage,fill_passages
            from serein.recall.entities import rebuild_entities
            functions={'prepare-passages':prepare_passages,'passage-coverage':passage_coverage,'fill-passages':fill_passages,'rebuild-entities':rebuild_entities}
            options={'batch_size':args.batch_size} if args.command=='fill-passages' else {}
            print(json.dumps(functions[args.command](settings,**options),ensure_ascii=False))
            return
    if args.command == "build-index":
        print(json.dumps(build_index(args.database, args.index, event_cache=args.event_cache), ensure_ascii=False))
        return
    if args.command == "search":
        embedding = json.loads(args.query_embedding.read_text(encoding="utf-8")) if args.query_embedding else None
        result = app.services.search(args.query, kind=args.kind, mode=args.mode, limit=args.limit,
                                     with_evidence=args.with_evidence, query_embedding=embedding, min_cosine=args.min_cosine)
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as file:
                file.write(output)
            print(json.dumps({"output": str(args.output), "status": result["status"], "hits": len(result["items"])}))
        else:
            print(output, end="")
        return
    if args.command == "export-archives":
        snapshot = export_archives(args.state)
        write_snapshot(snapshot, args.output)
        print(json.dumps({"output": str(args.output), "shadows": len(snapshot["window_shadows"]), "files": len(snapshot["files"])}))
        return
    if args.command in {"read", "materials"}:
        if args.command == "read":
            result = app.services.read(args.identifier, kind=args.kind, revision=args.revision,
                                       with_evidence=not args.without_evidence, include_blob=args.include_blob)
        else:
            result = app.services.materials(args.identifier, revision=args.revision,
                                            include_mentions=args.include_mentions, with_evidence=args.with_evidence,
                                            offset=args.offset, limit=args.limit)
        output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as file:
                file.write(output)
            print(json.dumps({"output": str(args.output), "status": result["status"]}))
        else:
            print(output, end="")
        return
    if args.command == "export-diaries":
        snapshot = export_diaries(args.state)
        write_snapshot(snapshot, args.output)
        print(json.dumps({"output": str(args.output), "entries": len(snapshot["diaries"])}))
        return
    if args.command == "export-events":
        snapshot = export_events(args.database)
        write_snapshot(snapshot, args.output)
        print(json.dumps({"output": str(args.output), "events": len(snapshot["events"])}))
        return
    if args.command == "export-narratives":
        snapshot = export_narratives(args.state)
        write_snapshot(snapshot, args.output)
        print(json.dumps({"output": str(args.output), "files": len(snapshot["files"])}))
        return
    if args.command == "export-scenes":
        snapshot = export_snapshot(args.buckets, args.state)
        write_snapshot(snapshot, args.output)
        print(json.dumps({"output": str(args.output), "scenes": len(snapshot["scenes"]),
                          "excluded": len(snapshot["excluded_documents"])}, ensure_ascii=False))
        return
    with Store(args.database) as store:
        if args.command == "import-archives":
            report = import_archives(store, json.loads(args.snapshot.read_text(encoding="utf-8")))
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in report.items() if k not in {"rows", "references"}}, ensure_ascii=False))
            return
        if args.command == "import-diaries":
            report = import_diaries(store, json.loads(args.snapshot.read_text(encoding="utf-8")))
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in report.items() if k not in
                              {"rows", "material_checks", "owner_checks", "legacy_checks", "source_ledger_checks"}}, ensure_ascii=False))
            return
        if args.command == "import-events":
            report = import_events(store, json.loads(args.snapshot.read_text(encoding="utf-8")))
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in report.items() if k not in
                              {"rows", "material_checks", "edge_checks", "receipt_checks", "legacy_predecessor_checks"}}, ensure_ascii=False))
            return
        if args.command == "import-narratives":
            report = import_narratives(store, json.loads(args.snapshot.read_text(encoding="utf-8")))
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in report.items() if k not in
                              {"rows", "material_checks", "local_file_references", "parent_checks"}}, ensure_ascii=False))
            return
        if args.command == "import-scenes":
            report = import_snapshot(store, json.loads(args.snapshot.read_text(encoding="utf-8")))
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            summary = {key: value for key, value in report.items()
                       if key not in {"rows", "evidence_checks", "relations", "excluded_documents"}}
            summary["relations"] = {key: value["count"] for key, value in report["relations"].items()}
            summary["excluded_documents"] = len(report["excluded_documents"])
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print(f"Initialized {args.database}")


if __name__ == "__main__":
    main()

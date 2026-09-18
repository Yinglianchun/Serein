"""Germany Narrative Scout orchestration with explicit source/store adapters."""
from __future__ import annotations
import hashlib
import json as _json_lib
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
from ...recall.germany.utils import strip_wikilinks
from ...core.store import Store
from ...core.reader import Reader
from .narrative_revision_scout import build_keyword_corridors, propose_new_roll_candidates
logger=logging.getLogger(__name__)

def _bool_value(value, default=False):
    if isinstance(value,bool):return value
    if value is None:return default
    return str(value).strip().lower() in ('1','true','yes','on')

def _int_between(value,default,low=1,high=10):
    try:return max(low,min(high,int(value)))
    except (TypeError,ValueError):return default

def _is_canonical_scene_bucket(bucket):
    meta=bucket.get('metadata',{})
    return meta.get('object_kind')=='scene' or meta.get('memory_value_source')=='authored_scene'

class NarrativeScout:

    def _event_is_covered_by_scene(self, event_id: str) -> bool:
        with Store(self.settings.database, read_only=True) as store:
            return 'covered_by_scene' in store.surface_state(event_id)['reasons']

    def _narrative_revision_scan_settings(self, config_arg: dict | None=None) -> dict[str, Any]:
        cfg_source = config_arg if isinstance(config_arg, dict) else self.config
        roll_cfg = cfg_source.get('narrative_rolls', {})
        if not isinstance(roll_cfg, dict):
            roll_cfg = {}
        timezone_name = str(roll_cfg.get('revision_scan_timezone') or 'Asia/Shanghai').strip()
        try:
            scan_timezone = ZoneInfo(timezone_name)
        except Exception:
            scan_timezone = ZoneInfo('Asia/Shanghai')
        return {'enabled': _bool_value(roll_cfg.get('revision_scan_enabled'), True), 'hour': _int_between(roll_cfg.get('revision_scan_hour'), 4, 0, 23), 'minute': _int_between(roll_cfg.get('revision_scan_minute'), 0, 0, 59), 'timezone': scan_timezone, 'check_interval_seconds': _int_between(roll_cfg.get('revision_scan_check_interval_minutes'), 15, 1, 1440) * 60, 'new_roll_scout_seed_limit': _int_between(roll_cfg.get('new_roll_scout_seed_limit'), 24, 2, 80), 'new_roll_scout_keywords_per_seed': _int_between(roll_cfg.get('new_roll_scout_keywords_per_seed'), 8, 2, 16), 'new_roll_scout_candidates_per_seed': _int_between(roll_cfg.get('new_roll_scout_candidates_per_seed'), 12, 2, 24)}

    def _narrative_timestamp(self, value: Any, scan_timezone: ZoneInfo) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=scan_timezone)
        return parsed.astimezone(timezone.utc)

    async def _narrative_material_freshness(self, narrative: dict, scan_timezone: ZoneInfo) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        automatic_linked_at = {str(link.get('event_id') or ''): str(link.get('linked_at') or '') for link in narrative.get('automatic_event_links') or [] if str(link.get('event_id') or '')}
        for event_id in list(dict.fromkeys(narrative.get('linked_event_ids') or [])):
            event = self.events.read(str(event_id), include_sources=False)
            if not event or str(event.get('status') or '') != 'active':
                continue
            event_updated_at = str(event.get('created_at') or '')
            link_updated_at = automatic_linked_at.get(str(event_id), '')
            updated_at = max((event_updated_at, link_updated_at), key=lambda value: self._narrative_timestamp(value, scan_timezone) or datetime.min.replace(tzinfo=timezone.utc))
            if self._narrative_timestamp(updated_at, scan_timezone) is None:
                continue
            sources.append({'source_type': 'event', 'source_id': str(event_id), 'updated_at': updated_at, 'title': str(event.get('title') or event_id), 'excerpt': str(event.get('body') or ''), 'source_sha256': str(event.get('fingerprint') or '')})
        for scene_id in list(dict.fromkeys(narrative.get('linked_scene_ids') or [])):
            scene = await self.scenes.get(str(scene_id))
            if not scene:
                continue
            metadata = scene.get('metadata', {}) if isinstance(scene.get('metadata'), dict) else {}
            updated_at = str(metadata.get('updated_at') or metadata.get('created') or metadata.get('created_at') or '')
            if self._narrative_timestamp(updated_at, scan_timezone) is None:
                continue
            content = str(scene.get('content') or '')
            sources.append({'source_type': 'scene', 'source_id': str(scene_id), 'updated_at': updated_at, 'title': str(metadata.get('name') or scene_id), 'excerpt': content, 'source_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
        for source_type, ids in (('diary', narrative.get('linked_diary_ids') or []), ('darkroom', narrative.get('linked_darkroom_ids') or [])):
            for source_id in list(dict.fromkeys(ids)):
                item = self.diaries.read(diary_id=int(source_id), limit=1, include_archived=True)
                if item.get('count') != 1:
                    continue
                updated_at = str(item.get('updated_at') or item.get('created_at') or item.get('date') or '')
                if self._narrative_timestamp(updated_at, scan_timezone) is None:
                    continue
                content = str(item.get('content') or '')
                sources.append({'source_type': source_type, 'source_id': str(source_id), 'updated_at': updated_at, 'title': str(item.get('title') or f'{source_type} {source_id}'), 'excerpt': content, 'source_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
        return sources

    def _narrative_material_link_index(self) -> dict[str, dict[str, set[str]]]:
        links = {kind: {} for kind in ('event', 'scene', 'diary')}
        for roll in self.rolls._load():
            if str(roll.get('lifecycle') or 'active') != 'active':
                continue
            narrative_id = str(roll.get('narrative_id') or '').strip()
            if not narrative_id:
                continue
            for event_id in roll.get('linked_event_ids') or []:
                safe_id = str(event_id or '').strip()
                if safe_id:
                    links['event'].setdefault(safe_id, set()).add(narrative_id)
            arc_key = str(roll.get('arc_key') or '').strip()
            if arc_key:
                for link in self.events.arc_event_links(arc_key):
                    safe_id = str(link.get('event_id') or '').strip()
                    if safe_id:
                        links['event'].setdefault(safe_id, set()).add(narrative_id)
            for kind in ('scene', 'diary'):
                for source_id in roll.get(f'linked_{kind}_ids') or []:
                    safe_id = str(source_id or '').strip()
                    if safe_id:
                        links[kind].setdefault(safe_id, set()).add(narrative_id)
        return links

    async def _active_narrative_material_inventory(self, *, exclude_covered_events: bool=False) -> list[dict[str, Any]]:
        """Return every active Event, canonical Scene and readable Diary for Arc routing."""
        links = self._narrative_material_link_index()
        materials: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.events.list(item_type='event', status='active', limit=500, offset=offset, include_sources=False)
            items = page.get('items') or []
            for event in items:
                event_id = str(event.get('item_id') or '').strip()
                if not event_id or (exclude_covered_events and self._event_is_covered_by_scene(event_id)):
                    continue
                bound_ids = sorted(links['event'].get(event_id, set()))
                materials.append({'source_type': 'event', 'source_id': event_id, 'date': str(event.get('local_date') or ''), 'title': str(event.get('title') or ''), 'summary': str(event.get('body') or ''), 'source_excerpt': '', 'search_text': '\n'.join((str(event.get('title') or ''), str(event.get('body') or ''))), 'updated_at': str(event.get('created_at') or ''), 'fingerprint': str(event.get('fingerprint') or ''), 'bound_narrative_ids': bound_ids, 'is_unbound': not bound_ids})
            offset += len(items)
            if not items or offset >= int(page.get('count') or 0):
                break
        for scene in await self.scenes.list_all(include_archive=False):
            if not _is_canonical_scene_bucket(scene):
                continue
            meta = scene.get('metadata', {}) if isinstance(scene.get('metadata'), dict) else {}
            if meta.get('active') is False or bool(meta.get('deprecated')):
                continue
            if str(meta.get('scene_status') or 'active').strip().lower() not in {'', 'active'}:
                continue
            scene_id = str(scene.get('id') or '').strip()
            content = strip_wikilinks(str(scene.get('content') or '')).strip()
            if not scene_id or not content:
                continue
            title = str(meta.get('name') or meta.get('title') or scene_id)
            bound_ids = sorted(links['scene'].get(scene_id, set()))
            materials.append({'source_type': 'scene', 'source_id': scene_id, 'date': str(meta.get('date') or meta.get('event_date') or meta.get('created') or ''), 'title': title, 'summary': content[:1200], 'source_excerpt': content[:1800], 'search_text': '\n'.join((title, content)), 'updated_at': str(meta.get('updated_at') or meta.get('created') or ''), 'fingerprint': hashlib.sha256(content.encode('utf-8')).hexdigest(), 'bound_narrative_ids': bound_ids, 'is_unbound': not bound_ids})
        with Reader(self.settings.database) as reader:
            for key, in reader.store.conn.execute("SELECT id FROM diary_entries WHERE kind='diary'").fetchall():
                result = reader.read(str(key), kind='diary', with_evidence=False)
                if not result['readable']:
                    continue
                doc = result['document']
                metadata = doc.get('metadata') or {}
                content = str(doc.get('body_md') or '')
                bound_ids = sorted(links['diary'].get(str(key), set()))
                materials.append({'source_type':'diary','source_id':str(key),
                    'date':str(metadata.get('date') or ''),'title':str(doc.get('title') or ''),
                    'summary':content[:1200],'source_excerpt':content[:1800],
                    'search_text':'\n'.join((str(doc.get('title') or ''),content)),
                    'updated_at':str(doc.get('updated_at') or metadata.get('updated_at') or metadata.get('date') or ''),
                    'fingerprint':hashlib.sha256(content.encode()).hexdigest(),
                    'bound_narrative_ids':bound_ids,'is_unbound':not bound_ids})
        return materials

    def _narrative_seed_sort_key(self, item: dict[str, Any]) -> tuple[str, str, str]:
        return (str(item.get('updated_at') or item.get('date') or ''), str(item.get('source_type') or ''), str(item.get('source_id') or ''))

    def _hydrate_event_scout_material(self, item: dict[str, Any]) -> dict[str, Any]:
        if str(item.get('source_type') or '') != 'event':
            return dict(item)
        event_id = str(item.get('source_id') or '')
        event = self.events.read(event_id, include_sources=True)
        if not event or str(event.get('status') or '') != 'active':
            raise RuntimeError(f'narrative_scout_event_drift:{event_id}')
        excerpts = []
        for ref in event.get('source_refs') or []:
            content = str(ref.get('content') or '').strip()
            expected_hash = str(ref.get('content_sha256') or '').strip().lower()
            if not content or hashlib.sha256(content.encode('utf-8')).hexdigest() != expected_hash:
                raise RuntimeError(f'narrative_scout_event_source_drift:{event_id}')
            excerpts.append(content)
        hydrated = dict(item)
        hydrated['source_excerpt'] = '\n'.join(excerpts)[:2400]
        hydrated['fingerprint'] = str(event.get('fingerprint') or '')
        return hydrated

    def _hydrate_scout_corridors(self, corridors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cache: dict[str, dict[str, Any]] = {}

        def hydrate(item: dict[str, Any]) -> dict[str, Any]:
            key = f"{item.get('source_type')}:{item.get('source_id')}"
            if key not in cache:
                cache[key] = self._hydrate_event_scout_material(item)
            return dict(cache[key])
        hydrated = []
        for corridor in corridors:
            hydrated.append({**corridor, 'seed': hydrate(corridor['seed']), 'candidates': [hydrate(item) for item in corridor.get('candidates') or []]})
        return hydrated

    def _hydrate_scout_seeds(self, seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._hydrate_event_scout_material(item) for item in seeds]

    async def _scan_narrative_revision_inbox(self, *, include_external: bool=True, force_external: bool=False) -> dict[str, Any]:
        """Route new material into Arcs and create review hints; never invoke Narrative Writer."""
        settings = self._narrative_revision_scan_settings(self.config)
        scan_timezone = settings['timezone']
        from ...deployment import read_settings, task_model
        nightly_enabled = bool(read_settings(self.settings.database)['features'].get('narrative_nightly_organize'))
        configured_model = task_model(self.settings.database, 'narrative_scout')
        scout_model = str((configured_model or {}).get('model') or '')
        scout_base_url = str((configured_model or {}).get('base_url') or '')
        scout_status = 'disabled'
        inventory = (await self._active_narrative_material_inventory(exclude_covered_events=True)
                     if include_external and nightly_enabled else [])
        seeds = sorted((item for item in inventory if item.get('is_unbound')),
                       key=self._narrative_seed_sort_key, reverse=True)[:int(settings['new_roll_scout_seed_limit'])]
        fingerprint = hashlib.sha256(_json_lib.dumps([
            (item['source_type'], item['source_id'], item.get('updated_at',''), item.get('fingerprint',''),
             tuple(item.get('bound_narrative_ids') or []))
            for item in sorted(inventory, key=lambda row:(row['source_type'],row['source_id']))
        ], ensure_ascii=False, separators=(',',':')).encode()).hexdigest()
        previous_scan = self.inbox.scan_metadata()
        arc_changes = []
        if include_external and nightly_enabled:
            if not seeds:
                scout_status = 'no_materials'
            elif fingerprint == previous_scan.get('external_input_sha256') and not force_external:
                scout_status = 'unchanged'
            elif not configured_model:
                scout_status = 'unavailable'
            else:
                client = None
                try:
                    from ...model_runtime import TaskClient
                    client = TaskClient(self.settings.database, 'narrative_scout')
                    hydrated = self._hydrate_scout_seeds(seeds)
                    hydrated_by_key = {f"{item['source_type']}:{item['source_id']}":item for item in hydrated}
                    search_inventory = [hydrated_by_key.get(f"{item['source_type']}:{item['source_id']}",item) for item in inventory]
                    corridors = build_keyword_corridors(search_inventory, list(hydrated_by_key),
                        max_keywords=int(settings['new_roll_scout_keywords_per_seed']),
                        max_candidates_per_seed=int(settings['new_roll_scout_candidates_per_seed']))
                    corridors = self._hydrate_scout_corridors(corridors)
                    existing_rolls = [{key:roll.get(key) for key in
                        ('narrative_id','title','query_cues','current_status_cue')}
                        for roll in self.rolls._load()
                        if roll.get('integrity_status') == 'ok' and roll.get('lifecycle') == 'active']
                    candidates = await propose_new_roll_candidates(client=client, model=scout_model,
                        corridors=corridors, role_rules=self.role_rules(),
                        completion_options={'temperature':0},
                        existing_candidates=[], existing_rolls=existing_rolls)
                    arc_changes = self.apply_arc_candidates(candidates, model=scout_model)
                    scout_status = 'ok' if corridors else 'no_keyword_matches'
                except Exception as exc:
                    scout_status = 'error'
                    logger.warning('Automatic Arc scout failed / 自动 Arc 整理失败: %s', exc)
                finally:
                    if client is not None:
                        await client.close()
        stale_created: list[dict[str, Any]] = []
        stale_narrative_ids: set[str] = set()
        checked_rolls = 0
        for summary in self.rolls.revision_targets():
            narrative_id = str(summary.get('narrative_id') or '')
            narrative = await self.read_narrative(narrative_id)
            if narrative.get('status') != 'ok':
                continue
            published = self._narrative_timestamp(narrative.get('published_at'), scan_timezone)
            if published is None:
                continue
            checked_rolls += 1
            sources = await self._narrative_material_freshness(narrative, scan_timezone)
            if not sources:
                continue
            latest = max(sources, key=lambda source: self._narrative_timestamp(source.get('updated_at'), scan_timezone) or datetime.min.replace(tzinfo=timezone.utc))
            latest_time = self._narrative_timestamp(latest.get('updated_at'), scan_timezone)
            if latest_time and latest_time > published:
                stale_narrative_ids.add(narrative_id)
                stale_created.extend(self.inbox.consider_stale_roll(narrative, latest_material=latest, material_count=len(sources)))
        stale_hints_removed = self.inbox.reconcile_stale_rolls(stale_narrative_ids)
        recorded = fingerprint if scout_status in {'ok','unchanged','no_materials','no_keyword_matches'} else str(previous_scan.get('external_input_sha256') or '')
        result = {'status':'ok','checked_rolls':checked_rolls,
            'nightly_arc_organize_enabled':nightly_enabled,
            'stale_roll_hints_created':len(stale_created),'stale_roll_hints_removed':len(stale_hints_removed),
            'active_materials_searched':len(inventory),'unbound_material_seeds_checked':len(seeds),
            'unbound_events_checked':sum(item['source_type']=='event' for item in seeds),
            'unbound_scenes_checked':sum(item['source_type']=='scene' for item in seeds),
            'unbound_diaries_checked':sum(item['source_type']=='diary' for item in seeds),
            'existing_arcs_updated':sum(item['type']=='existing_arc_materials' for item in arc_changes),
            'new_collecting_arcs_created':sum(item['type']=='new_collecting_arc' for item in arc_changes),
            'external_scout_status':scout_status,
            'external_model':scout_model if scout_status not in {'disabled','unavailable'} else '',
            'external_base_url':scout_base_url if scout_status not in {'disabled','unavailable'} else '',
            'external_search_mode':'new_materials_to_existing_or_collecting_arc',
            'external_input_sha256':recorded,
            'writes_performed':[{'type':'narrative_revision_hint','proposal_id':item.get('proposal_id'),'proposal_kind':item.get('proposal_kind')} for item in stale_created]
                + [{'type':'narrative_revision_hint_removed','proposal_id':proposal_id,'proposal_kind':'existing_roll_update'} for proposal_id in stale_hints_removed],
            'narrative_writes_performed':arc_changes}
        self.inbox.record_scan(result)
        return result

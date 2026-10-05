"""Manual-only fallback contracts; synthetic databases, no providers or credentials.

Also runnable without pytest:
    PYTHONPATH=src python -m unittest discover -s tests -p test_manual_recall_fallback.py
"""
import asyncio
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from serein.application import Application, Services
from serein.config import Settings
from serein.core.store import Store, encode
from serein.recall.entities import ensure_tables, evidence_stamp
from serein.recall.index import build_index, content_stamp
from serein.recall.manual import fallback_lookup
from serein.recall.service import Recall


class ManualFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root / 'memory.db', root / 'index.db')
        with Store(self.settings.database) as store:
            store.create('scene_allowed', 'scene', 'Synthetic harbor', 'lighthouse promise')
        build_index(self.settings.database, self.settings.index)

    def rebuild(self):
        self.settings.index.unlink()
        build_index(self.settings.database, self.settings.index)

    def tool(self):
        from serein.api.mcp import create_server
        server = create_server(Application(self.settings))
        return server, server._tool_manager.get_tool('recall_memory').fn

    def retry(self, query='lighthouse', **options):
        return fallback_lookup(Recall(self.settings), query,
            {'status':'no_match', 'method':'semantic'},
            {'mode':'surface', 'method':'semantic', 'limit':5, **options})

    def test_empty_primary_retries_once_and_labels_lookup(self):
        result = Services(self.settings).recall('lighthouse', method=None, manual_fallback=True)
        self.assertEqual(result['selected_refs'], ['scene:scene_allowed'])
        self.assertEqual(result['manual_fallback']['visibility'], 'surface_eligible')
        self.assertEqual(result['manual_fallback']['primary_status'], 'no_match')
        self.assertFalse(result['injected'])

    def test_empty_fallback_does_not_retry(self):
        engine = Mock()
        engine.run.return_value = {'status':'no_match', 'selected_refs':[]}
        result = fallback_lookup(engine, 'missing', {'status':'no_match'}, {'limit':3})
        self.assertEqual(engine.run.call_count, 1)
        self.assertEqual(result['status'], 'no_match')
        self.assertIn('manual_fallback', result)

    def test_success_and_errors_never_retry(self):
        for primary in (
            {'status':'matched', 'selected_refs':['scene:one']},
            {'status':'no_match', 'context':'existing text'},
            {'status':'no_match', 'reranker_error':'provider_score_unavailable'},
            {'status':'no_match', 'error':'unavailable'},
            {'status':'error'}, {'status':'empty_query'},
            {'status':'use_narrative_reader'}, {'status':'use_evidence_reader'},
        ):
            with self.subTest(primary=primary):
                engine = Mock()
                self.assertIs(fallback_lookup(engine, 'q', primary, {}), primary)
                engine.run.assert_not_called()

    def test_only_verified_published_route_skip_retries(self):
        for reason, action, expected in (
            ('published_skip_route','skip',1), ('published_skip_route','recall',0),
            ('hook_deadline_before_reranker','skip',0), ('domain_excluded','skip',0),
            ('scope_required_for_deictic_intent','skip',0), ('unknown','skip',0),
        ):
            with self.subTest(reason=reason, action=action):
                engine = Mock()
                engine.run.return_value = {'status':'no_match'}
                result = fallback_lookup(engine, 'q', {'status':'skipped', 'reason':reason,
                    'routing':{'action':action}}, {})
                self.assertEqual(engine.run.call_count, expected)
                if expected:
                    self.assertEqual(result['manual_fallback']['primary_reason'], reason)

    def test_retry_preserves_query_controls(self):
        engine = Mock()
        engine.run.return_value = {'status':'no_match'}
        options = {'mode':'surface', 'method':'semantic', 'min_cosine':.7,
                   'limit':2, 'topic':'harbor', 'exclude_ids':['scene:x'],
                   'use_passages':False, 'with_evidence':True}
        fallback_lookup(engine, 'original question', {'status':'no_match'}, options)
        engine.run.assert_called_once_with('original question', **{
            **options, 'mode':'lookup', 'method':'lexical', 'min_cosine':None, 'surface_only':True})
        self.assertEqual(options['mode'], 'surface')

    def test_service_and_fallback_exceptions_are_not_hidden(self):
        for error in (ValueError('provider failed'), RuntimeError('reranker failed')):
            with self.subTest(error=error), patch.object(Recall, 'run', side_effect=error) as run:
                with self.assertRaisesRegex(type(error), 'failed'):
                    Services(self.settings).recall('q', method=None, manual_fallback=True)
                self.assertEqual(run.call_count, 1)
        engine = Mock()
        engine.run.side_effect = ValueError('lookup failed')
        with self.assertRaisesRegex(ValueError, 'lookup failed'):
            fallback_lookup(engine, 'q', {'status':'no_match'}, {})

    def test_default_mcp_and_opt_out_explicit_controls(self):
        _, tool = self.tool()
        for arguments, expected in (
            ({}, 2), ({'mode':None, 'method':None}, 2), ({'fallback':False}, 1),
            ({'mode':'surface'}, 1), ({'mode':'lookup'}, 1),
            ({'method':'lexical'}, 1), ({'method':'semantic'}, 1),
            ({'intent':'latest'}, 1), ({'intent':'progress'}, 1),
            ({'intent':'timeline'}, 1), ({'intent':'narrative'}, 1), ({'intent':'exact'}, 1),
        ):
            with self.subTest(arguments=arguments), patch.object(Recall, 'run', return_value={
                    'status':'no_match', 'method':'lexical'}) as run:
                text = tool('synthetic', **arguments)
                self.assertEqual(run.call_count, expected)
                self.assertEqual('Manual fallback:' in text, expected == 2)
                self.assertEqual(run.call_args_list[0].kwargs['mode'], arguments.get('mode') or 'surface')

    def test_mcp_schema_exposes_simple_defaults(self):
        server, _ = self.tool()
        tool = next(t for t in asyncio.run(server.list_tools()) if t.name == 'recall_memory')
        self.assertIsNone(tool.inputSchema['properties']['mode']['default'])
        self.assertIsNone(tool.inputSchema['properties']['method']['default'])
        self.assertTrue(tool.inputSchema['properties']['fallback']['default'])

    def test_mcp_primary_match_and_reranker_error_are_visible(self):
        _, tool = self.tool()
        with patch.object(Recall, 'run', return_value={'status':'matched', 'context':'primary memory',
                                                     'selected_refs':['scene:one']}) as run:
            self.assertEqual(tool('q'), 'primary memory')
            self.assertEqual(run.call_count, 1)
        with patch.object(Recall, 'run', return_value={'status':'no_match',
                'reranker_error':'provider_score_unavailable'}) as run:
            text = tool('q')
            self.assertIn('reranker_error=provider_score_unavailable', text)
            self.assertNotIn('No matching memory.', text)
            self.assertEqual(run.call_count, 1)

    def test_automatic_service_call_remains_surface_only(self):
        result = Services(self.settings).recall('lighthouse', method=None)
        self.assertEqual(result['status'], 'no_match')
        self.assertNotIn('manual_fallback', result)
        self.assertEqual(result['selected_refs'], [])

    def test_same_effective_engine_is_used(self):
        configured = replace(self.settings, embedding={'endpoint':'https://unused.invalid'})
        engine = Mock(policy=SimpleNamespace(direct_threshold=.65))
        engine.run.side_effect = [{'status':'no_match','method':'semantic'}, {'status':'no_match'}]
        with patch('serein.configured_models.effective_settings', return_value=configured) as effective, \
                patch('serein.application.Recall', return_value=engine) as factory:
            Services(self.settings).recall('q', method=None, manual_fallback=True)
        effective.assert_called_once()
        factory.assert_called_once_with(configured)
        self.assertEqual(engine.run.call_args_list[0].kwargs['method'], 'semantic')
        self.assertEqual(engine.run.call_args_list[1].kwargs['method'], 'lexical')

    def test_hidden_lifecycle_and_deleted_objects_stay_hidden(self):
        with Store(self.settings.database) as store:
            for kind in ('event','scene'):
                for state in ('archived','superseded','deleted','manual_disabled','manual_unreviewed'):
                    key = kind+'_'+state
                    store.create(key, kind, key, 'lighthouse promise',
                                 lifecycle=state if state in ('archived','superseded','deleted') else 'active')
                    if state.startswith('manual_'):
                        store.set_manual_surface(key, False if state == 'manual_disabled' else None)
        self.rebuild()
        result = self.retry(limit=1)
        self.assertEqual(result['selected_refs'], ['scene:scene_allowed'])
        # An explicit lookup still has its pre-existing broader semantics.
        explicit = Recall(self.settings).run('lighthouse', mode='lookup', limit=100)
        self.assertIn('scene:scene_archived', explicit['selected_refs'])
        self.assertNotIn('scene:scene_deleted', explicit['selected_refs'])

    def test_domain_rules_and_exclude_ids_survive_fallback(self):
        with Store(self.settings.database) as store:
            for kind in ('scene','event'):
                for domain in ('excluded','explicit'):
                    store.create(kind+'_'+domain, kind, kind+' secret', 'lighthouse promise',
                        metadata={'canonical_domain':domain, 'scene_cues':['lighthouse anchor']})
        self.rebuild()
        # Use engine policy directly: synthetic domains need no deployed config.
        engine = Recall(self.settings)
        engine.policy = replace(engine.policy, domains={'excluded':'excluded','explicit':'explicit_only'})
        result = fallback_lookup(engine, 'lighthouse', {'status':'no_match'},
            {'mode':'surface','exclude_ids':['scene:scene_allowed']})
        self.assertEqual(result['selected_refs'], [])
        result = fallback_lookup(engine, 'lighthouse anchor', {'status':'no_match'}, {'mode':'surface'})
        self.assertEqual(result['selected_refs'], ['scene:scene_explicit'])

    def test_cue_and_entity_lanes_keep_surface_visibility(self):
        with Store(self.settings.database) as store:
            for state in ('active','archived','hidden'):
                store.create('cue_'+state, 'scene', 'Cue '+state, 'unrelated body',
                             lifecycle='archived' if state == 'archived' else 'active',
                             metadata={'scene_cues':['beacon']})
                if state == 'hidden': store.set_manual_surface('cue_'+state, False)
                store.create('entity_'+state, 'event', 'Entity '+state, 'other body',
                             lifecycle='archived' if state == 'archived' else 'active')
                if state == 'hidden': store.set_manual_surface('entity_'+state, False)
        self.rebuild()
        with sqlite3.connect(self.settings.index) as conn, Store(self.settings.database, read_only=True) as store:
            ensure_tables(conn)
            for state in ('active','archived','hidden'):
                key = 'entity_'+state
                conn.execute('INSERT INTO entity_observations VALUES (?,?,?,?,?,?,?)',
                    (key, 'beacon', 'beacon', content_stamp(store.read(key)), evidence_stamp([]), encode([]), 'synthetic'))
        result = self.retry('beacon')
        self.assertEqual(set(result['selected_refs']), {'scene:cue_active','event:entity_active'})

    def test_covered_event_is_not_revived(self):
        with Store(self.settings.database) as store:
            store.create('event_covered', 'event', 'Old harbor', 'lighthouse promise')
            source = store.add_source('synthetic:1', 'Exact synthetic source')
            store.bind('event_covered', source)
            store.bind('scene_allowed', source)
        self.rebuild()
        self.assertEqual(self.retry()['selected_refs'], ['scene:scene_allowed'])

    def test_replaced_event_is_not_revived(self):
        with Store(self.settings.database) as store:
            store.create('event_old', 'event', 'Old harbor', 'lighthouse promise')
            store.create('event_new', 'event', 'New harbor', 'different detail')
            store.conn.execute("INSERT INTO event_replacements VALUES (?,?,?,?)",
                               ('event_old', 'event_new', 'synthetic', '{}'))
        self.rebuild()
        self.assertEqual(self.retry()['selected_refs'], ['scene:scene_allowed'])

    def test_configuration_failure_never_constructs_fallback_engine(self):
        with patch('serein.configured_models.effective_settings', side_effect=ValueError('Prepare selected model')), \
                patch('serein.application.Recall') as factory:
            with self.assertRaisesRegex(ValueError, 'Prepare selected model'):
                Services(self.settings).recall('q', method=None, manual_fallback=True)
        factory.assert_not_called()

    def test_domain_suppression_is_not_reported_as_route_skip(self):
        from serein.api.read_text import recall_text
        text = recall_text({'status':'no_match', 'suppressed':{'domain_excluded':1},
            'manual_fallback':{'primary_status':'no_match', 'primary_suppressed':{'domain_explicit_only':2}}},
            with_evidence=False)
        self.assertIn('lookup_suppressed: domain_excluded=1', text)
        self.assertIn('primary_suppressed: domain_explicit_only=2', text)
        self.assertNotIn('published_skip_route', text)

    def test_fallback_does_not_expand_arc_menus(self):
        with patch('serein.recall.rendering._arcs', side_effect=AssertionError('broader menu read')):
            result = self.retry()
        self.assertEqual(result['selected_refs'], ['scene:scene_allowed'])
        self.assertEqual(result['menus_included'], [])
        self.assertNotIn('[arc_materials', result['context'])

    def test_final_materialization_rechecks_state_and_revision(self):
        from serein.core.reader import Reader
        original = Reader.read
        for change in ('state','revision'):
            seen = []
            def changed(reader, identifier, **options):
                obj = original(reader, identifier, **options)
                if identifier == 'scene_allowed':
                    seen.append(identifier)
                    if len(seen) == 2:
                        if change == 'state': obj['surface_state']['can_surface'] = False
                        else: obj['document']['revision'] += 1
                return obj
            with self.subTest(change=change), patch.object(Reader, 'read', changed):
                result = self.retry()
                self.assertEqual(result['selected_refs'], [])
                self.assertEqual(result['suppressed'][change+'_changed_before_read'], 1)

    def test_surface_only_cannot_be_used_with_semantic_or_surface(self):
        for options in ({'mode':'surface'}, {'mode':'lookup','method':'semantic'}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'reserved'):
                Recall(self.settings).run('q', surface_only=True, **options)


if __name__ == '__main__':
    unittest.main()

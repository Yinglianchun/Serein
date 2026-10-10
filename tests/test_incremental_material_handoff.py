"""Synthetic persisted material handoffs across ordinary Event extensions."""

import asyncio
import copy
import json

import pytest

from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_latest as latest
from serein.extensions.pipeline_admission import apply_gate
from test_public_features import output_for, settings


def _ingest_window(settings, number, *, exchanges=1):
    rows = []
    for exchange in range(exchanges):
        for offset, role in enumerate(('user', 'assistant')):
            rows.append({
                'source_event_id': f'window-{number}-{exchange}-{role}',
                'session_id': f'window-{number}', 'role': role,
                'text': f'Synthetic cover {number}-{exchange}-{role}. Also, the kettle is on.',
                'created_at': f'2025-01-{number:02}T00:{exchange * 2 + offset:02}:00Z',
            })
    result = raw_archive(settings).ingest(rows, source='synthetic')
    return [item['id'] for item in result['items']]


def _material(source_id, use='main', *, reason=None):
    return {'source_message_id': source_id, 'use': use,
            'reason': reason or f'Synthetic material {source_id}',
            'omit_quotes': ['Also, the kettle is on.'] if use == 'mixed' else []}


def _annotate(output, rows):
    assert len(output['events']) == 1
    output['decision_review']['events'][0]['materials'] = copy.deepcopy(rows)
    return output


def _writer_materials(request):
    return json.loads(request['prompt'].split('<curator_materials_json>\n', 1)[1]
                      .split('\n</curator_materials_json>', 1)[0])


def _active_details(database):
    with Store(database, read_only=True) as store:
        return [dict(row) for row in store.conn.execute(
            'SELECT e.item_id,e.fingerprint,e.body,d.details_json '
            'FROM fact_events e JOIN pipeline_event_details d ON d.event_id=e.item_id '
            "WHERE e.status='active' ORDER BY e.item_id")]


def _assert_snapshot(row, expected_materials):
    snapshot = json.loads(row['details_json'])['material_snapshot']
    assert snapshot['schema'] == 1
    assert snapshot['event_id'] == row['item_id']
    assert snapshot['event_fingerprint'] == row['fingerprint']
    assert snapshot['event_ref']
    assert sorted(snapshot['source_materials'], key=lambda item: item['source_message_id']) == sorted(
        expected_materials, key=lambda item: item['source_message_id'])
    fingerprints = snapshot['source_fingerprints']
    assert [item['source_message_id'] for item in fingerprints] == sorted(
        item['source_message_id'] for item in expected_materials)
    assert all(isinstance(item['fingerprint'], str) and item['fingerprint'] for item in fingerprints)
    return snapshot


def test_three_windows_persist_full_snapshots_but_curator_and_writer_annotate_only_delta(settings):
    save_settings(settings.database, {'pipeline': {'material_review_enabled': True}})
    all_materials, all_source_ids, previous = [], [], None
    versions, curator_outputs, writers = [], [], []
    for number in range(1, 4):
        source_ids = _ingest_window(settings, number, exchanges=2 if number == 1 else 1)
        new_materials = [_material(source_id) for source_id in source_ids]
        if number == 1:
            new_materials[0] = _material(source_ids[0], 'mixed')
            new_materials[2] = _material(source_ids[2], 'omit')
            new_materials[3] = _material(source_ids[3], 'background')
        all_materials.extend(new_materials)
        all_source_ids.extend(source_ids)

        async def runner(role, request):
            output = output_for(role, request)
            if role == 'event_curator':
                component = request['component']
                assert component['material_contract_version'] == 1
                assert {message['id'] for message in component['messages']} == set(source_ids)
                if previous is not None:
                    base, = component['base_event_candidates']
                    assert base['event_id'] == previous['item_id']
                    assert base['fingerprint'] == previous['fingerprint']
                    assert base['material_snapshot'] == json.loads(previous['details_json'])['material_snapshot']
                    assert output['events'][0]['action'] == 'extend'
                _annotate(output, new_materials)
                curator_outputs.append(copy.deepcopy(output))
            elif role == 'event_writer':
                writers.append(copy.deepcopy(request))
                assert {message['id'] for message in request['messages']} == set(source_ids)
                assert _writer_materials(request) == new_materials
                assert set(request['event']['source_message_ids']) == set(all_source_ids)
                assert {row['source_message_id'] for row in request['event']['source_bindings']} == set(all_source_ids)
                assert sorted(request['event']['source_materials'], key=lambda row: row['source_message_id']) == all_materials
                if previous is not None:
                    assert request['writer_mode'] == 'append'
                    assert json.dumps(previous['body'], ensure_ascii=False)[1:-1] in request['prompt']
            return output

        result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
        assert result['events'] == 1 and result['processed_originals'] == len(source_ids)
        row, = _active_details(settings.database)
        _assert_snapshot(row, all_materials)
        if previous is not None:
            assert row['body'] == previous['body'] + '\n\n' + output_for('event_writer', writers[-1])['event_draft']
        versions.append(row['item_id'])
        previous = row
    assert len(set(versions)) == 3
    assert [len(output['decision_review']['events'][0]['materials']) for output in curator_outputs] == [4, 2, 2]
    with Store(settings.database, read_only=True) as store:
        refs = store.conn.execute('SELECT message_id FROM fact_event_sources WHERE item_id=?',
                                  (versions[-1],)).fetchall()
        assert len(refs) == 8
        assert len({ref['message_id'] for ref in refs}) == 8
        assert store.conn.execute("SELECT count(*) FROM fact_events WHERE status='superseded'").fetchone()[0] == 2


def test_three_image_windows_keep_delta_scope_across_transcription_cache_refreshes(settings, monkeypatch):
    from test_event_handoff import PNG

    save_settings(settings.database, {'pipeline': {'material_review_enabled': True}})
    clock_stamp = '2025-02-01T00:00:00Z'
    monkeypatch.setattr('serein.image_transcription.now', lambda: clock_stamp)
    annotation_scopes, writer_scopes, transcription_calls = [], [], []
    all_materials, old_fingerprints, cache_stamps = [], {}, []
    for number in (1, 2, 3):
        clock_stamp = f'2025-02-{number:02}T00:00:00Z'
        ingested = raw_archive(settings).ingest([
            {'source_event_id': f'image-window-{number}-{role}',
             'session_id': f'image-window-{number}', 'role': role,
             'text': f'Synthetic image continuation {number} {role}.',
             'created_at': f'2025-01-{number:02}T00:{offset:02}:00Z',
             'metadata': {'attachments': [{'kind': 'image', 'url': PNG}]}
                 if number == 1 and role == 'user' else {}}
            for offset, role in enumerate(('user', 'assistant'))], source='synthetic')
        new_ids = [row['id'] for row in ingested['items']]
        new_materials = [_material(source_id) for source_id in new_ids]
        all_materials.extend(new_materials)

        async def runner(role, request):
            if request.get('transcription_only'):
                transcription_calls.append(copy.deepcopy(request))
                return {'image_transcriptions': [{'input_image': 1,
                    'text': 'Synthetic blue cover image', 'unreadable': False}]}
            output = output_for(role, request)
            if role == 'event_curator':
                component = request['component']
                assert component['material_contract_version'] == 1
                if number > 1:
                    candidate, = latest.event_curator_model_input(component)['base_events']
                    assert candidate['extend_material_mode'] == 'new_sources_only'
                    assert {row['source_message_id'] for row in candidate['inherited_materials']} == set(
                        range(1, new_ids[0]))
                _annotate(output, new_materials)
                annotation_scopes.append([row['source_message_id'] for row in
                                          output['decision_review']['events'][0]['materials']])
            elif role == 'event_writer':
                writer_scopes.append([message['id'] for message in request['messages']])
                assert _writer_materials(request) == new_materials
                if number > 1:
                    assert request['writer_mode'] == 'append'
                    assert request['curator_image_transcriptions'] == []
            return output

        result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
        assert result['events'] == 1 and result['processed_originals'] == 2
        current, = _active_details(settings.database)
        snapshot = _assert_snapshot(current, all_materials)
        fingerprints = {row['source_message_id']: row['fingerprint']
                        for row in snapshot['source_fingerprints']}
        assert all(fingerprints[source_id] == value for source_id, value in old_fingerprints.items())
        old_fingerprints = fingerprints
        with Store(settings.database, read_only=True) as store:
            cache = json.loads(store.conn.execute('SELECT image_transcription_json FROM raw_events WHERE id=1')
                               .fetchone()[0])
            cache_stamps.append(cache['updated_at'])
            assert cache['items'][0]['text'] == 'Synthetic blue cover image'
    assert annotation_scopes == writer_scopes == [[1, 2], [3, 4], [5, 6]]
    assert cache_stamps == [f'2025-02-{number:02}T00:00:00Z' for number in (1, 2, 3)]
    assert len(transcription_calls) == 1


@pytest.mark.parametrize('damage', ['missing', 'event_id', 'event_fingerprint', 'source_fingerprint',
                                    'source_ref_content', 'source_ref_role',
                                    'source_ref_created_at', 'source_ref_hash'])
def test_untrusted_persisted_snapshot_requires_full_annotation_before_writer(settings, damage):
    save_settings(settings.database, {'pipeline': {'material_review_enabled': True}})
    first_ids = _ingest_window(settings, 1)
    first_materials = [_material(first_ids[0], 'mixed'), _material(first_ids[1], 'omit')]

    async def first(role, request):
        output = output_for(role, request)
        return _annotate(output, first_materials) if role == 'event_curator' else output

    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=first))['events'] == 1
    old, = _active_details(settings.database)
    details = json.loads(old['details_json'])
    ref_changes = {'source_ref_content': ('content', 'Corrupted historical source content.'),
                   'source_ref_role': ('role', 'assistant'),
                   'source_ref_created_at': ('created_at', '2024-12-01T00:00:00Z'),
                   'source_ref_hash': ('content_sha256', '0' * 64)}
    if damage in ref_changes:
        column, value = ref_changes[damage]
        with Store(settings.database) as store:
            raw_before = dict(store.conn.execute('SELECT * FROM raw_events WHERE id=?', (first_ids[0],)).fetchone())
            store.conn.execute(f'UPDATE fact_event_sources SET {column}=? WHERE id=('
                               'SELECT MIN(id) FROM fact_event_sources WHERE item_id=?)',
                               (value, old['item_id']))
            assert dict(store.conn.execute('SELECT * FROM raw_events WHERE id=?', (first_ids[0],)).fetchone()) == raw_before
            assert json.loads(store.conn.execute('SELECT details_json FROM pipeline_event_details WHERE event_id=?',
                                                 (old['item_id'],)).fetchone()[0]) == details
    elif damage == 'missing':
        details.pop('material_snapshot')
    elif damage == 'source_fingerprint':
        details['material_snapshot']['source_fingerprints'][0]['fingerprint'] = 'invalid-source'
    else:
        details['material_snapshot'][damage] = 'invalid-event-version'
    if damage not in ref_changes:
        with Store(settings.database) as store:
            store.conn.execute('UPDATE pipeline_event_details SET details_json=? WHERE event_id=?',
                               (json.dumps(details), old['item_id']))
    next_ids = _ingest_window(settings, 2)
    new_materials = [_material(source_id) for source_id in next_ids]
    complete = first_materials + new_materials
    accepted_full, writer_calls = [], []

    async def second(role, request):
        output = output_for(role, request)
        if role == 'event_curator':
            delta = _annotate(copy.deepcopy(output), new_materials)
            with pytest.raises(ValueError, match='exactly cover'):
                latest.normalize_event_curator_output(delta, request['component'])
            _annotate(output, complete)
            accepted_full.append(latest.normalize_event_curator_output(copy.deepcopy(output), request['component']))
            assert accepted_full[-1]['events'][0]['source_materials'] == complete
        elif role == 'event_writer':
            writer_calls.append(copy.deepcopy(request))
            assert _writer_materials(request) == new_materials
            assert {message['id'] for message in request['messages']} == set(next_ids)
        return output

    if damage in {'source_ref_content', 'source_ref_hash'}:
        # Full review reaches Writer, but the independent evidence projection
        # must still reject a corrupted historical content binding on commit.
        with pytest.raises(ValueError, match='Event evidence must include its exact original content'):
            asyncio.run(p.advance(settings.database, include_recent=True, runner=second))
        assert len(accepted_full) == len(writer_calls) == 1
        assert _active_details(settings.database) == [old]
        with Store(settings.database, read_only=True) as store:
            assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id IN (?,?)',
                                      next_ids).fetchone()[0] == 0
        return
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=second))['events'] == 1
    assert len(accepted_full) == len(writer_calls) == 1
    current, = _active_details(settings.database)
    _assert_snapshot(current, complete)


@pytest.mark.parametrize('old_use, expected_rounds', [('main', 2), ('mixed', 2), ('omit', 1), ('background', 1)])
def test_incremental_gate_matches_legacy_full_material_count(settings, old_use, expected_rounds):
    save_settings(settings.database, {'pipeline': {'material_review_enabled': True}})
    first_ids = _ingest_window(settings, 1)
    first_materials = [_material(first_ids[0], old_use), _material(first_ids[1])]

    async def first(role, request):
        output = output_for(role, request)
        return _annotate(output, first_materials) if role == 'event_curator' else output

    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=first))['events'] == 1
    save_settings(settings.database, {'pipeline': {'round_gate_enabled': True}})
    next_ids = _ingest_window(settings, 2)
    new_materials = [_material(source_id) for source_id in next_ids]
    checked = []

    async def second(role, request):
        output = output_for(role, request)
        if role == 'event_curator':
            _annotate(output, new_materials)
            output['decision_review']['events'][0]['admission'] = {'closed_by': None}
            component = request['component']
            actual = apply_gate(latest.normalize_event_curator_output(copy.deepcopy(output), component), component)
            legacy_component = copy.deepcopy(component)
            legacy_component.pop('material_contract_version')
            legacy_output = _annotate(copy.deepcopy(output), first_materials + new_materials)
            expected = apply_gate(latest.normalize_event_curator_output(legacy_output, legacy_component), legacy_component)
            assert actual['admission_receipts'] == expected['admission_receipts']
            assert actual['admission_receipts'][0]['rounds'] == expected_rounds
            assert actual['admission_receipts'][0]['minimum'] == 2
            assert actual['defer_source_message_ids'] == expected['defer_source_message_ids']
            assert actual['skip_source_message_ids'] == expected['skip_source_message_ids']
            assert [event['source_bindings'] for event in actual['events']] == [
                event['source_bindings'] for event in expected['events']]
            checked.append(True)
        return output

    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=second))
    assert checked == [True]
    assert result['events'] == (1 if expected_rounds == 2 else 0)
    assert result['pending'] == (0 if expected_rounds == 2 else 2)


def test_interrupted_incremental_writer_resume_reuses_frozen_delta_and_saves_full_snapshot(settings):
    save_settings(settings.database, {'pipeline': {'material_review_enabled': True}})
    first_ids = _ingest_window(settings, 1)
    first_materials = [_material(first_ids[0], 'mixed'), _material(first_ids[1], 'omit')]

    async def first(role, request):
        output = output_for(role, request)
        return _annotate(output, first_materials) if role == 'event_curator' else output

    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=first))['events'] == 1
    next_ids = _ingest_window(settings, 2)
    new_materials = [_material(source_id) for source_id in next_ids]
    failed_request = []

    async def interrupted(role, request):
        if role == 'event_writer':
            failed_request.append(copy.deepcopy(request))
            raise ValueError('Synthetic interruption after accepted incremental Curator')
        output = output_for(role, request)
        return _annotate(output, new_materials) if role == 'event_curator' else output

    with pytest.raises(ValueError, match='Synthetic interruption'):
        asyncio.run(p.advance(settings.database, include_recent=True, runner=interrupted))
    with Store(settings.database, read_only=True) as store:
        row = store.conn.execute("SELECT request_json,output_json FROM pipeline_jobs WHERE role LIKE 'event_curator:%' "
                                 'ORDER BY rowid DESC LIMIT 1').fetchone()
        frozen = json.loads(row['request_json'])
        output = json.loads(row['output_json'])
        assert frozen['component']['material_contract_version'] == 1
        assert output['decision_review']['events'][0]['materials'] == new_materials
    resumed_roles = []

    async def resumed(role, request):
        resumed_roles.append(role)
        assert request['event']['source_materials'] == failed_request[0]['event']['source_materials']
        assert _writer_materials(request) == new_materials
        return output_for(role, request)

    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=resumed))
    assert result['events'] == 1 and resumed_roles == ['event_writer']
    current, = _active_details(settings.database)
    _assert_snapshot(current, first_materials + new_materials)


@pytest.mark.parametrize('reverse', [False, True])
def test_reordered_multiple_extensions_keep_each_persisted_candidate_materials(settings, reverse):
    save_settings(settings.database, {'pipeline': {'material_review_enabled': True}})
    previous_by_first_source, expected_by_first_source = {}, {}
    for number in (1, 2):
        source_ids = _ingest_window(settings, number, exchanges=2)

        async def runner(role, request):
            if role != 'event_curator':
                if role == 'event_writer':
                    writer_ids = [message['id'] for message in request['messages']]
                    assert writer_ids in [source_ids[:2], source_ids[2:]]
                    assert [row['source_message_id'] for row in _writer_materials(request)] == writer_ids
                output = output_for(role, request)
                if role == 'track_router':
                    for track in output['track_updates']:
                        track['event_policy'] = 'default'
                return output
            component = request['component']
            bases = sorted(component['base_event_candidates'], key=lambda base: min(base['source_message_ids']))
            events, reviews, evidence = [], [], []
            for index, ids in enumerate((source_ids[:2], source_ids[2:])):
                prior = bases[index] if bases else None
                first_source = min(prior['source_message_ids']) if prior else ids[0]
                if prior:
                    assert prior['event_id'] == previous_by_first_source[first_source]['item_id']
                    assert prior['fingerprint'] == previous_by_first_source[first_source]['fingerprint']
                new_rows = [_material(source_id, 'mixed' if index == 0 else 'main',
                                      reason=f'Activity {index}, window {number}') for source_id in ids]
                expected_by_first_source.setdefault(first_source, []).extend(new_rows)
                events.append({'action': 'extend' if prior else 'create',
                    'base_event_ids': [prior['event_id']] if prior else [],
                    'primary_track_id': component['track_ids'][0], 'owned_unit_roots': [
                        unit['unit_root_message_id'] for unit in component['memberships']
                        if set(unit['source_message_ids']).intersection(ids)]})
                reviews.append({'event_index': index, 'reason': f'Separate activity {index}',
                                'materials': new_rows})
                evidence.append({'source_message_id': ids[0],
                    'quote': next(row['content'] for row in component['messages'] if row['id'] == ids[0])})
            if number == 2 and reverse:
                events.reverse()
                reviews.reverse()
                for index, review in enumerate(reviews):
                    review['event_index'] = index
            return {'events': events, 'skip_unit_roots': [], 'defer_unit_roots': [],
                    'decision_review': {'events': reviews, 'dispositions': [], 'boundaries': [{
                        'left_event_index': 0, 'right_event_index': 1,
                        'reason': 'Independent cover activities', 'evidence': evidence}]}}

        result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
        assert result['events'] == 2 and result['processed_originals'] == 4
        active = _active_details(settings.database)
        assert len(active) == 2
        previous_by_first_source = {}
        for row in active:
            materials = json.loads(row['details_json'])['material_snapshot']['source_materials']
            first_source = min(item['source_message_id'] for item in materials)
            _assert_snapshot(row, expected_by_first_source[first_source])
            previous_by_first_source[first_source] = row
    assert set(previous_by_first_source) == {1, 3}


@pytest.mark.parametrize('reverse', [False, True])
def test_inherited_shared_bridge_uses_preserved_side_specific_quotes_before_audit(reverse):
    from serein.extensions.pipeline_materials import snapshot_for
    from test_shared_bridge_boundary import case

    component, output = case()
    previous = latest.normalize_event_curator_output(copy.deepcopy(output), component)
    bases = []
    for index, event in enumerate(previous['events']):
        base = {'event_id': f'persisted-{index}', 'fingerprint': f'version-{index}',
                'primary_track_id': event['primary_track_id'], 'session_ids': [1],
                'source_message_ids': event['source_message_ids'], 'active': True,
                'source_activity_roles': {str(row['source_message_id']): row['activity_role']
                                          for row in event['source_bindings']}}
        base['material_snapshot'] = {**snapshot_for(event, component['context_messages']),
                                     'event_id': base['event_id'], 'event_fingerprint': base['fingerprint']}
        bases.append(base)
    messages = [{'id': source_id, 'session_id': 1, 'role': 'user' if source_id % 2 else 'assistant',
                 'created_at': f'2025-01-02T00:{source_id:02}:00Z',
                 'content': f'Synthetic continuation {source_id}.'} for source_id in range(7, 11)]
    component.update(messages=messages, material_contract_version=1, base_event_candidates=bases)
    component['context_messages'].extend(messages)
    component['memberships'].extend([
        {'unit_root_message_id': root, 'source_message_ids': [root, root + 1],
         'session_id': 1, 'track_id': track, 'routing_role': 'primary_activity'}
        for root, track in [(7, 'a'), (9, 'b')]])
    events = [{'action': 'extend', 'base_event_ids': [f'persisted-{index}'],
               'primary_track_id': track, 'owned_unit_roots': [root]}
              for index, (track, root) in enumerate([('a', 7), ('b', 9)])]
    if reverse:
        events.reverse()
    output['events'] = events
    output['decision_review']['events'] = [
        {'event_index': index, 'reason': 'Continue this activity',
         'materials': [_material(source_id) for source_id in range(event['owned_unit_roots'][0],
                                                                    event['owned_unit_roots'][0] + 2)]}
        for index, event in enumerate(events)]
    frozen_output = copy.deepcopy(output)
    plan = latest.normalize_event_curator_output(output, component)
    by_track = {event['primary_track_id']: event for event in plan['events']}
    for index, track in enumerate(('a', 'b')):
        inherited = {item['source_message_id']: item for item in previous['events'][index]['source_materials']}
        actual = {item['source_message_id']: item for item in by_track[track]['source_materials']}
        assert {key: actual[key] for key in inherited} == inherited
        assert set(by_track[track]['source_message_ids']) == set(bases[index]['source_message_ids']) | (
            {7, 8} if track == 'a' else {9, 10})
        assert all(row['activity_role'] == 'bridge' for row in by_track[track]['source_bindings']
                   if row['source_message_id'] in {3, 4})
    assert output == frozen_output
    # With both historical owners retaining both quotes, ownership alone must
    # still fail the side-specific boundary proof, despite valid snapshots.
    invalid_component = copy.deepcopy(component)
    for base in invalid_component['base_event_candidates']:
        materials = copy.deepcopy(base['material_snapshot']['source_materials'])
        next(row for row in materials if row['source_message_id'] == 3).update(use='main', omit_quotes=[])
        base['material_snapshot'] = {**snapshot_for({
            'event_ref': base['material_snapshot']['event_ref'],
            'source_message_ids': base['source_message_ids'], 'source_materials': materials,
        }, invalid_component['context_messages']),
            'event_id': base['event_id'], 'event_fingerprint': base['fingerprint']}
    with pytest.raises(ValueError, match='boundary|boundaries'):
        latest.normalize_event_curator_output(copy.deepcopy(output), invalid_component)

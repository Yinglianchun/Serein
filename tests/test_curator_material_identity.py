"""Synthetic regressions for protected proposals and cross-window handoff."""
import asyncio
import copy
import itertools
import json

import pytest

from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p, pipeline_latest as latest
from test_event_handoff import PNG, shared_proposals
from test_public_features import settings, output_for
from test_track_continuation_parity import _two_track_bridge_batch


def annotated_proposals():
    component, output = shared_proposals()
    component['writer_material_review'] = True
    component['context_messages'] = [*component['messages'],
        {'id': 90, 'session_id': 1, 'content': 'Earlier synthetic activity.'}]
    return component, output


def annotate(output, component):
    bases = {base['event_id']: base for base in component['base_event_candidates']}
    units = {unit['unit_root_message_id']: unit['source_message_ids']
             for unit in component['memberships']}
    output['decision_review']['events'] = []
    for index, event in enumerate(output['events']):
        sources = [sid for base in event['base_event_ids'] for sid in bases[base]['source_message_ids']]
        sources += [sid for root in event['owned_unit_roots'] for sid in units[root]]
        output['decision_review']['events'].append({
            'event_index': index, 'reason': 'Synthetic activity', 'materials': [
                {'source_message_id': sid, 'use': 'main',
                 'reason': 'Material for Track ' + event['primary_track_id'], 'omit_quotes': []}
                for sid in dict.fromkeys(sources)]})


@pytest.mark.parametrize('order', list(itertools.permutations(range(4))))
def test_material_identity_survives_protected_bridge_filtering_and_reordering(order):
    component, output = annotated_proposals()
    output['events'] = [output['events'][index] for index in order]
    annotate(output, component)
    plan = latest.normalize_event_curator_output(output, component)
    assert [event['primary_track_id'] for event in plan['events']] == ['d']
    assert set(plan['defer_source_message_ids']) == {1, 2, 3, 4, 5}
    event = plan['events'][0]
    assert event['event_ref'] == f'event:{order.index(3) + 1}'
    assert [row['source_message_id'] for row in event['source_materials']] == [6, 7]
    assert all(row['reason'] == 'Material for Track d' for row in event['source_materials'])


def test_all_proposals_deferred_still_validate_their_materials():
    component, output = annotated_proposals()
    component['messages'] = component['messages'][:5]
    component['memberships'] = component['memberships'][:5]
    output['events'].pop()
    annotate(output, component)
    plan = latest.normalize_event_curator_output(output, component)
    assert plan['events'] == []
    assert set(plan['defer_source_message_ids']) == {1, 2, 3, 4, 5}
    forged = copy.deepcopy(output)
    forged['decision_review']['events'][0]['materials'][0].update(
        use='mixed', omit_quotes=['Invented source quote'])
    with pytest.raises(ValueError, match='verbatim'):
        latest.normalize_event_curator_output(forged, component)
    forged = copy.deepcopy(output)
    forged['decision_review']['events'][1]['materials'][0]['source_message_id'] = 999
    with pytest.raises(ValueError, match='exactly cover'):
        latest.normalize_event_curator_output(forged, component)


def test_protected_append_filters_writer_materials_to_new_owned_sources(settings):
    batch, _, _, second, _, component = _two_track_bridge_batch(settings)
    component.update(writer_material_review=True, append_protected=True)
    component['context_messages'].append({**component['messages'][0], 'id': 10,
        'session_id': 10, 'content': 'Earlier cover repair.'})
    component['context_session_ids'].append(10)
    component['base_event_candidates'] = [{
        'event_id': 'earlier-cover', 'primary_track_id': second, 'session_ids': [10],
        'source_message_ids': [10], 'predecessor_event_ids': [], 'active': True,
        'title': 'Cover repair', 'body': 'We repaired the cover earlier.',
        'protected': True, 'continuation_allowed': True}]
    output = {'events': [{'action': 'extend', 'primary_track_id': second,
        'base_event_ids': ['earlier-cover'], 'owned_unit_roots': [4]}],
        'skip_unit_roots': [2, 3], 'defer_unit_roots': [],
        'decision_review': {'events': [], 'boundaries': [], 'dispositions': [{
            'disposition': 'skip', 'unit_roots': [2, 3], 'reason': 'Unrelated units',
            'parked_source_message_ids': []}]}}
    annotate(output, component)
    plan = latest.normalize_event_curator_output(output, component)
    event = plan['events'][0]
    assert event['append_only'] and set(event['source_message_ids']) == {4, 10}
    requests = []
    async def runner(role, request):
        requests.append(request)
        return output_for(role, request)
    asyncio.run(p.first_event_writer_pass(settings.database, batch, component, plan, 0, runner))
    request = requests[0]
    assert request['writer_mode'] == 'append'
    assert 'We repaired the cover earlier.' in request['prompt']
    assert 'Earlier cover repair.' not in request['prompt']
    assert [message['id'] for message in request['messages']] == [4]
    materials = json.loads(request['prompt'].split('<curator_materials_json>\n')[1].split('\n</')[0])
    assert [row['source_message_id'] for row in materials] == [4]
    assert all(row['source_message_id'] in {4, 10} for row in event['source_materials'])


@pytest.mark.parametrize('with_image', [False, True])
def test_cross_window_event_extension_hands_correct_materials_to_writer(settings, with_image):
    save_settings(settings.database, {'pipeline': {'material_review_enabled': True}})
    seen = []
    curator_seen = []
    first_body = None
    transcription_calls = []
    async def runner(role, request):
        if request.get('transcription_only'):
            transcription_calls.append(request)
            return {'image_transcriptions': [{'input_image': index, 'text': 'Synthetic cover image',
                'unreadable': False} for index, _ in enumerate(request['images'], 1)]}
        output = output_for(role, request)
        if role == 'event_curator':
            curator_seen.append(copy.deepcopy(request))
            annotate(output, request['component'])
        elif role == 'event_writer':
            seen.append(request)
        return output
    for window, day in [('first-window', '2025-01-01'), ('second-window', '2025-01-02')]:
        raw_archive(settings).ingest([
            {'source_event_id': window + '-user', 'session_id': window, 'role': 'user',
             'text': 'Synthetic cover plan ' + window, 'created_at': day + 'T00:00:00Z',
             'metadata': {'attachments': [{'kind': 'image', 'url': PNG}]}
                 if with_image and window == 'first-window' else {}},
            {'source_event_id': window + '-reply', 'session_id': window, 'role': 'assistant',
             'text': 'Synthetic cover response ' + window, 'created_at': day + 'T00:01:00Z'},
        ], source='synthetic')
        result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
        assert result.get('events') == 1, result
        if window == 'first-window':
            with Store(settings.database, read_only=True) as store:
                first_body = store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0]
        if with_image and window == 'first-window':
            from serein.extensions.pipeline_images import expire_completed_media
            with Store(settings.database) as store:
                store.conn.execute("UPDATE pipeline_batches SET result_json=json_set(result_json,"
                    "'$.completed_at','2000-01-01T00:00:00Z') WHERE status='done'")
                expire_completed_media(store)
                assert store.conn.execute("SELECT json_extract(result_json,'$.media_cache_expired') "
                    "FROM pipeline_batches WHERE status='done'").fetchone()[0] == 1
                assert store.conn.execute('SELECT count(*) FROM pipeline_media').fetchone()[0] == 0
                raw = store.conn.execute('SELECT metadata_json,image_transcription_json FROM raw_events '
                    'WHERE id=1').fetchone()
                assert PNG in raw['metadata_json'] and 'Synthetic cover image' in raw['image_transcription_json']
    assert len(seen) == 2
    request = seen[-1]
    assert request['event']['action'] == 'extend'
    assert request['writer_mode'] == 'append'
    assert {message['original_session_id'] for message in request['messages']} == {'second-window'}
    assert first_body in request['prompt']
    assert 'Synthetic cover response first-window' not in request['prompt']
    if with_image:
        assert len(transcription_calls) == 1
        assert request['images'] == []
        assert request['curator_image_transcriptions'] == []
        transcript = curator_seen[-1]['curator_image_transcriptions'][0]
        assert transcript['text'] == 'Synthetic cover image'
        assert transcript['source_message_id'] == 1
    materials = json.loads(request['prompt'].split('<curator_materials_json>\n')[1].split('\n</')[0])
    assert {row['source_message_id'] for row in materials} == {message['id'] for message in request['messages']}
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM fact_events WHERE status='active'").fetchone()[0] == 1
        assert store.conn.execute("SELECT body FROM fact_events WHERE status='active'").fetchone()[0] == (
            first_body + '\n\n' + output_for('event_writer', request)['event_draft'])
        sources = store.conn.execute('SELECT session_id,message_id FROM fact_event_sources WHERE item_id IN '
            "(SELECT item_id FROM fact_events WHERE status='active')").fetchall()
    assert set(map(tuple, sources)) == {(window, window + '-' + message) for window in ('first-window', 'second-window')
                                      for message in ('user', 'reply')}

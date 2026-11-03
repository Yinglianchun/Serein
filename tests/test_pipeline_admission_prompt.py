"""Delivered admission instructions match the existing source-evidence checks."""
import asyncio
from copy import deepcopy
import json

import pytest

from test_public_features import settings, output_for
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions.pipeline_admission import apply_gate


@pytest.fixture
def curator_request(settings):
    archive = raw_archive(settings)
    archive.ingest([{'source_event_id': 'prelude', 'session_id': 'processed',
        'role': 'user', 'text': 'Synthetic processed prelude',
        'created_at': '2025-01-01T00:00:00Z'}], source='synthetic')
    p.initialize(settings.database)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO raw_processing VALUES (1,'synthetic-prelude','manual_skip')")
    save_settings(settings.database, {'pipeline': {'round_gate_enabled': True}})
    texts = [('user', '封面就选蓝色，这件事到此结束。'),
             ('assistant', '好的，蓝色封面已确定。'),
             ('user', '现在开始讨论书架的尺寸。'),
             ('assistant', '书架宽度按一米设计。')]
    archive.ingest([{'source_event_id': str(index), 'session_id': 'admission',
        'role': role, 'text': text, 'created_at': f'2025-01-01T00:0{index}:00Z'}
        for index, (role, text) in enumerate(texts, 1)], source='synthetic')
    batch = p.new_batch(settings.database, True)
    data = json.loads(batch['input_json'])
    async def router(role, request):
        output = output_for(role, request)
        for index, row in enumerate(output['message_assignments']):
            row['primary_track_ref'] = 'new:1' if index < 2 else 'new:2'
        output['track_updates'].append({**output['track_updates'][0],
            'track_ref': 'new:2', 'subject': 'Synthetic shelf'})
        for update in output['track_updates']:
            update['event_policy'] = 'default'
        return output
    routed = asyncio.run(p.route_batch(settings.database, batch, data, router))
    data = p.save_routing_snapshot(settings.database, batch, data, routed)
    return p.request_for(settings.database, batch, 'event_curator', component=data['components'][0])


def decision(request, closure):
    output = output_for('event_curator', request)
    output['decision_review']['events'][0]['admission'] = {'closed_by': closure}
    return output


def test_delivered_prompt_explains_closure_checks_and_preserves_source_projection(curator_request):
    delivered = curator_request['rules']+'\n'+curator_request['prompt']
    for rule in ('本轮实际提供的原话', '不能用 unit root 或位置序号代替',
                 '不得小于本 Event 全部 owned 来源的最大编号', '末条 owned 原话本身也可以',
                 'session_id 必须与本 Event 至少一条 owned 原话相同',
                 '非空、连续的逐字片段', '无法核对这样的引文时保持 null'):
        assert rule in delivered
    payload = json.loads(curator_request['prompt'].split('<event_curator_input_json>\n', 1)[1]
        .split('\n</event_curator_input_json>', 1)[0])
    shown = [row for unit in payload['units'] for row in unit['messages']]
    component = curator_request['component']
    originals = {row['id']: row for row in component['context_messages']}
    assert [row['source_message_id'] for row in shown] == [2, 3]
    for row in shown:
        original = originals[row['source_message_id']]
        assert row['text'] == original['content']
        assert row['session_id'] == original['session_id']
    assert component['curator_read_source_ids'] == [2, 3, 4, 5]


@pytest.mark.parametrize('source_id,quote,damage,accepted', [
    (2, '这件事到此结束。', None, False),
    (3, '蓝色封面已确定。', None, True),
    (4, '现在开始讨论书架的尺寸。', None, True),
    (3, '并不存在的原文', None, False),
    (4, '现在开始讨论书架的尺寸。', 'session', False),
    (4, '现在开始讨论书架的尺寸。', 'unread', False),
])
def test_closure_receipt_matches_documented_evidence(curator_request, source_id, quote, damage, accepted):
    request = deepcopy(curator_request)
    component = request['component']
    if damage == 'session':
        next(row for row in component['context_messages'] if row['id'] == source_id)['session_id'] += 1
    elif damage == 'unread':
        component['curator_read_source_ids'].remove(source_id)
    output = decision(request, {'source_message_id': source_id, 'quote': quote})
    if accepted:
        normalized = p.latest.normalize_event_curator_output(output, component)
        event_ref = normalized['events'][0]['event_ref']
        assert normalized['event_admissions'][event_ref]['closed_by']['source_message_id'] == source_id
    else:
        with pytest.raises(ValueError, match='Admission closure must quote later original in the same session'):
            p.latest.normalize_event_curator_output(output, component)


def test_null_closure_keeps_the_existing_round_decision(curator_request):
    component = curator_request['component']
    normalized = p.latest.normalize_event_curator_output(decision(curator_request, None), component)
    result = apply_gate(normalized, component)
    assert result['events'] == []
    assert result['defer_source_message_ids'] == [2, 3]
    assert result['skip_source_message_ids'] == []

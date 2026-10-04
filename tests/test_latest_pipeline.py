import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
import pytest
from test_public_features import settings, ingest, output_for, synthetic_runner, raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions import pipeline_latest as latest
from serein.extensions.pipeline_rules import normalize_event_track_message_output


def curator_task(settings):
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    while task['role']=='track_router':
        p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
        task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert task['role']=='event_curator'
    return task


def test_router_ignores_extra_fields_but_identifies_missing_track_updates():
    messages = [{'id': 11}]
    output = {
        'message_assignments': [{'source_message_id': 11, 'primary_track_ref': 'existing',
                                 'context_track_refs': [], 'routing_role': 'primary_activity', 'note': 'extra'}],
        'track_updates': []}
    tracks = [{'track_id': 'existing', 'subject': 'Plan', 'throughline': 'Keep planning', 'status': 'active'}]
    def normalize():
        return normalize_event_track_message_output(output, messages, tracks,
                                                    session_id=1, next_track_ordinal=1)
    output['comment'] = 'extra top-level field'
    with pytest.raises(ValueError, match=r"missing=\['existing'\], unused=\[\]"):
        normalize()
    output['track_updates'].append({'track_ref': 'existing', 'subject': 'Plan',
                                    'throughline': 'Keep planning', 'status': 'active', 'note': 'extra'})
    assert normalize()[0][0]['primary_track_id'] == 'existing'
    del output['message_assignments'][0]['routing_role']
    with pytest.raises(ValueError, match=r"assignment #1 fields missing: \['routing_role'\]"):
        normalize()


def test_router_extra_fields_are_removed_from_saved_job(settings):
    ingest(settings)
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    output = output_for('track_router', task['request'])
    output['comment'] = 'extra'
    output['message_assignments'][0]['note'] = 'extra'
    output['track_updates'][0]['note'] = 'extra'
    p.submit(settings.database, task['job_id'], output)
    with Store(settings.database, read_only=True) as store:
        saved = json.loads(store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',
                                              (task['job_id'],)).fetchone()[0])
    assert 'comment' not in saved
    assert 'note' not in saved['message_assignments'][0]
    assert 'note' not in saved['track_updates'][0]


def test_curator_error_identifies_unaccounted_source(settings):
    ingest(settings)
    task = curator_task(settings)
    output = output_for('event_curator', task['request'])
    omitted = output['events'][0]['owned_unit_roots'].pop()
    with pytest.raises(ValueError, match=f'unaccounted source_message_ids=\\[{omitted}\\]'):
        p.validate(task['request'], output)


def test_curator_repeated_omission_retains_scope_without_skipping(settings):
    ingest(settings)
    calls = []
    async def runner(role, request):
        output = output_for(role, request)
        if role == 'event_curator':
            calls.append(request['prompt'])
            output['events'][0]['owned_unit_roots'].pop()
        return output
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert len(calls) == 2 and 'missing_messages' in calls[1] and 'previous_output' in calls[1]
    assert result['status'] == 'processed' and result['events'] == 0
    assert result['curator_omission_deferrals'][0]['missing_source_message_ids']
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['status'] == 'current'
    ingest(settings, 2)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:02:00Z' WHERE id=3")
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:03:00Z' WHERE id=4")
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['status'] == 'current'
    assert len(calls) == 2  # New messages must not cause a same-day retry of the held range.
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0


def test_agent_curator_second_omission_retains_scope(settings):
    ingest(settings)
    task = curator_task(settings)
    output = output_for('event_curator', task['request'])
    output['events'][0]['owned_unit_roots'].pop()
    with pytest.raises(latest.CuratorCoverageError):
        p.submit(settings.database, task['job_id'], output)
    receipt = p.submit(settings.database, task['job_id'], output)
    assert receipt['status'] == 'host_deferred'
    result = asyncio.run(p.advance(settings.database, include_recent=True))
    assert result['status'] == 'processed' and result['events'] == 0
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?', (result['batch_id'],)).fetchone()[0] == 'done'
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0


def test_repeated_curator_omission_pauses_on_third_round_and_retry_can_correct(settings, monkeypatch):
    ingest(settings)
    fail = True
    async def runner(role, request):
        output = output_for(role, request)
        if fail and role == 'event_curator':
            output['events'][0]['owned_unit_roots'].pop()
        return output
    clock = datetime(2026, 1, 1, 12, tzinfo=p.TZ)
    monkeypatch.setattr(p, 'now', lambda: clock.isoformat())
    new_batch = p.new_batch
    monkeypatch.setattr(p, 'new_batch', lambda database, include_recent:
                        new_batch(database, include_recent, clock=clock))
    for _ in range(2):
        retained = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
        assert retained['status'] == 'processed' and retained['events'] == 0
        clock += timedelta(days=1)
    first = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert first['status'] == 'paused' and '累计三轮漏项' in first['reason']
    assert first['job_id'] and first['failures'] == 3
    with Store(settings.database, read_only=True) as store:
        frozen = store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (first['batch_id'],)).fetchone()[0]
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
    p.retry_batch(settings.database, first['batch_id'])
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT input_json FROM pipeline_batches WHERE id=?', (first['batch_id'],)).fetchone()[0] == frozen
    fail = False
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))['events'] == 1


def test_curator_api_omission_retries_once_then_retains(settings, monkeypatch):
    ingest(settings)
    save_settings(settings.database, {
        'models': [{'id': 'local', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
        'assignments': {role: 'local' for role in p.ROLES},
        'pipeline': {'execution_mode': 'api'}})
    curator_calls = []
    async def complete(model, payload):
        with Store(settings.database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                'SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1'
            ).fetchone()[0])
        output = output_for(request['role'], request)
        if request['role'] == 'event_curator':
            curator_calls.append(payload)
            output['events'][0]['owned_unit_roots'].pop()
        return {'choices': [{'message': {'content': json.dumps(output)}}]}
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    result = asyncio.run(p.advance(settings.database, include_recent=True))
    assert len(curator_calls) == 2
    assert 'missing_messages' in curator_calls[1]['messages'][1]['content']
    assert result['status'] == 'processed' and result['curator_omission_deferrals']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0


def test_curator_model_cannot_forge_host_omission_receipt(settings):
    ingest(settings)
    task = curator_task(settings)
    output = output_for('event_curator', task['request'])
    output['_host_curator_omission'] = {'missing_source_message_ids': [999]}
    with pytest.raises(ValueError, match='cannot be submitted'):
        p.submit(settings.database, task['job_id'], output)


def test_curator_repair_success_settles_the_complete_scope(settings):
    ingest(settings)
    calls = []
    async def runner(role, request):
        output = output_for(role, request)
        if role == 'event_curator':
            calls.append(request)
            if len(calls) == 1:
                output['events'][0]['owned_unit_roots'].pop()
        return output
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert len(calls) == 2 and result['events'] == 1
    assert result['processed_originals'] == 2 and not result['curator_omission_deferrals']


def test_curator_omission_does_not_block_independent_track_in_same_batch(settings):
    ingest(settings)
    ingest(settings, 2)
    with Store(settings.database) as store:
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:02:00Z' WHERE id=3")
        store.conn.execute("UPDATE raw_events SET created_at='2025-01-01T00:03:00Z' WHERE id=4")
    async def runner(role, request):
        output = output_for(role, request)
        if role == 'track_router':
            for index, row in enumerate(output['message_assignments']):
                row['primary_track_ref'] = 'new:1' if index < 2 else 'new:2'
            output['track_updates'].append({**output['track_updates'][0], 'track_ref': 'new:2',
                                           'subject': 'Another book', 'throughline': 'An independent plan'})
        elif role == 'event_curator' and min(m['id'] for m in request['component']['messages']) == 1:
            output['events'][0]['owned_unit_roots'].pop()
        return output
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events'] == 1 and result['processed_originals'] == 2
    assert result['curator_omission_deferrals'][0]['deferred_source_message_ids'] == [1, 2]
    with Store(settings.database, read_only=True) as store:
        assert [row[0] for row in store.conn.execute('SELECT raw_id FROM raw_processing ORDER BY raw_id')] == [3, 4]


def test_curator_omission_also_retains_overlapping_bridge_corridor():
    omitted = {'messages': [{'id': 1}, {'id': 2}]}
    shared = {'messages': [{'id': 2}, {'id': 3}]}
    independent = {'messages': [{'id': 4}]}
    plans = [
        (omitted, p.curator_omission_plan(omitted, {'_host_curator_omission': {
            'missing_source_message_ids': [1]}}), []),
        (shared, {'events': [{'event_ref': 'would-settle'}]}, [('event', 'draft')]),
        (independent, {'events': [{'event_ref': 'independent'}]}, [('event', 'draft')]),
    ]
    propagated = p.propagate_curator_omission(plans)
    assert propagated[1][1]['events'] == [] and propagated[1][2] == []
    assert propagated[1][1]['curator_omission_deferrals'][0]['reason'] == 'curator_omission_dependency'
    assert propagated[2] == plans[2]


@pytest.mark.parametrize('prior', [
    [('2026-01-01', [1]), ('2026-01-02', [2])],
    [('2026-01-01', [1]), ('2026-01-01', [1])],
])
def test_omission_pause_counts_rounds_for_the_same_source_not_unrelated_ranges(settings, prior):
    p.initialize(settings.database)
    component = {'messages': [{'id': 1}, {'id': 2}]}
    output = {'_host_curator_omission': {'job_id': 'current:curator', 'missing_source_message_ids': [1]}}
    with Store(settings.database) as store:
        for index, (day, source_ids) in enumerate(prior):
            store.conn.execute('INSERT INTO pipeline_batches(id,scope,status,input_json,result_json) VALUES (?,?,?,?,?)',
                (f'prior:{index}', 'scope', 'done', json.dumps({'day': day}), json.dumps({
                    'curator_omission_deferrals': [{'reason': 'curator_omitted',
                                                  'deferred_source_message_ids': source_ids}]})))
    p.pause_repeated_curator_omission(settings.database, {'id': 'current', 'scope': 'scope'}, component, output)


def test_curator_repair_over_prompt_limit_retains_scope_without_truncation(settings):
    ingest(settings)
    task = curator_task(settings)
    save_settings(settings.database, {'pipeline': {'max_prompt_chars':
        len(task['request']['prompt']) + len(task['request']['rules']) + 20}})
    calls = []
    async def runner(role, request):
        output = output_for(role, request)
        if role == 'event_curator':
            calls.append(request)
            output['events'][0]['owned_unit_roots'].pop()
        return output
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert len(calls) == 1 and result['status'] == 'processed'
    assert result['curator_omission_deferrals'] and result['processed_originals'] == 0


def test_writer_json_object_wrapper_is_removed_before_saving(settings):
    ingest(settings)
    curator = curator_task(settings)
    p.submit(settings.database, curator['job_id'], output_for('event_curator', curator['request']))
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    wrapped = {'type': 'json_object', 'content': output_for('event_writer', task['request'])}
    p.submit(settings.database, task['job_id'], wrapped)
    with Store(settings.database, read_only=True) as store:
        saved = json.loads(store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',
                                              (task['job_id'],)).fetchone()[0])
    assert saved['title'] == wrapped['content']['title']
    assert 'type' not in saved and 'content' not in saved
    assert asyncio.run(p.advance(settings.database, include_recent=True))['events'] == 1


def test_writer_api_unwraps_json_object_without_retry(settings, monkeypatch):
    ingest(settings)
    curator = curator_task(settings)
    p.submit(settings.database, curator['job_id'], output_for('event_curator', curator['request']))
    save_settings(settings.database, {
        'models': [{'id': 'local', 'model': 'synthetic', 'base_url': 'http://127.0.0.1:9/v1'}],
        'assignments': {role: 'local' for role in p.ROLES},
        'pipeline': {'execution_mode': 'api'}})
    calls = []
    async def complete(model, payload):
        calls.append(payload)
        with Store(settings.database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                "SELECT request_json FROM pipeline_jobs WHERE role LIKE 'event_writer%' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0])
        wrapped = {'type': 'json_object', 'content': output_for('event_writer', request)}
        return {'choices': [{'message': {'content': json.dumps(wrapped)}}]}
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    assert asyncio.run(p.advance(settings.database, include_recent=True))['events'] == 1
    assert len(calls) == 1


def test_parked_correction_is_readable_but_not_owned(settings):
    raw_archive(settings).ingest([
        {'source_event_id':'u','session_id':'one','role':'user','text':'Meet at three','created_at':'2025-02-02T02:00:00+08:00'},
        {'source_event_id':'a','session_id':'one','role':'assistant','text':'Agreed','created_at':'2025-02-02T02:01:00+08:00'},
        {'source_event_id':'tail','session_id':'one','role':'user','text':'Wait, I cannot make three','created_at':'2025-02-02T02:55:00+08:00'}],source='test')
    p.initialize(settings.database)
    batch=p.new_batch(settings.database,False,datetime.fromisoformat('2025-02-02T04:00:00+08:00'))
    task=curator_task(settings);component=task['request']['component']
    rendered=latest.event_curator_model_input(component)
    assert [u['scope'] for u in rendered['units']]==['stable','stable','parked']
    assert 'cannot make' in rendered['units'][-1]['messages'][0]['text']
    output=output_for('event_curator',task['request']);output['events'][0]['owned_unit_roots'].append(component['parked_context_source_ids'][0])
    with pytest.raises(ValueError):p.validate(task['request'],output)
    deferred={'events':[],'skip_unit_roots':[],'defer_unit_roots':[m['id'] for m in component['messages']],
              'decision_review':{'events':[],'boundaries':[],
                  'dispositions':[{'disposition':'defer','unit_roots':[m['id'] for m in component['messages']],
                                   'reason':'parked 原文撤回了约定时间','parked_source_message_ids':component['parked_context_source_ids']}]}}
    assert len(latest.normalize_event_curator_output(deferred,component)['defer_source_message_ids'])==2


def test_latest_rolling_policy_does_not_force_unrelated_leaves_and_defers_blockers(settings):
    ingest(settings);asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner));ingest(settings,2)
    task=curator_task(settings);component=copy.deepcopy(task['request']['component'])
    base=component['base_event_candidates'][0]
    unrelated={**base,'event_id':'unrelated','source_message_ids':[90,91]}
    component['base_event_candidates'].append(unrelated)
    plan=output_for('event_curator',task['request'])
    normalized=latest.normalize_event_curator_output(plan,component)
    assert normalized['events'][0]['base_event_ids']==[base['event_id']]
    base['protected']=True
    normalized=latest.normalize_event_curator_output(plan,component)
    assert normalized['events']==[] and len(normalized['defer_source_message_ids'])==2
    assert normalized['hard_skips'][0]['blocking_flags']==['protected']


def test_three_stages_and_writer_sees_predecessor_body_without_old_originals(settings):
    ingest(settings);seen=[]
    async def runner(role,request):seen.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1
    assert seen==list(p.ROLES)==['track_router','event_curator','event_writer']
    with Store(settings.database,read_only=True) as store:
        completed=json.loads(store.conn.execute("SELECT input_json FROM pipeline_batches WHERE status='done'").fetchone()[0])
        assert completed['runtime_revision']==p.runtime_revision()
        routes=[tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_routes ORDER BY raw_id')]
        provenance=[tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_route_provenance ORDER BY raw_id')]
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_routes ORDER BY raw_id')]==routes
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM pipeline_route_provenance ORDER BY raw_id')]==provenance
    ingest(settings,2)
    task=curator_task(settings);p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
    task=asyncio.run(p.advance(settings.database,include_recent=True));prompt=task['request']['prompt']
    assert len(task['request']['messages'])==2
    assert task['role']=='event_writer' and 'Book club plan 1' in prompt and 'Book club plan 2' in prompt
    reading=json.loads(prompt.split('<event_reading_block_json>\n',1)[1].split('\n</event_reading_block_json>',1)[0])
    assert {row['source_message_id'] for row in reading}=={3,4}
    assert 'Agreed to plan 1' not in prompt
    assert '<previous_events_json>' in prompt and '正文通常控制在 500 字以内' in prompt
    assert task['request']['rules'] and task['request']['rules'] not in prompt
    with Store(settings.database) as store:
        detail=json.loads(store.conn.execute('SELECT details_json FROM pipeline_event_details').fetchone()[0])
        assert 'evidence' not in detail
        assert set(detail['source_activity_roles'])=={'1','2'}


def test_settled_event_is_queued_only_when_arc_linker_is_selected(settings):
    save_settings(settings.database,{
        'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'arc_linker':'local'}})
    ingest(settings)
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))
    assert result['events']==1
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute('SELECT event_id,event_fingerprint,status FROM pipeline_arc_links').fetchone()
        fact=store.conn.execute('SELECT item_id,fingerprint FROM fact_events WHERE status=\'active\'').fetchone()
        assert tuple(row)==(fact['item_id'],fact['fingerprint'],'pending')


def test_writer_body_uses_1000_guidance_with_1500_tolerance():
    request={'messages':[{'id':1,'content':'A book was returned'}]}
    output=output_for('event_writer',request)
    output['event_draft']='书还了。'
    output['sentence_evidence'][0]['sentence']=output['event_draft']
    assert latest.validate_event_writer_result(output)==[]
    output['event_draft']='书'*1500
    output['sentence_evidence'][0]['sentence']=output['event_draft']
    assert latest.validate_event_writer_result(output)==[]
    output['event_draft']='书'*1501
    assert '正文超过容错上限 1500 字：1501 字' in ' '.join(latest.validate_event_writer_result(output))
    output['title']=''
    assert '标题为空' in latest.validate_event_writer_result(output)
    output['kept_details']=['anchor']*13
    assert any('最多 12 项' in error for error in latest.validate_event_writer_result(output))


def test_public_writer_materializes_source_grounded_rules_with_configured_names():
    with latest.identity_scope({'ai_name': 'Atlas', 'user_name': 'Lin'}):
        rules = latest.materialize_agent_rules('event_writer')
    assert '我是 Atlas，Lin是她' in rules
    assert '纠正后直接写最终结论' in rules
    assert '不按相隔多久机械补时间' in rules
    assert '不能提供当前的新行动、感受或结果' in rules
    assert '不能把我的解释算成她的看法' in rules
    assert '不能只用最新一段覆盖旧经历' in rules
    assert 'Haven' not in rules and '小雨' not in rules


def test_writer_source_timestamps_are_explicit_shanghai_time():
    rows = latest.writer_transcript_payload([
        {'id': 1, 'role': 'user', 'content': 'late note', 'created_at': '2025-01-01T17:30:00Z'},
        {'id': 2, 'role': 'assistant', 'content': 'legacy', 'created_at': '2025-01-01T18:00:00'}])
    assert rows[0]['created_at'] == '2025-01-02T01:30:00+08:00'
    assert rows[1]['created_at'] == '2025-01-02T02:00:00+08:00'
    assert latest.writer_source_time('not-a-time') == 'not-a-time'


def test_router_and_curator_keep_developing_activity_over_keyword_or_tone():
    router = latest.materialize_agent_rules('track_router')
    curator = latest.materialize_agent_rules('event_curator')
    assert '不是作品、项目、关系或生活领域的长期 Arc' in router
    assert '只共享人物、关系、作品、产品、项目或技术栈，不构成续接' in router
    assert '用一句短语标识这一次具体对象或事项' in router
    assert '只可纠正对象、去掉阶段性措辞或收窄' in router
    assert '不得为了容纳另一项活动而扩大' in router
    assert '《作品》更新第 N 话' in router
    assert '下一批判断直接续接的最小线索' in router
    assert '另一话更新而开启一次新的完整观看' in router
    assert '不按醒目的称呼、作品名或重复关键词投票归线' in router
    assert '从事实转成玩笑或幻想' in router
    assert '正常使用，不自动续接它的安装、调试 Track' in router
    assert '不因语气变化或转为调笑就拆分' in curator
    assert '不能只贴“技术／情感”等不同类别标签' in curator


def test_router_prompt_requests_concrete_track_scope():
    prompt = latest.build_event_track_message_prompt('2026-09-23', [], [])
    assert '"subject":"具体对象或事项"' in prompt
    assert '"throughline":"这段经历的最小续接线索"' in prompt
    assert '仅仅属于同一产品或系统不够' in prompt


def test_model_counting_tolerance_settles_without_truncation(settings):
    ingest(settings)
    curator=curator_task(settings)
    p.submit(settings.database,curator['job_id'],output_for(curator['role'],curator['request']))
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    output=output_for('event_writer',task['request'])
    output['event_draft']='书'*1500
    output['sentence_evidence'][0]['sentence']=output['event_draft']
    p.submit(settings.database,task['job_id'],output)
    assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1
    with Store(settings.database,read_only=True) as store:
        saved=store.conn.execute('SELECT body FROM fact_events').fetchone()[0]
        assert saved==output['event_draft'] and len(saved)==1500


def test_writer_prompt_examples_match_both_evidence_outcomes():
    prompt=latest.build_event_writer_prompt('2025-01-01','',[{'id':1,'role':'user','content':'A book was returned'}])
    samples=[json.loads(line) for line in prompt.splitlines() if line.startswith('{"evidence_sufficient":')]
    assert len(samples)==2
    sufficient,insufficient=samples
    for sample in samples:
        assert 'recallable' not in sample
        assert latest.validate_event_writer_result(sample)==[]
    sufficient['recallable']=False
    assert latest.validate_event_writer_result(sufficient)==[]
    assert insufficient['evidence_sufficient'] is False and 'recallable' not in insufficient
    assert insufficient['title']==insufficient['event_draft']==''
    assert insufficient['kept_details']==insufficient['discarded_details']==[]


def test_writer_accepts_current_receiptless_output_and_diagnostic_review(settings):
    ingest(settings)
    curator=curator_task(settings)
    p.submit(settings.database,curator['job_id'],output_for(curator['role'],curator['request']))
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    output=output_for('event_writer',task['request'])
    output.pop('claim_groups')
    output.pop('sentence_evidence')
    output['self_review']['result_preserved']=False
    p.submit(settings.database,task['job_id'],output)
    assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1


def test_writer_optional_receipt_still_checks_owned_quotes():
    output=output_for('event_writer',{'messages':[{'id':1,'content':'The blue notebook arrived.'}]})
    output['sentence_evidence'][0]['source_spans'][0]['quote']='invented quote'
    assert any('逐字' in error for error in latest.validate_event_writer_result(output,
        [{'id':1,'content':'The blue notebook arrived.'}]))


def test_writer_insufficient_output_requires_review_object_but_not_true_checks():
    output={'evidence_sufficient':False,'recallable':False,'title':'','event_draft':'',
            'kept_details':[],'discarded_details':[]}
    assert 'self_review 缺失或不是对象' in latest.validate_event_writer_result(output)
    output['self_review']={'owned_evidence_sufficient':True}
    assert latest.validate_event_writer_result(output)==[]


@pytest.mark.parametrize('accepted',[False,True])
def test_old_pending_evidence_job_is_bypassed_and_history_preserved(settings,accepted):
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.core.store import encode
    ingest(settings)
    task=curator_task(settings)
    p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
    batch_id=task['request']['batch_id'];old_id=batch_id+':event_evidence:0:0'
    old_request={'role':'event_evidence','batch_id':batch_id,'prompt':'Retired task','identity':task['request']['identity']}
    old_output=encode({'evidence_points':['historical receipt']}) if accepted else None
    with Store(settings.database) as store:
        store.conn.execute('INSERT INTO pipeline_jobs(id,batch_id,role,request_json,output_json) VALUES (?,?,?,?,?)',
            (old_id,batch_id,'event_evidence:0:0',encode(old_request),old_output))
        store.conn.execute("UPDATE background_state SET value_json=json_set(value_json,'$.stage','event_evidence','$.status','awaiting_agent') WHERE name='work:pipeline'")
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    assert client.get('/v1/pipeline/status').json()['stage']!='event_evidence'
    with pytest.raises(ValueError,match='retired'):p.submit(settings.database,old_id,{'anything':'old client'})
    writer=asyncio.run(p.advance(settings.database,include_recent=True))
    assert writer['role']=='event_writer' and len(writer['request']['messages'])==2
    p.submit(settings.database,writer['job_id'],output_for('event_writer',writer['request']))
    assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT output_json FROM pipeline_jobs WHERE id=?',(old_id,)).fetchone()[0]==old_output
        assert store.conn.execute('SELECT count(*) FROM pipeline_jobs').fetchone()[0]==4
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==2
        assert 'evidence' not in json.loads(store.conn.execute('SELECT details_json FROM pipeline_event_details').fetchone()[0])


def test_one_bounded_context_request_and_no_foreign_ownership(settings):
    ingest(settings);task=curator_task(settings);component=task['request']['component'];root=min(m['id'] for m in component['messages'])
    query={'context_request':{'track_id':component['track_ids'][0],'before_message_id':root,'reason':'missing_subject'}}
    p.submit(settings.database,task['job_id'],query)
    next_task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert next_task['request']['context_read'] and next_task['request']['component']['context_receipt']['read_source_ids']==[]
    with pytest.raises(ValueError):p.submit(settings.database,next_task['job_id'],query)
    p.submit(settings.database,next_task['job_id'],output_for(next_task['role'],next_task['request']))


def test_daytime_routing_does_not_create_events_or_consume_originals(settings,monkeypatch):
    for i in range(1,6):ingest(settings,i)
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],'assignments':{'track_router':'local'}})
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        assert request['role']=='track_router'
        return {'choices':[{'message':{'content':json.dumps(output_for('track_router',request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    asyncio.run(p.flush_routes(settings.database))
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT COUNT(*) FROM pipeline_routes').fetchone()[0]==10
        assert store.conn.execute('SELECT COUNT(*) FROM raw_processing').fetchone()[0]==0
        assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE kind='event'").fetchone()[0]==0
    assert asyncio.run(p.advance(settings.database,include_recent=True))['role']=='event_curator'


def test_identity_rendering_never_rewrites_source_words(settings):
    names={'user_name':'Nori','ai_name':'Atlas'};save_settings(settings.database,{'identity':names})
    original='Literal User and AI are words in the original.'
    with latest.identity_scope(names):
        prompt=latest.build_event_writer_prompt('2025-01-01','',[{'id':1,'role':'user','content':original}])
    assert original in prompt and 'Nori' in prompt and 'Atlas' in prompt and '{ai_name}' not in prompt
    assert '我是 Atlas，Nori是她' in prompt


def test_configured_names_are_literal_values_not_recursive_templates(settings):
    from serein.semantic_setup import render_example
    names={'user_name':'Reader {ai_name}', 'ai_name':'Guide "A"'}
    template='{user_name} asks {ai_name}'
    expected='Reader {ai_name} asks Guide "A"'
    assert render_example(template,names)==expected
    with latest.identity_scope(names):
        assert latest._identity_text(template)==expected
    # Freshly loaded Writer examples use the current saved instance names.
    save_settings(settings.database, {'identity':{'user_name':'NewReader','ai_name':'NewGuide'}})
    rules=p.rules('event_writer',settings.database)
    assert '我是 NewGuide，NewReader是她' in rules


def test_images_keep_ownership_and_only_curator_receives_pixels(settings,monkeypatch):
    uri='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    raw_archive(settings).ingest([
        {'source_event_id':'u','session_id':'image','role':'user','text':'This is the book','created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[{'kind':'image','url':uri,'mime_type':'image/png'}]}},
        {'source_event_id':'a','session_id':'image','role':'assistant','text':'The blue book','created_at':'2025-01-01T00:01:00Z'}],source='test')
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],'assignments':{r:'local' for r in p.ROLES}})
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        if request.get('transcription_only'):
            assert request['images'][0]['evidence_role']=='stable'
            assert payload['messages'][1]['content'][1]['image_url']['url']==uri
            return {'choices':[{'message':{'content':json.dumps({'image_transcriptions':[{'input_image':1,'text':'Visible book title','unreadable':False}]})}}]}
        if request['role']=='event_curator':
            assert request['images']==[] and request['pretranscribed']
        if request['role']=='event_writer':
            assert request['images']==[]
            assert request['curator_image_transcriptions'][0]['evidence_role']=='owned'
            assert isinstance(payload['messages'][1]['content'],str) and uri not in payload['messages'][1]['content']
        return {'choices':[{'message':{'content':json.dumps(output_for(request['role'],request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1

def test_curator_prompt_shows_complete_receipt_schema_and_safe_aliases(settings):
    from serein.extensions.pipeline_audit import canonicalize_curator_review
    ingest(settings)
    task=curator_task(settings)
    prompt=task['request']['prompt']
    assert '"left_event_index": 0' in prompt
    assert '"right_event_index": 1' in prompt
    assert '"disposition": "skip"' in prompt
    assert '"parked_source_message_ids": []' in prompt
    review=canonicalize_curator_review({'events':[],'boundaries':[],'dispositions':[
        {'status':'skip','unit_roots':[1],'reason':'background only'}]})
    assert review['dispositions']==[{
        'disposition':'skip','unit_roots':[1],'reason':'background only',
        'parked_source_message_ids':[]}]


def test_image_transcription_defaults_only_deterministic_unreadable_flag():
    import hashlib
    from serein.extensions.pipeline_images import bind_transcriptions, image_bytes
    uri='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    body,_=image_bytes(uri)
    receipt={'source_message_id':1,'position':1,'sha256':hashlib.sha256(body).hexdigest(),
             'evidence_role':'stable','url':uri}
    readable=bind_transcriptions({'image_transcriptions':[{'input_image':1,'text':'visible'}]},[receipt])
    assert readable[0]['unreadable'] is False
    blank=bind_transcriptions({'image_transcriptions':[{'input_image':1,'text':''}]},[receipt])
    assert blank[0]['unreadable'] is True


def test_runtime_revision_retires_all_unfinished_frozen_statuses(settings):
    p.initialize(settings.database)
    stale={'contract':p.CONTRACT,'runtime_revision':'stale','routing_messages':[]}
    with Store(settings.database) as store:
        for index,status in enumerate(('pending','needs_repair','routing_only','routed'),1):
            store.conn.execute('INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES (?,?,?,?)',
                               (f'stale-{index}','scope',json.dumps(stale),status))
        store.conn.execute('INSERT INTO pipeline_routes(raw_id,route_json) VALUES (?,?)',(999,'{}'))
        store.conn.execute('INSERT INTO pipeline_route_provenance(raw_id,batch_id,route_json) VALUES (?,?,?)',
                           (999,'stale-4','{}'))
    p.initialize(settings.database)
    with Store(settings.database,read_only=True) as store:
        rows=store.conn.execute("SELECT status FROM pipeline_batches WHERE id LIKE 'stale-%' ORDER BY id").fetchall()
        assert store.conn.execute('SELECT count(*) FROM pipeline_routes WHERE raw_id=999').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM pipeline_route_provenance WHERE raw_id=999').fetchone()[0]==0
    assert [row['status'] for row in rows]==['superseded_protocol']*4

def test_transcribe_component_prefers_frozen_exact_receipt(settings,monkeypatch):
    import hashlib
    from serein.extensions.pipeline_images import image_bytes
    uri='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    body,_=image_bytes(uri)
    image={'source_message_id':1,'position':1,'sha256':hashlib.sha256(body).hexdigest(),
           'evidence_role':'stable','url':uri}
    transcription={key:image[key] for key in ('source_message_id','position','sha256','evidence_role')}
    transcription.update(text='visible title',unreadable=False)
    component={'context_messages':[],'curator_image_transcriptions':[transcription]}
    monkeypatch.setattr(p,'request_for',lambda *args,**kwargs:{'images':[image]})
    used=asyncio.run(p.transcribe_component(settings.database,{'id':'frozen'},component,0,None))
    assert used is True
    assert component['curator_image_transcriptions']==[transcription]


def test_failed_image_budget_survives_new_batches_and_manual_retry(settings):
    from serein.image_transcription import image_failures
    uri='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    raw_archive(settings).ingest([
        {'source_event_id':'bad-u','session_id':'bad','role':'user','text':'Read this image','created_at':'2025-01-01T00:00:00Z',
         'metadata':{'attachments':[{'kind':'image','url':uri}]}},
        {'source_event_id':'bad-a','session_id':'bad','role':'assistant','text':'We discussed the image','created_at':'2025-01-01T00:01:00Z'}],source='test')
    calls=[]
    async def failing(role,request):
        if request.get('transcription_only'):
            calls.append(request['images'][0]['sha256'])
            raise ValueError('synthetic image failure')
        return output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError,match='synthetic image failure'):
            asyncio.run(p.advance(settings.database,include_recent=True,runner=failing))
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=failing))
    assert len(calls)==3 and image_failures(settings.database,calls[0])==3
    assert result['events']==0 and result['deferred']==2
    assert result['image_deferrals'][0]['status']=='failed'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM pipeline_image_holds').fetchone()[0]==2
        payload=json.loads(store.conn.execute('SELECT image_transcription_json FROM raw_events WHERE id=1').fetchone()[0])
        assert payload['status']=='failed' and payload['items']==[]
        assert 'unreadable' not in payload['failed_images'][0]
    ingest(settings)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=failing))['events']==1
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=failing))['status']=='current'
    assert len(calls)==3
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    client=TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'})
    assert client.get('/v1/pipeline/status').json()['failed_images'][0]['failures']==3
    unauthenticated=TestClient(create_app(settings,token='test',live=True))
    assert unauthenticated.post('/v1/pipeline/retry-image',json={'sha256':calls[0]}).status_code==401
    from serein.work_tasks import enqueue, pause
    enqueue(settings.database,'pipeline')
    assert client.post('/v1/pipeline/retry-image',json={'sha256':calls[0]}).json()['status']=='busy'
    assert image_failures(settings.database,calls[0])==3
    pause(settings.database,'pipeline')
    assert client.post('/v1/pipeline/retry-image',json={'sha256':calls[0]}).status_code==200
    async def success(role,request):
        if request.get('transcription_only'):
            return {'image_transcriptions':[{'input_image':1,'text':'Actual image text','unreadable':False}]}
        return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=success))['events']==1
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0]==4
        assert store.conn.execute('SELECT count(*) FROM pipeline_image_holds').fetchone()[0]==0
    assert client.get('/v1/pipeline/status').json()['failed_images']==[]


def test_image_holds_keep_independent_proposals_and_shared_bridge_pending(settings):
    from serein.image_transcription import apply_image_holds
    p.initialize(settings.database)
    failed={'source_message_id':1,'position':1,'sha256':'a'*64,'status':'failed','failures':3}
    component={'messages':[{'id':1,'role':'user'},{'id':2,'role':'assistant'},
                           {'id':3,'role':'user'},{'id':4,'role':'assistant'}],
               'memberships':[{'source_message_ids':[i]} for i in range(1,5)],
               'base_event_candidates':[],'unavailable_images':[failed]}
    event=lambda ids:{'source_message_ids':ids,'base_event_ids':[]}
    plan={'events':[event([1,2]),event([3,4])],
          'skip_source_message_ids':[],'defer_source_message_ids':[]}
    result=apply_image_holds(settings.database,plan,component)
    assert result['events']==[event([3,4])] and result['defer_source_message_ids']==[1,2]
    plan['events'][1]=event([2,3,4])
    result=apply_image_holds(settings.database,plan,component)
    assert result['events']==[] and result['defer_source_message_ids']==[1,2,3,4]
    context={**component,'unavailable_images':[{**failed,'source_message_id':99}]}
    result=apply_image_holds(settings.database,plan,context,held_sources={'a'*64:{1,2}})
    assert result['events']==[] and result['defer_source_message_ids']==[1,2,3,4]
    ingest(settings);ingest(settings,2)
    component['unavailable_images'].append({**failed,'source_message_id':3,'sha256':'b'*64})
    plan['events']=[event([1,2]),event([3,4])]
    apply_image_holds(settings.database,plan,component)
    with Store(settings.database,read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT raw_id,sha256 FROM pipeline_image_holds ORDER BY raw_id')]==[
            (1,'a'*64),(2,'a'*64),(3,'b'*64),(4,'b'*64)]


def test_image_failure_keeps_successful_sibling_receipt(settings):
    from serein.image_transcription import reusable_transcriptions, image_failures
    uri='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    import base64
    other='data:image/png;base64,'+base64.b64encode(base64.b64decode(uri.split(',')[1])+b'different bytes').decode()
    raw_archive(settings).ingest([
        {'source_event_id':'multi-u','session_id':'multi','role':'user','text':'Two images','created_at':'2025-01-01T00:00:00Z',
         'metadata':{'attachments':[{'kind':'image','url':uri},{'kind':'image','url':other}]}},
        {'source_event_id':'multi-a','session_id':'multi','role':'assistant','text':'Discussed both','created_at':'2025-01-01T00:01:00Z'}],source='test')
    calls={1:0,2:0}
    async def runner(role,request):
        if request.get('transcription_only'):
            position=request['images'][0]['position'];calls[position]+=1
            if position==1:raise ValueError('bad image')
            return {'image_transcriptions':[{'input_image':1,'text':'Good sibling','unreadable':False}]}
        return output_for(role,request)
    for _ in range(2):
        with pytest.raises(ValueError,match='bad image'):
            asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    result=asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))
    assert calls=={1:3,2:1} and result['events']==0
    with Store(settings.database,read_only=True) as store:
        payload=json.loads(store.conn.execute('SELECT image_transcription_json FROM raw_events WHERE id=1').fetchone()[0])
    assert payload['items'][0]['text']=='Good sibling'
    assert image_failures(settings.database,payload['items'][0]['sha256'])==0
    assert payload['failed_images'][0]['sha256']!=payload['items'][0]['sha256']


def test_image_api_counts_each_failed_request_and_waiting_agent_does_not(settings,monkeypatch):
    from serein.image_transcription import image_failures
    uri='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    raw_archive(settings).ingest([
        {'source_event_id':'api-u','session_id':'api','role':'user','text':'Image','created_at':'2025-01-01T00:00:00Z',
         'metadata':{'attachments':[{'kind':'image','url':uri}]}},
        {'source_event_id':'api-a','session_id':'api','role':'assistant','text':'Reply','created_at':'2025-01-01T00:01:00Z'}],source='test')
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    p.submit(settings.database,task['job_id'],output_for('track_router',task['request']))
    waiting=asyncio.run(p.advance(settings.database,include_recent=True))
    assert waiting['request']['transcription_only']
    assert waiting['request']['execution']['task']=='image_transcription'
    sha=waiting['request']['images'][0]['sha256']
    assert image_failures(settings.database,sha)==0
    save_settings(settings.database,{'pipeline':{'execution_mode':'api'},'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],
                                     'assignments':{r:'local' for r in p.ROLES}})
    calls=[]
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:
            request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        if request.get('transcription_only'):
            calls.append(1)
            raise TimeoutError('synthetic timeout')
        return {'choices':[{'message':{'content':json.dumps(output_for(request['role'],request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    result=asyncio.run(p.advance(settings.database,include_recent=True))
    assert len(calls)==3 and image_failures(settings.database,sha)==3
    assert result['events']==0 and result['deferred']==2


def test_curator_targeted_repair_can_finish(settings):
    ingest(settings)
    calls=[]
    async def runner(role, request):
        output=output_for(role, request)
        if role=='event_curator':
            calls.append(request)
            if len(calls)==1:
                output['events'][0]['owned_unit_roots'].pop()
            else:
                repair=json.loads(request['prompt'].split('\n')[-1])
                assert repair['missing_messages']
                assert [m['id'] for m in repair['missing_messages']]==repair['missing_source_message_ids']
                assert repair['previous_output']['events'][0]['owned_unit_roots']
                assert request['prompt'].startswith(calls[0]['prompt'])
        return output
    result=asyncio.run(p.advance(settings.database, include_recent=True, runner=runner))
    assert result['events']==1 and len(calls)==2


@pytest.mark.parametrize('api_mode', [False, True], ids=['agent', 'api'])
def test_router_request_sends_complete_role_rules_once(settings, monkeypatch, api_mode):
    if api_mode:
        save_settings(settings.database, {'models': [{'id': 'local', 'model': 'synthetic',
            'base_url': 'http://127.0.0.1:9/v1'}], 'assignments': {'track_router': 'local'}})
    ingest(settings)
    captured = []
    async def complete(model, payload):
        with Store(settings.database, read_only=True) as store:
            request = json.loads(store.conn.execute(
                "SELECT request_json FROM pipeline_jobs WHERE role LIKE 'track_router%' "
                "AND output_json IS NULL ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        captured.append((request, payload))
        return {'choices': [{'message': {'content': json.dumps(output_for('track_router', request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete', complete)
    task = asyncio.run(p.advance(settings.database, include_recent=True))
    request = captured[0][0] if api_mode else task['request']
    with latest.identity_scope(request['identity']):
        rules = latest.materialize_agent_rules('track_router')
    assert request['rules'] == rules
    assert rules not in request['prompt']
    assert (request['rules'] + request['prompt']).count(rules) == 1
    assert '<raw_messages_json>' in request['prompt']
    if api_mode:
        assert captured
        for _, payload in captured:
            assert payload['messages'][0] == {'role': 'system', 'content': rules}
            assert sum(str(message['content']).count(rules) for message in payload['messages']) == 1
    else:
        assert task['role'] == 'track_router' and not captured


def test_standalone_router_prompt_keeps_rules_and_preserves_data_when_separated():
    message = {'id': 1, 'session_id': 1, 'role': 'user', 'content': '原文 空格\n第二行'}
    rules = latest.materialize_agent_rules('track_router')
    standalone = latest.build_event_track_message_prompt('2026-10-04', [message], [])
    separated = latest.build_event_track_message_prompt('2026-10-04', [message], [], include_role_rules=False)
    assert standalone.count(rules) == 1
    assert rules not in separated
    assert len(standalone) - len(separated) == len(rules)
    for tag in ('active_tracks_json', 'raw_messages_json', 'bounded_recent_context_json'):
        extract = lambda prompt: json.loads(prompt.split(f'<{tag}>\n', 1)[1].split(f'\n</{tag}>', 1)[0])
        assert extract(standalone) == extract(separated)

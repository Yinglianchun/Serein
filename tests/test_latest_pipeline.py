import asyncio
import copy
import json
from datetime import datetime
import pytest
from test_public_features import settings, ingest, output_for, synthetic_runner, raw_archive
from serein.core.store import Store
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from serein.extensions import pipeline_latest as latest


def curator_task(settings):
    task=asyncio.run(p.advance(settings.database,include_recent=True))
    while task['role']=='track_router':
        p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
        task=asyncio.run(p.advance(settings.database,include_recent=True))
    assert task['role']=='event_curator'
    return task


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


def test_three_stages_and_writer_sees_exact_predecessor_originals(settings):
    ingest(settings);seen=[]
    async def runner(role,request):seen.append(role);return output_for(role,request)
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=runner))['events']==1
    assert seen==list(p.ROLES)==['track_router','event_curator','event_writer']
    ingest(settings,2)
    task=curator_task(settings);p.submit(settings.database,task['job_id'],output_for(task['role'],task['request']))
    task=asyncio.run(p.advance(settings.database,include_recent=True));prompt=task['request']['prompt']
    assert len(task['request']['messages'])==4
    assert task['role']=='event_writer' and 'Book club plan 1' in prompt and 'Book club plan 2' in prompt
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
    assert '局部回应不能截坏前句' in rules
    assert '纠正后直接写最终结论' in rules
    assert '不按相隔多久机械补时间' in rules
    assert 'claim_groups 与 sentence_evidence 不必返回' in rules
    assert '前版绑定原文已经作为 owned 完整并入' in rules
    assert '反例三' in rules and '台灯' in rules
    assert '我把这句话理解成' not in rules
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
    assert '不因内容类别或语气变了另开 Track' in router
    assert '工具的正常使用不自动继承施工线' in router
    assert 'primary 取主要推进的新线' in router
    assert '先识别整段实际活动' in curator
    assert '换问法、材料类别、语气或比喻也不是切分依据' in curator
    assert '不能只给两段贴不同类别' in curator


def test_router_prompt_requests_concrete_track_scope():
    prompt = latest.build_event_track_message_prompt('2026-09-23', [], [])
    assert '"subject":"具体对象或事项"' in prompt
    assert '"throughline":"这段经历的最小续接线索"' in prompt
    assert '设想、未来建议或同产品关联不够' in prompt


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
        assert latest.validate_event_writer_result(sample)==[]
    sufficient['recallable']=False
    assert latest.validate_event_writer_result(sufficient)==[]
    assert insufficient['evidence_sufficient'] is False and insufficient['recallable'] is False
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


@pytest.mark.parametrize('body', [
    '她说这本书的装订不错。我据此判断封面还能留着。',
    '“留下封面。”',
    '书' * 501,
    '书' * 1500,
])
def test_writer_validation_does_not_turn_style_or_soft_budget_into_rejection(body):
    result = {'evidence_sufficient': True, 'recallable': False, 'title': 'Notebook',
              'event_draft': body, 'kept_details': [], 'discarded_details': [],
              'self_review': {'result_preserved': False}}
    assert latest.validate_event_writer_result(result) == []


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
    assert 'Nori把台灯送修' in prompt


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
    assert 'NewReader把台灯送修' in rules and 'NewGuide' in rules


def test_images_keep_ownership_and_only_curator_receives_pixels(settings,monkeypatch):
    uri='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
    raw_archive(settings).ingest([
        {'source_event_id':'u','session_id':'image','role':'user','text':'This is the book','created_at':'2025-01-01T00:00:00Z','metadata':{'attachments':[{'kind':'image','url':uri,'mime_type':'image/png'}]}},
        {'source_event_id':'a','session_id':'image','role':'assistant','text':'The blue book','created_at':'2025-01-01T00:01:00Z'}],source='test')
    save_settings(settings.database,{'models':[{'id':'local','model':'synthetic','base_url':'http://127.0.0.1:9/v1'}],'assignments':{r:'local' for r in p.ROLES}})
    async def complete(model,payload):
        with Store(settings.database,read_only=True) as store:request=json.loads(store.conn.execute('SELECT request_json FROM pipeline_jobs WHERE output_json IS NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        if request['role']=='event_curator':
            assert request['images'][0]['evidence_role']=='stable'
            assert payload['messages'][1]['content'][1]['image_url']['url']==uri
        if request['role']=='event_writer':
            assert request['images']==[]
            assert request['curator_image_transcriptions'][0]['evidence_role']=='owned'
            assert isinstance(payload['messages'][1]['content'],str) and uri not in payload['messages'][1]['content']
        return {'choices':[{'message':{'content':json.dumps(output_for(request['role'],request))}}]}
    monkeypatch.setattr('serein.model_runtime.complete',complete)
    assert asyncio.run(p.advance(settings.database,include_recent=True))['events']==1

import asyncio
import io
import json
import sqlite3
import tarfile

import pytest

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store, digest
from serein.deployment import read_settings, save_settings
from serein.legacy_migration.scan import clean_body, is_daily_impression, is_old_self_anchor, scan, unpack
from serein.legacy_migration.models import import_models
from serein.legacy_migration.workflow import Migration, validate_cues
from serein.legacy_migration.cues import preview as cue_preview, run as repair_cues
from serein.legacy_migration.vectors import reuse_legacy, maintain


def legacy(root, group, key, body):
    file=root/'buckets'/group/(key+'.md');file.parent.mkdir(parents=True,exist_ok=True)
    file.write_text('---\nid: '+key+'\nname: 书店约定\ntags: [old]\ncreated: 2026-01-01\n---\n'+body,'utf-8')
    return file


@pytest.fixture
def setup(tmp_path):
    root=tmp_path/'old'
    legacy(root,'dynamic','a','Mira 约好周六去青禾书店。\n### reflection\n不要保留这个想法\n### affect_anchor\n不要保留这个锚点\n### moment\n周六见。')
    legacy(root,'permanent','b','周六，他们如约在青禾书店见面。')
    legacy(root,'feel','diary','今天在书店。')
    legacy(root,'archive','archived','旧经历仍需保留。')
    legacy(root/'state'/'deleted_backups','dynamic','a','不可导入的备份')
    (root/'state'/'memory_edges.jsonl').write_text(json.dumps({'source':'a','target':'b','relation':'next_context'})+'\n','utf-8')
    settings=Settings(tmp_path/'new'/'memory.db',tmp_path/'new'/'index.db',writable=True)
    initialize(settings)
    save_settings(settings.database,{'models':[{'id':'test','model':'mock','base_url':'http://127.0.0.1:9/v1'}],
        'assignments':{'operit_tagging':'test','embedding':'test'}})
    plan=scan(root)
    migration=Migration(settings,plan,{'user_name':'Mira','ai_name':'Sol','aliases':['米拉']})
    yield settings,root,plan,migration
    migration.close()


def response(value):return {'choices':[{'message':{'content':json.dumps(value)}}]}


@pytest.mark.parametrize('existing_policies', [False, True])
def test_wizard_domain_confirmation_does_not_submit_null_policies(tmp_path, monkeypatch, existing_policies):
    from serein.legacy_migration.__main__ import domains
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True)
    initialize(settings)
    if existing_policies:
        save_settings(settings.database, {'tagging': {'policies': {
            'event': {'life': 'explicit_only'}, 'scene': {'life': 'excluded'}}}})
    before=read_settings(settings.database)
    monkeypatch.setattr('builtins.input', lambda prompt: 'yes')
    domains(settings.database)
    after=read_settings(settings.database)
    assert after['tagging']==before['tagging']
    assert after['tagging_version']==before['tagging_version']
    assert after['identity']==before['identity']
    assert after['models']==before['models']


@pytest.mark.parametrize('answers,keys', [
    (['e','0','life','Edited life','Edited description','yes'], ['life','tech']),
    (['a','art','Art','Creative work','yes'], ['life','tech','art']),
    (['d','1','yes'], ['life']),
])
def test_wizard_domain_edits_preserve_policies_for_retained_domains(tmp_path, monkeypatch, answers, keys):
    from serein.legacy_migration.__main__ import domains
    settings=Settings(tmp_path/'memory.db',tmp_path/'index.db',writable=True)
    initialize(settings)
    catalog=[{'key': key, 'label': key.title(), 'description': key, 'policy': 'normal'}
             for key in ('life','tech')]
    policies={'event': {'life': 'explicit_only', 'tech': 'excluded'},
              'scene': {'life': 'excluded', 'tech': 'normal'}}
    save_settings(settings.database, {'tagging': {'domains': catalog, 'policies': policies}})
    prompts=iter(answers)
    monkeypatch.setattr('builtins.input', lambda prompt: next(prompts))
    domains(settings.database)
    saved=read_settings(settings.database)['tagging']
    assert [row['key'] for row in saved['domains']]==keys
    assert saved['policies']=={kind: {key: rule for key,rule in rules.items() if key in keys}
                              for kind,rules in policies.items()}
    if answers[0]=='e':
        assert saved['domains'][0]=={**catalog[0], 'label': 'Edited life', 'description': 'Edited description'}
    elif answers[0]=='a':
        assert saved['domains'][-1]=={'key':'art','label':'Art','description':'Creative work','policy':'normal'}


def test_section_cleanup():
    body='起点\n### reflection\n删除\n#### 小标题\n也删除\n### moment\n保留\n### affect_anchor\n删除锚点\n## 后续\n结尾'
    cleaned,count=clean_body(body)
    assert cleaned=='起点\n保留\n## 后续\n结尾' and count==2
    fenced='```md\n### reflection\n代码文字\n```'
    assert clean_body(fenced)==(fenced,0)


def test_only_removed_sections_skip_without_blocking(setup):
    _,root,_,_=setup
    legacy(root,'archive','only-reflection','### reflection\n只剩需要去掉的内容。\n### affect_anchor\n锚点')
    plan=scan(root)
    assert not plan['errors'] and plan['summary']['empty_after_cleanup_skipped']==1
    assert plan['skipped'][0]['old_id']=='only-reflection'


def test_retired_self_anchor_is_discarded_before_scene_import(setup):
    _,root,_,_=setup
    anchor=legacy(root,'permanent','self-one','我是旧的固定自我锚点。')
    anchor.write_text(anchor.read_text('utf-8').replace('tags: [old]','domain: [self_anchor]'),'utf-8')
    ordinary=legacy(root,'dynamic','ordinary-self-word','这里提到了自我，但仍是普通记忆。')
    plan=scan(root)
    assert plan['summary']['self_anchors_discarded']==1
    assert 'self-one' not in {item['old_id'] for item in plan['items']}
    assert 'ordinary-self-word' in {item['old_id'] for item in plan['items']}
    assert any(item['old_id']=='self-one' and item['reason']=='self_anchor_discarded' for item in plan['skipped'])


def test_self_anchor_markers_do_not_match_normal_prose():
    for metadata in ({'self_anchor':True},{'tags':['自我']},{'bucket_tags':['first_person_anchor']},
                     {'domain':['self_anchor']},{'anchor_kind':'first-person-anchor'}):
        assert is_old_self_anchor(metadata)
    assert not is_old_self_anchor({'tags':['关于自我的一次谈话'],'domain':['relationship']})


def test_daily_impressions_are_discarded_but_ordinary_feel_is_a_diary(setup):
    _,root,_,_=setup
    legacy(root,'feel','reflection_daily_2026-09-14','应当丢弃。')
    tagged=legacy(root,'feel','weather','也应当丢弃。')
    tagged.write_text(tagged.read_text('utf-8').replace('tags: [old]','tags: [relationship_weather]'),'utf-8')
    weekly=legacy(root,'feel','weekly','周印象也属于退役关系天气。')
    weekly.write_text(weekly.read_text('utf-8').replace('tags: [old]','tags: [weekly_impression]'),'utf-8')
    mentioned=legacy(root,'feel','ordinary-mention','普通心绪正文提到日印象，但不是日印象记录。')
    plan=scan(root)
    ids={item['old_id'] for item in plan['items']}
    assert {'reflection_daily_2026-09-14','weather','weekly'}.isdisjoint(ids)
    assert {'diary','ordinary-mention'} <= ids
    assert plan['summary']['daily_impressions_discarded']==3
    assert {row['old_id'] for row in plan['skipped'] if row['reason']=='daily_impression_discarded'}=={
        'reflection_daily_2026-09-14','weather','weekly'}
    assert is_daily_impression({'id':'reflection_daily_2026-09-15','tags':[]})
    assert is_daily_impression({'id':'other','tags':'daily_impression, old'})
    assert not is_daily_impression({'id':'other','tags':['old'],'name':'日印象讨论'})


def test_changed_source_is_not_silently_imported_as_another_batch(setup):
    settings,root,_,migration=setup
    legacy(root,'dynamic','a','有人编辑了旧库。')
    with pytest.raises(ValueError,match='避免重复导入'):
        Migration(settings,scan(root),migration.options)


def test_vector_cleanup_removes_orphans_without_canonical_writes(setup):
    settings,_,_,migration=setup;migration.import_bodies()
    save_settings(settings.database,{'assignments':{'embedding':'','reranker':''}})
    from serein.recall.index import refresh_index
    refresh_index(settings.database,settings.index,set(migration.ids.values()))
    with sqlite3.connect(settings.index) as conn:
        conn.execute('INSERT INTO vectors VALUES (?,?,?)',('orphan','[1,0]',2))
        conn.execute('INSERT INTO vectors VALUES (?,?,?)',(migration.ids['a'],'[0,1]',2))
    assert maintain(settings,'clean')['canonical_writes']==0
    with sqlite3.connect(settings.index) as conn:
        assert conn.execute('SELECT id FROM vectors').fetchall()==[(migration.ids['a'],)]
    with Store(settings.database) as store:assert store.read(migration.ids['a'])['revision']==1


def test_scan_and_import_resume_preserve_body_diaries_archive(setup):
    settings,root,plan,migration=setup
    assert not plan['errors'] and len(plan['items'])==4
    assert plan['summary']['diaries']==1 and plan['summary']['affect_sections_removed']==2
    migration.backup();migration.import_bodies();migration.import_bodies()
    with Store(settings.database) as store:
        doc=store.read(migration.ids['a'])
        assert doc['body_md']=='Mira 约好周六去青禾书店。\n周六见。'
        assert doc['title']=='书店约定' and 'tags' not in doc['metadata']
        assert store.read(migration.ids['archived'])['lifecycle']=='archived'
        assert store.conn.execute('SELECT count(*) FROM diaries').fetchone()[0]==1
        assert store.conn.execute('SELECT body_md FROM diary_entries').fetchone()[0]=='今天在书店。'
    with sqlite3.connect(migration.root/'before-import.db') as conn:
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0]==0


def test_archive_traversal_and_duplicate_rejected(tmp_path,setup):
    archive=tmp_path/'bad.tar'
    with tarfile.open(archive,'w') as tar:
        member=tarfile.TarInfo('../escape');member.size=1;tar.addfile(member,io.BytesIO(b'x'))
    with pytest.raises(ValueError,match='路径'):unpack(archive,tmp_path/'out')
    _,root,_,_=setup
    legacy(root,'permanent','a','重复')
    assert scan(root)['errors'][0]['error'].startswith('重复 id')


def test_named_cues_are_dropped_without_retry_while_entities_are_saved(setup,monkeypatch):
    settings,_,plan,migration=setup;calls=[]
    migration.import_bodies()
    async def complete(model,payload):
        text=payload['messages'][0]['content']
        assert '不生成经历或召回 cue' not in text
        data=json.loads(payload['messages'][1]['content']);calls.append(data)
        assert len(data['domains'])==7 and all('description' in d for d in data['domains'])
        material=data['materials'][0]
        entities=[]
        if 'Mira' in data['content']:
            entities=[{'name':'Mira','type':'person','supports':[{'source_id':material['source_id'],'quote':'Mira 约好周六去青禾书店。'}]}]
        return response({'domain':'life','entities':entities,'cues':['Mira 的书店约定'] if len(calls)==1 else ['周六书店约定']})
    monkeypatch.setattr('serein.legacy_migration.workflow.complete',complete)
    asyncio.run(migration.tag_all());asyncio.run(migration.tag_all())
    assert len(calls)==2 and all('validation_feedback' not in call for call in calls)
    with Store(settings.database) as store:
        doc=store.read(migration.ids['a'])
        assert doc['metadata']['scene_cues']==[]
        assert doc['metadata']['tagged_entities'][0]['name']=='Mira'
        assert doc['metadata']['canonical_domain']=='life'
        assert doc['revision']==2 and 'Mira' in doc['body_md']
    for cue in ['米拉的书店','SOL 的心事','User 的事情']:
        assert validate_cues([cue],['Mira','Sol','米拉'])==[]
    assert validate_cues(['Mira 的书店','周六书店约定'],['Mira','Sol'])==['周六书店约定']
    assert validate_cues(['Airdrop 的使用'],['Mira','Sol'])==['Airdrop 的使用']


def test_explicit_cue_repair_finishes_pending_and_preserves_completed_metadata(setup,monkeypatch):
    settings,_,_,migration=setup;migration.import_bodies()
    first=migration.ids['a'];second=migration.ids['b']
    with Store(settings.database) as store,store.transaction():
        doc=store.read(first)
        store.revise(first,expected_revision=doc['revision'],title=doc['title'],body_md=doc['body_md'],metadata={
            **doc['metadata'],'legacy_tagging_completed':True,'legacy_tagging_pending':False,
            'tagged_entities':[{'name':'preserve-me'}]})
    calls=[]
    async def complete(model,payload):
        calls.append(payload)
        content=json.loads(payload['messages'][1]['content'])['content']
        if payload['messages'][0]['content'].startswith('只返回 JSON'):
            return response({'cues':['周六书店约定']})
        assert 'materials' in json.loads(payload['messages'][1]['content'])
        return response({'domain':'life','entities':[],'cues':['书店如约见面']})
    monkeypatch.setattr('serein.legacy_migration.cues.complete',complete)
    assert cue_preview(settings.database)['candidates']==2
    result=asyncio.run(repair_cues(settings))
    assert result['completed']==2 and result['cues_only']==1 and result['full_tagging']==1
    with Store(settings.database) as store:
        one=store.read(first);two=store.read(second)
        assert one['metadata']['scene_cues']==['周六书店约定']
        assert one['metadata']['tagged_entities']==[{'name':'preserve-me'}]
        assert two['metadata']['scene_cues']==['书店如约见面']
        assert two['metadata']['legacy_tagging_completed'] and not two['metadata']['legacy_tagging_pending']
    assert cue_preview(settings.database)['candidates']==0


def test_explicit_cue_repair_drops_named_cue_without_retry(setup,monkeypatch):
    settings,_,_,migration=setup;migration.import_bodies();calls=[]
    async def complete(model,payload):
        data=json.loads(payload['messages'][1]['content']);calls.append(data)
        return response({'domain':'life','entities':[],'cues':['Mira 的书店'] if len(calls)==1 else ['周六书店约定']})
    monkeypatch.setattr('serein.legacy_migration.cues.complete',complete)
    result=asyncio.run(repair_cues(settings))
    assert len(calls)==2 and result['completed']==2 and result['empty']==1
    assert all('validation_feedback' not in call for call in calls)


def test_tagging_retry_errors_never_echo_arbitrary_provider_content():
    from serein.legacy_migration.workflow import tagging_failure
    assert tagging_failure(ValueError('provider response with synthetic-secret'))=='模型请求或输出校验失败'
    assert tagging_failure(ValueError('Upstream returned HTTP 429'))=='模型请求或输出校验失败'
    assert tagging_failure(json.JSONDecodeError('secret details','private response',0))=='模型未返回有效 JSON'


@pytest.mark.parametrize('extra',[{}, {'cues':['Mira']}, {'cues':'invalid'}])
def test_cues_opt_out_preserves_saved_cues_and_does_not_schedule_paid_backfill(setup,monkeypatch,extra):
    settings,_,plan,migration=setup
    migration.import_bodies()
    # Old ledgers omitted the new option; switching it must retain IDs and progress.
    options={k:v for k,v in migration.options.items() if k!='generate_cues'}
    migration.db.execute("UPDATE config SET value=? WHERE key='options'",(json.dumps(options),))
    key=migration.ids['a']
    with Store(settings.database) as store,store.transaction():
        doc=store.read(key)
        store.revise(key,expected_revision=doc['revision'],title=doc['title'],body_md=doc['body_md'],
            metadata={**doc['metadata'],'scene_cues':['已人工保存的线索']})
    resumed=Migration(settings,plan,{**options,'generate_cues':False});calls=[]
    async def complete(model,payload):
        assert '不生成经历或召回 cue' in payload['messages'][0]['content']
        assert '额外返回 cues 数组' not in payload['messages'][0]['content']
        assert 'thinking' not in payload  # Unknown providers do not receive private parameters.
        data=json.loads(payload['messages'][1]['content']);assert 'forbidden_names' not in data
        calls.append(data)
        return response({'domain':'life','entities':[],**extra})
    monkeypatch.setattr('serein.legacy_migration.workflow.complete',complete)
    async def forbidden(*args,**kwargs):raise AssertionError('Unexpected automatic model call')
    monkeypatch.setattr('serein.model_runtime.complete',forbidden)
    try:
        asyncio.run(resumed.tag_all());asyncio.run(resumed.tag_all())
        assert len(calls)==2 and resumed.ids['a']==key
        with Store(settings.database) as store:
            doc=store.read(key)
            assert doc['metadata']['scene_cues']==['已人工保存的线索']
            assert doc['metadata']['legacy_tagging_completed'] and not doc['metadata']['legacy_tagging_pending']
            assert not doc['metadata'].get('legacy_cues_rebuilt')
            assert doc['metadata']['canonical_domain']=='life'
            assert store.read(resumed.ids['b'])['metadata']['scene_cues']==[]
        from serein.import_tagging import process
        asyncio.run(process(settings.database))
    finally:resumed.close()


def test_disable_cues_after_failure_keeps_successes_and_clears_cue_feedback(setup,monkeypatch):
    settings,_,plan,migration=setup;migration.import_bodies();calls=[]
    async def complete(model,payload):
        data=json.loads(payload['messages'][1]['content']);calls.append(data)
        if len(calls)==1:return response({'domain':'life','entities':[],'cues':['周六的书店约定']})
        if len(calls)==2:return response({'domain':'life','entities':[],'cues':'invalid'})
        assert 'validation_feedback' not in data and 'forbidden_names' not in data
        return response({'domain':'life','entities':[]})
    monkeypatch.setattr('serein.legacy_migration.workflow.complete',complete)
    with pytest.raises(ValueError,match='停止自动重试'):asyncio.run(migration.tag_all())
    with Store(settings.database) as store:before=store.read(migration.ids['a'])
    resumed=Migration(settings,plan,{**migration.options,'generate_cues':False})
    try:asyncio.run(resumed.tag_all())
    finally:resumed.close()
    assert len(calls)==3
    with Store(settings.database) as store:
        assert store.read(migration.ids['a'])==before
        assert store.read(migration.ids['b'])['metadata']['operit_tagging_status']=='done'
    enabled=Migration(settings,plan,migration.options)
    try:asyncio.run(enabled.tag_all())
    finally:enabled.close()
    assert len(calls)==3


def test_model_import_and_freeze(setup):
    settings,root,_,migration=setup
    (root/'config.yaml').write_text('dehydration:\n  model: tag-old\n  base_url: http://localhost:99/v1\n  api_key: ${OLD_KEY}\n','utf-8')
    (root/'.env').write_text('OLD_KEY=synthetic-secret\n','utf-8')
    report=import_models(settings.database,root)
    assert 'synthetic-secret' not in json.dumps(report)
    state=read_settings(settings.database)
    assert next(m for m in state['models'] if m['id']=='legacy-operit_tagging')['api_key']=='synthetic-secret'
    assert state['assignments']['operit_tagging']=='test'
    migration.freeze_configuration();migration.freeze_configuration()
    state['models'][0]['model']='different';save_settings(settings.database,{'models':state['models']})
    # The first model may be an unassigned imported model; change the active assignment too.
    save_settings(settings.database,{'assignments':{'operit_tagging':state['models'][0]['id']}})
    with pytest.raises(ValueError,match='续跑'):migration.freeze_configuration()


def test_model_import_reentry_preserves_grouped_legacy_routes(setup):
    settings,root,_,_=setup
    (root/'config.yaml').write_text('dehydration:\n  model: tag-old\n  base_url: http://localhost:99/v1\n  api_key: synthetic-key\n','utf-8')
    import_models(settings.database,root)
    public=read_settings(settings.database,public=True)
    for upstream in public['upstreams']:upstream.pop('api_key_configured')
    save_settings(settings.database,{'models':[],'upstreams':public['upstreams']})
    before=read_settings(settings.database)
    import_models(settings.database,root)
    assert read_settings(settings.database)==before


def test_edges_convert_without_model_and_do_not_repeat(setup,monkeypatch):
    settings,_,_,migration=setup;migration.import_bodies()
    async def forbidden(*args):raise AssertionError('edge conversion must not call a model')
    monkeypatch.setattr('serein.legacy_migration.workflow.complete',forbidden)
    asyncio.run(migration.edges());asyncio.run(migration.edges())
    from serein.compat.germany.scene_linker import SceneEdgeStore
    edges=SceneEdgeStore({'serein_database':str(settings.database)},create=False).list_edges()
    assert edges==[]
    report=migration.report()
    assert report['edge']=={'held':1}
    assert report['edge_records'][0]['original']['relation']=='next_context'
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM scene_relations WHERE active=1').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM legacy_edge_imports').fetchone()[0]==1


def test_legacy_named_env_overrides_are_limited_to_selected_backup(tmp_path,monkeypatch):
    from serein.legacy_migration.models import load_models
    (tmp_path/'config.yaml').write_text('dehydration:\n  model: old-tag\n  base_url: https://old.example/v1\nembedding:\n  model: old-embed\n  base_url: https://old.example/v1\ngateway:\n  upstreams:\n    - name: old\n      base_url: https://provider.example/v1\n      api_key_envs: [MISSING, PROVIDER_KEY]\n      models: [chat]\n','utf-8')
    (tmp_path/'.env').write_text('OMBRE_API_KEY=backup-key\nOMBRE_DEHYDRATION_MODEL=new-tag\nOMBRE_DEHYDRATION_BASE_URL=https://new.example/v1\nOMBRE_EMBEDDING_API_KEY=embedding-key\nPROVIDER_KEY=provider-key\n','utf-8')
    monkeypatch.setenv('OMBRE_API_KEY','host-key-must-not-be-used')
    patch,issues=load_models(tmp_path)
    tag,embedding=patch['models']
    assert tag['api_key']=='backup-key' and tag['model']=='new-tag' and tag['base_url']=='https://new.example/v1'
    assert embedding['api_key']=='embedding-key'
    assert patch['upstreams'][0]['api_key']=='provider-key'
    assert not issues


def test_program_edges_keep_exact_types_and_skip_inactive(setup):
    settings,_,plan,migration=setup;migration.import_bodies()
    plan['edges']=[{'source':'a','target':'b','relation_type':'continues','directionality':'directed'},
                   {'source':'b','target':'a','relation_type':'resolves','directionality':'directed','active':0},
                   {'source':'a','target':'missing','relation':'echoes'},
                   {'source':'a','target':'archived','relation':'echoes'}]
    asyncio.run(migration.edges())
    from serein.compat.germany.scene_linker import SceneEdgeStore
    edges=SceneEdgeStore({'serein_database':str(settings.database)},create=False).list_edges()
    assert len(edges)==1 and edges[0]['relation_type']=='continues'
    assert edges[0]['source']==migration.ids['a'] and edges[0]['target']==migration.ids['b']


@pytest.mark.parametrize('name',[r'..\escape',r'C:\escape','C:escape',r'\\host\escape','../escape','dir/../../escape','CON','file:stream','dir./escape','.complete'])
def test_archive_windows_paths_rejected_everywhere(tmp_path,name):
    archive=tmp_path/'bad.tar'
    with tarfile.open(archive,'w') as tar:
        member=tarfile.TarInfo(name);member.size=1;tar.addfile(member,io.BytesIO(b'x'))
    with pytest.raises(ValueError,match='路径'):unpack(archive,tmp_path/'out')


def test_sqlite_graph_scan_supported_and_corrupt_format_reported(setup):
    _,root,_,_=setup
    with sqlite3.connect(root/'state'/'graph.sqlite') as conn:
        conn.execute('CREATE TABLE scene_edges(source_scene_id TEXT,target_scene_id TEXT,relation_type TEXT,directionality TEXT,active INTEGER)')
        conn.execute("INSERT INTO scene_edges VALUES ('b','a','echoes','symmetric',1)")
    plan=scan(root)
    assert len(plan['edges'])==2 and not plan['errors']
    assert plan['summary']['edge_sources'][1]['format']=='scene_edges'
    with sqlite3.connect(root/'state'/'bad.db') as conn:
        conn.execute('CREATE TABLE scene_edges(wrong TEXT)');conn.execute("INSERT INTO scene_edges VALUES ('x')")
    assert any(e['path']=='state/bad.db' for e in scan(root)['errors'])


def test_exact_text_vector_reuse_and_unverifiable_whole(setup):
    settings,root,plan,migration=setup;migration.import_bodies()
    (root/'config.yaml').write_text('embedding:\n  model: mock\n  base_url: http://127.0.0.1:9/v1\n  query_instruction: search\n','utf-8')
    save_settings(settings.database,{'models':[{'id':'test','query_instruction':'search'}]})
    profile={'model':'mock','dimension':2,'provider_host':'127.0.0.1','document_instruction':'','query_instruction':'search','max_chars':12000}
    with sqlite3.connect(settings.index) as conn:
        conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)",(json.dumps(profile),))
        conn.execute("INSERT INTO settings VALUES ('embedding_dimension','2')")
    text=plan['items'][1]['body']
    # Use the exact cleaned document text, not title/tags/body concatenation.
    item=next(i for i in plan['items'] if i['old_id']=='b');text=item['body']
    with sqlite3.connect(root/'buckets'/'embeddings.db') as conn:
        conn.executescript('CREATE TABLE embeddings(bucket_id TEXT,embedding TEXT);CREATE TABLE scene_embedding_chunks(scene_id TEXT,text TEXT,content_hash TEXT,model TEXT,dimension INTEGER,embedding TEXT);')
        conn.execute('INSERT INTO embeddings VALUES (?,?)',('a','[0,1]'))
        conn.execute('INSERT INTO scene_embedding_chunks VALUES (?,?,?,?,?,?)',('b',text,digest(text),'mock',2,'[0,1]'))
    result=reuse_legacy(settings,profile,plan,migration.ids)
    assert result['whole_reused']==1 and result['unverifiable_whole']==1
    with sqlite3.connect(settings.index) as conn:
        assert conn.execute('SELECT id FROM vectors').fetchone()[0]==migration.ids['b']
    assert reuse_legacy(settings,profile,plan,migration.ids)['whole_reused']==0


@pytest.mark.parametrize('kind','triggers causes precedes context_of same_event next_context previous_context reflects_on evidenced_by contradicts supports promises blocks belongs_to emotional_echo relates_to related_to unknown'.split())
def test_uncertain_types_do_not_invent_a_relation(kind):
    from serein.legacy_migration.edges import conversion
    result,reason=conversion({'relation_type':kind})
    assert result is None and reason


def seed_v1(settings,migration,old,*,active=True,project=True):
    from serein.legacy_migration.edges import initialize_edges,OLD_VERSION
    from serein.compat.germany.scene_linker import _scene_hash
    from serein.compat.scenes import scene_payload
    from serein.core.store import encode
    initialize_edges(settings.database)
    left,right=sorted((migration.ids[old['source']],migration.ids[old['target']]))
    with Store(settings.database) as store:
        values=('early-edge',left,right,'related_to','symmetric',0,'旧记录：'+encode(old),'','',
                _scene_hash(scene_payload(store.read(left))),_scene_hash(scene_payload(store.read(right))),
                'early-proposal',OLD_VERSION,int(active),'2026-01-01','legacy_migration',
                'active' if active else 'cancelled','2026-01-01')
        store.conn.execute("INSERT INTO scene_edges (edge_id,source_scene_id,target_scene_id,relation_type,directionality,confidence,reason,source_evidence,target_evidence,source_hash,target_hash,proposal_id,linker_version,active,accepted_at,accepted_by,lifecycle_status,updated_at) VALUES ("+','.join('?' for _ in values)+')',values)
        if project:
            row=dict(store.conn.execute("SELECT * FROM scene_edges WHERE edge_id='early-edge'").fetchone())
            store.conn.execute('INSERT INTO scene_relations VALUES (?,?,?,?,?,?,?)',
                               ('early-edge','germany_scene_linker',left,right,row['lifecycle_status'],int(active),encode(row)))


def test_updates_reverse_and_hold_are_persisted_without_models(setup,monkeypatch):
    settings,_,plan,migration=setup;migration.import_bodies()
    plan['edges']=[{'source':'b','target':'a','relation_type':'updates'},
                   {'source':'a','target':'b','relation_type':'supports'}]
    async def forbidden(*a,**kw):raise AssertionError('no model calls')
    monkeypatch.setattr('serein.model_runtime.complete',forbidden)
    asyncio.run(migration.edges())
    with Store(settings.database,read_only=True) as store:
        row=store.conn.execute('SELECT * FROM scene_edges WHERE active=1').fetchone()
        assert row['relation_type']=='continues'
        assert row['source_scene_id']==migration.ids['a'] and row['target_scene_id']==migration.ids['b']
        assert row['confidence']==0 and row['source_evidence']==row['target_evidence']==''
        assert store.conn.execute('SELECT count(*) FROM legacy_edge_imports').fetchone()[0]==2
    assert migration.report()['edge']=={'done':1,'held':1}


@pytest.mark.parametrize('project',[True,False])
def test_repair_v1_fallback_without_original_backup_and_repeat(setup,project):
    from serein.legacy_migration.repair import collect,run_repair
    settings,_,_,migration=setup;migration.import_bodies()
    old={'source':'b','target':'a','relation_type':'updates'}
    seed_v1(settings,migration,old,project=project)
    with Store(settings.database,read_only=True) as store:
        before=[tuple(row) for row in store.conn.execute('SELECT * FROM revisions')]
    records,issues=collect(settings.database)
    result=run_repair(settings,records,issues)
    assert result['counts']=={'done':1}
    with sqlite3.connect(result['backup']) as conn:
        assert conn.execute("SELECT active FROM scene_edges WHERE edge_id='early-edge'").fetchone()[0]==1
    with Store(settings.database) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM revisions')]==before
        assert store.conn.execute("SELECT active FROM scene_edges WHERE edge_id='early-edge'").fetchone()[0]==0
        edge=store.conn.execute("SELECT * FROM scene_edges WHERE active=1").fetchone()
        assert edge['relation_type']=='continues' and edge['source_scene_id']==migration.ids['a']
        assert store.conn.execute('SELECT count(*) FROM scene_relations WHERE active=1').fetchone()[0]==1
        # A user's subsequent cancellation must not be undone by re-running.
        store.conn.execute("UPDATE scene_edges SET active=0,lifecycle_status='cancelled' WHERE active=1")
    again=run_repair(settings,*collect(settings.database))
    assert again['counts']=={'repeated':1}
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM scene_edges WHERE active=1').fetchone()[0]==0


@pytest.mark.parametrize('mode',['cancelled','edited','uncertain'])
def test_repair_preserves_cancelled_changed_and_unmappable_records(setup,mode):
    from serein.legacy_migration.repair import collect,run_repair
    settings,_,_,migration=setup;migration.import_bodies()
    old={'source':'b','target':'a','relation_type':'supports' if mode=='uncertain' else 'updates'}
    seed_v1(settings,migration,old,active=mode!='cancelled')
    if mode=='edited':
        with Store(settings.database) as store:
            doc=store.read(migration.ids['a'])
            store.revise(doc['id'],expected_revision=doc['revision'],title=doc['title'],body_md='手工编辑后的正文',metadata=doc['metadata'])
    result=run_repair(settings,*collect(settings.database))
    assert result['counts']==({'skipped':1} if mode=='cancelled' else {'held':1})
    assert result['records'][0]['original']==old
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM scene_edges WHERE active=1').fetchone()[0]==0
        assert store.conn.execute('SELECT count(*) FROM scene_edges').fetchone()[0]==1


def test_repair_missing_edge_file_keeps_existing_scene_ids(setup):
    from serein.legacy_migration.repair import collect,run_repair
    settings,root,plan,migration=setup;migration.import_bodies()
    original_ids=set(migration.ids.values())
    old={'source':'b','target':'a','relation_type':'updates'}
    (root/'state/memory_edges.jsonl').write_text(json.dumps(old)+'\n','utf-8')
    new_plan=scan(root)
    assert new_plan['fingerprint']!=plan['fingerprint']
    records,issues=collect(settings.database,new_plan)
    result=run_repair(settings,records,issues)
    assert result['counts']=={'done':1}
    with Store(settings.database,read_only=True) as store:
        assert {row[0] for row in store.conn.execute("SELECT id FROM documents WHERE kind='scene'")}==original_ids
        edge=store.conn.execute('SELECT * FROM scene_edges').fetchone()
        assert edge['source_scene_id']==migration.ids['a'] and edge['target_scene_id']==migration.ids['b']


def test_repair_does_not_guess_from_legacy_id_alone(setup):
    from serein.legacy_migration.repair import collect,run_repair
    settings,root,_,migration=setup;migration.import_bodies()
    legacy(root,'dynamic','a','另一份内容，不是当时导入的快照。')
    (root/'state/memory_edges.jsonl').write_text(json.dumps({'source':'b','target':'a','relation_type':'updates'})+'\n','utf-8')
    result=run_repair(settings,*collect(settings.database,scan(root)))
    assert result['counts']=={'skipped':1} and result['issues']
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM scene_edges').fetchone()[0]==0


def test_repair_rollback_does_not_retire_v1_on_projection_failure(setup,monkeypatch):
    from serein.legacy_migration.repair import collect,run_repair
    settings,_,_,migration=setup;migration.import_bodies()
    seed_v1(settings,migration,{'source':'b','target':'a','relation_type':'updates'})
    def fail(*a):raise ValueError('synthetic projection failure')
    monkeypatch.setattr('serein.compat.relation_storage.project_relations',fail)
    with pytest.raises(ValueError,match='synthetic'):
        run_repair(settings,*collect(settings.database))
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT active FROM scene_edges WHERE edge_id='early-edge'").fetchone()[0]==1
        assert store.conn.execute('SELECT count(*) FROM scene_edges').fetchone()[0]==1
    from pathlib import Path
    report=json.loads(next((settings.database.parent/'migrations/edge-repair').glob('*/report.json')).read_text('utf-8'))
    assert report['counts']=={'failed':1}


def test_old_done_ledger_does_not_hide_new_conversion(setup):
    settings,_,plan,migration=setup;migration.import_bodies()
    old={'source':'b','target':'a','relation_type':'updates'};plan['edges']=[old]
    from serein.core.store import encode
    migration.mark(digest(encode(old)),'edge','done',{'relation_type':'related_to'})
    asyncio.run(migration.edges())
    assert migration.report()['edge_records'][0]['relation_type']=='continues'



def test_repair_cli_preview_is_read_only_and_apply_is_edge_only(setup,monkeypatch,capsys,tmp_path):
    import sys
    from serein.legacy_migration.__main__ import main
    settings,root,_,migration=setup;migration.import_bodies()
    (root/'state/memory_edges.jsonl').write_text(json.dumps({'source':'b','target':'a','relation_type':'updates'})+'\n','utf-8')
    config=tmp_path/'config.toml'
    config.write_text('[storage]\ndatabase='+json.dumps(settings.database.as_posix())+'\nindex='+json.dumps(settings.index.as_posix())+'\n[runtime]\nwritable=true\n','utf-8')
    args=['migration','--config',str(config),'repair-edges',str(root)]
    monkeypatch.setattr(sys,'argv',args);main()
    assert '当前仅预览' in capsys.readouterr().out
    with Store(settings.database,read_only=True) as store:
        assert not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_edge_imports'").fetchone()
        before=[tuple(row) for row in store.conn.execute('SELECT * FROM revisions')]
    monkeypatch.setattr(sys,'argv',args+['--apply']);main()
    output=capsys.readouterr().out
    assert '"done": 1' in output and 'before-edge-repair.db' in output and 'report.json' in output
    with Store(settings.database,read_only=True) as store:
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM revisions')]==before
        assert store.conn.execute('SELECT relation_type FROM scene_edges WHERE active=1').fetchone()[0]=='continues'


def test_repair_preserves_an_existing_reviewed_target(setup):
    from serein.legacy_migration.repair import collect,run_repair
    from serein.legacy_migration.edges import initialize_edges,save_edge
    from serein.compat.scenes import scene_payload
    settings,_,_,migration=setup;migration.import_bodies()
    old={'source':'b','target':'a','relation_type':'updates'}
    initialize_edges(settings.database)
    with Store(settings.database,read_only=True) as store:
        source,target=(scene_payload(store.read(migration.ids[k])) for k in ('b','a'))
    # Same endpoints/type already belong to another reviewed relation.
    saved=save_edge(settings.database,source,target,{**old,'reason':'earlier independent relation'})
    with Store(settings.database) as store:
        store.conn.execute("UPDATE scene_edges SET linker_version='scene-linker-v2',accepted_by='user',source_evidence=?,target_evidence=?,confidence=.9 WHERE edge_id=?",
                           (target['content'],source['content'],saved['edge_id']))
        expected=tuple(store.conn.execute('SELECT * FROM scene_edges').fetchone())
    seed_v1(settings,migration,old)
    result=run_repair(settings,*collect(settings.database))
    assert any(r['reason']=='目标关系已存在，保留其审核或停用状态' for r in result['records'])
    with Store(settings.database,read_only=True) as store:
        assert tuple(store.conn.execute('SELECT * FROM scene_edges WHERE edge_id=?',(saved['edge_id'],)).fetchone())==expected
        assert store.conn.execute("SELECT active FROM scene_edges WHERE edge_id='early-edge'").fetchone()[0]==0


def test_unprojected_v1_batch_does_not_block_each_other(setup):
    from serein.legacy_migration.repair import collect,run_repair
    settings,_,_,migration=setup;migration.import_bodies()
    seed_v1(settings,migration,{'source':'b','target':'a','relation_type':'updates'},project=False)
    with Store(settings.database) as store:
        # A separate original that the early importer had not projected either.
        row=dict(store.conn.execute('SELECT * FROM scene_edges').fetchone())
        row.update(edge_id='another-old',proposal_id='another-proposal',reason='旧记录：'+json.dumps({'source':'a','target':'b','relation_type':'supports'}))
        store.conn.execute('INSERT INTO scene_edges ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    result=run_repair(settings,*collect(settings.database))
    assert result['counts']=={'done':1,'held':1}
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM scene_relations WHERE active=1').fetchone()[0]==1


def test_migration_owns_tagging_and_missing_edges_only_warn(setup,monkeypatch):
    from serein.import_tagging import process
    settings,root,plan,migration=setup
    migration.import_bodies()
    calls=[]
    async def unexpected(*args,**kwargs):calls.append(args);raise AssertionError('duplicate paid call')
    monkeypatch.setattr('serein.model_runtime.complete',unexpected)
    asyncio.run(process(settings.database))
    assert not calls
    with Store(settings.database) as store:
        assert store.conn.execute('SELECT count(*) FROM import_tag_jobs').fetchone()[0]==0
    (root/'state'/'memory_edges.jsonl').unlink()
    scanned=scan(root)
    assert not scanned['errors'] and scanned['summary']['scenes']==plan['summary']['scenes']
    assert scanned['summary']['old_edges']==0 and 'Docker' in scanned['summary']['edge_warning']

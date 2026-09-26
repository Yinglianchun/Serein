from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.compat.diaries import Diaries
from serein.compat.events import Events
from serein.compat.scenes import Scenes, initialize_scene_ids
from serein.config import Settings
from serein.core import Store
from serein.core.reader import Reader
from serein.recall.index import build_index
from test_live_events import item


@pytest.fixture
def live(tmp_path):
    settings=Settings(tmp_path/'serein.db',tmp_path/'index.sqlite',writable=True)
    with Store(settings.database):
        pass
    Events(settings.database,initialize=True)
    Diaries(settings.database,initialize=True)
    initialize_scene_ids(settings.database)
    build_index(settings.database,settings.index)
    with TestClient(create_app(settings,token='test',live=True),headers={'Authorization':'Bearer test'}) as client:
        yield settings,client


def test_settlement_is_visible_to_frontend_and_continuity_picker(live):
    settings,client=live
    body={'operation_id':'test_batch','items':[item()]}
    response=client.post('/api/fact-events/settlement',json=body)
    assert response.status_code==200,response.text
    key=response.json()['items'][0]['item_id']
    listed=client.get('/api/fact-events?type=event&status=active&include_sources=1&limit=500&offset=0').json()
    assert listed['count']==1
    assert listed['items'][0]['source_refs'][0]['message_id']=='7'
    read=client.post('/api/fact-events/read-many',json={'item_ids':[key],'include_sources':True,'resolve_active_successors':True})
    assert read.status_code==200,read.text
    assert read.json()['resolved_items'][0]['item_id']==key
    assert client.get('/v1/memories/'+key).json()['document']['body_md']==item()['body']
    assert client.post('/api/fact-events/settlement',json=body).json()['items'][0]['item_id']==key


def test_diary_save_edit_comment_delete_and_auth(live):
    settings,client=live
    created=client.post('/diaries',json={'content':'雨天日记','date':'2026-09-07','author':'user','title':'下雨了'})
    assert created.status_code==200,created.text
    key=created.json()['id']
    original=client.get(f'/diaries/{key}').json()
    response=client.put(f'/diaries/{key}',json={'content':'修改后的雨天日记','author':'ai'})
    assert response.status_code==200,response.text
    current=client.get(f'/diaries/{key}').json()
    assert current['author']=='user'
    assert current['created_at']==original['created_at']
    assert current['content']=='修改后的雨天日记'
    comment=client.post(f'/diaries/{key}/comments',json={'content':'一起看雨'}).json()
    with Reader(settings.database) as reader:
        obj=reader.read(f'diary:{key}')
        assert obj['document']['body_md']==current['content']
        assert obj['comments'][0]['content']=='一起看雨'
    assert client.delete(f'/diaries/{key}/comments/{comment["id"]}').status_code==200
    with Reader(settings.database) as reader:
        assert reader.read(f'diary:{key}')['comments']==[]
    assert client.delete(f'/diaries/{key}').status_code==200
    assert client.get(f'/v1/memories/diary:{key}').json()['readable'] is False
    client.headers.pop('Authorization')
    assert client.post('/diaries',json={'content':'unauthorized'}).status_code==401


def test_locked_diary_does_not_expose_body(live):
    settings,client=live
    created=client.post('/diaries',json={'content':'尚未解锁的正文','date':'2026-09-07','unlock_at':'2099-01-01T00:00:00+08:00'})
    assert created.status_code==200,created.text
    key=created.json()['id']
    assert '尚未解锁的正文' not in client.get(f'/diaries/{key}').text
    assert client.get(f'/v1/memories/diary:{key}').json()['readable'] is False
    assert client.put(f'/diaries/{key}',json={'content':'overwrite'}).status_code==423


@pytest.mark.parametrize('as_list',[False,True])
def test_scene_semicolon_cues_are_split_on_write_and_edit(live,as_list):
    settings,client=live
    # The combined value exceeds 80 characters; each separate cue is valid.
    first='窗边的雨声'*8
    second='一起看雨'*12
    cues=f' {first};； {second};{first}； '
    saved=client.post('/v1/tools/call',json={'name':'write_scene','arguments':{
        'content':'一起坐在窗边看雨','cues':[cues] if as_list else cues}})
    assert saved.status_code==200,saved.text
    key=saved.json()['result'].split('[scene_id:')[1].split(']')[0]
    scene=Scenes(settings.database).read(key)
    assert scene['metadata']['scene_cues']==[first,second]
    cues=' 听雨；窗边 ;听雨； '
    changed=client.post('/v1/tools/call',json={'name':'edit_scene','arguments':{
        'scene_id':key,'expected_updated_at':scene['metadata']['updated_at'],
        'cues':[cues] if as_list else cues}})
    assert changed.status_code==200,changed.text
    assert Scenes(settings.database).read(key)['metadata']['scene_cues']==['听雨','窗边']


@pytest.mark.parametrize('cues',[';；', ';'.join(str(i) for i in range(9)), 'a'*81+'；b'])
def test_scene_semicolon_cues_still_enforce_limits(cues):
    with pytest.raises(ValueError):
        Scenes._cues([cues])


def test_scene_edit_evidence_and_handoff_picker_use_current_canonical(live):
    settings,client=live
    event=client.post('/api/fact-events/batch',json={'items':[item()]}).json()['items'][0]['item_id']
    def tool(name,arguments):
        response=client.post('/v1/tools/call',json={'name':name,'arguments':arguments})
        assert response.status_code==200,response.text
        return response.json()['result']
    saved=tool('write_scene',{'title':'雨天','content':'一起坐在窗边看雨','cues':['一起看雨'],'date':'2026-09-07','evidence_refs':item()['source_refs']})
    key=saved.split('[scene_id:')[1].split(']')[0]
    assert client.get('/v1/memories/'+event).json()['surface_state']['can_surface'] is False
    listed=client.get('/api/fact-events?type=event').json()['items'][0]
    assert listed['recallable'] is True
    assert 'covered_by_scene' in listed['surface_state']['reasons']
    old=tool('read_memory',{'memory_type':'scene','memory_id':key})
    changed=tool('edit_scene',{'scene_id':key,'expected_updated_at':old['metadata']['updated_at'],'content':'窗边一起听雨声'})
    assert changed['status']=='updated'
    assert tool('edit_scene',{'scene_id':key,'expected_updated_at':old['metadata']['updated_at'],'content':'过期覆盖'})['status']=='conflict'
    rows=client.get('/api/handoff-scenes').json()['items']
    assert rows[0]['content']=='窗边一起听雨声'
    assert rows[0]['source_message_ids']==['7']
    proof=tool('read_scene_evidence',{'scene_id':key})['evidence_refs'][0]
    assert type(proof['id']) is int
    assert tool('unbind_scene_evidence',{'scene_id':key,'evidence_ids':[proof['id']]})['status']=='unbound'
    assert client.get('/v1/memories/'+event).json()['surface_state']['can_surface'] is True
    current=tool('read_memory',{'memory_type':'scene','memory_id':key})
    tool('set_scene_status',{'scene_id':key,'expected_updated_at':current['metadata']['updated_at'],'status':'archived'})
    assert client.get('/api/handoff-scenes').json()['items']==[]
    archived=tool('read_memory',{'memory_type':'scene','memory_id':key})
    assert archived['content']=='窗边一起听雨声'
    restored=tool('set_scene_status',{'scene_id':key,'expected_updated_at':archived['metadata']['updated_at'],'status':'active'})
    assert restored['status']=='updated'
    assert client.get('/api/handoff-scenes').json()['items'][0]['id']==key
    for status in ('archived','active'):
        response=client.post('/api/fact-events/status',json={'item_id':event,'status':status})
        assert response.status_code==200,response.text
        assert response.json()['item']['status']==status
        current=client.get('/v1/memories/'+event).json()
        assert current['status']==status and current['document']['body_md']==item()['body']
    deleted=client.post('/api/buckets/delete',json={'bucket_ids':[key],'confirm':'DELETE'})
    assert deleted.status_code==200 and deleted.json()['deleted']==1
    assert client.post('/api/serein/memory-projection',json={}).json()['scenes']==[]
    assert client.get('/v1/memories/'+key).json()['readable'] is False
    deleted=client.post('/api/fact-events/delete',json={'item_id':event})
    assert deleted.status_code==200 and event in deleted.json()['item_ids']
    assert client.get('/api/fact-events?type=event&status=all').json()['items']==[]
    assert client.get('/v1/memories/'+event).json()['readable'] is False

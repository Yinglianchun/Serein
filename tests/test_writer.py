import json

import pytest

from serein.application import Application
from serein.config import Settings
from serein.core import Store, Conflict
from serein.core.search import build_index


@pytest.fixture
def writable(tmp_path):
    database, index = tmp_path / "runtime.db", tmp_path / "index.db"
    with Store(database):
        pass
    build_index(database, index)
    return Application(Settings(database, index, writable=True)).services


def event(body="雨天归航"):
    return {"kind": "event", "title": "归航", "body_md": body,
            "sources": [{"source_key": "message:1", "content": "原文证据"}]}


def test_atomic_save_retry_recall_and_stale_revision(writable):
    result = writable.write("save-1", "save", event())
    assert result["index"]["status"] == "current"
    assert writable.write("save-1", "save", event())["id"] == result["id"]
    assert writable.read(result["id"])["evidence"][0]["content"] == "原文证据"
    assert writable.recall("归航")["pools"]["event"]["items"][0]["id"] == result["id"]
    with pytest.raises(Conflict):
        writable.write("save-1", "save", event("different"))
    updated = writable.write("edit-1", "save", {**event("新雪落下"), "document_id": result["id"], "expected_revision": 1})
    assert updated["revision"] == 2
    with pytest.raises(Conflict):
        writable.write("edit-stale", "save", {**event(), "document_id": result["id"], "expected_revision": 1})
    assert writable.search("新雪")["items"][0]["id"] == result["id"]


def test_invalid_event_rolls_back_body_sources_and_receipt(writable):
    with pytest.raises(ValueError, match="evidence"):
        writable.write("no-source", "save", {**event(), "sources": []})
    with Store(writable._settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert store.conn.execute("SELECT count(*) FROM write_receipts").fetchone()[0] == 0


def test_bindings_suppress_without_archiving_and_last_event_source_cannot_vanish(writable):
    saved = writable.write("e", "save", event())
    scene = writable.write("s", "save", {**event(), "kind": "scene"})
    current = writable.read(saved["id"])
    assert current["status"] == "active"
    assert current["surface_state"]["reasons"] == ["covered_by_scene"]
    binding = writable.read(scene["id"])["evidence"][0]["binding_id"]
    writable.write("unbind-s", "evidence", {"document_id": scene["id"], "expected_revision": 1, "unbind": [binding]})
    assert writable.read(saved["id"])["surface_state"]["can_surface"]
    with pytest.raises(ValueError, match="retain"):
        writable.write("unbind-e", "evidence", {"document_id": saved["id"], "expected_revision": 1,
                                                "unbind": [current["evidence"][0]["binding_id"]]})
    assert writable.read(saved["id"])["evidence"]


def test_promote_event_creates_edited_scene_and_keeps_original_evidence(writable):
    from serein.deployment import save_settings
    save_settings(writable._settings.database, {"features": {"event_to_scene": True}})
    event = writable.write('event-original', 'save', {"kind": "event", "title": "出行摘要",
        "body_md": "我们聊了第一次出发。", "sources": [
            {"source_key": "message:outbound", "content": "她说想记住第一次出发"},
            {"source_key": "message:ticket", "content": "我说会把车票收好"}]})
    request = {"event_id": event["id"], "expected_revision": 1,
               "title": "留下的车票", "body_md": "她留下车票，我也想记住那天。"}
    writable.write('choose-event','mailbox_select',{'event_id':event['id'],'expected_revision':1,'expected_queue_revision':0})
    request['expected_queue_revision']=1
    promoted = writable.write('promote-once', 'promote_event', request)
    again = writable.write('promote-once', 'promote_event', request)
    assert promoted['id'] == again['id']
    scene = writable.read(promoted['id'])
    original = writable.read(event['id'])
    assert scene['document']['body_md'] == request['body_md']
    assert scene['document']['metadata']['promoted_from_event']['id'] == event['id']
    assert {row['source_id'] for row in scene['evidence']} == {row['source_id'] for row in original['evidence']}
    assert original['document']['body_md'] == "我们聊了第一次出发。"
    assert original['status'] == 'active'
    assert original['surface_state']['reasons'] == ['promoted_to_scene', 'covered_by_scene']
    assert promoted['event_surface']['reasons'] == ['promoted_to_scene', 'covered_by_scene']
    assert scene['surface_state']['can_surface']
    with pytest.raises(Conflict, match='already has'):
        writable.write('promote-again', 'promote_event', request)
    assert len(writable.search('车票')['items']) == 1
    writable.write('archive-scene', 'state', {"document_id": promoted['id'],
        "expected_revision": 1, "lifecycle": "archived"})
    assert writable.read(event['id'])['surface_state']['reasons'] == ['promoted_to_scene']


def test_promote_event_rejects_stale_revision_without_creating_scene(writable):
    from serein.deployment import save_settings
    save_settings(writable._settings.database, {"features": {"event_to_scene": True}})
    event = writable.write('event-original', 'save', {"kind": "event", "title": "摘要",
        "body_md": "当时说了什么", "sources": [{"source_key": "source:1", "content": "原话"}]})
    writable.write('event-edit', 'save', {"kind": "event", "document_id": event['id'],
        "expected_revision": 1, "title": "更正摘要", "body_md": "更正后的摘要"})
    with pytest.raises(Conflict):
        writable.write('stale-promotion', 'promote_event', {"event_id": event['id'],
            "expected_revision": 1, "title": "Scene", "body_md": "改写"})
    with Store(writable._settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='scene'").fetchone()[0] == 0
        assert store.conn.execute("SELECT count(*) FROM write_receipts WHERE operation_id='stale-promotion'").fetchone()[0] == 0


def test_promotion_disabled_blocks_direct_and_cached_calls_and_keeps_receipts(writable):
    from serein.deployment import save_settings
    from serein.extensions.optional import tools_for
    event_id = writable.write('source-event', 'save', event())['id']
    request = {'event_id':event_id, 'expected_revision':1, 'title':'Scene', 'body_md':'Edited body'}
    with pytest.raises(ValueError, match='disabled'):
        writable.write('promote', 'promote_event', request)
    save_settings(writable._settings.database, {'features':{'event_to_scene':True}})
    writable.write('choose-source','mailbox_select',{'event_id':event_id,'expected_revision':1,'expected_queue_revision':0})
    request={'candidate_id':event_id,'title':'Scene','body':'Edited body','cues':'cue'}
    cached = tools_for(writable._settings)['promote_event_to_scene']
    scene_id = cached(**request)['id']
    save_settings(writable._settings.database, {'features':{'event_to_scene':False}})
    for operation in ('promote', 'another-operation'):
        with pytest.raises(ValueError, match='disabled'):
            cached(**request)
    assert writable.read(scene_id)['document']['body_md'] == 'Edited body'
    save_settings(writable._settings.database, {'features':{'event_to_scene':True}})
    assert cached(**request)['id'] == scene_id


def test_candidate_acceptance_is_frozen_atomic_and_retry_safe(writable):
    candidate = writable.write("propose", "propose", event())
    assert not writable.search("归航")["items"]
    assert writable.candidates()["items"][0]["request"] == event()
    accepted = writable.write("accept", "review", {"candidate_id": candidate["id"], "decision": "accept"})
    assert accepted["status"] == "accepted"
    assert not writable.candidates()["items"]
    assert writable.write("accept", "review", {"candidate_id": candidate["id"], "decision": "accept"})["document"] == accepted["document"]
    assert writable.search("归航")["items"][0]["id"] == accepted["document"]["id"]


def test_replacement_and_soft_delete_keep_history_but_hide_body(writable):
    old = writable.write("old", "save", event())
    new = writable.write("new", "save", {**event(), "replaces": [{"id": old["id"], "revision": 1}]})
    assert writable.read(old["id"])["status"] == "superseded"
    assert {i["id"] for i in writable.search("归航")["items"]} == {new["id"]}
    writable.write("delete", "state", {"document_id": new["id"], "expected_revision": 1, "lifecycle": "deleted"})
    assert writable.read(new["id"], revision=1)["document"] is None
    assert not writable.search("归航")["items"]


def test_failed_index_refresh_does_not_lose_committed_write(tmp_path):
    database, index = tmp_path / "runtime.db", tmp_path / "missing-index.db"
    with Store(database):
        pass
    services = Application(Settings(database, index, writable=True)).services
    saved = services.write("saved", "save", event())
    assert saved["index"]["status"] == "pending"
    assert services.read(saved["id"])["readable"]
    build_index(database, index)
    assert services.sync_index()["status"] == "current"
    assert services.search("归航")["items"][0]["id"] == saved["id"]


def test_diary_authors_comments_revisions_delete_and_darkroom_lock(writable):
    draft = {"kind": "diary", "author": "ai", "day": "2026-09-06", "body_md": "我们的日记"}
    diary = writable.write("diary", "diary_save", draft)
    writable.write("comment", "diary_comment", {"entry_id": int(diary["id"]), "author": "user", "body_md": "我的评论"})
    read = writable.read("diary:" + diary["id"])
    assert read["document"]["author"] == "ai" and read["comments"][0]["author"] == "user"
    writable.write("revise", "diary_save", {**draft, "entry_id": int(diary["id"]), "expected_revision": 1, "body_md": "补全正文"})
    assert writable.read("diary:" + diary["id"], revision=1)["document"]["body_md"] == "我们的日记"
    writable.write("delete", "diary_delete", {"entry_id": int(diary["id"]), "expected_revision": 2})
    assert not writable.read("diary:" + diary["id"])["readable"]
    dark = writable.write("dark", "diary_save", {**draft, "kind": "darkroom", "unlock_at": "2099-01-01T00:00:00+08:00"})
    assert writable.read("darkroom:" + dark["id"])["status"] == "locked"
    with pytest.raises(Conflict):
        writable.write("locked-comment", "diary_comment", {"entry_id": int(dark["id"]), "author": "user", "body_md": "hidden"})


def test_read_only_profile_rejects_write(writable):
    services = Application(Settings(writable._settings.database, writable._settings.index)).services
    with pytest.raises(ValueError, match="read-only"):
        services.write("forbidden", "save", event())

@pytest.mark.parametrize('kind', ['event','scene'])
def test_favorite_save_and_existing_state_are_atomic_and_retry_safe(writable,kind):
    from serein.deployment import save_settings
    from serein.core.personal import Personal
    db=writable._settings.database;personal=Personal(db)
    save_settings(db,{'features':{'favorites':True}})
    saved=writable.write('favorite-save','save',{**event(),'kind':kind,'favorite':True})
    assert saved['favorite'] is True and saved['favorite_revision']==1
    assert personal.read_favorites()['items'][0]['id']==saved['id']
    before=writable.read(saved['id'])
    args={'document_id':saved['id'],'expected_revision':1,'favorite':False}
    changed=writable.write('favorite-off','state',args)
    assert changed['revision']==1 and changed['favorite'] is False
    assert writable.read(saved['id'])==before  # No body/evidence/surface/injection changes.
    personal.save('favorite',saved['id'],{'favorite':True},document_id=saved['id'],expected_revision=2)
    assert writable.write('favorite-off','state',args)['favorite'] is False  # Original receipt.
    assert personal.list('favorite')['items'][0]['value']['favorite'] is True
    edit=writable.write('body-edit','save',{**event('Revised text'),'kind':kind,'document_id':saved['id'],'expected_revision':1})
    assert edit['revision']==2 and personal.list('favorite')['items'][0]['value']['favorite'] is True
    with pytest.raises(Conflict):writable.write('stale-favorite','state',args)
    archived=writable.write('archive-favorite','state',{'document_id':saved['id'],'expected_revision':2,'lifecycle':'archived','favorite':False})
    assert archived['revision']==3
    writable.write('archive-on','state',{'document_id':saved['id'],'expected_revision':3,'favorite':True})
    assert personal.read_favorites(include_archived=True)['items'][0]['status']=='archived'


def test_favorite_validation_rolls_back_and_switch_preserves_ordinary_writes(writable):
    from serein.deployment import save_settings
    from serein.core.personal import Personal
    db=writable._settings.database
    with pytest.raises(ValueError,match='disabled'):writable.write('disabled','save',{**event(),'favorite':True})
    save_settings(db,{'features':{'favorites':True}})
    for i,draft in enumerate([{**event(),'favorite':'yes'},{**event(),'favorite':1},{**event(),'kind':'narrative','favorite':True},{**event(),'sources':[],'favorite':True}]):
        with pytest.raises(ValueError):writable.write('invalid-'+str(i),'save',draft)
    with Store(db,read_only=True) as store:
        for table in ['documents','personal_records','write_receipts','sources']:
            assert store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
    saved=writable.write('plain','save',event())
    with pytest.raises(ValueError):writable.write('delete-favorite','state',{'document_id':saved['id'],'expected_revision':1,'lifecycle':'deleted','favorite':True})
    assert writable.read(saved['id'])['status']=='active'
    save_settings(db,{'features':{'favorites':False}})
    with pytest.raises(ValueError,match='disabled'):writable.write('off','state',{'document_id':saved['id'],'expected_revision':1,'favorite':False})
    writable.write('ordinary-edit','save',{**event('Ordinary edit'),'document_id':saved['id'],'expected_revision':1})
    Personal(db).save('favorite',saved['id'],{'favorite':True},document_id=saved['id'])
    assert Personal(db).read_favorites()['items']  # UI remains independent.


def test_candidate_favorite_waits_for_accept_and_rechecks_feature(writable):
    from serein.deployment import save_settings
    from serein.core.personal import Personal
    db=writable._settings.database
    save_settings(db,{'features':{'favorites':True}})
    candidate=writable.write('propose-favorite','propose',{**event(),'favorite':True})
    assert not Personal(db).list('favorite')['items']
    save_settings(db,{'features':{'favorites':False}})
    accept={'candidate_id':candidate['id'],'decision':'accept'}
    with pytest.raises(ValueError,match='disabled'):writable.write('accept-favorite','review',accept)
    assert writable.candidates()['items'][0]['status']=='pending'
    save_settings(db,{'features':{'favorites':True}})
    result=writable.write('accept-favorite','review',accept)
    assert result['document']['favorite'] is True

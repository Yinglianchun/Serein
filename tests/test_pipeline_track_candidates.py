import json
from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_public_features import settings, ingest
from serein.api.settings import PipelinePatch
from serein.core.store import encode
from serein.deployment import read_settings, save_settings
from serein.extensions import pipeline as p
from serein.extensions.pipeline_track_candidates import select, tokens


def card(key, text, time='2026-10-01T00:00:00Z', **extra):
    return {'track_id':key,'subject':text,'throughline':'进度待验证','status':'parked',
            'recent_turns':[{'text':text,'created_at':time}], **extra}


def test_direct_window_retrieval_dedup_and_complete_originals():
    cards=[card('recent','数据库','2026-10-03T23:00:00Z'),
           card('old','检修数据库'),card('other','布达佩斯电影')]
    original=deepcopy(cards)
    messages=[{'content':'哦哦哦 感觉 可以，数据库终于修好了','created_at':'2026-10-04T00:00:00Z'}]
    assert select(cards,messages,[],{})==cards
    result=select(cards,messages,[],{'track_candidates_enabled':True,'track_candidate_limit':1})
    assert [c['track_id'] for c in result]==['old','recent']
    assert cards==original and result[0]['recent_turns']==original[1]['recent_turns']
    assert not set(tokens('哦哦哦哦 感觉 可以'))
    assert '数据库' in tokens('数据库')


def test_whole_block_nearby_context_and_all_card_fields_are_searched():
    cards=[card('clue','记录',throughline='修理数据库'),
           card('original','笔记',recent_turns=[{'text':'明信片','created_at':'2026-10-01T00:00:00Z'}]),
           card('noise','不相关')]
    messages=[{'text':'昨天那个好了','created_at':'2026-10-04T00:00:00Z'},
              {'text':'就是数据库那个','created_at':'2026-10-04T00:01:00Z'}]
    result=select(cards,messages,[{'text':'明信片'}],{'track_candidates_enabled':True})
    assert {c['track_id'] for c in result}=={'clue','original'}


def test_direct_boundary_and_unknown_timestamps_do_not_drop_cards():
    cards=[card('boundary','无关联','2026-10-03T12:00:00Z'),
           card('outside','另件事','2026-10-03T11:59:59Z'),card('unknown','未知','invalid')]
    messages=[{'text':'哎','created_at':'2026-10-04T00:00:00Z'}]
    result=select(cards,messages,[],{'track_candidates_enabled':True})
    assert {c['track_id'] for c in result}=={'boundary','unknown'}
    assert select(cards,[{'text':'哎'}],[],{'track_candidates_enabled':True})==cards


@pytest.mark.parametrize('values',[{'track_candidate_limit':0},{'track_candidate_limit':True},
                                  {'track_direct_hours':13},{'track_candidates_enabled':'yes'}])
def test_invalid_settings_are_rejected(values):
    with pytest.raises(ValidationError):PipelinePatch(**values)


def test_request_uses_frozen_opt_in_policy_and_keeps_all_stored_cards(settings):
    assert read_settings(settings.database)['pipeline']['track_candidates_enabled'] is False
    save_settings(settings.database,{'pipeline':{'track_candidates_enabled':True,'track_candidate_limit':1}})
    ingest(settings);p.initialize(settings.database)
    batch=p.new_batch(settings.database,True)
    data=json.loads(batch['input_json'])
    # All unmatched older cards are still retained in the batch state.
    data['tracks']=[card('unmatched','不相关','2000-01-01T00:00:00Z')]
    batch={**batch,'input_json':encode(data)}
    request=p.request_for(settings.database,batch,'track_router')
    assert request['active_tracks']==[] and len(data['tracks'])==1
    save_settings(settings.database,{'pipeline':{'track_candidates_enabled':False}})
    assert p.request_for(settings.database,batch,'track_router')['active_tracks']==[]

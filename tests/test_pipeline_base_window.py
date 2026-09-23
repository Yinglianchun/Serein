"""Track lookback settings and the self-use active-leaf fail-closed boundary."""
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from serein.api.settings import PipelinePatch
from serein.extensions import pipeline


@pytest.fixture
def catalog(monkeypatch):
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.executescript('''
        CREATE TABLE fact_events(item_id TEXT PRIMARY KEY,status TEXT,created_at TEXT,updated_at TEXT,
            supersedes_item_id TEXT NOT NULL DEFAULT '');
        CREATE TABLE pipeline_track_events(track_id TEXT,event_id TEXT);
        CREATE TABLE fact_event_sources(id INTEGER PRIMARY KEY,item_id TEXT,source_system TEXT,session_id TEXT,
            message_id TEXT,role TEXT,content TEXT,created_at TEXT);
        CREATE TABLE raw_events(id INTEGER PRIMARY KEY,source TEXT,session_id TEXT,source_event_id TEXT);
        CREATE TABLE pipeline_event_details(event_id TEXT,details_json TEXT);
    ''')
    class ReadStore:
        def __init__(self,database,*,read_only=False):
            assert read_only
        def __enter__(self):return SimpleNamespace(conn=conn)
        def __exit__(self,*args):return False
    monkeypatch.setattr(pipeline,'Store',ReadStore)
    monkeypatch.setattr(pipeline,'reference_blockers',lambda conn,key:[])
    def add(key,*,status='active',track='track',created='2020-01-01T00:00:00Z'):
        conn.execute('INSERT INTO fact_events VALUES (?,?,?,?,?)',(key,status,created,created,''))
        conn.execute('INSERT INTO pipeline_track_events VALUES (?,?)',(track,key))
        conn.execute('INSERT INTO fact_event_sources(item_id,source_system,session_id,message_id,role,content,created_at) '
                     'VALUES (?,\'synthetic\',\'window\',?,\'user\',?,?)',
                     (key,key,'complete original '+key,created))
    yield conn,add
    conn.close()


@pytest.mark.parametrize('days',[1,3,7,30,365])
def test_track_lookback_setting(days):
    assert PipelinePatch(track_lookback_days=days).track_lookback_days==days


@pytest.mark.parametrize('days',[0,-1,366,True,3.5,'3'])
def test_invalid_track_lookback_is_rejected(days):
    with pytest.raises(ValidationError):PipelinePatch(track_lookback_days=days)


def test_active_events_do_not_expire_by_creation_time(catalog):
    _,add=catalog
    add('old',created='2020-01-01T00:00:00Z')
    add('new',created='2026-09-23T00:00:00Z')
    rows=pipeline.candidates('unused',['track'])
    assert [row['event_id'] for row in rows]==['old','new']
    assert [row['originals'][0]['content'] for row in rows]==[
        'complete original old','complete original new']


def test_only_active_leaves_on_the_requested_track_are_loaded(catalog):
    _,add=catalog
    add('active')
    add('archived',status='archived')
    add('foreign',track='other')
    assert [row['event_id'] for row in pipeline.candidates('unused',['track'])]==['active']


def test_eight_active_leaves_are_complete_candidates(catalog):
    _,add=catalog
    for index in range(pipeline.EVENT_CURATOR_MAX_ACTIVE_LEAVES_PER_TRACK):
        add(f'event-{index}')
    overflow=[]
    rows=pipeline.candidates('unused',['track'],overflow_out=overflow)
    assert len(rows)==pipeline.EVENT_CURATOR_MAX_ACTIVE_LEAVES_PER_TRACK
    assert not overflow
    assert all(row['originals'] for row in rows)


def test_ninth_active_leaf_fails_closed_before_reading_sources(catalog):
    conn,add=catalog
    for index in range(pipeline.EVENT_CURATOR_MAX_ACTIVE_LEAVES_PER_TRACK+1):
        add(f'event-{index}')
    calls=[]
    conn.set_trace_callback(calls.append)
    overflow=[]
    assert pipeline.candidates('unused',['track'],overflow_out=overflow)==[]
    assert overflow==[{
        'track_id':'track',
        'eligible_active_leaf_count':9,
        'limit':pipeline.EVENT_CURATOR_MAX_ACTIVE_LEAVES_PER_TRACK,
        'event_ids':[f'event-{index}' for index in range(9)],
    }]
    assert not any('FROM fact_event_sources' in sql or 'FROM raw_events' in sql for sql in calls)
    with pytest.raises(ValueError,match='bounded active Event leaf limit'):
        pipeline.candidates('unused',['track'])


def test_component_overflow_defers_every_stable_source_without_model_materials(catalog,monkeypatch):
    _,add=catalog
    for index in range(pipeline.EVENT_CURATOR_MAX_ACTIVE_LEAVES_PER_TRACK+1):
        add(f'event-{index}')
    message={'id':1,'source':'test','source_event_id':'1','original_session_id':'session',
             'session_id':1,'role':'user','content':'new work','created_at':'2026-09-23T00:00:00Z',
             'metadata':{}}
    card={'track_id':'track','subject':'Build','throughline':'Continue build',
          'event_policy':'rolling_engineering','status':'active'}
    assignment={'source_message_id':1,'primary_track_id':'track','context_track_ids':[],
                'routing_role':'primary_activity'}
    monkeypatch.setattr(pipeline,'route_result',lambda data,routed:([assignment],[card],1))
    data={'messages':[message],'routing_messages':[message]}
    component=pipeline.components('unused',data,{})[0]
    assert component['base_event_candidates']==[]
    assert component['context_messages']==[message]
    assert component['base_event_candidate_overflow'][0]['eligible_active_leaf_count']==9
    plan=pipeline.overflow_plan(component)
    assert plan['events']==[] and plan['defer_source_message_ids']==[1]
    assert plan['host_deferrals']==component['base_event_candidate_overflow']

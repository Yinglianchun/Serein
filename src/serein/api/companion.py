"""Read-only Persona and human memo management, independent of chat switches."""
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..compat.memo_store import ReminderStore
from ..compat.persona_engine import GATEWAY_PERSONA_SESSION, PersonaStateEngine
from ..deployment import read_settings, task_model

class MemoEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=12000)
    repeat_rule: Literal['once','none','every_n_rounds','daily','morning_evening'] = 'every_n_rounds'
    interval_rounds: int = Field(default=6, ge=0, le=10000)
    daily_limit: int | None = Field(default=None, ge=0, le=10000)
    start_at: str = ''
    end_at: str = ''
    next_due_at: str = ''
    cooldown_minutes: int = Field(default=0, ge=0, le=525600)
    max_injections: int = Field(default=0, ge=0, le=100000)
    channel: str = Field(default='global', min_length=1, max_length=100)
    session_id: str = Field(default='', max_length=1000)

    @field_validator('start_at','end_at','next_due_at')
    @classmethod
    def valid_time(cls,value):
        return ReminderStore._validate_optional_time(value)

    @model_validator(mode='after')
    def valid_interval(self):
        if self.repeat_rule=='every_n_rounds' and self.interval_rounds<1:
            raise ValueError('Round interval must be at least one')
        return self


class MemoStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['active','done','archived'] | None = None
    snooze_minutes: int | None = Field(default=None, ge=1, le=525600)

    @model_validator(mode='after')
    def one_action(self):
        if (self.status is None)==(self.snooze_minutes is None):
            raise ValueError('Choose status or snooze_minutes')
        return self


def persona_state(engine, conn, session_id):
    row=conn.execute('SELECT * FROM persona_global_state WHERE profile_id=?',(engine.profile_id,)).fetchone()
    global_state=dict(row) if row else {**engine.default_relationship,'updated_at':''}
    row=conn.execute('SELECT * FROM persona_session_state WHERE profile_id=? AND session_id=?',
                     (engine.profile_id,session_id)).fetchone() if session_id else None
    session=dict(row) if row else None
    return {'relationship':{key:global_state[key] for key in engine.RELATIONSHIP_KEYS},
            'global_updated_at':global_state['updated_at'],'session':session}


def routes(settings, auth):
    router=APIRouter(prefix='/v1/companion',dependencies=auth)

    def persona():
        state=read_settings(settings.database)
        engine=PersonaStateEngine({'serein_database':settings.database,'identity':state['identity'],
                                  'persona':{'enabled':state['features']['persona']}})
        return state,engine

    @router.get('/persona')
    def read_persona(session_id: str=''):
        state,engine=persona()
        sessions=engine._list_sessions(100)
        session_ids = {session['session_id'] for session in sessions}
        shared = GATEWAY_PERSONA_SESSION if GATEWAY_PERSONA_SESSION in session_ids else ''
        selected = session_id or shared or (sessions[0]['session_id'] if sessions else '')
        with closing(engine._connect()) as conn:
            snapshot=persona_state(engine,conn,selected)
            if not snapshot['session'] and shared:
                selected = shared
                snapshot = persona_state(engine,conn,selected)
        return {**snapshot,'session_id':selected,'sessions':sessions,
                'events':engine._list_events(20, (selected or None) if session_id else None),
                'ai_name':state['identity']['ai_name'],
                'enabled':state['features']['persona'],'model_configured':bool(task_model(settings.database,'persona'))}

    def memos():
        return ReminderStore({'serein_database':settings.database})

    @router.get('/memos')
    def read_memos():
        return {'items':memos().list(status='all',limit=200,archive=False),
                'enabled':read_settings(settings.database)['features']['memos']}

    @router.post('/memos')
    def create_memo(body: MemoEdit):
        values=body.model_dump()
        if body.repeat_rule!='every_n_rounds':values['interval_rounds']=0
        return memos().create(**values,source='manual')

    @router.put('/memos/{key}')
    def edit_memo(key: str,body: MemoEdit):
        values=body.model_dump()
        if body.repeat_rule!='every_n_rounds':values['interval_rounds']=0
        current=memos().get(key)
        if values['daily_limit'] is None or (current and current['daily_limit']==values['daily_limit']):values.pop('daily_limit')
        row=memos().update(key,**values)
        if row is None:raise HTTPException(404,'备忘不存在。')
        return row

    @router.patch('/memos/{key}')
    def status_memo(key: str,body: MemoStatus):
        row=memos().snooze(key,minutes=body.snooze_minutes) if body.snooze_minutes is not None else memos().set_status(key,body.status)
        if row is None:raise HTTPException(404,'备忘不存在。')
        return row

    @router.delete('/memos/{key}')
    def delete_memo(key: str):
        return status_memo(key,MemoStatus(status='archived'))

    return router

"""Automatic Event inbox with explicit restore, draft and removal operations."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictInt
from typing import Literal
from ..core.event_mailbox import EventMailbox
from ..core.store import Conflict
from ..deployment import feature_enabled


class MailboxWrite(BaseModel):
    model_config=ConfigDict(extra='forbid')
    operation_id: str
    action: Literal['select','restore','draft','remove']
    expected_revision: StrictInt
    expected_queue_revision: StrictInt
    title: str | None=None
    body_md: str | None=None
    cues: list[str] | str | None=None


def routes(settings,services,auth):
    router=APIRouter(dependencies=auth)
    mailbox=EventMailbox(settings.database)

    @router.get('/api/event-mailbox')
    def listing(status: str='pending',limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0),cursor:str | None=None):
        return mailbox.list(status=status,limit=limit,offset=offset,cursor=cursor)

    @router.get('/api/event-mailbox/{event_id}')
    def read(event_id:str):return mailbox.read(event_id)

    @router.post('/api/event-mailbox/{event_id}')
    def write(event_id:str,body:MailboxWrite):
        if not feature_enabled(settings.database,'event_to_scene'):raise ValueError('Event to Scene promotion is disabled')
        values=body.model_dump(exclude_none=True)
        operation_id=values.pop('operation_id');action=values.pop('action')
        try:return services.write(operation_id,'mailbox_'+action,{'event_id':event_id,**values})
        except Conflict as exc:raise HTTPException(409,str(exc)) from None

    return router

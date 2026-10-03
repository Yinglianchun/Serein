"""Persisted foreground-requested work, leased across HTTP/MCP processes."""
import asyncio
import json
import time
from contextvars import ContextVar
from uuid import uuid4
from .core.store import Store, encode

ACTIVE=ContextVar('serein_work',default=None)
LEASE_SECONDS=60


def _get(store,key):
    row=store.conn.execute('SELECT value_json FROM background_state WHERE name=?',('work:'+key,)).fetchone()
    return json.loads(row[0]) if row else {'id':key,'status':'idle','stage':'idle','completed':0,'error':''}


def _save(store,key,value):
    store.conn.execute('INSERT INTO background_state VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',('work:'+key,encode(value)))


def _recover(value):
    if value['status']=='running' and value.get('lease_until',0)<time.time():
        value.update(status='interrupted',error='后台任务已中断；已完成的步骤保留，点击继续可恢复。')
    return value


def status(database,key):
    with Store(database) as store,store.transaction(immediate=True):
        value=_recover(_get(store,key))
        if value['status']=='interrupted':_save(store,key,value)
        return value


def _queue(value,arguments):
    value.pop('followup_arguments',None)
    value.update(status='queued',stage='queued',error='',arguments=arguments or {},run_id=uuid4().hex,
                 result=None,updated_at=time.time(),stop_requested=False,completed=0,events=0,
                 job_id='',attempt=0,prompt_chars=0)


def enqueue(database,key,arguments=None,*,followup_if_running=False):
    with Store(database) as store,store.transaction(immediate=True):
        value=_recover(_get(store,key))
        if value['status'] in ('queued','running'):
            if followup_if_running and (value['status']=='running' or value.get('arguments')!=arguments):
                value['followup_arguments']=arguments or {}
                _save(store,key,value)
            return value
        _queue(value,arguments)
        _save(store,key,value)
        return value


def enqueue_image(database, message_id):
    """Archive only the source ID, never credentials, in the existing durable queue."""
    key = f'image:{int(message_id)}'
    with Store(database) as store, store.transaction(immediate=True):
        value = _get(store, key)
        if value['status'] != 'idle':
            return value
        value.update(status='queued', stage='queued', run_id=uuid4().hex,
                     arguments={'message_id': int(message_id)}, attempts=0,
                     updated_at=time.time(), next_attempt_at=0)
        _save(store, key, value)
        return value


def recover_image_work(database):
    with Store(database) as store, store.transaction(immediate=True):
        rows = store.conn.execute(
            "SELECT value_json FROM background_state WHERE name LIKE 'work:image:%' "
            "AND (json_extract(value_json,'$.status') IN ('running','interrupted') "
            "OR (json_extract(value_json,'$.status')='failed' "
            "AND json_extract(value_json,'$.retryable')=1 "
            "AND json_extract(value_json,'$.attempts')<4))").fetchall()
        for row in rows:
            value = _recover(json.loads(row[0]))
            if (value['status'] == 'interrupted'
                    or (value['status'] == 'failed' and value.get('retryable'))):
                if value.get('attempts', 0) < 4:
                    value.update(status='queued', run_id=uuid4().hex, lease_until=0)
                else:
                    value.update(status='failed', retryable=False)
                _save(store, value['id'], value)


def pause(database,key):
    with Store(database) as store,store.transaction(immediate=True):
        value=_recover(_get(store,key))
        if value['status']=='queued':value['status']='paused'
        elif value['status']=='running':value['stop_requested']=True
        _save(store,key,value)
        return value


def progress(**fields):
    active=ACTIVE.get()
    if not active:return
    database,key,run_id=active
    with Store(database) as store,store.transaction(immediate=True):
        value=_get(store,key)
        if value.get('run_id')==run_id and value['status']=='running':
            value.update(fields,updated_at=time.time())
            _save(store,key,value)


def failure_reason(error):
    import httpx
    if isinstance(error,(TimeoutError,httpx.TimeoutException)):return '模型或连接超时，请稍后继续。'
    if isinstance(error,httpx.HTTPStatusError):return f'上游返回 HTTP {error.response.status_code}，请检查模型配置。'
    if isinstance(error,httpx.HTTPError):return '无法连接上游模型，请检查地址和网络。'
    if isinstance(error,json.JSONDecodeError):return '模型返回了无法解析的 JSON，请重试当前阶段。'
    if isinstance(error,ValueError):return str(error)[:500]
    return f'任务未完成（{type(error).__name__}）；已完成的步骤保留。'


async def execute(database,key,operation,*,queued_id=None):
    with Store(database) as store,store.transaction(immediate=True):
        value=_recover(_get(store,key))
        if queued_id and value.get('next_attempt_at', 0) > time.time():
            return {'status': 'waiting', 'task': value}
        if value['status']=='running' or (value['status']=='queued' and value.get('run_id')!=queued_id):
            return {'status':'busy','task':value}
        if queued_id and (value['status']!='queued' or value.get('run_id')!=queued_id):
            return {'status':'busy','task':value}
        previous=dict(value)
        run_id=queued_id or uuid4().hex
        value.update(status='running',run_id=run_id,lease_until=time.time()+LEASE_SECONDS,error='',
                     stage='starting',stop_requested=False,updated_at=time.time())
        _save(store,key,value)
    token=ACTIVE.set((database,key,run_id))
    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            progress(lease_until=time.time()+LEASE_SECONDS)
    beat=asyncio.create_task(heartbeat())
    try:
        result=await operation()
        state=result.get('status')
        # Finish under the same lock used by enqueue so a request arriving while
        # a scheduled pass is returning cannot be overwritten or silently lost.
        with Store(database) as store,store.transaction(immediate=True):
            value=_get(store,key)
            if value.get('run_id')==run_id and value['status']=='running':
                followup=value.get('followup_arguments')
                if followup is not None and not value.get('stop_requested') and state in (
                        'current','processed','settled_today','waiting_settlement_window','auto_paused'):
                    _queue(value,followup)
                elif followup is None and state in ('current','settled_today','waiting_settlement_window','auto_paused') and previous['status'] not in ('queued','running'):
                    value=previous
                else:
                    value.update(result=result,status=state if state in ('awaiting_agent','paused','needs_repair') else 'completed',
                                 stage=state or 'completed',lease_until=0,updated_at=time.time(),
                                 error=str(result.get('reason','归线材料需要修复')) if state=='needs_repair' else '')
                _save(store,key,value)
        return result
    except asyncio.CancelledError:
        progress(status='interrupted',lease_until=0,error='服务已停止；已完成步骤保留，点击继续可恢复。')
        raise
    except Exception as error:
        progress(status='failed',lease_until=0,error=failure_reason(error))
        raise
    finally:
        beat.cancel();await asyncio.gather(beat,return_exceptions=True)
        ACTIVE.reset(token)


async def work(settings,key,arguments):
    if key.startswith('image:'):
        from .deployment import read_settings
        from .api.chat import transcribe_archived_images
        from .model_runtime import UpstreamError
        import httpx
        if not read_settings(settings.database)['features'].get('image_transcription_async'):
            return {'status': 'paused'}
        attempts = status(settings.database, key).get('attempts', 0) + 1
        progress(attempts=attempts, retryable=False)
        try:
            _text, receipt = await transcribe_archived_images(settings, arguments['message_id'])
            return receipt
        except Exception as error:
            retryable = isinstance(error, (TimeoutError, httpx.TransportError, json.JSONDecodeError))
            delay = 60 * 2 ** (attempts - 1)
            if isinstance(error, UpstreamError):
                retryable = error.response.status_code == 429 or error.response.status_code >= 500
                retry_after = error.response.headers.get('Retry-After', '')
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
            elif isinstance(error, ValueError) and not retryable:
                retryable = str(error).startswith(('Image transcription returned', '图片转录', '每张输入图片', '空白转录'))
            progress(retryable=retryable, next_attempt_at=time.time()+delay)
            raise
    if key.startswith('legacy:'):
        from .legacy_migration.web import run as migrate
        batch=asyncio.create_task(asyncio.to_thread(migrate,settings,key.removeprefix('legacy:')))
        try:return await asyncio.shield(batch)
        except asyncio.CancelledError:
            pause(settings.database,key)
            await batch
            raise
    if key=='pipeline':
        from .extensions.pipeline import _advance
        events=0;deferred=0;skipped=0;protected=[];curator_omissions=[]
        # This worker is explicitly enqueued by Continue; scheduled_advance does
        # not set this flag. Recheck at most the first held batch per request.
        retry_repair=True
        while True:
            result=await _advance(settings.database,include_recent=arguments.get('include_recent',True),retry_repair=retry_repair)
            retry_repair=False
            events+=result.get('events',0)
            deferred+=result.get('deferred',0);skipped+=result.get('skipped',0)
            protected.extend(result.get('protected_deferrals',[]))
            batch_curator_omissions=result.get('curator_omission_deferrals',[])
            curator_omissions.extend(batch_curator_omissions)
            result={**result,'deferred':deferred,'skipped':skipped,'protected_deferrals':protected,
                    'curator_omission_deferrals':curator_omissions}
            progress(events=events)
            if result['status']=='paused' and result.get('job_id'):
                continue  # The held scope is excluded; try independent chats.
            if result['status']!='processed':return {**result,'events':events}
            if not result.get('processed_originals',0):
                if batch_curator_omissions:
                    continue  # Host-retained scope is excluded today; process other ready scopes.
                return {**result,'status':'current','events':events,'note':'本批需要后续上下文，原话仍待整理；不会反复请求同一批。'}
    from .imports import advance_import
    identifier=key.removeprefix('import:')
    while True:
        batch=asyncio.create_task(asyncio.to_thread(advance_import,settings,identifier))
        try:result=await asyncio.shield(batch)
        except asyncio.CancelledError:
            await batch  # finish the bounded disk batch before releasing its lease
            raise
        progress(stage='importing',completed=result['processed'],total=result['total'],
                 failed=result['failed'],result=result)
        if result['status']=='completed':return result
        if status(settings.database,key).get('stop_requested'):return {**result,'status':'paused'}


async def run(settings):
    """Queued work survives page navigation; stale running work is explicitly resumable."""
    while True:
        recover_image_work(settings.database)
        with Store(settings.database,read_only=True) as store:
            queued=[json.loads(row[0]) for row in store.conn.execute(
                "SELECT value_json FROM background_state WHERE name LIKE 'work:%' AND json_extract(value_json,'$.status')='queued'")]
        for value in queued:
            if value.get('next_attempt_at', 0) > time.time():
                continue
            try:
                await execute(settings.database,value['id'],lambda:work(settings,value['id'],value.get('arguments',{})),queued_id=value['run_id'])
            except Exception:
                pass  # The task's durable error is returned by the status endpoint.
        await asyncio.sleep(.5)

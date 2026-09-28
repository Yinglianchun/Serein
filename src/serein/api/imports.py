from fastapi import APIRouter, Body
from pydantic import BaseModel, Field
from typing import Literal
import json
from ..imports import stage, list_imports, initialize_imports, release_imported_originals
from ..core.store import Store
from ..work_tasks import enqueue, status, pause


def routes(settings,auth):
    router=APIRouter(dependencies=auth)
    class Upload(BaseModel):
        filename: str = Field(min_length=1,max_length=250)
        content: str = Field(min_length=1,max_length=32_000_000)
        mode: Literal['auto','conversation','operit'] = 'auto'
        tagging: bool = True

    @router.post('/v1/imports/preview')
    def preview(body:Upload):
        return stage(settings.database,body.content,body.filename,body.mode,body.tagging)

    @router.get('/v1/imports')
    def listing():
        entries=list_imports(settings.database)
        for entry in entries:
            entry['task']=status(settings.database,'import:'+entry['id'])
        with Store(settings.database,read_only=True) as store:
            tags={row['status']:row['n'] for row in store.conn.execute('SELECT status,COUNT(*) n FROM import_tag_jobs GROUP BY status')}
            failures=[dict(row) for row in store.conn.execute("SELECT document_id,error FROM import_tag_jobs WHERE status='failed' LIMIT 20")]
            boundaries={row['upload_id'] for row in store.conn.execute('SELECT upload_id FROM pipeline_import_boundaries WHERE released=0')}
        for entry in entries:
            entry['event_boundary_active']=entry['id'] in boundaries
        return {'items':entries,'tagging':tags,'tagging_errors':failures}

    @router.post('/v1/imports/{identifier}/continue')
    def proceed(identifier:str):
        initialize_imports(settings.database)
        with Store(settings.database,read_only=True) as store:
            if not store.conn.execute('SELECT 1 FROM file_imports WHERE id=?',(identifier,)).fetchone():
                raise ValueError('找不到导入任务')
        return enqueue(settings.database,'import:'+identifier)

    @router.post('/v1/imports/{identifier}/pause')
    def stop(identifier:str):
        return pause(settings.database,'import:'+identifier)

    @router.post('/v1/imports/{identifier}/include-in-events')
    def include_in_events(identifier:str):
        return release_imported_originals(settings.database,identifier)

    @router.get('/v1/pipeline/status')
    def pipeline_status():
        value=status(settings.database,'pipeline')
        from ..deployment import read_settings, configured_models
        config=read_settings(settings.database)
        value['execution_mode']=config['pipeline']['execution_mode']
        if value['execution_mode']!='agent':
            known={item['id'] for item in configured_models(config)}
            value['unassigned_roles']=[role for role in ('track_router','event_curator','event_writer')
                                       if config['assignments'].get(role) not in known]
        else:
            value['unassigned_roles']=[]
        from ..image_transcription import initialize_failures
        with Store(settings.database) as store:initialize_failures(store.conn)
        if value.get('stage')=='event_evidence':
            value.update(status='idle',stage='idle',result=None,job_id='',error='旧证据整理阶段已撤掉，继续整理会进入下一阶段。')
        with Store(settings.database,read_only=True) as store:
            value['auto_boundary_originals']=(store.conn.execute(
                "SELECT count(*) FROM raw_processing WHERE outcome='auto_boundary'").fetchone()[0]
                if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_processing'").fetchone() else 0)
            if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_batches'").fetchone():
                value['paused_batches']=[json.loads(row[0]) for row in store.conn.execute("SELECT result_json FROM pipeline_batches WHERE status='paused_failure' ORDER BY rowid")]
            value['failed_images']=[dict(row) for row in store.conn.execute(
                'SELECT sha256,failures,error FROM pipeline_image_failures WHERE failures>=3 ORDER BY updated_at DESC')]
            quiet=value['status']=='completed' and value.get('stage') in ('settled_today','waiting_settlement_window')
            if (value['status']=='idle' or quiet) and store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_jobs'").fetchone():
                row=store.conn.execute("SELECT j.* FROM pipeline_jobs j JOIN pipeline_batches b ON b.id=j.batch_id WHERE b.status='pending' AND j.output_json IS NULL AND json_extract(j.request_json,'$.role')!='event_evidence' ORDER BY j.rowid LIMIT 1").fetchone()
                if row:
                    from ..deployment import task_model, read_settings
                    request=json.loads(row['request_json'])
                    ready=read_settings(settings.database)['pipeline']['execution_mode']=='agent' or not task_model(settings.database,request['role'])
                    task={'status':'awaiting_agent','job_id':row['id'],'role':request['role'],'request':request}
                    value.update(status='awaiting_agent' if ready else 'interrupted',stage=request['role'],
                        job_id=row['id'],batch_id=row['batch_id'],result=task if ready else None,
                        completed=store.conn.execute("SELECT count(*) FROM pipeline_jobs WHERE batch_id=? AND output_json IS NOT NULL AND json_extract(request_json,'$.role')!='event_evidence'",(row['batch_id'],)).fetchone()[0],
                        error='' if ready else '发现尚未完成的整理阶段，可点击继续；旧版本未保存的错误返回无法还原。')
            value['attempts']=[dict(row) for row in store.conn.execute(
                'SELECT id,attempt,created_at,error,length(output_text) output_chars FROM pipeline_attempts WHERE job_id=? ORDER BY id DESC LIMIT 5',
                (value.get('job_id',''),))] if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_attempts'").fetchone() else []
        return value

    @router.post('/v1/pipeline/retry-batch')
    async def retry_pipeline_batch(body:dict):
        from ..work_tasks import execute
        from ..extensions.pipeline import retry_batch
        async def retry():return retry_batch(settings.database,body.get('batch_id'))
        return await execute(settings.database,'pipeline',retry)

    @router.post('/v1/pipeline/restore-auto-boundary')
    async def restore_pipeline_auto_boundary(body:dict):
        from ..work_tasks import execute
        from ..extensions.pipeline import restore_auto_boundary
        async def restore():return restore_auto_boundary(settings.database,body.get('confirm'))
        return await execute(settings.database,'pipeline',restore)

    @router.post('/v1/pipeline/retry-image')
    async def retry_image(body:dict):
        from ..work_tasks import execute
        from ..image_transcription import retry_failed_image
        from ..extensions.pipeline import initialize
        async def retry():
            initialize(settings.database)
            return retry_failed_image(settings.database,body.get('sha256'))
        return await execute(settings.database,'pipeline',retry)

    @router.get('/v1/pipeline/attempts/{attempt_id}')
    def pipeline_attempt(attempt_id:int):
        with Store(settings.database,read_only=True) as store:
            if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_attempts'").fetchone():raise ValueError('找不到这次模型返回')
            row=store.conn.execute('SELECT * FROM pipeline_attempts WHERE id=?',(attempt_id,)).fetchone()
        if row is None:raise ValueError('找不到这次模型返回')
        return dict(row)

    @router.post('/v1/pipeline/rebuild')
    async def pipeline_rebuild(body:dict):
        from ..extensions.pipeline_recovery import rebuild
        return await rebuild(settings.database,body.get('batch_id'),body.get('confirm'))

    @router.post('/v1/pipeline/next')
    def pipeline_next(body:dict):
        if type(body.get('include_recent',True)) is not bool:raise ValueError('include_recent must be boolean')
        return enqueue(settings.database,'pipeline',{'include_recent':body.get('include_recent',True)})

    @router.post('/v1/imports/retry-tagging')
    def retry(body:dict | None=Body(default=None)):
        initialize_imports(settings.database)
        limit=(body or {}).get('limit')
        if limit is not None and (type(limit) is not int or not 1<=limit<=10000):raise ValueError('重试数量必须为 1 至 10000')
        with Store(settings.database) as store:
            count=store.conn.execute("UPDATE import_tag_jobs SET status='pending' WHERE document_id IN "
                "(SELECT document_id FROM import_tag_jobs WHERE status='failed' ORDER BY rowid LIMIT ?)",
                (-1 if limit is None else limit,)).rowcount
        return {'queued':count}
    return router

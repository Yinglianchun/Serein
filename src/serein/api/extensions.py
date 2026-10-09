"""HTTP access to exactly the same enabled contributions as MCP."""
from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool


def routes(application, auth):
    router = APIRouter(dependencies=auth)
    builtins = {'memory_read','memory_materials','memory_search','memory_write','memory_candidates',
                'memory_recall','source_messages','source_read'}

    @router.post('/v1/extensions/{name}')
    async def call(name: str, arguments: dict):
        application.refresh_optional()
        tools = {k:v for k,v in application.contributions.tools.items() if k not in builtins}
        if name not in tools:
            raise HTTPException(404, 'Feature is disabled or unavailable')
        if name in {'pipeline_next','pipeline_submit','pipeline_rebuild'}:
            from ..deployment import feature_enabled
            if not feature_enabled(application.settings.database,'pipeline_agent'):
                raise HTTPException(404, 'Automatic-summary Agent tools are disabled')
        if name=='resume' and arguments.get('selection') is not None:
            from .settings import ResumePatch
            from pydantic import ValidationError
            try:
                selection=ResumePatch.model_validate(arguments['selection'],strict=True)
                if {'mode', 'command_enabled', 'mcp_enabled'} & selection.model_fields_set:
                    raise ValueError('Preview selection cannot change resume entry switches')
                arguments={**arguments,'selection':selection.model_dump(exclude_none=True)}
            except (ValidationError,ValueError):
                raise HTTPException(422,'Invalid resume preview selection; use content options only') from None
        import inspect
        try:
            inspect.signature(tools[name]).bind(**arguments)
        except TypeError as exc:
            raise HTTPException(400, str(exc)) from None
        from ..core.store import Conflict
        from ..extensions.handoff import ResumeLimit
        try:
            if inspect.iscoroutinefunction(tools[name]):
                return await tools[name](**arguments)
            return await run_in_threadpool(tools[name], **arguments)
        except Conflict as exc:
            if name in {'promote_event_to_scene','save_event_mailbox_draft'}:
                raise HTTPException(409,str(exc)) from None
            raise
        except ResumeLimit as exc:
            raise HTTPException(413,str(exc)) from None

    return router

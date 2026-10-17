"""Read-only event pipeline backlog, throughput and ETA."""

from fastapi import APIRouter

from ..extensions.pipeline_progress import progress_report


def routes(settings, auth):
    router = APIRouter(dependencies=auth)

    @router.get('/api/pipeline/progress')
    def progress():
        return progress_report(settings.database)

    return router

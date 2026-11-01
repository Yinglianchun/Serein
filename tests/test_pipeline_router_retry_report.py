"""Synthetic reproduction of the 88,420-character Router failure report."""
import asyncio
import json
from functools import partial

import httpx
import pytest

from serein.compat.raw_archive import raw_archive
from serein.core.store import Store, now
from serein.deployment import save_settings
from serein.extensions import pipeline as p
from test_pipeline_batch_recovery import short_output
from test_public_features import settings


def configure(settings, *, daytime=False):
    save_settings(settings.database, {
        'models': [{'id': 'synthetic', 'model': 'synthetic', 'base_url': 'https://synthetic.invalid/v1', 'api_key': 'synthetic'}],
        'assignments': {role: 'synthetic' for role in p.ROLES},
        'features': {'pipeline_agent': False},
        'upstream': {'writer_enabled': False},
        'pipeline': {'execution_mode': 'api', 'max_prompt_chars': 200000},
    })
    count = 10 if daytime else 2
    size, extra = divmod(88420, count)
    raw_archive(settings).ingest([
        {'source_event_id': str(i), 'session_id': 'router-report',
         'role': 'user' if i % 2 == 0 else 'assistant', 'text': 'x' * (size + (i < extra)),
         'created_at': f'2025-01-01T00:{i:02}:00Z'} for i in range(count)
    ], source='synthetic')


def transport(settings, monkeypatch, handler):
    # Exercise the real API request/HTTP error path without any network traffic.
    native = httpx.AsyncClient
    def reply(request):
        assert request.url.host == 'synthetic.invalid'
        with Store(settings.database, read_only=True) as store:
            row = store.conn.execute('SELECT request_json FROM pipeline_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
        return handler(request, json.loads(row[0]))
    monkeypatch.setattr(httpx, 'AsyncClient', partial(native, transport=httpx.MockTransport(reply)))


def invalid_response(request, frozen, kind):
    if kind == 'http400':
        return httpx.Response(400, json={'error': {'message': 'synthetic rejected request'}}, request=request)
    if kind == 'json':
        text = '{malformed' + ' ' * 3010
    else:
        output = short_output('track_router', frozen)
        if kind == 'coverage':
            output['message_assignments'].pop()
        else:
            item = output['message_assignments'][0]
            item['context_track_refs'] = [item['primary_track_ref']]
        text = json.dumps(output)
    return httpx.Response(200, json={'choices': [{'message': {'content': text}}]}, request=request)


@pytest.mark.parametrize('kind', ['json', 'coverage', 'context', 'http400'])
def test_router_stops_after_three_failures_and_retains_originals(settings, monkeypatch, kind):
    configure(settings)
    calls = []
    def reply(request, frozen):
        calls.append(frozen['role'])
        return invalid_response(request, frozen, kind)
    transport(settings, monkeypatch, reply)
    for _ in range(3):
        try:
            result = asyncio.run(p.advance(settings.database, include_recent=True))
        except (ValueError, httpx.HTTPError):
            continue
        if result['status'] == 'retained':
            break
    assert len(calls) == 3 and set(calls) == {'track_router'}
    for _ in range(10):
        assert asyncio.run(p.advance(settings.database, include_recent=True))['status'] == 'current'
    assert len(calls) == 3
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_attempts').fetchone()[0] == 3
        assert store.conn.execute('SELECT count(*) FROM fact_events').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
        assert store.conn.execute('SELECT sum(length(text)) FROM raw_events').fetchone()[0] == 88420
        assert store.conn.execute("SELECT count(*) FROM pipeline_pending_materials WHERE status='open'").fetchone()[0] == 1


def test_mixed_errors_share_one_budget_instead_of_restarting_per_error_type(settings, monkeypatch):
    configure(settings)
    kinds = ['json', 'coverage', 'context', 'http400']
    calls = []
    def reply(request, frozen):
        kind = kinds[min(len(calls), 3)]
        calls.append(kind)
        return invalid_response(request, frozen, kind)
    transport(settings, monkeypatch, reply)
    assert asyncio.run(p.advance(settings.database, include_recent=True))['status'] == 'retained'
    for _ in range(10):
        asyncio.run(p.scheduled_advance(settings.database))
    assert calls == ['json', 'coverage', 'context']


def test_daytime_router_http400_keeps_its_budget_across_scheduler_ticks(settings, monkeypatch):
    configure(settings, daytime=True)
    calls = []
    def reply(request, frozen):
        calls.append(frozen['role'])
        return invalid_response(request, frozen, 'http400')
    transport(settings, monkeypatch, reply)
    for _ in range(12):
        asyncio.run(p.flush_routes(settings.database))
        # Advance retry eligibility without actually waiting 5/10 minutes.
        with Store(settings.database) as store:
            store.conn.execute("UPDATE pipeline_batches SET result_json=json_set(result_json,'$.next_retry_at','2000-01-01T00:00:00+00:00') WHERE status='retry_wait'")
    assert len(calls) == 3
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_batches').fetchone()[0] == 1
        assert store.conn.execute("SELECT count(*) FROM pipeline_pending_materials WHERE status='open'").fetchone()[0] == 1


def test_api_event_writer_works_without_agent_or_narrative_api_writer(settings, monkeypatch):
    configure(settings)
    calls = []
    def reply(request, frozen):
        calls.append(frozen['role'])
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(short_output(frozen['role'], frozen))}}]}, request=request)
    transport(settings, monkeypatch, reply)
    result = asyncio.run(p.advance(settings.database, include_recent=True))
    assert result['events'] == 1 and result['processed_originals'] == 2
    assert calls == ['track_router', 'event_curator', 'event_writer']


def test_old_2274_attempt_records_do_not_grant_unlimited_new_retries(settings, monkeypatch):
    configure(settings)
    p.initialize(settings.database)
    batch = p.new_batch(settings.database, True)
    calls = []
    def reply(request, frozen):
        calls.append(frozen['role'])
        return invalid_response(request, frozen, 'http400')
    transport(settings, monkeypatch, reply)
    # Seed the old diagnostic history, leaving the current failure counter absent.
    key = batch['id'] + ':track_router:0'
    with Store(settings.database) as store:
        store.conn.executemany('INSERT INTO pipeline_attempts(job_id,attempt,created_at,output_text,error) VALUES (?,?,?,?,?)',
            [(key, number, now(), '', 'Upstream returned HTTP 400') for number in range(1, 2275)])
    for _ in range(8):
        try:
            asyncio.run(p.advance(settings.database, include_recent=True))
        except ValueError:
            pass
    assert len(calls) == 3
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM pipeline_attempts').fetchone()[0] == 2277
        assert {row[0] for row in store.conn.execute('SELECT DISTINCT job_id FROM pipeline_attempts')} == {key}
        assert store.conn.execute('SELECT max(attempt) FROM pipeline_attempts').fetchone()[0] == 2277

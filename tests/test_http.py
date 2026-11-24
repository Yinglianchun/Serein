from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.config import Settings
from serein.core import Store
from serein.recall.index import build_index


@pytest.fixture
def profile(tmp_path):
    settings = Settings(tmp_path / 'memory.db', tmp_path / 'index.sqlite')
    with Store(settings.database) as store:
        store.create('event_a', 'event', '归航', '雨天归航')
    build_index(settings.database, settings.index)
    return settings


def test_auth_and_read_only_boundary(profile):
    before = profile.database.read_bytes()
    with TestClient(create_app(profile, token='test-secret')) as client:
        assert client.get('/health').json()['read_only'] is True
        assert client.get('/v1/memories/event_a').status_code == 401
        assert client.get('/v1/memories/event_a', headers={'Authorization':'Bearer wrong'}).status_code == 401
        client.headers['Authorization'] = 'Bearer test-secret'
        assert client.get('/v1/memories/event_a').json()['document']['body_md'] == '雨天归航'
        assert client.get('/v1/memories/missing').json()['status'] == 'missing'
        assert client.delete('/v1/memories/event_a').status_code == 405
        assert client.post('/v1/memories/event_a', json={'body_md':'changed'}).status_code == 405
        assert client.get('/v1/capabilities').json()['records_injections'] is False
    assert profile.database.read_bytes() == before


def test_liveness_checks_storage_until_first_success_and_health_always_checks(profile, monkeypatch):
    from serein.api import http

    probes = []
    original_reader = http.Reader

    def counted_reader(database):
        probes.append(database)
        return original_reader(database)

    monkeypatch.setattr(http, 'Reader', counted_reader)
    with TestClient(create_app(profile, token='test-secret')) as client:
        moved = profile.database.with_suffix('.offline')
        profile.database.rename(moved)
        assert client.get('/live').status_code == 503
        moved.rename(profile.database)
        assert client.get('/live').status_code == 200
        profile.database.rename(moved)
        assert client.get('/live').status_code == 200
        assert len(probes) == 2
        assert client.get('/health').status_code == 503
        moved.rename(profile.database)
        assert client.get('/health').status_code == 200
        assert len(probes) == 4


def test_recall_crosses_http_boundary_without_writing(profile):
    before = profile.database.read_bytes()
    with TestClient(create_app(profile, token='test-secret'), headers={'Authorization':'Bearer test-secret'}) as client:
        response = client.post('/v1/recall', json={'query':'归航', 'method':'lexical', 'mode':'lookup'})
        assert response.status_code == 200, response.text
        assert 'event_a' in response.text
        assert client.post('/v1/recall', json={'query':'归航','record_injection':True}).status_code == 422
        assert client.post('/v1/recall', json={'query':'归航','limit':10000}).status_code == 422
    assert profile.database.read_bytes() == before


def test_writable_profile_and_missing_auth_cannot_start(profile):
    with pytest.raises(ValueError, match='writable=false'):
        create_app(replace(profile, writable=True), token='test-secret')
    with pytest.raises(ValueError, match='credential'):
        create_app(profile, token='')

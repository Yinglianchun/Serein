from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.config import Settings
from serein.deployment import read_settings, task_model


def client_for(tmp_path):
    settings = Settings(tmp_path / 'memory.db', index=tmp_path / 'index.db', writable=True)
    initialize(settings)
    client = TestClient(
        create_app(settings, token='synthetic', live=True),
        headers={'Authorization': 'Bearer synthetic'},
    )
    return settings, client


def vision_model():
    return {
        'id': 'vision',
        'label': 'Synthetic Vision',
        'model': 'synthetic-vision',
        'base_url': 'http://127.0.0.1:9999/v1',
    }


def test_image_transcription_assignment_saves_and_reads_back(tmp_path):
    settings, client = client_for(tmp_path)
    response = client.patch('/v1/settings', json={
        'models': [vision_model()],
        'assignments': {'image_transcription': 'vision'},
        'features': {'image_eyes': True},
    })
    assert response.status_code == 200, response.text
    assert response.json()['assignments']['image_transcription'] == 'vision'
    assert response.json()['features']['image_eyes'] is True

    reopened = TestClient(
        create_app(settings, token='synthetic', live=True),
        headers={'Authorization': 'Bearer synthetic'},
    )
    saved = reopened.get('/v1/settings').json()
    assert saved['assignments']['image_transcription'] == 'vision'
    assert saved['features']['image_eyes'] is True
    assert read_settings(settings.database)['assignments']['image_transcription'] == 'vision'
    assert task_model(settings.database, 'image_transcription')['model'] == 'synthetic-vision'


def test_image_modes_are_mutually_exclusive_and_legacy_maps_to_eyes(tmp_path):
    _, client = client_for(tmp_path)
    configured={'models':[vision_model()],'assignments':{'image_transcription':'vision'}}
    client.patch('/v1/settings',json=configured).raise_for_status()
    invalid=client.patch('/v1/settings',json={'features':{
        'image_transcription_async':True,'image_eyes':True}})
    assert invalid.status_code==400
    assert invalid.json()=={'detail':'“异步图片转录”和“眼睛”只能开启一个'}
    legacy=client.patch('/v1/settings',json={'features':{'image_transcription':True}})
    assert legacy.status_code==200,legacy.text
    assert legacy.json()['features']['image_eyes'] is True


def test_api_event_mode_reports_the_missing_stage_assignments(tmp_path):
    _, client = client_for(tmp_path)
    response = client.patch('/v1/settings', json={
        'models': [vision_model()],
        'assignments': {'image_transcription': 'vision'},
        'pipeline': {'execution_mode': 'api'},
    })
    assert response.status_code == 400
    assert response.json() == {'detail': 'API 自动摘要需要选择模型：原话 · 归线、原话 · 切分、原话 · Event 写作'}

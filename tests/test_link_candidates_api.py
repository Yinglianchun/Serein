"""Full-checkout integration: exercise the authenticated shared picker endpoint."""
import pytest

from serein.core.store import Store
from test_public_settings import deployment


def test_link_lookup_without_models_does_not_change_settings_or_documents(deployment):
    settings,client=deployment
    with Store(settings.database) as store:
        store.create('scene_link_test','scene','河边散步','我们沿着河边散步。',metadata={'date':'2026-09-20'})
    before=client.get('/v1/settings').json()
    found=client.get('/v1/settings/resume-candidates',params={'purpose':'link','kind':'scene','q':'河边 散步'})
    assert found.status_code==200,found.text
    assert found.headers['Cache-Control']=='no-store'
    assert found.json()['purpose']=='link'
    assert found.json()['items'][0]['id']=='scene_link_test'
    assert 'body_md' not in found.json()['items'][0]
    assert client.get('/v1/settings').json()==before
    with Store(settings.database,read_only=True) as store:
        assert store.read('scene_link_test')['revision']==1


def test_existing_resume_picker_keeps_full_body_and_event_scene_contract(deployment):
    settings,client=deployment
    with Store(settings.database) as store:
        store.create('scene_resume_test','scene','原有续接测试','原有正文',metadata={'date':'2026-09-20'})
    response=client.get('/v1/settings/resume-candidates',params={'kind':'scene','q':'原有续接'})
    assert response.status_code==200
    assert response.json()['items'][0]['body_md']=='原有正文'
    assert client.get('/v1/settings/resume-candidates',params={'kind':'diary'}).status_code==422


@pytest.mark.parametrize('params', [
    {'purpose':'link','kind':''}, {'purpose':'link','kind':'narrative'},
    {'purpose':'other'}, {'purpose':'link','offset':-1},
    {'purpose':'link','limit':51}, {'purpose':'link','q':'x'*201},
])
def test_candidate_route_rejects_invalid_inputs(deployment,params):
    _,client=deployment
    assert client.get('/v1/settings/resume-candidates',params=params).status_code==422

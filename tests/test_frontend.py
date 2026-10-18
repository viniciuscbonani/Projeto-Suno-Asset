"""The studio is installed and served locally without shadowing API routes."""
from fastapi.testclient import TestClient
from suno_asset.api.app import create_app
from suno_asset.services.local_storage import LocalStorage


def test_studio_and_assets_are_served_without_exposing_server_files(tmp_path):
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    home = client.get('/')
    assert home.status_code == 200
    assert 'text/html' in home.headers['content-type']
    assert 'lang="pt-BR"' in home.text
    for asset in ['app.mjs', 'core.mjs', 'styles.css', 'assets/suno-asset.svg']:
        response = client.get('/studio/' + asset)
        assert response.status_code == 200 and response.content
    for path in ['/studio/.env', '/studio/%2e%2e/config.py', '/studio/assets/.env']:
        assert client.get(path).status_code == 404
    assert client.get('/health').json() == {'status': 'ok'}
    assert len(client.get('/capabilities').json()['architectures']['literal-v1']['combinations']) == 9

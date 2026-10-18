from fastapi.testclient import TestClient

import suno_asset
from suno_asset.api.app import create_app


def test_package_import_and_health() -> None:
    assert suno_asset.__version__
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

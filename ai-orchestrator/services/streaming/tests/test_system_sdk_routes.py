from fastapi.testclient import TestClient

from services.streaming import create_app


def test_sdk_routes_serve_nested_assets():
    app = create_app()
    client = TestClient(app, raise_server_exceptions=True)

    listing = client.get("/sdk")
    assert listing.status_code == 200
    files = listing.json()["files"]
    assert "setup/setup-client.sh" in files
    assert "docker/docker-compose.client.yml" in files

    compose = client.get("/sdk/docker/docker-compose.client.yml")
    assert compose.status_code == 200
    assert "orchestrator-client" in compose.text

    setup = client.get("/sdk/setup/setup-client.sh")
    assert setup.status_code == 200
    assert "setup-client.sh" in setup.text

from fastapi.testclient import TestClient

from services.streaming import create_app


def test_dashboard_reference_style_renders_clean_utf8_markup():
    app = create_app()
    client = TestClient(app, raise_server_exceptions=True)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "SINC | Cognitive NOC Dashboard" in response.text
    assert "Component Registry Health" in response.text
    assert "Live Task Pipeline" in response.text
    assert "Agent Reputation" in response.text
    assert "Live Feed" in response.text
    assert "metric-autonomy-score" in response.text
    assert "rep-list" in response.text
    assert "activity-feed-container" in response.text
    assert "registry-health-container" in response.text
    assert "task-list-container" in response.text
    assert "system-mode-display" in response.text
    assert "/static/dashboard.css" in response.text
    assert "/static/dashboard.js" in response.text
    assert "Ã‚Â·" not in response.text
    assert "vÃƒ" not in response.text


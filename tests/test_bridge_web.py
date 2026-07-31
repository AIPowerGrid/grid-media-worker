from fastapi.testclient import TestClient

from bridge.web import app as web_app


def test_configured_bridge_pages_render(monkeypatch):
    monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
    client = TestClient(web_app.app)

    for path in ("/", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

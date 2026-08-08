import asyncio

import pytest
from fastapi.testclient import TestClient

from bridge.web import app as web_app
from bridge import ws_worker


def test_configured_bridge_pages_render(monkeypatch):
    monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
    client = TestClient(web_app.app)

    for path in ("/", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_worker_supervisor_retries_startup_failure(monkeypatch):
    started_again = asyncio.Event()
    instances = []

    class FakeComfy:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeWorker:
        def __init__(self):
            self.comfy = FakeComfy()
            instances.append(self)

        async def run(self):
            if len(instances) == 1:
                raise ConnectionError("ComfyUI is still starting")
            started_again.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(ws_worker, "WSWorker", FakeWorker)
    monkeypatch.setattr(web_app, "WORKER_START_RETRY_SECONDS", 0)

    task = asyncio.create_task(web_app._run_worker())
    await asyncio.wait_for(started_again.wait(), timeout=1)

    assert len(instances) == 2
    assert instances[0].comfy.closed is True
    assert web_app.worker_state["running"] is True
    assert web_app.worker_state["error"] is None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instances[1].comfy.closed is True
    assert web_app.worker_state["running"] is False
    assert web_app.worker_state["bridge"] is None

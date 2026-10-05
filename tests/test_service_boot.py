"""Service-boot conditions: an auto-started bridge begins life in a system
directory with ComfyUI not yet running. Config must resolve install-relatively
and the waiting state must be authored and visible, not a traceback."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from bridge.config import ENV_PATH, REPO_ROOT, Settings
from bridge.web import app as web_app
from bridge.web import routes


def test_env_path_is_install_relative_and_shared():
    """The .env a service-started bridge reads must be the same file the setup
    wizard writes, independent of the launch directory."""
    assert ENV_PATH == REPO_ROOT / ".env"
    assert routes.ENV_PATH is ENV_PATH


@pytest.mark.asyncio
async def test_comfyui_down_raises_authored_waiting_message(monkeypatch):
    import bridge.ws_worker as ws

    monkeypatch.setattr(Settings, "GRID_PROFILE_PATH", "")
    monkeypatch.setattr(Settings, "GRID_MODELS", [])
    monkeypatch.setattr(Settings, "GRID_TRUST_MODELS", False)
    monkeypatch.setattr(Settings, "GRID_SCHEDULE", "")
    monkeypatch.setattr(Settings, "THREADS", 1)
    monkeypatch.setattr(ws, "COMFY_MODELS_WAIT_S", 0)
    monkeypatch.setattr(ws.model_mapper, "available_files", set())
    monkeypatch.setattr(ws, "initialize_model_mapper", AsyncMock())
    worker = ws.WSWorker()
    monkeypatch.setattr(
        worker,
        "_check_runtime_health",
        AsyncMock(side_effect=httpx.ConnectError("refused")),
    )
    try:
        with pytest.raises(RuntimeError, match="Waiting for ComfyUI"):
            await worker.run()
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
async def test_supervisor_surfaces_authored_message_in_status(monkeypatch):
    """The dashboard's worker_error must carry the real waiting message, not a
    generic 'unavailable' — it is the only signal an operator gets at login."""
    message = "Waiting for ComfyUI at http://127.0.0.1:8188 — it is not running yet"
    seen = asyncio.Event()

    class FakeComfy:
        async def aclose(self):
            pass

    class FakeWorker:
        comfy = FakeComfy()

        async def run(self):
            raise RuntimeError(message)

    monkeypatch.setattr(web_app, "WORKER_START_RETRY_SECONDS", 0)
    import bridge.ws_worker as ws

    monkeypatch.setattr(ws, "WSWorker", lambda: FakeWorker())

    original_sleep = asyncio.sleep

    async def stop_after_first(_delay):
        seen.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(web_app.asyncio, "sleep", stop_after_first)
    with pytest.raises(asyncio.CancelledError):
        await web_app._run_worker()
    assert seen.is_set()
    assert web_app.worker_state["error"] == message
    await original_sleep(0)

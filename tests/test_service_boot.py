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
@pytest.mark.parametrize(
    "exc_factory, expected",
    [
        # Authored startup states reach the dashboard verbatim…
        (
            lambda ws: ws.StartupPending(
                "Waiting for ComfyUI at http://127.0.0.1:8188 — it is not running"
            ),
            "Waiting for ComfyUI at http://127.0.0.1:8188 — it is not running",
        ),
        # …but raw runtime error text (e.g. an ACE-Step readiness failure that
        # interpolates the underlying exception) never does.
        (
            lambda ws: RuntimeError(
                "ACE-Step readiness check failed: ConnectError('boom')"
            ),
            "Worker unavailable; retrying",
        ),
    ],
)
async def test_supervisor_surfaces_only_authored_messages(
    monkeypatch, exc_factory, expected
):
    """worker_error must carry StartupPending messages verbatim — the only
    signal an operator gets at login — and stay generic for everything else."""
    import bridge.ws_worker as ws

    seen = asyncio.Event()

    class FakeComfy:
        async def aclose(self):
            pass

    class FakeWorker:
        comfy = FakeComfy()

        async def run(self):
            raise exc_factory(ws)

    monkeypatch.setattr(web_app, "WORKER_START_RETRY_SECONDS", 0)
    monkeypatch.setattr(ws, "WSWorker", lambda: FakeWorker())

    async def stop_after_first(_delay):
        seen.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(web_app.asyncio, "sleep", stop_after_first)
    monkeypatch.setitem(web_app.worker_state, "error", None)
    with pytest.raises(asyncio.CancelledError):
        await web_app._run_worker()
    assert seen.is_set()
    assert web_app.worker_state["error"] == expected


@pytest.mark.asyncio
async def test_unservable_candidates_keep_the_no_servable_message(monkeypatch):
    """Inventory present but nothing qualifies → the genuine 'No servable
    models' guidance, not a ComfyUI-waiting message."""
    import bridge.ws_worker as ws

    monkeypatch.setattr(Settings, "GRID_PROFILE_PATH", "")
    monkeypatch.setattr(Settings, "GRID_MODELS", [])
    monkeypatch.setattr(Settings, "GRID_PREFLIGHT", False)
    monkeypatch.setattr(Settings, "GRID_TRUST_MODELS", False)
    monkeypatch.setattr(Settings, "GRID_SCHEDULE", "")
    monkeypatch.setattr(Settings, "THREADS", 1)
    monkeypatch.setattr(ws.model_mapper, "available_files", {"present.safetensors"})
    monkeypatch.setattr(ws, "initialize_model_mapper", AsyncMock())
    monkeypatch.setattr(ws, "get_grid_models", lambda: ["some-model"])
    monkeypatch.setattr(ws, "is_servable", lambda m: (False, "workflow file missing"))
    worker = ws.WSWorker()
    monkeypatch.setattr(worker, "_check_runtime_health", AsyncMock())
    try:
        with pytest.raises(ws.StartupPending, match="No servable models"):
            await worker.run()
    finally:
        await worker.comfy.aclose()


def test_windowless_process_gets_file_streams(monkeypatch, tmp_path):
    """Under pythonw.exe stdout/stderr are None; the first logging write then
    killed the bridge silently. _ensure_streams must give it a logfile."""
    import sys as real_sys

    from bridge import cli

    monkeypatch.setattr(real_sys, "stdout", None)
    monkeypatch.setattr(real_sys, "stderr", None)
    log = tmp_path / "bridge-service.log"
    stream = cli._ensure_streams(log)
    try:
        assert real_sys.stdout is stream and real_sys.stderr is stream
        print("alive under pythonw")
        real_sys.stdout.flush()
        assert "alive under pythonw" in log.read_text()
        assert real_sys.stdout.isatty() is False  # what crashed uvicorn before
    finally:
        stream.close()


def test_ensure_streams_is_noop_with_a_console():
    from bridge import cli

    assert cli._ensure_streams() is None


@pytest.mark.asyncio
async def test_waiting_message_is_visible_during_the_wait(monkeypatch):
    """The authored waiting text must be readable the WHOLE time the worker
    sits in the ComfyUI wait loop — on the Windows box it showed in only 2 of
    74 status samples because it existed solely in the post-failure window."""
    import bridge.ws_worker as ws
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    monkeypatch.setattr(Settings, "GRID_PROFILE_PATH", "")
    monkeypatch.setattr(Settings, "GRID_MODELS", [])
    monkeypatch.setattr(Settings, "GRID_TRUST_MODELS", False)
    monkeypatch.setattr(Settings, "GRID_SCHEDULE", "")
    monkeypatch.setattr(Settings, "THREADS", 1)
    monkeypatch.setattr(ws, "COMFY_MODELS_WAIT_S", 60)
    monkeypatch.setattr(ws.model_mapper, "available_files", set())
    monkeypatch.setattr(ws, "initialize_model_mapper", AsyncMock())
    worker = ws.WSWorker()
    sampled = {}

    async def sample_sleep(_d):
        # What /api/status reports mid-wait, with no supervisor error set.
        monkeypatch.setitem(web_app.worker_state, "error", None)
        monkeypatch.setitem(web_app.worker_state, "bridge", worker)
        monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
        body = (
            TestClient(web_app.app, base_url="http://127.0.0.1:7860")
            .get("/api/status")
            .json()
        )
        sampled["error"] = body["worker_error"]
        raise asyncio.CancelledError

    monkeypatch.setattr(ws.asyncio, "sleep", sample_sleep)
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker.run()
    finally:
        await worker.comfy.aclose()
    assert sampled["error"] and "Waiting for ComfyUI" in sampled["error"]

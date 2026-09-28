"""Regressions for the first-run rough edges: cwd-dependent defaults, the
opaque port-conflict crash, the untruthful /api/status model list, and the
silent unpriced-model trap."""
import asyncio
import errno
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
import respx
from fastapi.testclient import TestClient

from bridge import cli as bridge_cli
from bridge.config import REPO_ROOT, Settings
from bridge.pricing_check import fetch_priced_model_names, unsellable_names
from bridge.web import app as web_app


LOCAL_ORIGIN = "http://127.0.0.1:7860"


def test_workflow_dir_default_is_install_relative():
    """Launching comfy-bridge from any directory must find the same
    workflows/ folder; a cwd-relative default silently emptied the map."""
    if os.getenv("WORKFLOW_DIR"):
        pytest.skip("operator override in effect")
    assert os.path.isabs(Settings.WORKFLOW_DIR)
    assert Settings.WORKFLOW_DIR == str(REPO_ROOT / "workflows")
    assert os.path.isdir(Settings.WORKFLOW_DIR)


def test_unsellable_names_is_case_insensitive_and_fail_open():
    priced = {"flux.2 klein 4b fp8", "ltx-2.3"}
    assert unsellable_names(["FLUX.2 Klein 4B FP8", "LTX-2.3"], priced) == []
    assert unsellable_names(["SDXL 1.0", "LTX-2.3"], priced) == ["SDXL 1.0"]
    # A failed price-book fetch (None) must never produce warnings.
    assert unsellable_names(["anything"], None) == []


@pytest.mark.parametrize("error_number", [errno.EADDRINUSE, 10048, 48])
def test_port_conflict_exits_with_plain_explanation(monkeypatch, caplog, error_number):
    import uvicorn

    def bind_fails(*a, **k):
        raise OSError(error_number, "error while attempting to bind on address")

    monkeypatch.setattr(uvicorn, "run", bind_fails)
    with pytest.raises(SystemExit) as exc:
        bridge_cli.main()
    assert exc.value.code == 1
    assert any("already in use" in r.message for r in caplog.records)


def test_port_in_use_probe_on_closed_port(monkeypatch):
    def refused(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(bridge_cli.socket, "create_connection", refused)
    assert bridge_cli.port_in_use("127.0.0.1", 1) is False


@pytest.mark.asyncio
async def test_pricing_includes_only_aliases_with_priced_targets():
    with respx.mock() as mock:
        mock.get("https://api.grid.test/v1/pricing").respond(200, json={
            "price_book": {"models": [{"model": "LTX-2.3"}],
                           "aliases": {"LTX Director 2.0": "ltx-2.3", "bad": "missing"}}
        })
        assert await fetch_priced_model_names("https://ws.grid.test/") == {
            "ltx-2.3", "ltx director 2.0"
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "timeout", "schema"])
async def test_pricing_fetch_failure_is_advisory(failure):
    with respx.mock() as mock:
        route = mock.get("https://grid.test/v1/pricing")
        if failure == "timeout":
            route.mock(side_effect=httpx.ReadTimeout("unavailable"))
        else:
            route.respond(503 if failure == "status" else 200, json={})
        assert await fetch_priced_model_names("https://grid.test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [True, False])
async def test_cold_start_discovers_models_after_inventory(monkeypatch, ready):
    import bridge.ws_worker as ws

    monkeypatch.setattr(Settings, "GRID_PROFILE_PATH", "")
    monkeypatch.setattr(Settings, "GRID_MODELS", [])
    monkeypatch.setattr(Settings, "GRID_PREFLIGHT", False)
    monkeypatch.setattr(Settings, "GRID_TRUST_MODELS", False)
    monkeypatch.setattr(Settings, "GRID_SCHEDULE", "")
    monkeypatch.setattr(Settings, "THREADS", 1)
    monkeypatch.setattr(ws.model_mapper, "available_files", set())
    # A zero deadline tests the failure path without sleeping.
    monkeypatch.setattr(ws, "COMFY_MODELS_WAIT_S", 60 if ready else 0)
    calls = 0

    async def initialize(url):
        nonlocal calls
        calls += 1
        if ready and calls == 2:
            ws.model_mapper.available_files = {"test.safetensors"}

    monkeypatch.setattr(ws, "initialize_model_mapper", initialize)
    monkeypatch.setattr(ws.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(ws, "get_grid_models", lambda: ["test-model"] if ws.model_mapper.available_files else [])
    monkeypatch.setattr(ws, "is_servable", lambda model: (True, "ready"))
    monkeypatch.setattr(ws, "fetch_priced_model_names", AsyncMock(return_value=None))
    worker = ws.WSWorker()
    monkeypatch.setattr(worker, "_check_runtime_health", AsyncMock())
    monkeypatch.setattr(worker, "_wait_until_available", AsyncMock())
    session = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(worker, "_session", session)
    try:
        if ready:
            with pytest.raises(asyncio.CancelledError):
                await worker.run()
            assert calls == 2
            assert worker.models == ["test-model"]
            session.assert_awaited_once()
        else:
            with pytest.raises(RuntimeError, match="No servable models"):
                await worker.run()
            session.assert_not_awaited()
    finally:
        await worker.comfy.aclose()


def test_api_status_reports_actually_advertised_models(monkeypatch):
    monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
    monkeypatch.setitem(
        web_app.worker_state,
        "bridge",
        SimpleNamespace(models=["LTX-2.3", "FLUX.2 Klein 4B FP8"]),
    )
    body = TestClient(web_app.app, base_url=LOCAL_ORIGIN).get("/api/status").json()
    assert body["advertised"] == ["LTX-2.3", "FLUX.2 Klein 4B FP8"]

    monkeypatch.setitem(web_app.worker_state, "bridge", None)
    body = TestClient(web_app.app, base_url=LOCAL_ORIGIN).get("/api/status").json()
    assert body["advertised"] == []

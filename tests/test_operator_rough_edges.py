"""Regressions for the first-run rough edges: cwd-dependent defaults, the
opaque port-conflict crash, the untruthful /api/status model list, and the
silent unpriced-model trap."""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bridge import cli as bridge_cli
from bridge.config import REPO_ROOT, Settings
from bridge.pricing_check import unsellable_names
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


def test_port_conflict_exits_with_plain_explanation(monkeypatch, caplog):
    import uvicorn

    def bind_fails(*a, **k):
        raise OSError(98, "error while attempting to bind on address")

    monkeypatch.setattr(uvicorn, "run", bind_fails)
    with pytest.raises(SystemExit) as exc:
        bridge_cli.main()
    assert exc.value.code == 1
    assert any("already in use" in r.message for r in caplog.records)


def test_port_in_use_probe_on_closed_port():
    # Nothing listens on this reserved-by-convention discard-ish high port.
    assert bridge_cli.port_in_use("127.0.0.1", 1) is False


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

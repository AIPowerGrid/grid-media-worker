from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from bridge.profiles.hardware import AcceleratorInfo, HardwareSnapshot
from bridge.profiles.profile import bundled_profile_path, load_profile
from bridge.profiles.state import profile_digest
from bridge.web import manager


def _config(tmp_path: Path) -> manager.ManagerWebConfig:
    root = tmp_path / "worker"
    return manager.ManagerWebConfig(
        profile=bundled_profile_path(),
        allow_unsigned_draft=True,
        install_root=root,
        state=root / "profile-state.json",
        credentials=root / "worker-credentials.json",
        pending=root / "worker-enrollment.json",
        key=root / "worker-key.json",
        delegation=root / "delegation.json",
        grid_url="https://api.aipowergrid.io",
        host="127.0.0.1",
        port=8791,
        launch_browser=False,
    )


def _client(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    controller = manager.ManagerProcessController(config)
    token = "local-test-token"
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        65536,
        131072,
        (
            AcceleratorInfo(
                "nvidia",
                "NVIDIA GeForce RTX 5090",
                32607,
                "580.1",
                "12.8",
                1,
                "GPU-larger",
            ),
            AcceleratorInfo(
                "nvidia",
                "NVIDIA GeForce RTX 3090",
                24576,
                "580.1",
                "12.8",
                0,
                "GPU-test",
            ),
        ),
    )
    monkeypatch.setattr(manager, "detect_hardware", lambda *_a, **_k: snapshot)
    app = manager.create_manager_app(config, controller, token)
    client = TestClient(app, base_url=config.origin)
    return client, config, controller, token


def test_manager_requires_bootstrap_session_and_sets_security_headers(
    tmp_path, monkeypatch,
):
    client, _config_value, _controller, token = _client(tmp_path, monkeypatch)

    denied = client.get("/", follow_redirects=False)
    assert denied.status_code == 403
    assert "Open the manager from its launch link" in denied.text
    assert denied.headers["content-type"].startswith("text/html")

    denied_api = client.get("/api/manager/status")
    assert denied_api.status_code == 403
    assert denied_api.json() == {"detail": "local session required"}

    response = client.get(f"/bootstrap?token={token}")
    assert response.status_code == 200
    assert manager.SESSION_COOKIE in client.cookies
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in response.text
    assert '/static/logo.png' in response.text
    assert '/static/favicon-32x32.png' in response.text

    logo = client.get("/static/logo.png")
    assert logo.status_code == 200
    assert logo.headers["content-type"] == "image/png"


def test_manager_actions_require_exact_origin_and_json(tmp_path, monkeypatch):
    client, config, controller, token = _client(tmp_path, monkeypatch)
    client.get(f"/bootstrap?token={token}")
    controller.start = AsyncMock()

    wrong_origin = client.post(
        "/api/manager/action",
        headers={"Origin": "https://attacker.example"},
        json={"action": "setup"},
    )
    assert wrong_origin.status_code == 403

    wrong_type = client.post(
        "/api/manager/action",
        headers={"Origin": config.origin, "Content-Type": "text/plain"},
        content=json.dumps({"action": "setup"}),
    )
    assert wrong_type.status_code == 415

    accepted = client.post(
        "/api/manager/action",
        headers={"Origin": config.origin},
        json={"action": "setup"},
    )
    assert accepted.status_code == 200
    controller.start.assert_awaited_once_with("setup")


def test_manager_capacity_is_bounded_persistent_and_watched(tmp_path, monkeypatch):
    client, config, controller, token = _client(tmp_path, monkeypatch)
    client.get(f"/bootstrap?token={token}")

    page = client.get("/")
    assert 'id="capacity-form"' in page.text
    assert "Maximum simultaneous jobs" in page.text

    wrong_origin = client.post(
        "/api/manager/capacity",
        headers={"Origin": "https://attacker.example"},
        json={"mode": "paused"},
    )
    assert wrong_origin.status_code == 403

    paused = client.post(
        "/api/manager/capacity",
        headers={"Origin": config.origin},
        json={"mode": "paused"},
    )
    assert paused.status_code == 200
    assert paused.json()["capacity"]["accepting_jobs"] is False
    path = config.install_root / manager.CAPACITY_FILE_NAME
    assert path.read_text(encoding="utf-8") == '[{"days":"daily","concurrency":0}]'
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert controller._environment()["GRID_CAPACITY_FILE"] == str(path)

    maintenance = client.post(
        "/api/manager/capacity",
        headers={"Origin": config.origin},
        json={
            "mode": "maintenance",
            "days": "mon-fri",
            "start": "22:00",
            "end": "02:00",
        },
    )
    assert maintenance.status_code == 200
    capacity = maintenance.json()["capacity"]
    assert capacity["mode"] == "maintenance"
    assert capacity["days"] == "mon-fri"
    assert capacity["start"] == "22:00"
    assert capacity["end"] == "02:00"
    assert capacity["max_concurrency"] == 1
    assert capacity["effective_concurrency"] in {0, 1}
    assert capacity["accepting_jobs"] is bool(capacity["effective_concurrency"])
    assert capacity["error"] is None

    invalid = client.post(
        "/api/manager/capacity",
        headers={"Origin": config.origin},
        json={
            "mode": "maintenance",
            "days": "daily",
            "start": "02:00",
            "end": "02:00",
        },
    )
    assert invalid.status_code == 400

    always = client.post(
        "/api/manager/capacity",
        headers={"Origin": config.origin},
        json={"mode": "always"},
    )
    assert always.status_code == 200
    assert always.json()["capacity"]["accepting_jobs"] is True


def test_status_keeps_worker_api_key_private(tmp_path, monkeypatch):
    client, _config_value, _controller, token = _client(tmp_path, monkeypatch)
    client.get(f"/bootstrap?token={token}")
    document = load_profile(bundled_profile_path(), allow_unsigned_draft=True)
    _config_value.state.parent.mkdir(parents=True)
    _config_value.state.write_text(
        json.dumps(
            {
                "state_version": 2,
                "profile_id": document.profile["id"],
                "profile_version": document.profile["version"],
                "profile_digest": profile_digest(document.profile),
                "signature_verified": False,
                "signing_key_id": None,
                "capability_tier": "audio.ace-step.standard",
                "runtime_device": "GPU-test",
                "runtime_adapter": document.profile["runtime"]["adapter"],
                "runtime_ready": True,
                "installed_at": "2026-07-16T00:00:00+00:00",
                "artifacts": [],
                "canary": {"passed": True},
                "capabilities": document.profile["capabilities_after_validation"],
            }
        ),
        encoding="utf-8",
    )
    credentials = _config_value.credentials
    credentials.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "load_worker_credentials",
        lambda *_a, **_k: {
            "api_key": "grid_super_secret_value",
            "worker_name": "audio-rig",
        },
    )

    response = client.get("/api/manager/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["identity"]["connected"] is True
    assert payload["identity"]["worker_name"] == "audio-rig"
    assert "grid_super_secret_value" not in response.text
    assert payload["hardware"]["gpu"]["vram_mb"] == 24576
    assert payload["hardware"]["gpu"]["name"] == "NVIDIA GeForce RTX 3090"


def test_process_commands_are_shell_free_and_fixed_by_action(tmp_path):
    config = _config(tmp_path)
    controller = manager.ManagerProcessController(config)

    command = controller._command("setup")

    assert command[:3] == [manager.sys.executable, "-m", "bridge.manager_cli"]
    assert "setup" in command
    assert "--grid-url" in command
    assert config.grid_url in command
    assert not any(value in command for value in ("sh", "bash", "-c"))

    canary_command = controller._command("canary")
    assert "canary" in canary_command
    assert "--launch-runtime" in canary_command
    assert "--allow-unsigned-draft" in canary_command


def test_manager_log_redaction_covers_grid_keys_and_bearer_tokens():
    value = manager._redact_log(
        "api_key=grid_abcdefghijklmnopqrstuvwxyz Authorization: Bearer token-value"
    )

    assert "grid_abcdefghijklmnopqrstuvwxyz" not in value
    assert "token-value" not in value
    assert value.count("[redacted]") == 2


def test_manager_status_does_not_return_profile_exception_details(tmp_path, monkeypatch):
    config = _config(tmp_path)
    controller = manager.ManagerProcessController(config)
    monkeypatch.setattr(
        manager,
        "load_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("invalid profile at /secret/operator/path")
        ),
    )

    result = manager._manager_status(config, controller)

    assert result["profile"]["error"] == "Profile is unavailable or invalid"
    assert "/secret/operator/path" not in json.dumps(result)


def test_non_loopback_manager_bind_is_rejected(tmp_path):
    class Args:
        host = "0.0.0.0"
        port = 8791

    try:
        manager.run_manager_ui(Args())
    except RuntimeError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("non-loopback manager bind was accepted")

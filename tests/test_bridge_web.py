import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bridge.web import app as web_app
from bridge.web import routes
from bridge import comfyui_detect
from bridge import cli as bridge_cli
from bridge.config import Settings
from bridge import ws_worker
from bridge import model_mapper


LOCAL_ORIGIN = "http://127.0.0.1:7860"


def _client():
    return TestClient(web_app.app, base_url=LOCAL_ORIGIN)


def test_configured_bridge_pages_render(monkeypatch):
    monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
    client = _client()

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


def test_bridge_rejects_untrusted_host_and_cross_origin_mutation(monkeypatch):
    monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
    untrusted_host = TestClient(web_app.app, base_url="http://attacker.example")
    assert untrusted_host.get("/").status_code == 400

    client = _client()
    response = client.post(
        "/api/worker/restart",
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "invalid origin"}


def test_bridge_requires_json_for_settings():
    client = _client()
    response = client.post(
        "/api/settings",
        headers={"Origin": LOCAL_ORIGIN, "Content-Type": "text/plain"},
        content="{}",
    )
    assert response.status_code == 415
    assert response.json() == {"detail": "JSON required"}


def test_setup_inventory_reports_compatibility_without_advertising(monkeypatch):
    async def fake_initialize(self, _url):
        self.available_files = {"weights.safetensors"}

    monkeypatch.setattr(model_mapper.ModelMapper, "initialize", fake_initialize)
    monkeypatch.setattr(
        model_mapper.ModelMapper,
        "capability_report",
        lambda _self: [
            {
                "model": "example-model",
                "workflow": "example.json",
                "compatible": True,
                "reason": "ok",
            }
        ],
    )
    monkeypatch.setitem(web_app.worker_state, "bridge", None)
    client = _client()

    response = client.post(
        "/api/setup/inventory",
        headers={"Origin": LOCAL_ORIGIN},
        json={"url": "http://127.0.0.1:8188"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "detected": True,
        "weight_count": 1,
        "capabilities": [
            {
                "model": "example-model",
                "workflow": "example.json",
                "compatible": True,
                "reason": "ok",
            }
        ],
        "advertised": [],
    }


def test_setup_inventory_rejects_extra_fields():
    response = _client().post(
        "/api/setup/inventory",
        headers={"Origin": LOCAL_ORIGIN},
        json={"url": "http://127.0.0.1:8188", "command": "bad"},
    )
    assert response.status_code == 400


def test_media_settings_do_not_expose_unused_threads_control(monkeypatch):
    monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
    response = _client().get("/settings")
    assert response.status_code == 200
    assert "GRID_THREADS" not in response.text
    assert ">Threads<" not in response.text


def test_setup_exposes_capability_states_and_operator_selection():
    response = _client().get("/setup")
    assert response.status_code == 200
    for state in ("Detected", "Compatible", "Qualified", "Advertised"):
        assert state in response.text
    assert "toggleModel(capability.model)" in response.text


def test_browser_cannot_select_comfy_executable(monkeypatch):
    called = False

    def fake_install():
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(routes, "install_comfyui_via_cli", fake_install)
    client = _client()
    response = client.post(
        "/api/setup/install-comfyui",
        headers={"Origin": LOCAL_ORIGIN},
        json={"path": "~/ComfyUI", "comfy_bin": "/tmp/attacker"},
    )

    assert response.status_code == 400
    assert called is False


def test_comfy_installer_uses_only_server_discovered_executable(monkeypatch):
    commands = []
    monkeypatch.setattr(comfyui_detect, "_find_comfy_cli", lambda: "/trusted/bin/comfy")
    monkeypatch.setattr(
        comfyui_detect,
        "_validated_install_path",
        lambda _value: comfyui_detect.Path("/safe/ComfyUI"),
    )

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr(comfyui_detect.subprocess, "run", fake_run)

    result = comfyui_detect.install_comfyui_via_cli("~/ComfyUI")

    assert result["ok"] is True
    assert commands == [[
        "/trusted/bin/comfy",
        "install",
        "--skip-prompt",
        "--path",
        str(comfyui_detect.Path("/safe/ComfyUI")),
    ]]


def test_comfy_installer_does_not_return_exception_details(monkeypatch):
    monkeypatch.setattr(comfyui_detect, "_find_comfy_cli", lambda: "/trusted/bin/comfy")
    monkeypatch.setattr(
        comfyui_detect,
        "_validated_install_path",
        lambda _value: comfyui_detect.Path("/safe/ComfyUI"),
    )
    monkeypatch.setattr(
        comfyui_detect.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("/secret/path")),
    )

    result = comfyui_detect.install_comfyui_via_cli()

    assert result == {
        "ok": False,
        "error": "ComfyUI installation failed; see local worker logs",
    }
    assert "/secret/path" not in result["error"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1:8188",
        "http://user:password@127.0.0.1:8188",
        "http://169.254.169.254/latest/meta-data",
        "http://100.100.100.200/latest/meta-data",
        "http://[fd00:ec2::254]/latest/meta-data",
        "http://127.0.0.1:8188/?target=metadata",
    ],
)
def test_comfyui_url_rejects_unsafe_targets(url):
    with pytest.raises(ValueError):
        comfyui_detect.validated_comfyui_url(url)


def test_settings_reject_unknown_environment_keys():
    with pytest.raises(ValueError):
        routes._validated_settings_form({"PYTHONPATH": "/tmp/attacker"})


def test_settings_reject_environment_line_injection():
    with pytest.raises(ValueError):
        routes._validated_settings_form(
            {"GRID_WORKER_NAME": "worker\nGRID_API_KEY=attacker"}
        )


def test_settings_values_are_json_encoded_in_script(monkeypatch):
    marker = "</script><script>window.pwned=true</script>"
    monkeypatch.setattr(Settings, "GRID_WORKER_NAME", marker)
    monkeypatch.setitem(web_app.worker_state, "setup_complete", True)
    client = _client()

    response = client.get("/settings")

    assert response.status_code == 200
    assert marker not in response.text
    assert "\\u003c/script\\u003e" in response.text


def test_legacy_bridge_bind_is_loopback_only():
    assert bridge_cli.validated_bridge_host("LOCALHOST") == "localhost"
    with pytest.raises(RuntimeError):
        bridge_cli.validated_bridge_host("0.0.0.0")


def test_browser_install_path_is_home_contained():
    assert comfyui_detect._validated_install_path(None) == (
        comfyui_detect.Path.home() / "ComfyUI"
    ).resolve()
    outside = comfyui_detect.Path.home().resolve().parent / "outside-home"
    with pytest.raises(ValueError):
        comfyui_detect._validated_install_path(str(outside))


def test_legacy_browser_dependency_is_version_and_integrity_pinned():
    template = (Path(__file__).parents[1] / "bridge/web/templates/base.html").read_text()
    assert "alpinejs@3.16.1" in template
    assert "alpinejs@3.x.x" not in template
    assert 'integrity="sha384-' in template

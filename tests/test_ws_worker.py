import asyncio
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response
from eth_account import Account
from eth_account.messages import encode_defunct

from bridge.config import Settings
from bridge.identity import (
    create_delegation_request,
    delegation_message,
    generate_worker_key,
    install_delegation_certificate,
)
import bridge.ws_worker as ws_worker_module
from bridge.ws_worker import WSWorker, grid_ws_url, media_result_hash, resolve_output_seeds


def test_grid_ws_url_rejects_plaintext_outside_loopback(monkeypatch):
    monkeypatch.setattr(Settings, "GRID_STREAMING_URL", "")
    monkeypatch.setattr(Settings, "GRID_API_URL", "http://grid.example")
    monkeypatch.setattr(Settings, "GRID_WS_INSECURE", False)

    with pytest.raises(RuntimeError, match="refusing plaintext"):
        grid_ws_url()


def test_grid_ws_url_allows_loopback_or_explicit_insecure_mode(monkeypatch):
    monkeypatch.setattr(Settings, "GRID_STREAMING_URL", "")
    monkeypatch.setattr(Settings, "GRID_WS_INSECURE", False)
    monkeypatch.setattr(Settings, "GRID_API_URL", "http://127.0.0.1:8000")
    assert grid_ws_url() == "ws://127.0.0.1:8000/v1/workers/ws"

    monkeypatch.setattr(Settings, "GRID_API_URL", "http://grid.test")
    monkeypatch.setattr(Settings, "GRID_WS_INSECURE", True)
    assert grid_ws_url() == "ws://grid.test/v1/workers/ws"


def test_resolve_output_seeds_preserves_explicit_seed():
    assert resolve_output_seeds({"seed": 0}, 3) == [0, 1, 2]
    assert resolve_output_seeds({"seed": "42"}, 2) == [42, 43]


def test_resolve_output_seeds_preserves_seed_list():
    assert resolve_output_seeds({"seed": 9, "seeds": [5, "6"]}, 2) == [5, 6]


def test_resolve_output_seeds_rejects_invalid_seed():
    with pytest.raises(ValueError):
        resolve_output_seeds({"seed": -1}, 1)


@pytest.mark.asyncio
async def test_registration_payload_exposes_only_coarse_profile_metadata(tmp_path, monkeypatch):
    key_path = tmp_path / "worker-key.json"
    delegation_path = tmp_path / "delegation.json"
    generate_worker_key(key_path)
    wallet = Account.from_key("0x" + "44" * 32)
    request = create_delegation_request(
        worker_key_path=key_path,
        payout_wallet=wallet.address,
        worker_name=Settings.GRID_WORKER_NAME,
        chain_id=8453,
        audience="api.aipowergrid.io",
    )
    signature = Account.sign_message(
        encode_defunct(text=delegation_message(request["payload"])), wallet.key
    ).signature.hex()
    install_delegation_certificate(request, signature, delegation_path)
    monkeypatch.setattr(Settings, "GRID_WORKER_KEY_PATH", str(key_path))
    monkeypatch.setattr(Settings, "GRID_WORKER_DELEGATION_PATH", str(delegation_path))
    worker = WSWorker()
    worker.models = ["ace-step-v1.5-xl-turbo"]
    worker.job_types = ["audio"]
    worker.profile_metadata = {
        "id": "ace-step-v1.5-xl-turbo",
        "version": "0.1.0",
        "digest": "a" * 64,
        "signing_key_id": "release-key",
        "capability_tier": "audio.ace-step.standard",
        "runtime_adapter": "ace-step-1.5-api",
        "runtime_digest": "c" * 64,
        "recipe_root": "b" * 64,
        "canary_completed_at": "2026-07-15T00:00:00+00:00",
        "canary_elapsed_seconds": 8.5,
    }
    try:
        payload = worker.registration_payload()
    finally:
        await worker.comfy.aclose()

    assert payload["models"] == ["ace-step-v1.5-xl-turbo"]
    assert payload["job_types"] == ["audio"]
    assert payload["worker_profile"]["capability_tier"] == "audio.ace-step.standard"
    assert payload["worker_identity"]["payload"]["profile_digest"] == "a" * 64
    assert payload["worker_identity"]["payload"]["profile_recipe_root"] == "b" * 64
    assert "accelerator" not in str(payload)
    assert "ram" not in str(payload)
    assert "private_key" not in str(payload)


def test_media_result_hash_orders_outputs_by_index():
    ordered = [
        {"index": 0, "sha256": "a" * 64},
        {"index": 1, "sha256": "b" * 64},
    ]
    assert media_result_hash(list(reversed(ordered))) == media_result_hash(ordered)


@pytest.mark.asyncio
async def test_worker_observes_capacity_file_changes_without_restart(tmp_path, monkeypatch):
    capacity_file = tmp_path / "capacity.json"
    capacity_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(Settings, "GRID_CAPACITY_FILE", str(capacity_file))
    monkeypatch.setattr(Settings, "GRID_SCHEDULE", "")
    monkeypatch.setattr(Settings, "THREADS", 1)
    worker = WSWorker()
    try:
        assert worker._accepting_jobs() is True

        capacity_file.write_text(
            '[{"days":"daily","concurrency":0}]', encoding="utf-8"
        )
        assert worker._accepting_jobs() is False

        capacity_file.write_text("not-json", encoding="utf-8")
        assert worker._accepting_jobs() is False
        assert worker._capacity_error == "Schedule must be valid JSON"

        capacity_file.write_text("", encoding="utf-8")
        assert worker._accepting_jobs() is True
        assert worker._capacity_error is None
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
async def test_job_error_does_not_expose_worker_exception_details(monkeypatch):
    worker = WSWorker()
    socket = AsyncMock()
    monkeypatch.setattr(
        worker,
        "_generate_and_upload",
        AsyncMock(side_effect=RuntimeError("private path /operator/models/secret")),
    )
    try:
        await worker._handle_job(
            socket,
            {"id": "job-1", "model": "model", "job_type": "image", "payload": {}},
        )
    finally:
        await worker.comfy.aclose()

    message = ws_worker_module.json.loads(socket.send.await_args.args[0])
    assert message == {
        "type": "error",
        "id": "job-1",
        "message": "Worker generation failed; see operator logs",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("produced", [0, 1, 4, 5])
@pytest.mark.respx(assert_all_called=False)
async def test_batch_requires_exact_outputs_before_any_upload(monkeypatch, tmp_path, produced, respx_mock):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    monkeypatch.setattr(Settings, "GRID_WORKER_KEY_PATH", str(tmp_path / "absent-key"))
    prompt = respx_mock.post("http://127.0.0.1:8188/prompt").mock(
        return_value=Response(200, json={"prompt_id": "prompt-1"}))
    uploads = [respx_mock.put(f"https://storage.example/{i}").mock(return_value=Response(200)) for i in range(4)]
    worker, socket = WSWorker(), AsyncMock()
    monkeypatch.setattr(worker, "_relay_progress", AsyncMock())
    monkeypatch.setattr(worker, "_collect_outputs", AsyncMock(return_value=[
        (f"image-{i}".encode(), "image", f"{i}.webp") for i in range(produced)]))
    job = {
        "id": "batch-job", "model": "model", "job_type": "image",
        "payload": {"n": 4, "batch_size": 1, "seed": 20, "recipe_engine": "comfyui", "recipe_spec": {
            "1": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
            "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "test", "images": ["1", 0]}},
        }},
        "upload": [{"put_url": f"https://storage.example/{i}", "key": str(i), "content_type": "image/webp"} for i in range(4)],
    }
    try:
        await worker._handle_job(socket, job)
    finally:
        await worker.comfy.aclose()
    graph = ws_worker_module.json.loads(prompt.calls.last.request.content)["prompt"]
    assert graph["1"]["inputs"]["batch_size"] == 4
    assert job["payload"]["batch_size"] == 1
    message = ws_worker_module.json.loads(socket.send.await_args.args[0])
    if produced == 4:
        assert message["type"] == "done"
        assert [r["index"] for r in message["results"]] == [0, 1, 2, 3]
        assert [r["seed"] for r in message["results"]] == [20, 21, 22, 23]
        assert all(route.call_count == 1 for route in uploads)
    else:
        assert message["type"] == "error"
        assert all(not route.called for route in uploads)


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_count", [0, 3, 5])
async def test_batch_rejects_wrong_upload_slot_count_before_render(monkeypatch, slot_count):
    worker, socket = WSWorker(), AsyncMock()
    build = AsyncMock(side_effect=AssertionError("must not render"))
    monkeypatch.setattr(ws_worker_module, "build_workflow", build)
    try:
        await worker._handle_job(socket, {"id": "bad-slots", "model": "model", "job_type": "image",
            "payload": {"n": 4}, "upload": [{} for _ in range(slot_count)]})
    finally:
        await worker.comfy.aclose()
    build.assert_not_awaited()
    assert ws_worker_module.json.loads(socket.send.await_args.args[0])["type"] == "error"


@pytest.mark.asyncio
@respx.mock
async def test_comfy_health_check_requires_ready_http_runtime(monkeypatch):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    respx.get("http://127.0.0.1:8188/system_stats").mock(
        return_value=Response(503, text="starting")
    )
    worker = WSWorker()
    try:
        with pytest.raises(Exception):
            await worker._check_runtime_health()
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
async def test_sustained_runtime_failure_withdraws_worker(monkeypatch):
    monkeypatch.setattr(ws_worker_module, "RUNTIME_HEALTH_INTERVAL_S", 0)
    monkeypatch.setattr(ws_worker_module, "RUNTIME_HEALTH_FAILURE_LIMIT", 2)
    worker = WSWorker()

    async def unhealthy():
        raise RuntimeError("runtime down")

    monkeypatch.setattr(worker, "_check_runtime_health", unhealthy)
    try:
        with pytest.raises(RuntimeError, match="withdrawing worker capability"):
            await worker._monitor_runtime_health()
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
async def test_paused_schedule_waits_before_joining_grid(monkeypatch):
    worker = WSWorker()
    states = iter((False, False, True))
    sleeps = []
    monkeypatch.setattr(worker, "_accepting_jobs", lambda: next(states))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ws_worker_module.asyncio, "sleep", fake_sleep)
    try:
        await worker._wait_until_available()
    finally:
        await worker.comfy.aclose()

    assert sleeps == [ws_worker_module.CAPACITY_POLL_INTERVAL_S] * 2


@pytest.mark.asyncio
async def test_schedule_pause_waits_for_active_job_before_disconnect(monkeypatch):
    worker = WSWorker()
    worker.models = ["model"]
    socket = AsyncMock()
    receive_count = 0

    async def receive():
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return '{"type":"ready","worker_id":"worker-1"}'
        if receive_count == 2:
            return '{"type":"job","id":"job-1","model":"model","payload":{}}'
        await asyncio.Event().wait()

    socket.recv = AsyncMock(side_effect=receive)
    active_job_started = asyncio.Event()
    release_active_job = asyncio.Event()
    schedule_paused = asyncio.Event()

    async def handle_job(_socket, _message):
        active_job_started.set()
        await release_active_job.wait()

    async def wait_until_paused():
        await schedule_paused.wait()

    class SocketContext:
        async def __aenter__(self):
            return socket

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(worker, "_handle_job", handle_job)
    monkeypatch.setattr(worker, "_wait_until_paused", wait_until_paused)
    monkeypatch.setattr(worker, "_monitor_runtime_health", AsyncMock(side_effect=asyncio.Event().wait))
    monkeypatch.setattr(ws_worker_module, "grid_ws_url", lambda: "wss://grid.test/v1/workers/ws")
    monkeypatch.setattr(ws_worker_module, "grid_ws_ssl", lambda _url: None)
    monkeypatch.setattr(
        ws_worker_module.websockets,
        "connect",
        lambda *_args, **_kwargs: SocketContext(),
    )

    session = asyncio.create_task(worker._session())
    await asyncio.wait_for(active_job_started.wait(), timeout=1)
    schedule_paused.set()
    await asyncio.sleep(0)
    assert not session.done()
    release_active_job.set()
    try:
        await asyncio.wait_for(session, timeout=1)
    finally:
        await worker.comfy.aclose()

    assert socket.recv.await_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_collect_outputs_accepts_vhs_mp4_under_gifs(monkeypatch):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    worker = WSWorker()
    output = {
        "filename": "grid_job-1_00001-audio.mp4",
        "subfolder": "video",
        "type": "output",
    }
    respx.get("http://127.0.0.1:8188/history/prompt-1").mock(
        return_value=Response(
            200,
            json={
                "prompt-1": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {"140": {"gifs": [output]}},
                }
            },
        )
    )
    respx.get(
        "http://127.0.0.1:8188/view",
        params={
            "filename": output["filename"],
            "subfolder": output["subfolder"],
            "type": output["type"],
        },
    ).mock(return_value=Response(200, content=b"mp4-bytes"))

    try:
        items = await worker._collect_outputs("prompt-1", "video")
    finally:
        await worker.comfy.aclose()

    assert items == [(b"mp4-bytes", "video", output["filename"])]


@pytest.mark.asyncio
@respx.mock
async def test_collect_outputs_waits_past_partial_unsupported_output(monkeypatch):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(ws_worker_module.asyncio, "sleep", no_sleep)
    worker = WSWorker()
    output = {"filename": "grid_job-2.mp4", "type": "output"}
    respx.get("http://127.0.0.1:8188/history/prompt-partial").mock(
        side_effect=[
            Response(
                200,
                json={
                    "prompt-partial": {
                        "status": {"status_str": "running", "completed": False},
                        "outputs": {"log": {"text": ["still rendering"]}},
                    }
                },
            ),
            Response(
                200,
                json={
                    "prompt-partial": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"140": {"gifs": [output]}},
                    }
                },
            ),
        ]
    )
    respx.get(
        "http://127.0.0.1:8188/view",
        params={"filename": output["filename"], "type": output["type"]},
    ).mock(return_value=Response(200, content=b"video"))

    try:
        items = await worker._collect_outputs("prompt-partial", "video")
    finally:
        await worker.comfy.aclose()

    assert items[0][0] == b"video"


@pytest.mark.asyncio
@respx.mock
async def test_collect_outputs_fails_fast_when_completed_without_output(monkeypatch):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    worker = WSWorker()
    respx.get("http://127.0.0.1:8188/history/prompt-2").mock(
        return_value=Response(
            200,
            json={
                "prompt-2": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {},
                }
            },
        )
    )

    try:
        with pytest.raises(RuntimeError, match="completed but produced no supported"):
            await worker._collect_outputs("prompt-2", "video")
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_collect_outputs_surfaces_comfy_execution_error(monkeypatch):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    worker = WSWorker()
    respx.get("http://127.0.0.1:8188/history/prompt-3").mock(
        return_value=Response(
            200,
            json={
                "prompt-3": {
                    "status": {
                        "status_str": "error",
                        "completed": False,
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "node_type": "SaveVideo",
                                    "exception_type": "EncoderError",
                                    "exception_message": "encoder failed\nprivate details",
                                },
                            ]
                        ],
                    },
                    "outputs": {},
                }
            },
        )
    )

    try:
        with pytest.raises(RuntimeError, match="SaveVideo: EncoderError"):
            await worker._collect_outputs("prompt-3", "video")
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_collect_outputs_interrupts_at_deadline(monkeypatch):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    monkeypatch.setattr(Settings, "COMFYUI_JOB_TIMEOUT", 0)
    monkeypatch.setattr(Settings, "COMFYUI_STUCK_PROCESS_PATTERN", "")
    worker = WSWorker()
    respx.get("http://127.0.0.1:8188/history/prompt-4").mock(
        return_value=Response(
            200,
            json={
                "prompt-4": {
                    "status": {"status_str": "running", "completed": False},
                    "outputs": {},
                }
            },
        )
    )
    interrupt = respx.post("http://127.0.0.1:8188/interrupt").mock(
        return_value=Response(200)
    )

    try:
        with pytest.raises(RuntimeError, match="interrupted ComfyUI"):
            await worker._collect_outputs("prompt-4", "video")
    finally:
        await worker.comfy.aclose()

    assert interrupt.called

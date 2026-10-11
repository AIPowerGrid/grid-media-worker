# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Actual local SQLite crash/race proofs; HTTP/GPU/storage remain synthetic."""

import asyncio
import copy
import hashlib
import json
import multiprocessing
import os
import sqlite3
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import respx

import bridge.render_journal as module
import bridge.ws_worker as ws_module
from bridge.config import Settings
from bridge.render_journal import (
    MARKER,
    DurableVideoRenderer,
    RenderFailed,
    RenderJournal,
    RenderUncertain,
    digest,
    graph_digest,
)
from bridge.ws_worker import WSWorker

ORIGIN = "http://127.0.0.1:8188"
GRAPH = {"1": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "synthetic"}}}
VIDEO = b"\x00\x00\x00\x18ftypisom" + b"synthetic-video-not-a-codec-proof"
NAMESPACE = digest({"runtime": "test"})
BINDING = digest({"job": "test"})


@pytest.fixture
def journal(tmp_path):
    return RenderJournal(tmp_path / "private", namespace=NAMESPACE)


def ready(journal, job_id=None):
    job_id = job_id or str(uuid4())
    journal.begin(job_id, BINDING)
    assert journal.arm(job_id, digest(GRAPH))
    journal.running(job_id)
    return journal.get(job_id)


def history(row, *, completed=True):
    return {
        row.prompt_id: {
            "prompt": [
                1,
                row.prompt_id,
                copy.deepcopy(GRAPH),
                {
                    MARKER: {
                        "job_id": row.job_id,
                        "binding": row.binding,
                        "graph_hash": row.graph_hash,
                    }
                },
            ],
            "status": {"completed": completed, "status_str": "success"},
            "outputs": {
                "1": {
                    "videos": [
                        {"filename": "test.mp4", "subfolder": "", "type": "output"}
                    ]
                }
            },
        }
    }


def _arm_process(arguments):
    directory, job_id = arguments
    return RenderJournal(directory, namespace=NAMESPACE).arm(job_id, digest(GRAPH))


def test_actual_sqlite_process_race_allows_one_submission(journal):
    job_id = str(uuid4())
    journal.begin(job_id, BINDING)
    with multiprocessing.get_context("spawn").Pool(4) as processes:
        results = processes.map(_arm_process, [(str(journal.directory), job_id)] * 12)
    assert results.count(True) == 1
    assert journal.get(job_id).phase == "submitting"


def test_identity_conflicts_and_unknown_cache_fail_closed(journal):
    row = ready(journal)
    with pytest.raises(RenderUncertain, match="conflicts"):
        journal.begin(row.job_id, digest("different request"))
    with pytest.raises(RenderUncertain, match="another runtime"):
        RenderJournal(journal.directory, namespace=digest("another identity"))
    with pytest.raises(RenderUncertain, match="missing"):
        journal.cache(str(uuid4()), VIDEO)
    for invalid in ("not-uuid", str(uuid4()).upper(), "../render"):
        with pytest.raises(RenderUncertain, match="UUID"):
            journal.begin(invalid, BINDING)
    with pytest.raises(RenderUncertain, match="SHA-256"):
        journal.begin(str(uuid4()), "not-a-hash")
    with pytest.raises(RenderUncertain, match="SHA-256"):
        journal.arm(row.job_id, "A" * 64)


def test_numeric_commitment_preserves_boolean_strings_fraction_and_large_integer():
    assert digest({"fps": 24}) != digest({"fps": 24.0})
    assert graph_digest({"fps": 24}) == graph_digest({"fps": 24.0})
    assert graph_digest({"value": 1}) != graph_digest({"value": True})
    assert graph_digest({"fps": 24}) != graph_digest({"fps": "24"})
    assert graph_digest({"fps": 24}) != graph_digest({"fps": 24.01})
    assert graph_digest({"seed": 2**53}) != graph_digest({"seed": float(2**53)})


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [24.0, 24.1, "24", True])
@respx.mock
async def test_runtime_number_normalization_uses_presubmission_commitment(
    journal, observed
):
    job_id = str(uuid4())
    graph = copy.deepcopy(GRAPH)
    graph["1"]["inputs"]["fps"] = 24

    def accepted(request):
        row = journal.require(job_id)
        assert row.graph_hash == digest(graph)
        assert row.normalized_graph_hash == graph_digest(graph)
        return httpx.Response(200, json={"prompt_id": row.prompt_id})

    post = respx.post(ORIGIN + "/prompt").mock(side_effect=accepted)

    def observed_history(_):
        row = journal.require(job_id)
        data = history(row)
        data[row.prompt_id]["prompt"][2] = copy.deepcopy(graph)
        data[row.prompt_id]["prompt"][2]["1"]["inputs"]["fps"] = observed
        return httpx.Response(200, json=data)

    respx.get(url__regex=ORIGIN + r"/history/.*").mock(side_effect=observed_history)
    download = respx.get(ORIGIN + "/view").mock(
        return_value=httpx.Response(200, content=VIDEO)
    )
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        renderer = DurableVideoRenderer(client, journal)
        if type(observed) is float and observed == 24:
            assert (
                await renderer.render(job_id, BINDING, AsyncMock(return_value=graph))
            )[0] == VIDEO
            assert download.call_count == 1
        else:
            with pytest.raises(RenderUncertain, match="conflicts"):
                await renderer.render(job_id, BINDING, AsyncMock(return_value=graph))
            assert download.call_count == 0
    assert post.call_count == 1


def test_old_state_migration_does_not_infer_a_graph_commitment(journal):
    row = ready(journal)
    assert row.normalized_graph_hash is None
    with sqlite3.connect(journal.path) as database:
        database.execute("ALTER TABLE renders DROP COLUMN normalized_graph_hash")
    restarted = RenderJournal(journal.directory, namespace=NAMESPACE)
    retained = restarted.require(row.job_id)
    assert retained.prompt_id == row.prompt_id
    assert retained.normalized_graph_hash is None
    # Old rows can still match their exact original hash, but never gain a
    # numeric-normalized commitment fabricated from whatever history reports.
    assert DurableVideoRenderer._matches(
        history(retained)[row.prompt_id]["prompt"], retained
    )


def test_cache_commit_survives_restart_ack_and_unknown_legacy_ack(journal):
    row = ready(journal)
    journal.cache(row.job_id, VIDEO)
    restarted = RenderJournal(journal.directory, namespace=NAMESPACE)
    assert restarted.cached(row.job_id) == (VIDEO, row.prompt_id + ".mp4")
    restarted.acknowledge(row.job_id)
    restarted.acknowledge(row.job_id)
    restarted.acknowledge("legacy-image-job")
    restarted.acknowledge(str(uuid4()))
    assert restarted.get(row.job_id).phase == "acknowledged"
    assert restarted.get(row.job_id).closed_at is not None
    assert not (journal.directory / (row.prompt_id + ".mp4")).exists()
    with pytest.raises(RenderUncertain, match="closed"):
        restarted.cached(row.job_id)
    with pytest.raises(RenderUncertain, match="closed"):
        restarted.cache(row.job_id, VIDEO + b"different")


def test_crash_after_rename_recovers_only_committed_bytes(journal, monkeypatch):
    row = ready(journal)
    real_sync = module._sync_directory
    monkeypatch.setattr(
        module,
        "_sync_directory",
        lambda _: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        journal.cache(row.job_id, VIDEO)
    assert journal.get(row.job_id).phase == "caching"
    monkeypatch.setattr(module, "_sync_directory", real_sync)
    restarted = RenderJournal(journal.directory, namespace=NAMESPACE)
    assert restarted.cached(row.job_id)[0] == VIDEO
    assert restarted.get(row.job_id).phase == "cached"


def test_crash_before_rename_reloads_same_history_bytes(journal, monkeypatch):
    row = ready(journal)
    real_replace = module.os.replace
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(RuntimeError("before rename")),
    )
    with pytest.raises(RuntimeError, match="before rename"):
        journal.cache(row.job_id, VIDEO)
    assert journal.get(row.job_id).phase == "caching"
    assert journal.cached(row.job_id) is None
    monkeypatch.setattr(module.os, "replace", real_replace)
    journal.cache(row.job_id, VIDEO)
    assert journal.cached(row.job_id)[0] == VIDEO


@pytest.mark.parametrize("attack", ["tamper", "symlink", "missing"])
def test_retained_file_tampering_never_becomes_new_render(journal, attack):
    row = ready(journal)
    journal.cache(row.job_id, VIDEO)
    path = journal.directory / (row.prompt_id + ".mp4")
    if attack == "tamper":
        path.write_bytes(VIDEO + b"bad")
    else:
        path.unlink()
        if attack == "symlink":
            path.symlink_to(journal.directory / "missing-target")
    with pytest.raises(RenderUncertain):
        journal.cached(row.job_id)
    assert journal.get(row.job_id).phase == "cached"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permissions; Windows ACL qualification remains gated"
)
def test_private_directory_and_database_required(tmp_path, journal):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(RenderUncertain, match="private"):
        RenderJournal(public, namespace=NAMESPACE)
    journal.path.chmod(0o644)
    with pytest.raises(RenderUncertain, match="private"):
        journal.get(str(uuid4()))


def test_storage_and_commitment_bounds(journal, monkeypatch):
    row = ready(journal)
    monkeypatch.setattr(module, "MAX_CACHE_BYTES", len(VIDEO) - 1)
    with pytest.raises(RenderUncertain, match="cache is full"):
        journal.cache(row.job_id, VIDEO)
    assert journal.get(row.job_id).sha256 is None
    monkeypatch.setattr(module, "MAX_CACHE_BYTES", 1024)
    monkeypatch.setattr(module, "MAX_JOBS", 1)
    with pytest.raises(RenderUncertain, match="journal is full"):
        journal.begin(str(uuid4()), BINDING)
    monkeypatch.setattr(module, "MAX_GRAPH_BYTES", 8)
    with pytest.raises(RenderUncertain, match="size limit"):
        digest(GRAPH)
    with pytest.raises(RenderUncertain, match="Invalid render"):
        digest(float("nan"))
    with pytest.raises(RenderFailed, match="MP4"):
        journal.cache(row.job_id, b"not-a-video")


@pytest.mark.asyncio
@respx.mock
async def test_lost_acceptance_reply_recovers_same_prompt_with_one_post(journal):
    job_id, build = str(uuid4()), AsyncMock(return_value=copy.deepcopy(GRAPH))

    def lost_reply(request):
        submitted = json.loads(request.content)
        restarted = RenderJournal(journal.directory, namespace=NAMESPACE)
        row = restarted.get(job_id)
        assert row.phase == "submitting"
        assert submitted["prompt_id"] == row.prompt_id
        assert submitted["client_id"] == row.prompt_id
        assert submitted["extra_data"][MARKER]["graph_hash"] == digest(GRAPH)
        raise httpx.ReadError("synthetic reply loss", request=request)

    post = respx.post(ORIGIN + "/prompt").mock(side_effect=lost_reply)
    respx.get(url__regex=ORIGIN + r"/history/.*").mock(
        side_effect=lambda _: httpx.Response(200, json=history(journal.get(job_id)))
    )
    respx.get(ORIGIN + "/view").mock(return_value=httpx.Response(200, content=VIDEO))
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        rendered = await DurableVideoRenderer(client, journal).render(
            job_id, BINDING, build
        )
        # A new process object needs neither the original graph nor ComfyUI/R2.
        restarted = RenderJournal(journal.directory, namespace=NAMESPACE)
        another_build = AsyncMock(side_effect=AssertionError("must not rebuild"))
        assert (
            await DurableVideoRenderer(client, restarted).render(
                job_id, BINDING, another_build
            )
            == rendered
        )
    assert rendered[0] == VIDEO
    assert post.call_count == 1
    build.assert_awaited_once()

    another_build.assert_not_awaited()


@pytest.mark.asyncio
@respx.mock
async def test_restart_while_running_reads_history_without_reposting(journal):
    row = ready(journal)
    build = AsyncMock(side_effect=AssertionError("must not rebuild"))
    respx.get(ORIGIN + f"/history/{row.prompt_id}").mock(
        return_value=httpx.Response(200, json=history(row))
    )
    respx.get(ORIGIN + "/view").mock(return_value=httpx.Response(200, content=VIDEO))
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        assert (
            await DurableVideoRenderer(
                client, RenderJournal(journal.directory, namespace=NAMESPACE)
            ).render(row.job_id, BINDING, build)
        )[0] == VIDEO
    build.assert_not_awaited()


@pytest.mark.asyncio
@respx.mock
async def test_native_savevideo_images_envelope_recovers_as_video(journal):
    row = ready(journal)
    data = history(row)
    data[row.prompt_id]["outputs"]["1"] = {
        "images": [{"filename": "test.mp4", "subfolder": "avd", "type": "output"}],
        "animated": [True],
    }
    respx.get(ORIGIN + f"/history/{row.prompt_id}").mock(
        return_value=httpx.Response(200, json=data)
    )
    view = respx.get(ORIGIN + "/view").mock(
        return_value=httpx.Response(200, content=VIDEO)
    )
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        assert (
            await DurableVideoRenderer(client, journal).render(
                row.job_id, BINDING, AsyncMock()
            )
        )[0] == VIDEO
    assert view.calls[0].request.url.params["subfolder"] == "avd"


@pytest.mark.asyncio
@respx.mock
async def test_legacy_ws_collector_preserves_native_mp4_not_image(monkeypatch):
    monkeypatch.setattr(Settings, "COMFYUI_URL", ORIGIN)
    worker = WSWorker()
    respx.get(ORIGIN + "/history/native").mock(
        return_value=httpx.Response(
            200,
            json={
                "native": {
                    "status": {"completed": True},
                    "outputs": {
                        "save": {
                            "images": [
                                {
                                    "filename": "test.mp4",
                                    "subfolder": "avd",
                                    "type": "output",
                                }
                            ],
                            "animated": [True],
                        }
                    },
                }
            },
        )
    )
    respx.get(ORIGIN + "/view").mock(return_value=httpx.Response(200, content=VIDEO))
    try:
        assert await worker._collect_outputs("native", "video") == [
            (VIDEO, "video", "test.mp4")
        ]
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_crash_between_arm_and_post_does_not_resubmit_or_interrupt(
    journal, monkeypatch
):
    row = journal.begin(str(uuid4()), BINDING)
    journal.arm(row.job_id, digest(GRAPH))
    clock = [row.created + 1]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])

    async def advance_clock(_):
        clock[0] += 3

    monkeypatch.setattr(module.asyncio, "sleep", advance_clock)
    respx.get(ORIGIN + f"/history/{row.prompt_id}").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(ORIGIN + "/queue").mock(
        return_value=httpx.Response(
            200, json={"queue_running": [], "queue_pending": []}
        )
    )
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        with pytest.raises(RenderUncertain, match="deadline"):
            await DurableVideoRenderer(
                client, journal, timeout=2, poll_seconds=0.001
            ).render(row.job_id, BINDING, AsyncMock())
    assert journal.get(row.job_id).phase == "submitting"
    assert all(call.request.method == "GET" for call in respx.calls)


@pytest.mark.asyncio
@respx.mock
async def test_expired_unsubmitted_render_never_builds_or_posts(journal):
    row = journal.begin(str(uuid4()), BINDING)
    with sqlite3.connect(journal.path) as database:
        database.execute("UPDATE renders SET created=?", (time.time() - 1000,))
    build = AsyncMock()
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        with pytest.raises(RenderUncertain, match="deadline"):
            await DurableVideoRenderer(client, journal).render(
                row.job_id, BINDING, build
            )
    build.assert_not_awaited()
    assert not respx.calls
    assert journal.get(row.job_id).phase == "prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"prompt_id": str(uuid4())}, {"error": "accepted?"}])
@respx.mock
async def test_unproven_acceptance_is_retained_not_retried(journal, reply):
    row = journal.begin(str(uuid4()), BINDING)
    post = respx.post(ORIGIN + "/prompt").mock(
        return_value=httpx.Response(200, json=reply)
    )
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        with pytest.raises(RenderUncertain, match="acceptance"):
            await DurableVideoRenderer(client, journal).render(
                row.job_id, BINDING, AsyncMock(return_value=GRAPH)
            )
    assert post.call_count == 1
    assert journal.get(row.job_id).phase == "submitting"


@pytest.mark.asyncio
@respx.mock
async def test_definitive_rejection_stays_failed_without_second_post(journal):
    row = journal.begin(str(uuid4()), BINDING)
    post = respx.post(ORIGIN + "/prompt").mock(
        return_value=httpx.Response(400, json={"error": "invalid graph"})
    )
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        for _ in range(2):
            with pytest.raises(RenderFailed):
                await DurableVideoRenderer(client, journal).render(
                    row.job_id, BINDING, AsyncMock(return_value=GRAPH)
                )
    assert post.call_count == 1
    assert journal.get(row.job_id).phase == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack",
    [
        "binding",
        "graph",
        "status",
        "queue-shape",
        "duplicate-prompt",
        "oversize",
        "invalid-json",
    ],
)
@respx.mock
async def test_untrusted_observations_cannot_rebind_or_rerender(
    journal, monkeypatch, attack
):
    row = ready(journal)
    data = history(row)
    if attack == "binding":
        data[row.prompt_id]["prompt"][3][MARKER]["binding"] = digest("wrong")
    elif attack == "graph":
        data[row.prompt_id]["prompt"][2]["1"]["inputs"]["filename_prefix"] = "different"
    elif attack == "status":
        data[row.prompt_id]["status"] = ["not an object"]
    elif attack in {"queue-shape", "duplicate-prompt"}:
        entry = data[row.prompt_id]["prompt"]
        data = {}
        queue = (
            {"queue_running": [entry, entry], "queue_pending": []}
            if attack == "duplicate-prompt"
            else {"queue_running": "bad", "queue_pending": []}
        )
        respx.get(ORIGIN + "/queue").mock(return_value=httpx.Response(200, json=queue))
    response = httpx.Response(200, json=data)
    if attack == "oversize":
        monkeypatch.setattr(module, "MAX_OBSERVATION_BYTES", 8)
    elif attack == "invalid-json":
        response = httpx.Response(200, content=b"not-json")
    respx.get(ORIGIN + f"/history/{row.prompt_id}").mock(return_value=response)
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        with pytest.raises(RenderUncertain):
            await DurableVideoRenderer(client, journal).render(
                row.job_id, BINDING, AsyncMock()
            )
    assert all(call.request.method == "GET" for call in respx.calls)
    assert journal.get(row.job_id).phase == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["backend-error", "missing-output", "bad-mp4", "oversize-mp4", "unsafe-path"],
)
@respx.mock
async def test_terminal_backend_output_failure_is_not_paid_success(
    journal, monkeypatch, outcome
):
    row = ready(journal)
    data = history(row)
    if outcome == "backend-error":
        data[row.prompt_id]["status"] = {"status_str": "error", "completed": False}
    elif outcome == "missing-output":
        data[row.prompt_id]["outputs"] = {}
    elif outcome == "unsafe-path":
        data[row.prompt_id]["outputs"]["1"]["videos"][0]["subfolder"] = "../private"
    else:
        video = b"not MP4 data" if outcome == "bad-mp4" else VIDEO
        if outcome == "oversize-mp4":
            monkeypatch.setattr(module, "MAX_VIDEO_BYTES", len(VIDEO) - 1)
        respx.get(ORIGIN + "/view").mock(
            return_value=httpx.Response(200, content=video)
        )
    respx.get(ORIGIN + f"/history/{row.prompt_id}").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient(base_url=ORIGIN) as client:
        with pytest.raises(
            RenderUncertain if outcome == "unsafe-path" else RenderFailed
        ):
            await DurableVideoRenderer(client, journal).render(
                row.job_id, BINDING, AsyncMock()
            )
    assert journal.get(row.job_id).phase == (
        "running" if outcome == "unsafe-path" else "failed"
    )
    assert journal.cached(row.job_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["upload", "done"])
@respx.mock
async def test_actual_ws_job_recovers_cached_bytes_on_new_worker(
    journal, monkeypatch, tmp_path, failure
):
    monkeypatch.setattr(Settings, "COMFYUI_URL", ORIGIN)
    monkeypatch.setattr(Settings, "GRID_API_KEY", "synthetic-noncredential")
    monkeypatch.setattr(Settings, "GRID_COMFYUI_STATE_DIR", str(journal.directory))
    monkeypatch.setattr(Settings, "GRID_WORKER_KEY_PATH", str(tmp_path / "absent-key"))
    build = AsyncMock(return_value=copy.deepcopy(GRAPH))
    monkeypatch.setattr(ws_module, "build_workflow", build)
    monkeypatch.setattr(WSWorker, "_relay_progress", AsyncMock())
    job = {
        "id": str(uuid4()),
        "model": "ltx-test",
        "job_type": "video",
        "payload": {
            "_grid_media_job_version": 1,
            "n": 1,
            "seed": 7,
            "seeds": [7],
            "recipe_engine": "comfyui",
            "recipe_spec": copy.deepcopy(GRAPH),
        },
        "upload": [
            {
                "put_url": "https://storage.example/expired",
                "key": "test",
                "content_type": "video/mp4",
            }
        ],
    }
    # The real worker owns a namespace bound to credential/runtime/name, not our unit-test namespace.
    state_dir = tmp_path / "worker-state"
    monkeypatch.setattr(Settings, "GRID_COMFYUI_STATE_DIR", str(state_dir))
    worker, socket = WSWorker(), AsyncMock()
    core_worker_id = str(uuid4())
    worker._grid_worker_id = core_worker_id
    monkeypatch.setattr(
        ws_module, "fetch_states", AsyncMock(return_value={job["id"]: "held"})
    )

    def accepted(request):
        assert worker._render_journal.get(job["id"]).phase == "submitting"
        return httpx.Response(
            200, json={"prompt_id": json.loads(request.content)["prompt_id"]}
        )

    post = respx.post(ORIGIN + "/prompt").mock(side_effect=accepted)
    respx.get(url__regex=ORIGIN + r"/history/.*").mock(
        side_effect=lambda _: httpx.Response(
            200, json=history(worker._render_journal.get(job["id"]))
        )
    )
    respx.get(ORIGIN + "/view").mock(return_value=httpx.Response(200, content=VIDEO))
    first_upload = respx.put("https://storage.example/expired").mock(
        return_value=httpx.Response(403 if failure == "upload" else 200)
    )
    if failure == "done":
        socket.send.side_effect = httpx.WriteError("synthetic disconnect")
    try:
        with pytest.raises(RenderUncertain, match="recovery"):
            await worker._handle_job(socket, job)
        socket.close.assert_awaited_once()
        assert not any(
            json.loads(call.args[0])["type"] == "error"
            for call in socket.send.await_args_list
        )
        assert worker._render_journal.cached(job["id"])[0] == VIDEO
    finally:
        await worker.comfy.aclose()
    assert first_upload.call_count == 1
    second, recovered_socket = WSWorker(), AsyncMock()
    second._grid_worker_id = core_worker_id
    monkeypatch.setattr(Settings, "GRID_API_KEY", "rotated-synthetic-noncredential")
    job["upload"][0]["put_url"] = "https://storage.example/fresh"
    job["resume"] = True
    fresh_upload = respx.put("https://storage.example/fresh").mock(
        return_value=httpx.Response(200)
    )
    try:
        await second._handle_job(recovered_socket, job)
        result = json.loads(recovered_socket.send.await_args.args[0])
        assert result["type"] == "done"
        assert result["results"] == [
            {"index": 0, "seed": 7, "sha256": hashlib.sha256(VIDEO).hexdigest()}
        ]
        assert fresh_upload.calls[0].request.content == VIDEO
        assert (
            "async-video-resume-v1"
            not in second.registration_payload()["media_features"]
        )
        second._render_journal.acknowledge(job["id"])
    finally:
        await second.comfy.aclose()
    assert post.call_count == 1
    build.assert_awaited_once()

    other, other_socket = WSWorker(), AsyncMock()
    other._grid_worker_id = str(uuid4())
    try:
        with pytest.raises(RenderUncertain):
            await other._handle_job(other_socket, job)
        other_socket.send.assert_not_awaited()
        other_socket.close.assert_awaited_once()
    finally:
        await other.comfy.aclose()
    assert post.call_count == 1
    build.assert_awaited_once()


@pytest.mark.asyncio
async def test_core_resume_with_missing_local_identity_never_renders(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(Settings, "COMFYUI_URL", ORIGIN)
    monkeypatch.setattr(Settings, "GRID_API_KEY", "synthetic-noncredential")
    monkeypatch.setattr(
        Settings, "GRID_COMFYUI_STATE_DIR", str(tmp_path / "missing-history")
    )
    build = AsyncMock(
        side_effect=AssertionError("must not rebuild missing recovery state")
    )
    monkeypatch.setattr(ws_module, "build_workflow", build)
    job = {
        "id": str(uuid4()),
        "resume": True,
        "model": "ltx-test",
        "job_type": "video",
        "payload": {
            "_grid_media_job_version": 1,
            "n": 1,
            "seed": 7,
            "seeds": [7],
            "recipe_engine": "comfyui",
            "recipe_spec": copy.deepcopy(GRAPH),
        },
        "upload": [
            {"put_url": "https://storage.example/test", "content_type": "video/mp4"}
        ],
    }
    worker, socket = WSWorker(), AsyncMock()
    worker._grid_worker_id = str(uuid4())
    try:
        with pytest.raises(RenderUncertain):
            await worker._handle_job(socket, job)
        build.assert_not_awaited()
        socket.send.assert_not_awaited()
        socket.close.assert_awaited_once()
        assert worker._render_journal.get(job["id"]) is None
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
async def test_comfy_submission_client_does_not_use_environment_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "unsupported://127.0.0.1:9")
    monkeypatch.setattr(Settings, "COMFYUI_URL", ORIGIN)
    worker = WSWorker()
    try:
        assert worker.comfy.trust_env is False
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
async def test_authenticated_ready_captures_core_identity_and_rechecks_journal(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(Settings, "GRID_API_KEY", "synthetic-noncredential")
    monkeypatch.setattr(Settings, "GRID_WORKER_KEY_PATH", str(tmp_path / "absent-key"))
    monkeypatch.setattr(
        Settings, "GRID_WORKER_DELEGATION_PATH", str(tmp_path / "absent-delegation")
    )
    core_id, worker, socket = str(uuid4()), WSWorker(), AsyncMock()
    worker._render_journal = (
        object()
    )  # A prior session's handle must not bypass its new owner check.
    socket.recv.side_effect = [
        json.dumps({"type": "ready", "worker_id": core_id}),
        asyncio.CancelledError(),
    ]

    async def wait_until_cancelled():
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_monitor_runtime_health", wait_until_cancelled)
    monkeypatch.setattr(worker, "_wait_until_paused", wait_until_cancelled)
    monkeypatch.setattr(
        ws_module, "grid_ws_url", lambda: "wss://grid.test/v1/workers/ws"
    )
    monkeypatch.setattr(ws_module, "grid_ws_ssl", lambda _: None)

    class SocketContext:
        async def __aenter__(self):
            return socket

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(
        ws_module.websockets, "connect", lambda *_, **__: SocketContext()
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker._session()
        assert worker._grid_worker_id == core_id
        assert worker._render_journal is None
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("core_id", [None, "worker-not-a-uuid", str(uuid4()).upper()])
async def test_durable_video_requires_a_canonical_core_identity(
    monkeypatch, tmp_path, core_id
):
    monkeypatch.setattr(Settings, "COMFYUI_URL", ORIGIN)
    monkeypatch.setattr(Settings, "GRID_COMFYUI_STATE_DIR", str(tmp_path / "state"))
    build = AsyncMock(
        side_effect=AssertionError("must not render an unregistered worker")
    )
    monkeypatch.setattr(ws_module, "build_workflow", build)
    worker, socket = WSWorker(), AsyncMock()
    worker._grid_worker_id = core_id
    job = {
        "id": str(uuid4()),
        "model": "ltx-test",
        "job_type": "video",
        "payload": {
            "_grid_media_job_version": 1,
            "n": 1,
            "seed": 7,
            "seeds": [7],
            "recipe_engine": "comfyui",
            "recipe_spec": copy.deepcopy(GRAPH),
        },
        "upload": [
            {"put_url": "https://storage.example/test", "content_type": "video/mp4"}
        ],
    }
    try:
        with pytest.raises(RenderUncertain):
            await worker._handle_job(socket, job)
        build.assert_not_awaited()
        socket.send.assert_not_awaited()
        socket.close.assert_awaited_once()
        assert worker._render_journal is None
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["missing-state", "remote-runtime", "batch", "version", "n-type"]
)
async def test_worker_rejects_unsupported_durable_contract_without_gpu(
    monkeypatch, invalid, tmp_path
):
    monkeypatch.setattr(Settings, "COMFYUI_URL", ORIGIN)
    monkeypatch.setattr(Settings, "GRID_COMFYUI_STATE_DIR", str(tmp_path / "state"))
    job = {
        "id": str(uuid4()),
        "model": "ltx-test",
        "job_type": "video",
        "payload": {
            "_grid_media_job_version": 1,
            "n": 1,
            "seed": 7,
            "seeds": [7],
            "recipe_engine": "comfyui",
            "recipe_spec": copy.deepcopy(GRAPH),
        },
        "upload": [
            {"put_url": "https://storage.example/test", "content_type": "video/mp4"}
        ],
    }
    if invalid == "missing-state":
        monkeypatch.setattr(Settings, "GRID_COMFYUI_STATE_DIR", "")
    elif invalid == "remote-runtime":
        monkeypatch.setattr(Settings, "COMFYUI_URL", "https://runtime.example")
    elif invalid == "batch":
        job["payload"]["n"] = 2
    elif invalid == "version":
        job["payload"]["_grid_media_job_version"] = True
    else:
        job["payload"]["n"] = "1"
    worker, socket = WSWorker(), AsyncMock()
    try:
        await worker._handle_job(socket, job)
        assert json.loads(socket.send.await_args.args[0])["type"] == "error"
        socket.close.assert_not_awaited()
        assert worker._render_journal is None
    finally:
        await worker.comfy.aclose()

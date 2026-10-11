# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real private SQLite/cache; Core/Comfy transports remain synthetic."""

import asyncio
import json
import multiprocessing
import os
import sqlite3
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import respx

import bridge.render_journal as journal_module
import bridge.render_retention as module
import bridge.ws_worker as ws_module
from bridge.config import Settings
from bridge.render_journal import RenderJournal, RenderUncertain, digest
from bridge.ws_worker import WSWorker

ORIGIN = "https://grid.example"
URL = ORIGIN + "/v1/workers/self/media-jobs"
NAMESPACE = digest("synthetic-retention-owner")
VIDEO = b"\x00\x00\x00\x18ftypisom" + b"synthetic-not-decoded"


def cached(journal):
    row = journal.begin(str(uuid4()), digest("synthetic-binding"))
    journal.arm(row.job_id, digest("synthetic-graph"))
    journal.running(row.job_id)
    journal.cache(row.job_id, VIDEO)
    return journal.require(row.job_id)


def envelope(worker_id, job_ids, state="closed"):
    return {
        "schema": "aipg.worker.media-journal.v1",
        "worker_id": worker_id,
        "jobs": [{"job_id": jid, "state": state} for jid in job_ids],
    }


@pytest.fixture
def journal(tmp_path):
    return RenderJournal(tmp_path / "private", namespace=NAMESPACE)


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "https://user:secret@grid.example",
        "https://grid.example?token=private",
        "wss://grid.example",
        "https://grid.example/v1",
        "https://grid.example#private",
    ],
)
def test_origin_refuses_remote_plaintext_credentials_queries_and_paths(origin):
    with pytest.raises(RenderUncertain, match="origin"):
        module.status_url(origin)
    assert (
        module.status_url("http://127.0.0.1:8000/")
        == "http://127.0.0.1:8000/v1/workers/self/media-jobs"
    )


@pytest.mark.asyncio
@respx.mock
async def test_rig_bound_bounded_request_and_no_proxy_or_redirect(monkeypatch):
    worker_id, ids = str(uuid4()), [str(uuid4()) for _ in range(32)]
    original = httpx.AsyncClient
    options = []

    def factory(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    post = respx.post(URL).mock(
        return_value=httpx.Response(200, json=envelope(worker_id, ids))
    )
    assert await module.fetch_states(
        ORIGIN, "synthetic-noncredential", worker_id, ids
    ) == dict.fromkeys(ids, "closed")
    request = post.calls[0].request
    assert request.headers["apikey"] == "synthetic-noncredential"
    assert json.loads(request.content) == {"job_ids": ids}
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "schema",
        "foreign",
        "duplicate",
        "missing",
        "extra",
        "state",
        "huge",
        "redirect",
        "forbidden",
        "bad-json",
        "timeout",
    ],
)
@respx.mock
async def test_unprovable_status_cannot_authorize_deletion(fault):
    wid, ids = str(uuid4()), [str(uuid4()), str(uuid4())]
    data = envelope(wid, ids)
    response = None
    if fault == "identity":
        data["worker_id"] = str(uuid4())
    if fault == "schema":
        data["schema"] = "future-unknown"
    if fault == "foreign":
        data["jobs"][0]["job_id"] = str(uuid4())
    if fault == "duplicate":
        data["jobs"][1] = data["jobs"][0]
    if fault == "missing":
        data["jobs"].pop()
    if fault == "extra":
        data["private"] = "unexpected"
    if fault == "state":
        data["jobs"][0]["state"] = "expired"
    if fault == "huge":
        response = httpx.Response(200, content=b"x" * 4097)
    if fault == "bad-json":
        response = httpx.Response(200, content=b"{")
    if fault == "redirect":
        response = httpx.Response(
            307, headers={"location": "https://storage.example/stolen"}
        )
    if fault == "forbidden":
        response = httpx.Response(403)
    if fault == "timeout":
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("private failure"))
    else:
        respx.post(URL).mock(return_value=response or httpx.Response(200, json=data))
    with pytest.raises(RenderUncertain):
        await module.fetch_states(ORIGIN, "synthetic-noncredential", wid, ids)


@pytest.mark.asyncio
@respx.mock
async def test_total_deadline_refuses_slow_core_response(monkeypatch):
    monkeypatch.setattr(module, "STATUS_DEADLINE_SECONDS", 0.01)

    async def slow(_):
        await asyncio.sleep(1)
        return httpx.Response(200)

    respx.post(URL).mock(side_effect=slow)
    with pytest.raises(RenderUncertain, match="unavailable"):
        await module.fetch_states(
            ORIGIN, "synthetic-noncredential", str(uuid4()), [str(uuid4())]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids", [[], [str(uuid4())] * 2, [str(uuid4()) for _ in range(33)], ["not-uuid"]]
)
@respx.mock
async def test_invalid_status_identity_never_sends_credentials(ids):
    with pytest.raises(RenderUncertain, match="identity"):
        await module.fetch_states(ORIGIN, "synthetic-noncredential", str(uuid4()), ids)
    assert not respx.calls


def _close_then_crash(directory, job_id):
    journal = RenderJournal(directory, namespace=NAMESPACE)
    journal.cleanup_closed = lambda: os._exit(87)
    journal.confirm_closed([job_id])


def test_actual_process_crash_after_closure_commit_before_unlink_is_recoverable(
    journal,
):
    row = cached(journal)
    process = multiprocessing.get_context("spawn").Process(
        target=_close_then_crash, args=(str(journal.directory), row.job_id)
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail("closure crash fixture did not stop")
    assert process.exitcode == 87
    assert journal.require(row.job_id).closed_at is not None
    assert (journal.directory / (row.prompt_id + ".mp4")).exists()
    journal.cleanup_closed()
    assert not (journal.directory / (row.prompt_id + ".mp4")).exists()
    with pytest.raises(RenderUncertain, match="closed"):
        journal.cached(row.job_id)


def test_unlink_failure_never_forgets_pending_or_closed_identity(journal, monkeypatch):
    closed, pending = cached(journal), cached(journal)
    part = journal.directory / (pending.prompt_id + ".part")
    part.write_bytes(b"partial")
    part.chmod(0o600)
    original = journal_module._private

    def refused(path, **kwargs):
        if path.name == closed.prompt_id + ".mp4":
            raise OSError("synthetic disk failure")
        original(path, **kwargs)

    monkeypatch.setattr(journal_module, "_private", refused)
    with pytest.raises(OSError):
        journal.confirm_closed([closed.job_id])
    assert journal.require(closed.job_id).closed_at is not None
    assert journal.require(pending.job_id).closed_at is None
    monkeypatch.setattr(journal_module, "_private", original)
    journal.cleanup_closed()
    assert journal.cached(pending.job_id)[0] == VIDEO and part.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/private-state guard")
def test_replaced_directory_cannot_delete_content_through_a_symlink(journal):
    row = cached(journal)
    retained = journal.directory.with_name("retained")
    journal.directory.rename(retained)
    journal.directory.symlink_to(retained, target_is_directory=True)
    with pytest.raises(RenderUncertain, match="links"):
        journal.confirm_closed([row.job_id])
    assert (retained / (row.prompt_id + ".mp4")).exists()


def test_bounded_closed_history_never_prunes_unconfirmed_jobs(journal, monkeypatch):
    monkeypatch.setattr(journal_module, "MAX_CLOSED_JOBS", 3)
    pending = cached(journal)
    rows = [cached(journal) for _ in range(8)]
    for row in rows:
        journal.confirm_closed([row.job_id])
    assert journal.require(pending.job_id).closed_at is None
    assert journal.cached(pending.job_id)[0] == VIDEO
    assert sum(journal.get(row.job_id) is not None for row in rows) == 3
    assert len(list(journal.directory.glob("*.mp4"))) == 1
    assert journal.unresolved_ids() == [pending.job_id]


def test_old_ack_rows_require_fresh_core_closure_and_closed_cache_frees_capacity(
    journal, monkeypatch
):
    row = cached(journal)
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "UPDATE renders SET phase='acknowledged' WHERE job_id=?", (row.job_id,)
        )
        db.execute("ALTER TABLE renders DROP COLUMN closed_at")
    journal = RenderJournal(journal.directory, namespace=NAMESPACE)
    journal.cleanup_closed()
    assert journal.require(row.job_id).closed_at is None
    assert journal.cached(row.job_id)[0] == VIDEO
    monkeypatch.setattr(journal_module, "MAX_CACHE_BYTES", len(VIDEO))
    journal.confirm_closed([row.job_id])
    assert cached(journal).size == len(VIDEO)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "closed"])
async def test_worker_preflight_refuses_render_even_when_closed_history_was_pruned(
    tmp_path, monkeypatch, state
):
    monkeypatch.setattr(Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    monkeypatch.setattr(Settings, "GRID_API_KEY", "synthetic-noncredential")
    monkeypatch.setattr(Settings, "GRID_COMFYUI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(Settings, "GRID_WORKER_KEY_PATH", str(tmp_path / "absent-key"))
    job_id = str(uuid4())
    job = {
        "id": job_id,
        "model": "ltx-test",
        "job_type": "video",
        "payload": {
            "_grid_media_job_version": 1,
            "n": 1,
            "seed": 7,
            "seeds": [7],
            "recipe_engine": "comfyui",
            "recipe_spec": {"1": {"class_type": "SaveVideo"}},
        },
        "upload": [
            {"content_type": "video/mp4", "put_url": "https://storage.example/not-used"}
        ],
    }
    monkeypatch.setattr(
        ws_module, "fetch_states", AsyncMock(return_value={job_id: state})
    )
    build = AsyncMock()
    monkeypatch.setattr(ws_module, "build_workflow", build)
    worker, socket = WSWorker(), AsyncMock()
    worker._grid_worker_id = str(uuid4())
    try:
        with pytest.raises(RenderUncertain):
            await worker._handle_job(socket, job)
        assert worker._render_journal.get(job_id) is None
        build.assert_not_awaited()
        socket.send.assert_not_awaited()
        socket.close.assert_awaited_once()
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
async def test_periodic_reconciliation_recovers_lost_ack_and_leaves_unknown(
    journal, monkeypatch
):
    closed, unknown = cached(journal), cached(journal)
    monkeypatch.setattr(Settings, "GRID_COMFYUI_STATE_DIR", str(journal.directory))
    worker = WSWorker()
    monkeypatch.setattr(worker, "_video_journal", AsyncMock(return_value=journal))
    monkeypatch.setattr(
        ws_module,
        "fetch_states",
        AsyncMock(return_value={closed.job_id: "closed", unknown.job_id: "unknown"}),
    )

    async def stop(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ws_module.asyncio, "sleep", stop)
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker._reconcile_render_cache()
        assert journal.require(closed.job_id).closed_at is not None
        assert journal.cached(unknown.job_id)[0] == VIDEO
    finally:
        await worker.comfy.aclose()

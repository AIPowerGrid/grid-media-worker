# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private, bounded local identity for single-video ComfyUI execution.

A persisted submitting state permits observation, never another POST. ComfyUI
accepts a requested prompt ID but does not deduplicate it. Core remains the
financial authority; local state and cache files grant no charge or payout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

MAX_GRAPH_BYTES = 256 * 1024
MAX_OBSERVATION_BYTES = 4 * 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 1024 * 1024 * 1024
MAX_JOBS = 1024
MARKER = "aipg_render_v1"


class RenderUncertain(RuntimeError):
    """Keep the render and hold recoverable; never send a failure or rerender."""


class RenderFailed(RuntimeError):
    """The backend definitively rejected/failed this render."""


def digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise RenderUncertain("Invalid render commitment") from exc
    if len(encoded) > MAX_GRAPH_BYTES:
        raise RenderUncertain("Render commitment exceeds its size limit")
    return hashlib.sha256(encoded).hexdigest()


def graph_digest(value: Any) -> str:
    """Also bind exact-number normalization done by ComfyUI input validation.

    Its FLOAT validators change e.g. 24 to 24.0 in history. Do not normalize
    booleans, strings, fractional numbers or integers outside the safe range.
    This is a second pre-submission commitment, not a hash inferred from history.
    """
    digest(value)  # Bound and validate before making the normalized copy.

    def normalize(item: Any) -> Any:
        if type(item) is float and item.is_integer() and abs(item) <= 2**53 - 1:
            return int(item)
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item

    try:
        return digest(normalize(value))
    except RecursionError as exc:
        raise RenderUncertain("Invalid render commitment") from exc


def _uuid(value: str) -> str:
    if not isinstance(value, str):
        raise RenderUncertain("Canonical render UUID required")
    try:
        parsed = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise RenderUncertain("Canonical render UUID required") from exc
    if parsed != value:
        raise RenderUncertain("Canonical render UUID required")
    return parsed


def _hash(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RenderUncertain("Canonical SHA-256 commitment required")
    return value


def _private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise RenderUncertain("Render state must not use links or special files")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RenderUncertain("Render state must be private to the worker operator")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class RenderRecord:
    job_id: str
    binding: str
    prompt_id: str
    phase: str
    created: float
    graph_hash: str | None
    normalized_graph_hash: str | None
    sha256: str | None
    size: int | None


class RenderJournal:
    """One private state directory per Grid identity and ComfyUI instance."""

    def __init__(self, directory: str | Path, *, namespace: str) -> None:
        namespace = _hash(namespace)
        self.directory = Path(directory).expanduser().absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private(self.directory, directory=True)
        self.path = self.directory / "renders.sqlite3"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _private(self.path)
        else:
            os.close(fd)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK (id=1), namespace TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS renders (
                    job_id TEXT PRIMARY KEY, binding TEXT NOT NULL, prompt_id TEXT NOT NULL UNIQUE,
                    phase TEXT NOT NULL CHECK (phase IN ('prepared','submitting','running','caching','cached','failed','acknowledged')),
                    created REAL NOT NULL, graph_hash TEXT, sha256 TEXT, size INTEGER
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(renders)")}
            if "normalized_graph_hash" not in columns:
                db.execute("ALTER TABLE renders ADD COLUMN normalized_graph_hash TEXT")
            db.execute("INSERT OR IGNORE INTO owner VALUES (1, ?)", (namespace,))
            if (
                db.execute("SELECT namespace FROM owner WHERE id=1").fetchone()[0]
                != namespace
            ):
                raise RenderUncertain(
                    "Render directory belongs to another runtime/worker"
                )
        _sync_directory(self.directory)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        _private(self.path)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(row: sqlite3.Row | None) -> RenderRecord:
        if row is None:
            raise RenderUncertain("Retained render identity is missing")
        return RenderRecord(**dict(row))

    def get(self, job_id: str) -> RenderRecord | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM renders WHERE job_id=?", (_uuid(job_id),)
            ).fetchone()
        return self._record(row) if row else None

    def require(self, job_id: str) -> RenderRecord:
        row = self.get(job_id)
        if row is None:
            raise RenderUncertain("Retained render identity is missing")
        return row

    def begin(self, job_id: str, binding: str) -> RenderRecord:
        job_id = _uuid(job_id)
        binding = _hash(binding)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM renders WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                if db.execute("SELECT COUNT(*) FROM renders").fetchone()[0] >= MAX_JOBS:
                    raise RenderUncertain(
                        "Render journal is full; operator review required"
                    )
                db.execute(
                    "INSERT INTO renders (job_id,binding,prompt_id,phase,created) VALUES (?,?,?,'prepared',?)",
                    (job_id, binding, str(uuid4()), time.time()),
                )
                row = db.execute(
                    "SELECT * FROM renders WHERE job_id=?", (job_id,)
                ).fetchone()
            record = self._record(row)
            if record.binding != binding:
                raise RenderUncertain("Render identity conflicts with retained state")
            return record

    def arm(
        self, job_id: str, graph_hash: str, *, normalized_graph_hash: str | None = None
    ) -> bool:
        graph_hash = _hash(graph_hash)
        if normalized_graph_hash is not None:
            normalized_graph_hash = _hash(normalized_graph_hash)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE renders SET phase='submitting', graph_hash=?, normalized_graph_hash=? WHERE job_id=? AND phase='prepared'",
                (graph_hash, normalized_graph_hash, _uuid(job_id)),
            )
            return changed.rowcount == 1

    def running(self, job_id: str) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE renders SET phase='running' WHERE job_id=? AND phase='submitting'",
                (_uuid(job_id),),
            )

    def fail(self, job_id: str) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE renders SET phase='failed' WHERE job_id=? AND phase IN ('prepared','submitting','running')",
                (_uuid(job_id),),
            )

    def acknowledge(self, job_id: str) -> None:
        # Other modalities and legacy Core jobs have no entry in this journal.
        try:
            job_id = _uuid(job_id)
        except RenderUncertain:
            return
        with self._db() as db:
            db.execute(
                "UPDATE renders SET phase='acknowledged' WHERE job_id=? AND phase='cached'",
                (job_id,),
            )

    def _asset(self, row: RenderRecord) -> Path:
        return self.directory / (_uuid(row.prompt_id) + ".mp4")

    def cached(self, job_id: str) -> tuple[bytes, str] | None:
        row = self.get(job_id)
        if row is None or row.phase not in {"caching", "cached", "acknowledged"}:
            return None
        path = self._asset(row)
        try:
            _private(path)
        except FileNotFoundError as exc:
            if row.phase == "caching":
                return (
                    None  # Interrupted cache write: observe/download the same prompt.
                )
            raise RenderUncertain("Retained video is missing") from exc
        if type(row.size) is not int or not 0 < row.size <= MAX_VIDEO_BYTES:
            raise RenderUncertain("Invalid retained video size")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            content = stream.read(row.size + 1)
        if (
            len(content) != row.size
            or hashlib.sha256(content).hexdigest() != row.sha256
        ):
            raise RenderUncertain("Retained video failed its byte commitment")
        if row.phase == "caching":
            with self._db() as db:
                db.execute(
                    "UPDATE renders SET phase='cached' WHERE job_id=? AND phase='caching'",
                    (row.job_id,),
                )
        return content, path.name

    def cache(self, job_id: str, content: bytes) -> None:
        if (
            not 0 < len(content) <= MAX_VIDEO_BYTES
            or len(content) < 12
            or content[4:8] != b"ftyp"
        ):
            raise RenderFailed("ComfyUI did not return a bounded MP4")
        sha = hashlib.sha256(content).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._record(
                db.execute(
                    "SELECT * FROM renders WHERE job_id=?", (_uuid(job_id),)
                ).fetchone()
            )
            if row.phase not in {"running", "caching", "cached", "acknowledged"}:
                raise RenderUncertain("Render cache has no accepted prompt")
            if row.sha256 is not None and (
                row.sha256 != sha or row.size != len(content)
            ):
                raise RenderUncertain("Render output changed during recovery")
            if row.sha256 is None:
                used = db.execute(
                    "SELECT COALESCE(SUM(size),0) FROM renders"
                ).fetchone()[0]
                if used + len(content) > MAX_CACHE_BYTES:
                    raise RenderUncertain(
                        "Render cache is full; operator review required"
                    )
                db.execute(
                    "UPDATE renders SET phase='caching', sha256=?, size=? WHERE job_id=?",
                    (sha, len(content), row.job_id),
                )
        # Persist the expected digest before the filesystem write. A crash on
        # either side of rename can recover only these same committed bytes.
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._record(
                db.execute("SELECT * FROM renders WHERE job_id=?", (job_id,)).fetchone()
            )
            if row.phase in {"cached", "acknowledged"}:
                self.cached(job_id)
                return
            path = self._asset(row)
            temporary = path.with_suffix(".part")
            if os.path.lexists(temporary):
                _private(temporary)
            fd = os.open(
                temporary,
                os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(self.directory)
            db.execute(
                "UPDATE renders SET phase='cached' WHERE job_id=? AND phase='caching'",
                (job_id,),
            )


class DurableVideoRenderer:
    """Observe and cache one ComfyUI prompt; never repost uncertain acceptance."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        journal: RenderJournal,
        *,
        timeout: float = 720,
        poll_seconds: float = 1,
    ) -> None:
        if not 0 < timeout <= 3600 or not 0 <= poll_seconds <= 60:
            raise RenderUncertain("Invalid bounded render observation deadline")
        self.client, self.journal = client, journal
        self.timeout, self.poll_seconds = timeout, poll_seconds

    async def _json(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        async with self.client.stream(method, path, **kwargs) as response:
            content = bytearray()
            async for part in response.aiter_bytes():
                if len(content) + len(part) > MAX_OBSERVATION_BYTES:
                    raise RenderUncertain("ComfyUI observation exceeds its size limit")
                content.extend(part)
            try:
                return response.status_code, json.loads(content)
            except (ValueError, RecursionError) as exc:
                raise RenderUncertain("Invalid ComfyUI observation") from exc

    @staticmethod
    def _matches(entry: Any, row: RenderRecord) -> bool:
        return (
            isinstance(entry, (list, tuple))
            and len(entry) >= 4
            and entry[1] == row.prompt_id
            and isinstance(entry[3], dict)
            and entry[3].get(MARKER)
            == {
                "job_id": row.job_id,
                "binding": row.binding,
                "graph_hash": row.graph_hash,
            }
            and (
                digest(entry[2]) == row.graph_hash
                or (
                    row.normalized_graph_hash is not None
                    and graph_digest(entry[2]) == row.normalized_graph_hash
                )
            )
        )

    def _check_deadline(self, row: RenderRecord) -> None:
        if time.time() > row.created + self.timeout:
            raise RenderUncertain(
                "Render observation deadline elapsed; no resubmission"
            )

    async def render(
        self, job_id: str, binding: str, build: Callable[[], Awaitable[dict[str, Any]]]
    ) -> tuple[bytes, str]:
        row = await asyncio.to_thread(self.journal.begin, job_id, binding)
        if row.phase == "failed":
            raise RenderFailed("Retained render definitively failed")
        retained = await asyncio.to_thread(self.journal.cached, job_id)
        if retained is not None:
            return retained
        self._check_deadline(row)
        if row.phase == "prepared":
            graph = await build()
            self._check_deadline(row)
            if await asyncio.to_thread(
                self.journal.arm,
                job_id,
                digest(graph),
                normalized_graph_hash=graph_digest(graph),
            ):
                row = await asyncio.to_thread(self.journal.require, job_id)
                self._check_deadline(row)
                try:
                    status, reply = await self._json(
                        "POST",
                        "/prompt",
                        json={
                            "prompt": graph,
                            "prompt_id": row.prompt_id,
                            "client_id": row.prompt_id,
                            "extra_data": {
                                MARKER: {
                                    "job_id": row.job_id,
                                    "binding": row.binding,
                                    "graph_hash": row.graph_hash,
                                }
                            },
                        },
                    )
                except httpx.HTTPError:
                    # The persisted submitting state survives an accepted POST
                    # whose response was lost. Observe the preassigned ID.
                    pass
                else:
                    if status == 400 and isinstance(reply, dict) and reply.get("error"):
                        await asyncio.to_thread(self.journal.fail, job_id)
                        raise RenderFailed("ComfyUI rejected the governed workflow")
                    if (
                        status != 200
                        or not isinstance(reply, dict)
                        or reply.get("prompt_id") != row.prompt_id
                    ):
                        raise RenderUncertain(
                            "ComfyUI acceptance could not be verified"
                        )
                    await asyncio.to_thread(self.journal.running, job_id)

        while time.time() <= row.created + self.timeout:
            row = await asyncio.to_thread(self.journal.require, job_id)
            retained = await asyncio.to_thread(self.journal.cached, job_id)
            if retained is not None:
                return retained
            if row.phase == "failed":
                raise RenderFailed("Retained render definitively failed")
            code, history = await self._json("GET", f"/history/{row.prompt_id}")
            if code != 200 or not isinstance(history, dict):
                raise RenderUncertain("ComfyUI history unavailable")
            record = history.get(row.prompt_id)
            if record:
                if not isinstance(record, dict) or not self._matches(
                    record.get("prompt"), row
                ):
                    raise RenderUncertain(
                        "ComfyUI history conflicts with the retained render"
                    )
                await asyncio.to_thread(self.journal.running, job_id)
                execution_status = record.get("status") or {}
                if not isinstance(execution_status, dict):
                    raise RenderUncertain("Invalid ComfyUI execution status")
                if execution_status.get("status_str") == "error":
                    await asyncio.to_thread(self.journal.fail, job_id)
                    raise RenderFailed("ComfyUI execution failed")
                if execution_status.get("completed") is True:
                    try:
                        output = await self._download_video(record)
                        await asyncio.to_thread(self.journal.cache, job_id, output)
                    except RenderFailed:
                        await asyncio.to_thread(self.journal.fail, job_id)
                        raise
                    retained = await asyncio.to_thread(self.journal.cached, job_id)
                    if retained is None:
                        raise RenderUncertain("Retained video is missing")
                    return retained
            else:
                code, queue = await self._json("GET", "/queue")
                if code != 200 or not isinstance(queue, dict):
                    raise RenderUncertain("ComfyUI queue unavailable")
                running, pending = (
                    queue.get("queue_running", []),
                    queue.get("queue_pending", []),
                )
                if not isinstance(running, list) or not isinstance(pending, list):
                    raise RenderUncertain("Invalid ComfyUI queue metadata")
                entries = running + pending
                matching = [
                    item
                    for item in entries
                    if isinstance(item, (list, tuple))
                    and len(item) > 1
                    and item[1] == row.prompt_id
                ]
                if len(matching) > 1 or (
                    matching and not self._matches(matching[0], row)
                ):
                    raise RenderUncertain(
                        "ComfyUI queue conflicts with the retained render"
                    )
                if matching:
                    await asyncio.to_thread(self.journal.running, job_id)
            await asyncio.sleep(self.poll_seconds)
        # Do not interrupt a shared ComfyUI process or infer non-execution from
        # absent history. Core reconciles financial expiry independently.
        raise RenderUncertain("Render observation deadline elapsed; no resubmission")

    async def _download_video(self, record: dict[str, Any]) -> bytes:
        outputs = record.get("outputs")
        if not isinstance(outputs, dict):
            raise RenderFailed("Video completed without supported output")
        videos = []
        for item in outputs.values():
            if not isinstance(item, dict):
                raise RenderUncertain("Invalid ComfyUI output metadata")
            for field in ("videos", "gifs", "video"):
                value = item.get(field, [])
                if isinstance(value, dict):
                    value = [value]
                if not isinstance(value, list):
                    raise RenderUncertain("Invalid ComfyUI output metadata")
                videos.extend(value)
            # Native SaveVideo uses PreviewVideo's legacy images/animated
            # envelope, not the videos key. Still require a real MP4 below.
            native = item.get("images", [])
            if not isinstance(native, list):
                raise RenderUncertain("Invalid ComfyUI output metadata")
            for output in native:
                if (
                    isinstance(output, dict)
                    and isinstance(output.get("filename"), str)
                    and output["filename"].lower().endswith(".mp4")
                ):
                    videos.append(output)
        if len(videos) != 1 or not isinstance(videos[0], dict):
            raise RenderFailed("Single-video render did not return exactly one video")
        info = videos[0]
        filename, subfolder = info.get("filename"), info.get("subfolder", "")
        if (
            not isinstance(filename, str)
            or not filename.lower().endswith(".mp4")
            or "/" in filename
            or "\\" in filename
            or len(filename) > 255
            or not isinstance(subfolder, str)
            or len(subfolder) > 1024
            or "\\" in subfolder
            or any(part in {".", ".."} for part in subfolder.split("/"))
            or subfolder.startswith("/")
            or info.get("type") != "output"
        ):
            raise RenderUncertain("Invalid ComfyUI video identity")
        async with self.client.stream(
            "GET",
            "/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": "output",
            },
        ) as response:
            response.raise_for_status()
            content = bytearray()
            async for part in response.aiter_bytes():
                if len(content) + len(part) > MAX_VIDEO_BYTES:
                    raise RenderFailed("Video exceeds the cache limit")
                content.extend(part)
        return bytes(content)

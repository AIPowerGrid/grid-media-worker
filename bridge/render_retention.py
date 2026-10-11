# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded, rig-bound Core authority; local time never closes a paid job."""

from __future__ import annotations

import asyncio
import json
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .enrollment import EnrollmentClientError, grid_api_base_url
from .render_journal import RenderUncertain

MAX_STATUS_IDS = 32
MAX_STATUS_BYTES = 4096
STATUS_DEADLINE_SECONDS = 10
State = Literal["held", "closed", "unknown"]


def status_url(grid_api_url: str) -> str:
    try:
        base = grid_api_base_url(grid_api_url)
        if urlsplit(base).path not in {"", "/"}:
            raise EnrollmentClientError("Grid status requires the API origin")
    except (EnrollmentClientError, ValueError) as exc:
        raise RenderUncertain("Invalid Grid status origin") from exc
    return cast(str, base) + "/v1/workers/self/media-jobs"


async def fetch_states(
    grid_api_url: str, api_key: str, worker_id: str, job_ids: list[str]
) -> dict[str, State]:
    """A failure preserves all identities/bytes; never follow a credential redirect."""
    try:
        if (
            not 1 <= len(job_ids) <= MAX_STATUS_IDS
            or len(set(job_ids)) != len(job_ids)
            or str(UUID(worker_id)) != worker_id
            or any(str(UUID(job_id)) != job_id for job_id in job_ids)
            or not api_key
        ):
            raise ValueError("Invalid bounded status identity")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RenderUncertain("Invalid bounded Grid status identity") from exc
    url = status_url(grid_api_url)

    async def read() -> dict[str, State]:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(5, connect=3),
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream(
                "POST", url, headers={"apikey": api_key}, json={"job_ids": job_ids}
            ) as response,
        ):
            if response.status_code != 200:
                raise RenderUncertain("Core could not confirm private render state")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > MAX_STATUS_BYTES:
                    raise RenderUncertain("Core render state exceeds its size limit")
                content.extend(chunk)
        value = json.loads(content)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "worker_id", "jobs"}
            or value["schema"] != "aipg.worker.media-journal.v1"
            or value["worker_id"] != worker_id
            or not isinstance(value["jobs"], list)
            or len(value["jobs"]) != len(job_ids)
        ):
            raise RenderUncertain("Core render state conflicts with this worker")
        states: dict[str, State] = {}
        for row in value["jobs"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"job_id", "state"}
                or not isinstance(row["job_id"], str)
                or row["job_id"] not in job_ids
                or row["job_id"] in states
                or row["state"] not in ("held", "closed", "unknown")
            ):
                raise RenderUncertain("Invalid bound Core render state")
            states[row["job_id"]] = cast(State, row["state"])
        return states

    try:
        return await asyncio.wait_for(read(), timeout=STATUS_DEADLINE_SECONDS)
    except (httpx.HTTPError, ValueError, RecursionError, asyncio.TimeoutError) as exc:
        raise RenderUncertain(
            "Core render state is unavailable; retain local state"
        ) from exc

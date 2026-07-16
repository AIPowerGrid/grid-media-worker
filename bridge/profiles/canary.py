# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ACE-Step runtime canary used to unlock signed profile capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
import wave
from dataclasses import asdict, dataclass
from typing import Any, Mapping
from urllib.parse import urljoin

import httpx

MAX_CANARY_BYTES = 64 * 1024 * 1024


class CanaryError(RuntimeError):
    """The runtime did not prove its advertised audio capability."""


@dataclass(frozen=True)
class CanaryResult:
    elapsed_seconds: float
    output_bytes: int
    output_sha256: str
    audio_seconds: float
    sample_rate: int
    channels: int

    def as_state(self) -> Mapping[str, Any]:
        return asdict(self)


async def run_ace_step_canary(
    base_url: str,
    profile: Mapping[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
    poll_interval: float = 2.0,
    api_key: str = "",
) -> CanaryResult:
    """Generate, retrieve, and structurally validate one deterministic WAV."""

    canary = profile["canary"]
    if canary["adapter"] != "ace-step-api-audio-v1":
        raise CanaryError(f"unsupported canary adapter: {canary['adapter']}")
    from ..audio_runtime import (
        _same_origin_url,
        download_bounded_output,
        local_runtime_base_url,
    )

    base = local_runtime_base_url(base_url)
    owns_client = client is None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    http = client or httpx.AsyncClient(timeout=60.0, headers=headers)
    started = time.monotonic()
    try:
        health = await http.get(urljoin(base, "health"))
        health.raise_for_status()
        if health.json().get("data", {}).get("status") != "ok":
            raise CanaryError("ACE-Step health response is not ready")

        response = await http.post(
            urljoin(base, "release_task"),
            json={
                **profile["recipe"]["spec"]["fixed"],
                "prompt": canary["prompt"],
                "lyrics": "",
                "audio_duration": canary["seconds"],
                "inference_steps": 2,
                "seed": canary["seed"],
            },
        )
        response.raise_for_status()
        payload = response.json()
        task_id = (payload.get("data") or {}).get("task_id")
        if payload.get("code") != 200 or not task_id:
            raise CanaryError(f"ACE-Step rejected canary: {payload.get('error') or payload}")

        deadline = time.monotonic() + canary["timeout_seconds"]
        output_url = await _wait_for_audio(http, base, task_id, deadline, poll_interval)
        try:
            audio = await download_bounded_output(
                http,
                _same_origin_url(base, output_url),
                max_bytes=MAX_CANARY_BYTES,
                label="canary output",
            )
        except RuntimeError as exc:
            raise CanaryError(str(exc)) from exc
        audio_seconds, sample_rate, channels = validate_wav(audio)
        if audio_seconds < max(1.0, canary["seconds"] * 0.75):
            raise CanaryError(
                f"canary audio is too short: {audio_seconds:.2f}s for {canary['seconds']}s request"
            )
        return CanaryResult(
            elapsed_seconds=round(time.monotonic() - started, 3),
            output_bytes=len(audio),
            output_sha256=hashlib.sha256(audio).hexdigest(),
            audio_seconds=round(audio_seconds, 3),
            sample_rate=sample_rate,
            channels=channels,
        )
    except httpx.HTTPError as exc:
        raise CanaryError(f"ACE-Step canary HTTP failure: {exc}") from exc
    finally:
        if owns_client:
            await http.aclose()


async def _wait_for_audio(
    client: httpx.AsyncClient,
    base: str,
    task_id: str,
    deadline: float,
    poll_interval: float,
) -> str:
    while time.monotonic() < deadline:
        response = await client.post(
            urljoin(base, "query_result"),
            json={"task_id_list": [task_id]},
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") or []
        row = next((item for item in rows if item.get("task_id") == task_id), None)
        if row is None or row.get("status") == 0:
            await asyncio.sleep(poll_interval)
            continue
        if row.get("status") == 2:
            raise CanaryError("ACE-Step reported canary generation failure")
        results = row.get("result") or []
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except json.JSONDecodeError as exc:
                raise CanaryError("ACE-Step returned malformed result JSON") from exc
        output = next(
            (item.get("file") for item in results if item.get("status") == 1 and item.get("file")),
            None,
        )
        if not output:
            raise CanaryError("ACE-Step completed without an audio output URL")
        return output
    raise CanaryError("ACE-Step canary timed out")


def validate_wav(content: bytes) -> tuple[float, int, int]:
    try:
        with wave.open(io.BytesIO(content), "rb") as audio:
            frames = audio.getnframes()
            sample_rate = audio.getframerate()
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            frame_bytes = audio.readframes(frames)
    except (EOFError, wave.Error) as exc:
        raise CanaryError("canary output is not a valid WAV") from exc
    if frames <= 0 or sample_rate <= 0 or channels not in (1, 2) or sample_width <= 0:
        raise CanaryError("canary WAV has invalid audio metadata")
    if not frame_bytes or not any(frame_bytes):
        raise CanaryError("canary WAV contains no signal")
    return frames / sample_rate, sample_rate, channels

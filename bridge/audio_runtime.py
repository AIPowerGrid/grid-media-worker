# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Constrained local ACE-Step job execution for managed audio profiles."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import httpx

from .profiles.canary import validate_wav

MAX_AUDIO_BYTES = 256 * 1024 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class AudioRuntimeError(RuntimeError):
    """The local audio runtime violated the signed profile contract."""


@dataclass(frozen=True)
class GeneratedAudio:
    content: bytes
    filename: str
    seconds: float
    sample_rate: int
    channels: int


def local_runtime_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in LOOPBACK_HOSTS:
        raise AudioRuntimeError("managed ACE-Step runtime must use a loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AudioRuntimeError("managed ACE-Step runtime URL is invalid")
    return value.rstrip("/") + "/"


async def check_ace_step_runtime(
    base_url: str,
    runtime_model: str,
    *,
    api_key: str = "",
    client: httpx.AsyncClient | None = None,
) -> None:
    base = local_runtime_base_url(base_url)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0, headers=_headers(api_key))
    try:
        health = await http.get(urljoin(base, "health"))
        health.raise_for_status()
        health_body = health.json()
        health_data = health_body.get("data") if isinstance(health_body, Mapping) else None
        if not isinstance(health_data, Mapping) or health_data.get("status") != "ok":
            raise AudioRuntimeError("ACE-Step health response is not ready")
        inventory = await http.get(urljoin(base, "v1/model_inventory"))
        inventory.raise_for_status()
        inventory_body = inventory.json()
        inventory_data = (
            inventory_body.get("data") if isinstance(inventory_body, Mapping) else None
        )
        if not isinstance(inventory_data, Mapping):
            raise AudioRuntimeError("ACE-Step model inventory response is malformed")
        rows = inventory_data.get("models")
        if not isinstance(rows, list):
            raise AudioRuntimeError("ACE-Step model inventory response is malformed")
        names = {row.get("name") for row in rows if isinstance(row, Mapping)}
        if runtime_model not in names:
            raise AudioRuntimeError(f"ACE-Step runtime model is not loaded: {runtime_model}")
    except httpx.HTTPError as exc:
        raise AudioRuntimeError(f"ACE-Step readiness check failed: {exc}") from exc
    finally:
        if owns_client:
            await http.aclose()


async def generate_ace_step_audio(
    base_url: str,
    payload: Mapping[str, Any],
    recipe: Mapping[str, Any],
    *,
    api_key: str = "",
    client: httpx.AsyncClient | None = None,
    poll_interval: float = 2.0,
    timeout_seconds: float = 900.0,
) -> GeneratedAudio:
    """Execute only fields declared by the signed local API recipe."""
    base = local_runtime_base_url(base_url)
    if recipe.get("adapter") != "ace-step-api-audio-v1":
        raise AudioRuntimeError("unsupported audio recipe adapter")
    fixed = recipe["fixed"]
    limits = recipe["limits"]
    prompt = _bounded_text(payload.get("prompt"), "prompt", limits["prompt_chars"], required=True)
    lyrics = _bounded_text(payload.get("lyrics", ""), "lyrics", limits["lyrics_chars"])
    duration = _bounded_number(payload.get("seconds"), "seconds", limits["audio_duration"])
    steps = int(_bounded_number(payload.get("inference_steps", 8), "inference_steps", limits["inference_steps"]))
    seed = payload.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2**53 - 1:
        raise AudioRuntimeError("seed is outside the Grid integer range")

    request = {
        **fixed,
        "prompt": prompt,
        "lyrics": lyrics,
        "audio_duration": duration,
        "inference_steps": steps,
        "seed": seed,
    }
    bpm = _optional_bounded_int(payload.get("bpm"), "bpm", limits["bpm"])
    key_scale = _optional_pattern(
        payload.get("key_scale"),
        "key_scale",
        limits["key_scale_pattern"],
    )
    time_signature = _optional_choice(
        payload.get("time_signature"),
        "time_signature",
        limits["time_signatures"],
    )
    vocal_language = _optional_pattern(
        payload.get("vocal_language"),
        "vocal_language",
        limits["vocal_language_pattern"],
    )
    for key, value in {
        "bpm": bpm,
        "key_scale": key_scale,
        "time_signature": time_signature,
        "vocal_language": vocal_language,
    }.items():
        if value is not None:
            request[key] = value
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=60.0, headers=_headers(api_key))
    try:
        response = await http.post(urljoin(base, "release_task"), json=request)
        response.raise_for_status()
        body = response.json()
        task_id = (body.get("data") or {}).get("task_id")
        if body.get("code") != 200 or not task_id:
            raise AudioRuntimeError(f"ACE-Step rejected audio job: {body.get('error') or body}")
        output_path = await _wait_for_output(
            http, base, task_id, time.monotonic() + timeout_seconds, poll_interval
        )
        output_url = _same_origin_url(base, output_path)
        content = await download_bounded_output(
            http,
            output_url,
            max_bytes=MAX_AUDIO_BYTES,
            label="ACE-Step output",
        )
        actual_seconds, sample_rate, channels = validate_wav(content)
        if actual_seconds < duration * 0.75:
            raise AudioRuntimeError(
                f"ACE-Step output is too short: {actual_seconds:.2f}s for {duration:.2f}s request"
            )
        return GeneratedAudio(
            content=content,
            filename=f"ace-step-{task_id}.wav",
            seconds=actual_seconds,
            sample_rate=sample_rate,
            channels=channels,
        )
    except httpx.HTTPError as exc:
        raise AudioRuntimeError(f"ACE-Step audio job failed: {exc}") from exc
    finally:
        if owns_client:
            await http.aclose()


async def _wait_for_output(client, base, task_id, deadline, poll_interval) -> str:
    while time.monotonic() < deadline:
        response = await client.post(
            urljoin(base, "query_result"), json={"task_id_list": [task_id]}
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
        row = next((item for item in rows if item.get("task_id") == task_id), None)
        if row is None or row.get("status") == 0:
            await asyncio.sleep(poll_interval)
            continue
        if row.get("status") == 2:
            raise AudioRuntimeError("ACE-Step reported generation failure")
        results = row.get("result") or []
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except json.JSONDecodeError as exc:
                raise AudioRuntimeError("ACE-Step returned malformed result JSON") from exc
        output = next(
            (item.get("file") for item in results if item.get("status") == 1 and item.get("file")),
            None,
        )
        if not output:
            raise AudioRuntimeError("ACE-Step completed without an audio output URL")
        return output
    raise AudioRuntimeError("ACE-Step audio job timed out")


async def download_bounded_output(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    content = bytearray()
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    raise AudioRuntimeError(f"{label} exceeds the safety bound")
            except ValueError as exc:
                raise AudioRuntimeError(f"{label} has an invalid output length") from exc
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > max_bytes:
                raise AudioRuntimeError(f"{label} exceeds the safety bound")
            content.extend(chunk)
    if not content:
        raise AudioRuntimeError(f"{label} is empty")
    return bytes(content)


def _same_origin_url(base: str, value: str) -> str:
    candidate = urljoin(base, str(value).lstrip("/"))
    expected = urlparse(base)
    actual = urlparse(candidate)
    if (actual.scheme, actual.hostname, actual.port) != (
        expected.scheme,
        expected.hostname,
        expected.port,
    ):
        raise AudioRuntimeError("ACE-Step returned a non-local output URL")
    return candidate


def _headers(api_key: str) -> Mapping[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _bounded_text(value: Any, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > int(limit):
        raise AudioRuntimeError(f"{name} is invalid or exceeds {limit} characters")
    return value


def _bounded_number(value: Any, name: str, bounds) -> float:
    if isinstance(value, bool):
        raise AudioRuntimeError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioRuntimeError(f"{name} must be numeric") from exc
    if not float(bounds[0]) <= result <= float(bounds[1]):
        raise AudioRuntimeError(f"{name} must be between {bounds[0]} and {bounds[1]}")
    return result


def _optional_bounded_int(value: Any, name: str, bounds) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise AudioRuntimeError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise AudioRuntimeError(f"{name} must be an integer") from exc
    if str(result) != str(value).strip():
        raise AudioRuntimeError(f"{name} must be an integer")
    if not int(bounds[0]) <= result <= int(bounds[1]):
        raise AudioRuntimeError(f"{name} must be between {bounds[0]} and {bounds[1]}")
    return result


def _optional_pattern(value: Any, name: str, pattern: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise AudioRuntimeError(f"{name} is invalid")
    return value


def _optional_choice(value: Any, name: str, choices) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in choices:
        raise AudioRuntimeError(f"{name} is invalid")
    return value

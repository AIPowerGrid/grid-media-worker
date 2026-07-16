# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Repeatable, privacy-split benchmark evidence for managed worker profiles."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .canary import CanaryResult
from .hardware import HardwareSnapshot, Recommendation
from .state import profile_digest

BENCHMARK_VERSION = 1


async def run_profile_benchmark(
    profile: Mapping[str, Any],
    snapshot: HardwareSnapshot,
    recommendation: Recommendation,
    run_once: Callable[[], Awaitable[CanaryResult]],
    *,
    runs: int = 3,
    sample_interval: float = 0.5,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Run identical signed canaries and return private/public reports."""

    if not 1 <= runs <= 20:
        raise ValueError("benchmark runs must be between 1 and 20")
    if recommendation.status == "unsupported" or not recommendation.capability_tier:
        raise ValueError("cannot benchmark unsupported hardware")

    stop = asyncio.Event()
    samples: list[Mapping[str, int | None]] = []
    sampler = asyncio.create_task(
        _sample_until_stopped(
            stop,
            samples,
            runtime_device=(
                recommendation.selected_accelerator.runtime_selector()
                if recommendation.selected_accelerator
                else None
            ),
            interval=sample_interval,
        )
    )
    results: list[CanaryResult] = []
    try:
        for _ in range(runs):
            results.append(await run_once())
    finally:
        stop.set()
        await sampler

    metrics = _metrics(results, samples)
    created_at = datetime.now(timezone.utc).isoformat()
    commitment = {
        "benchmark_version": BENCHMARK_VERSION,
        "created_at": created_at,
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "profile_digest": profile_digest(profile),
        "runtime_digest": profile["runtime"]["digest"],
        "recipe_root": profile["recipe"]["sha256"],
        "recipe_vault_root": profile["recipe"]["onchain_root"],
        "recommendation_status": recommendation.status,
        "capability_tier": recommendation.capability_tier,
        "runs": len(results),
        "metrics": metrics,
    }
    private = {
        **commitment,
        "privacy": "local-only; contains exact hardware inventory",
        "hardware": snapshot.public_summary(),
        "selected_runtime_device": (
            recommendation.selected_accelerator.runtime_selector()
            if recommendation.selected_accelerator
            else None
        ),
        "canary_results": [item.as_state() for item in results],
    }
    public = {
        **commitment,
        "privacy": "shareable; exact hardware inventory removed",
    }
    return private, public


def write_benchmark_report(path: str | Path, report: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def _metrics(
    results: Sequence[CanaryResult],
    samples: Sequence[Mapping[str, int | None]],
) -> Mapping[str, Any]:
    elapsed = [item.elapsed_seconds for item in results]
    realtime = [
        item.audio_seconds / item.elapsed_seconds
        for item in results
        if item.elapsed_seconds > 0
    ]
    gpu_values = [
        int(item["gpu_used_mb"])
        for item in samples
        if item.get("gpu_used_mb") is not None
    ]
    ram_values = [
        int(item["host_ram_used_mb"])
        for item in samples
        if item.get("host_ram_used_mb") is not None
    ]
    return {
        "elapsed_seconds": {
            "min": round(min(elapsed), 3),
            "median": round(statistics.median(elapsed), 3),
            "max": round(max(elapsed), 3),
        },
        "audio_seconds_per_wall_second_median": (
            round(statistics.median(realtime), 4) if realtime else None
        ),
        "peak_gpu_used_mb": max(gpu_values) if gpu_values else None,
        "peak_host_ram_used_mb": max(ram_values) if ram_values else None,
    }


async def _sample_until_stopped(
    stop: asyncio.Event,
    samples: list[Mapping[str, int | None]],
    *,
    runtime_device: str | None,
    interval: float,
) -> None:
    while not stop.is_set():
        samples.append(await asyncio.to_thread(_resource_sample, runtime_device))
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.1, interval))
        except asyncio.TimeoutError:
            pass
    samples.append(await asyncio.to_thread(_resource_sample, runtime_device))


def _resource_sample(runtime_device: str | None) -> Mapping[str, int | None]:
    return {
        "gpu_used_mb": _gpu_used_mb(runtime_device),
        "host_ram_used_mb": _host_ram_used_mb(),
    }


def _gpu_used_mb(runtime_device: str | None) -> int | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=index,uuid,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    values: list[tuple[str, str, int]] = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 3:
            continue
        try:
            values.append((fields[0], fields[1], int(float(fields[2]))))
        except ValueError:
            continue
    if runtime_device:
        matches = [used for index, uuid, used in values if runtime_device in {index, uuid}]
        return matches[0] if len(matches) == 1 else None
    return max((used for _index, _uuid, used in values), default=None)


def _host_ram_used_mb() -> int | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return (status.total_physical - status.available_physical) // (1024 * 1024)

    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            name, raw = line.split(":", 1)
            values[name] = int(raw.strip().split()[0])
        return (values["MemTotal"] - values["MemAvailable"]) // 1024
    except (OSError, KeyError, ValueError):
        return None

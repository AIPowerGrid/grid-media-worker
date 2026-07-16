# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os

import pytest

from bridge.profiles import benchmark
from bridge.profiles.benchmark import run_profile_benchmark, write_benchmark_report
from bridge.profiles.canary import CanaryResult
from bridge.profiles.hardware import AcceleratorInfo, HardwareSnapshot, Recommendation
from bridge.profiles.profile import bundled_profile_path


def _profile():
    return json.loads(bundled_profile_path().read_text(encoding="utf-8"))["profile"]


@pytest.mark.asyncio
async def test_benchmark_splits_private_hardware_from_shareable_evidence(monkeypatch):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        65536,
        131072,
        (
            AcceleratorInfo(
                "nvidia",
                "Secret GPU Name",
                24576,
                "575.1",
                "12.8",
                2,
                "GPU-secret-uuid",
            ),
        ),
    )
    recommendation = Recommendation(
        "recommended",
        "audio.ace-step.standard",
        snapshot.accelerators[0],
        ("ok",),
    )
    values = iter((8.0, 10.0, 9.0))

    async def run_once():
        elapsed = next(values)
        return CanaryResult(elapsed, 1024, "a" * 64, 10.0, 48000, 2)

    monkeypatch.setattr(
        benchmark,
        "_resource_sample",
        lambda _device: {"gpu_used_mb": 11000, "host_ram_used_mb": 20000},
    )
    private, public = await run_profile_benchmark(
        _profile(),
        snapshot,
        recommendation,
        run_once,
        runs=3,
        sample_interval=0.001,
    )

    assert private["hardware"]["accelerators"][0]["name"] == "Secret GPU Name"
    assert private["selected_runtime_device"] == "GPU-secret-uuid"
    encoded_public = json.dumps(public)
    assert "Secret GPU Name" not in encoded_public
    assert "GPU-secret-uuid" not in encoded_public
    assert "hardware" not in public
    assert public["recipe_root"] == _profile()["recipe"]["sha256"]
    assert public["recipe_vault_root"] is None
    assert public["metrics"]["elapsed_seconds"]["median"] == 9.0
    assert public["metrics"]["audio_seconds_per_wall_second_median"] == 1.1111
    assert public["metrics"]["peak_gpu_used_mb"] == 11000


@pytest.mark.asyncio
async def test_benchmark_rejects_bad_run_count():
    snapshot = HardwareSnapshot("linux", "x86_64", 1, 1, ())
    recommendation = Recommendation("recommended", "tier", None, ("ok",))

    async def never():  # pragma: no cover - validation runs first
        raise AssertionError

    with pytest.raises(ValueError, match="between 1 and 20"):
        await run_profile_benchmark(
            _profile(), snapshot, recommendation, never, runs=0,
        )


def test_benchmark_report_is_private_on_disk(tmp_path):
    destination = tmp_path / "benchmark.json"
    write_benchmark_report(destination, {"ok": True})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"ok": True}
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600

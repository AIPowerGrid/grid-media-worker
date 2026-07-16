# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from types import SimpleNamespace

import pytest

from bridge.profiles import hardware
from bridge.profiles.hardware import (
    AcceleratorInfo,
    HardwareSnapshot,
    detect_hardware,
    evaluate_hardware,
)
from bridge.profiles.profile import bundled_profile_path, load_profile


@pytest.fixture(scope="module")
def profile():
    return load_profile(bundled_profile_path(), allow_unsigned_draft=True).profile


@pytest.mark.parametrize(
    ("label", "vram_mb", "ram_mb", "disk_mb", "driver", "expected"),
    [
        ("minimum RTX class", 6144, 16384, 24576, "570.26", "supported"),
        ("midrange RTX class", 12288, 32768, 32768, "570.86.15", "recommended"),
        ("datacenter class", 81920, 262144, 1048576, "575.57.08", "recommended"),
    ],
)
def test_realistic_nvidia_tiers(
    profile, label, vram_mb, ram_mb, disk_mb, driver, expected
):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        ram_mb,
        disk_mb,
        (AcceleratorInfo("nvidia", label, vram_mb, driver, "12.8"),),
    )

    result = evaluate_hardware(snapshot, profile)

    assert result.status == expected
    assert result.capability_tier is not None
    assert result.registration_summary() == {
        "profile_status": expected,
        "capability_tier": result.capability_tier,
    }
    assert "accelerator" not in result.registration_summary()


def test_old_driver_is_unsupported(profile):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        32768,
        32768,
        (AcceleratorInfo("nvidia", "RTX", 12288, "520.1", "12.0"),),
    )

    result = evaluate_hardware(snapshot, profile)

    assert result.status == "unsupported"
    assert any("driver" in reason for reason in result.reasons)


def test_old_sixteen_gib_disk_floor_is_unsupported(profile):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        16384,
        16384,
        (AcceleratorInfo("nvidia", "RTX", 6144, "570.26", "12.8"),),
    )

    result = evaluate_hardware(snapshot, profile)

    assert result.status == "unsupported"
    assert any("free disk" in reason for reason in result.reasons)


def test_cuda_12_minor_compatibility_driver_is_not_assumed(profile):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        32768,
        32768,
        (AcceleratorInfo("nvidia", "RTX", 12288, "525.60.13", "12.8"),),
    )

    result = evaluate_hardware(snapshot, profile)

    assert result.status == "unsupported"
    assert any("570.26" in reason for reason in result.reasons)


def test_reported_cuda_compatibility_below_profile_is_unsupported(profile):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        32768,
        32768,
        (AcceleratorInfo("nvidia", "RTX", 12288, "580.1", "12.4"),),
    )

    result = evaluate_hardware(snapshot, profile)

    assert result.status == "unsupported"
    assert any("CUDA compatibility 12.4 is below 12.8" in reason for reason in result.reasons)


def test_cpu_only_host_is_unsupported(profile):
    snapshot = HardwareSnapshot("linux", "x86_64", 32768, 32768, ())

    result = evaluate_hardware(snapshot, profile)

    assert result.status == "unsupported"
    assert result.selected_accelerator is None


def test_multi_gpu_host_selects_largest_compatible_gpu(profile):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        65536,
        65536,
        (
            AcceleratorInfo("nvidia", "small", 4096, "575.1", "12.8", 0, "GPU-small"),
            AcceleratorInfo("nvidia", "large", 24576, "575.1", "12.8", 1, "GPU-large"),
        ),
    )

    result = evaluate_hardware(snapshot, profile)

    assert result.status == "recommended"
    assert result.selected_accelerator.name == "large"

    selected = evaluate_hardware(snapshot, profile, accelerator_selector="0")
    assert selected.status == "unsupported"
    assert selected.selected_accelerator.name == "small"
    assert any("VRAM" in reason for reason in selected.reasons)

    selected = evaluate_hardware(snapshot, profile, accelerator_selector="GPU-large")
    assert selected.status == "recommended"
    assert selected.selected_accelerator.runtime_selector() == "GPU-large"


def test_unknown_or_ambiguous_gpu_selector_fails_closed(profile):
    snapshot = HardwareSnapshot(
        "linux",
        "x86_64",
        65536,
        65536,
        (
            AcceleratorInfo("nvidia", "same", 12288, "575.1", "12.8", 0, "GPU-a"),
            AcceleratorInfo("nvidia", "same", 24576, "575.1", "12.8", 1, "GPU-b"),
        ),
    )

    missing = evaluate_hardware(snapshot, profile, accelerator_selector="9")
    assert missing.status == "unsupported"
    assert "not detected" in missing.reasons[0]

    ambiguous = evaluate_hardware(snapshot, profile, accelerator_selector="same")
    assert ambiguous.status == "unsupported"
    assert "ambiguous" in ambiguous.reasons[0]


def test_nvidia_smi_detection(monkeypatch, tmp_path):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    def fake_run(command, **_kwargs):
        if "--query-gpu=index,uuid,name,memory.total,driver_version" in command:
            return SimpleNamespace(
                stdout=(
                    "0, GPU-4090, NVIDIA RTX 4090, 24564, 575.57.08\n"
                    "1, GPU-3060, NVIDIA RTX 3060, 12288, 575.57.08\n"
                )
            )
        return SimpleNamespace(stdout="NVIDIA-SMI 575.57 CUDA Version: 12.9")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)
    monkeypatch.setattr(hardware, "_total_memory_mb", lambda: 65536)

    snapshot = detect_hardware(tmp_path)

    assert [item.name for item in snapshot.accelerators] == [
        "NVIDIA RTX 4090",
        "NVIDIA RTX 3060",
    ]
    assert snapshot.accelerators[0].memory_mb == 24564
    assert snapshot.accelerators[0].driver_version == "575.57.08"
    assert snapshot.accelerators[0].runtime_version == "12.9"
    assert snapshot.accelerators[0].device_index == 0
    assert snapshot.accelerators[0].device_uuid == "GPU-4090"

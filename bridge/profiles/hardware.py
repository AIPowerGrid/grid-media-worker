# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local hardware inventory and Worker Profile V1 recommendation."""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class AcceleratorInfo:
    vendor: str
    name: str
    memory_mb: int
    driver_version: str | None = None
    runtime_version: str | None = None
    device_index: int | None = None
    device_uuid: str | None = None

    def runtime_selector(self) -> str | None:
        """Return the stable CUDA selector retained only in private local state."""

        if self.device_uuid:
            return self.device_uuid
        return str(self.device_index) if self.device_index is not None else None


@dataclass(frozen=True)
class HardwareSnapshot:
    os: str
    architecture: str
    ram_mb: int
    disk_free_mb: int
    accelerators: tuple[AcceleratorInfo, ...]

    def public_summary(self) -> Mapping[str, Any]:
        """Return local diagnostic data; callers must not send this to Core."""

        return {
            "os": self.os,
            "architecture": self.architecture,
            "ram_mb": self.ram_mb,
            "disk_free_mb": self.disk_free_mb,
            "accelerators": [asdict(item) for item in self.accelerators],
        }


@dataclass(frozen=True)
class Recommendation:
    status: str
    capability_tier: str | None
    selected_accelerator: AcceleratorInfo | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def registration_summary(self) -> Mapping[str, Any]:
        """Return the coarse, privacy-preserving result safe to advertise."""

        return {
            "profile_status": self.status,
            "capability_tier": self.capability_tier,
        }


def detect_hardware(target_path: str | Path | None = None) -> HardwareSnapshot:
    """Detect host resources without importing a GPU framework."""

    target = Path(target_path or Path.cwd()).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return HardwareSnapshot(
        os=_normalize_os(platform.system()),
        architecture=_normalize_architecture(platform.machine()),
        ram_mb=_total_memory_mb(),
        disk_free_mb=shutil.disk_usage(target).free // (1024 * 1024),
        accelerators=tuple(_detect_nvidia()),
    )


def evaluate_hardware(
    snapshot: HardwareSnapshot,
    profile: Mapping[str, Any],
    *,
    accelerator_selector: str | None = None,
) -> Recommendation:
    """Classify a host as recommended, supported, or unsupported."""

    platform_rule = next(
        (
            rule
            for rule in profile["platforms"]
            if rule["os"] == snapshot.os
            and snapshot.architecture in rule["architectures"]
        ),
        None,
    )
    if platform_rule is None:
        return Recommendation(
            "unsupported",
            None,
            None,
            (f"{snapshot.os}/{snapshot.architecture} is not supported by this profile",),
        )

    vendor = platform_rule["accelerator"]["vendor"]
    candidates = [item for item in snapshot.accelerators if item.vendor == vendor]
    if not candidates:
        return Recommendation(
            "unsupported",
            None,
            None,
            (f"a {vendor} accelerator is required but none was detected",),
        )
    try:
        selected = _select_accelerator(candidates, accelerator_selector)
    except ValueError as exc:
        return Recommendation("unsupported", None, None, (str(exc),))

    minimum = profile["hardware"]["minimum"]
    recommended = profile["hardware"]["recommended"]
    failures: list[str] = []
    _require_at_least(failures, "VRAM", selected.memory_mb, minimum["vram_mb"])
    _require_at_least(failures, "RAM", snapshot.ram_mb, minimum["ram_mb"])
    _require_at_least(failures, "free disk", snapshot.disk_free_mb, minimum["disk_free_mb"])

    minimum_driver = platform_rule["accelerator"].get("minimum_driver")
    if minimum_driver and (
        not selected.driver_version
        or _version_tuple(selected.driver_version) < _version_tuple(minimum_driver)
    ):
        failures.append(
            f"NVIDIA driver {selected.driver_version or 'unknown'} is below {minimum_driver}"
        )
    required_runtime = profile["runtime"].get("cuda")
    if required_runtime and selected.runtime_version and (
        _version_tuple(selected.runtime_version) < _version_tuple(required_runtime)
    ):
        failures.append(
            f"NVIDIA CUDA compatibility {selected.runtime_version} is below {required_runtime}"
        )
    if failures:
        return Recommendation("unsupported", None, selected, tuple(failures))

    recommended_checks = (
        selected.memory_mb >= recommended["vram_mb"],
        snapshot.ram_mb >= recommended["ram_mb"],
        snapshot.disk_free_mb >= recommended["disk_free_mb"],
    )
    recommended_driver = platform_rule["accelerator"].get("recommended_driver")
    if recommended_driver:
        recommended_checks += (
            bool(selected.driver_version)
            and _version_tuple(selected.driver_version) >= _version_tuple(recommended_driver),
        )

    if all(recommended_checks):
        return Recommendation(
            "recommended",
            profile["hardware"]["recommended_tier"],
            selected,
            ("host meets the recommended hardware and driver profile",),
        )

    warnings = _supported_warnings(snapshot, selected, recommended, recommended_driver)
    return Recommendation(
        "supported",
        profile["hardware"]["minimum_tier"],
        selected,
        ("host meets the minimum profile and will use conservative settings",),
        tuple(warnings),
    )


def _detect_nvidia() -> Sequence[AcceleratorInfo]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return ()
    command = [
        executable,
        "--query-gpu=index,uuid,name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ()

    runtime_version = _nvidia_runtime_version(executable)
    accelerators: list[AcceleratorInfo] = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 5:
            continue
        try:
            device_index = int(fields[0])
            memory_mb = int(float(fields[3]))
        except ValueError:
            continue
        accelerators.append(
            AcceleratorInfo(
                "nvidia",
                fields[2],
                memory_mb,
                fields[4],
                runtime_version,
                device_index,
                fields[1],
            )
        )
    return accelerators


def _nvidia_runtime_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"CUDA Version:\s*([0-9.]+)", result.stdout)
    return match.group(1) if match else None


def _total_memory_mb() -> int:
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
        status.length = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.total_physical // (1024 * 1024)

    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size) // (1024 * 1024)
    except (AttributeError, OSError, ValueError):
        return 0


def _supported_warnings(
    snapshot: HardwareSnapshot,
    accelerator: AcceleratorInfo,
    recommended: Mapping[str, int],
    recommended_driver: str | None,
) -> list[str]:
    warnings: list[str] = []
    if accelerator.memory_mb < recommended["vram_mb"]:
        warnings.append("VRAM is below the recommended tier; CPU offload may reduce throughput")
    if snapshot.ram_mb < recommended["ram_mb"]:
        warnings.append("RAM is below the recommended tier")
    if snapshot.disk_free_mb < recommended["disk_free_mb"]:
        warnings.append("free disk is below the recommended tier")
    if recommended_driver and accelerator.driver_version and (
        _version_tuple(accelerator.driver_version) < _version_tuple(recommended_driver)
    ):
        warnings.append(f"driver {accelerator.driver_version} is supported but {recommended_driver}+ is preferred")
    return warnings


def _select_accelerator(
    candidates: Sequence[AcceleratorInfo],
    selector: str | None,
) -> AcceleratorInfo:
    if selector is None:
        return max(candidates, key=lambda item: item.memory_mb)
    wanted = selector.strip().lower()
    if not wanted:
        raise ValueError("GPU selector cannot be empty")
    matches = [
        item
        for item in candidates
        if wanted
        in {
            str(item.device_index).lower() if item.device_index is not None else "",
            (item.device_uuid or "").lower(),
            item.name.lower(),
        }
    ]
    if not matches:
        raise ValueError(f"requested GPU {selector!r} was not detected")
    if len(matches) > 1:
        raise ValueError(
            f"GPU selector {selector!r} is ambiguous; use its index or UUID"
        )
    return matches[0]


def _require_at_least(
    failures: list[str],
    label: str,
    actual_mb: int,
    required_mb: int,
) -> None:
    if actual_mb < required_mb:
        failures.append(f"{label} {actual_mb} MiB is below required {required_mb} MiB")


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(item) for item in numbers) if numbers else (0,)


def _normalize_os(value: str) -> str:
    return {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(
        value.lower(), value.lower()
    )


def _normalize_architecture(value: str) -> str:
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }.get(value.lower(), value.lower())

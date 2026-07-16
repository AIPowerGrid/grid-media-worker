# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline release qualification from private benchmark reports."""

from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .hardware import AcceleratorInfo, HardwareSnapshot, evaluate_hardware
from .profile import canonical_profile_bytes
from .state import profile_digest


class QualificationError(ValueError):
    """Benchmark evidence does not satisfy the signed release policy."""


def qualify_reports(
    profile: Mapping[str, Any],
    report_paths: Mapping[str, str | Path],
) -> tuple[Mapping[str, Any], str]:
    """Verify all required private reports and return a privacy-safe manifest."""

    policy = profile["release_qualification"]
    required = tuple(policy["required_classes"])
    if set(report_paths) != set(required):
        missing = sorted(set(required) - set(report_paths))
        extra = sorted(set(report_paths) - set(required))
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise QualificationError("qualification reports must match policy: " + "; ".join(detail))

    entries = []
    report_digests = set()
    for hardware_class in required:
        path = Path(report_paths[hardware_class]).expanduser().resolve()
        raw = path.read_bytes()
        report_sha256 = hashlib.sha256(raw).hexdigest()
        if report_sha256 in report_digests:
            raise QualificationError("each qualification class requires a distinct report")
        report_digests.add(report_sha256)
        try:
            report = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QualificationError(f"{hardware_class} report is not valid JSON") from exc
        entries.append(
            _verify_report(
                profile,
                policy,
                hardware_class,
                report_sha256,
                report,
            )
        )

    manifest = {
        "schema": "aipg-worker-qualification-v1",
        "policy_version": policy["policy_version"],
        "scope": policy["scope"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "draft_profile_digest": profile_digest(profile),
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "runtime_digest": profile["runtime"]["digest"],
        "recipe_root": profile["recipe"]["sha256"],
        "recipe_vault_root": profile["recipe"]["onchain_root"],
        "reports": entries,
    }
    digest = hashlib.sha256(canonical_profile_bytes(manifest)).hexdigest()
    return manifest, digest


def _verify_report(
    profile: Mapping[str, Any],
    policy: Mapping[str, Any],
    hardware_class: str,
    report_sha256: str,
    report: Any,
) -> Mapping[str, Any]:
    if not isinstance(report, dict) or not isinstance(report.get("hardware"), dict):
        raise QualificationError(f"{hardware_class} requires a private hardware report")
    expected = {
        "benchmark_version": 1,
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "profile_digest": profile_digest(profile),
        "runtime_digest": profile["runtime"]["digest"],
        "recipe_root": profile["recipe"]["sha256"],
    }
    for field, value in expected.items():
        if report.get(field) != value:
            raise QualificationError(f"{hardware_class} report has mismatched {field}")

    snapshot = _hardware_snapshot(hardware_class, report["hardware"])
    selector = report.get("selected_runtime_device")
    if not isinstance(selector, str) or not selector:
        raise QualificationError(f"{hardware_class} report has no exact GPU binding")
    recommendation = evaluate_hardware(
        snapshot,
        profile,
        accelerator_selector=selector,
    )
    if recommendation.status != report.get("recommendation_status"):
        raise QualificationError(f"{hardware_class} recommendation status is inconsistent")
    if recommendation.capability_tier != report.get("capability_tier"):
        raise QualificationError(f"{hardware_class} capability tier is inconsistent")
    selected = recommendation.selected_accelerator
    if selected is None or recommendation.status == "unsupported":
        raise QualificationError(f"{hardware_class} report is not supported by the profile")
    _verify_hardware_class(profile, policy, hardware_class, recommendation.status, selected.memory_mb)

    created_at = report.get("created_at")
    try:
        timestamp = datetime.fromisoformat(created_at)
    except (TypeError, ValueError) as exc:
        raise QualificationError(f"{hardware_class} report timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise QualificationError(f"{hardware_class} report timestamp must include a timezone")

    runs = report.get("runs")
    results = report.get("canary_results")
    if not isinstance(runs, int) or runs < policy["runs_per_class"]:
        raise QualificationError(
            f"{hardware_class} requires at least {policy['runs_per_class']} canary runs"
        )
    if not isinstance(results, list) or len(results) != runs:
        raise QualificationError(f"{hardware_class} canary result count does not match runs")
    _verify_canary_results(profile, hardware_class, results, report.get("metrics"))
    _verify_resource_metrics(
        hardware_class,
        report["metrics"],
        selected.memory_mb,
        snapshot.ram_mb,
    )

    return {
        "class": hardware_class,
        "report_sha256": report_sha256,
        "created_at": created_at,
        "recommendation_status": recommendation.status,
        "capability_tier": recommendation.capability_tier,
        "runs": runs,
        "metrics": report["metrics"],
    }


def _hardware_snapshot(hardware_class: str, value: Mapping[str, Any]) -> HardwareSnapshot:
    try:
        accelerators = tuple(
            AcceleratorInfo(
                vendor=item["vendor"],
                name=item["name"],
                memory_mb=int(item["memory_mb"]),
                driver_version=item.get("driver_version"),
                runtime_version=item.get("runtime_version"),
                device_index=item.get("device_index"),
                device_uuid=item.get("device_uuid"),
            )
            for item in value["accelerators"]
        )
        return HardwareSnapshot(
            os=value["os"],
            architecture=value["architecture"],
            ram_mb=int(value["ram_mb"]),
            disk_free_mb=int(value["disk_free_mb"]),
            accelerators=accelerators,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationError(f"{hardware_class} hardware inventory is malformed") from exc


def _verify_hardware_class(
    profile: Mapping[str, Any],
    policy: Mapping[str, Any],
    hardware_class: str,
    status: str,
    vram_mb: int,
) -> None:
    recommended_vram = profile["hardware"]["recommended"]["vram_mb"]
    datacenter_vram = policy["datacenter_min_vram_mb"]
    valid = {
        "minimum": status == "supported" and vram_mb < recommended_vram,
        "midrange": status == "recommended" and vram_mb < datacenter_vram,
        "datacenter": status == "recommended" and vram_mb >= datacenter_vram,
    }[hardware_class]
    if not valid:
        raise QualificationError(
            f"{hardware_class} report does not satisfy its VRAM/recommendation class"
        )


def _verify_canary_results(
    profile: Mapping[str, Any],
    hardware_class: str,
    results: list[Any],
    metrics: Any,
) -> None:
    elapsed = []
    realtime = []
    minimum_seconds = max(1.0, profile["canary"]["seconds"] * 0.75)
    for result in results:
        if not isinstance(result, dict):
            raise QualificationError(f"{hardware_class} has a malformed canary result")
        try:
            elapsed_seconds = float(result["elapsed_seconds"])
            audio_seconds = float(result["audio_seconds"])
            output_bytes = int(result["output_bytes"])
            sample_rate = int(result["sample_rate"])
            channels = int(result["channels"])
            output_sha256 = result["output_sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationError(f"{hardware_class} has a malformed canary result") from exc
        if (
            elapsed_seconds <= 0
            or audio_seconds < minimum_seconds
            or output_bytes <= 0
            or sample_rate <= 0
            or channels not in (1, 2)
            or not isinstance(output_sha256, str)
            or len(output_sha256) != 64
        ):
            raise QualificationError(f"{hardware_class} contains a failed canary result")
        try:
            int(output_sha256, 16)
        except ValueError as exc:
            raise QualificationError(
                f"{hardware_class} contains a non-hex canary output hash"
            ) from exc
        elapsed.append(elapsed_seconds)
        realtime.append(audio_seconds / elapsed_seconds)

    if not isinstance(metrics, dict):
        raise QualificationError(f"{hardware_class} benchmark metrics are missing")
    expected_elapsed = {
        "min": round(min(elapsed), 3),
        "median": round(statistics.median(elapsed), 3),
        "max": round(max(elapsed), 3),
    }
    if metrics.get("elapsed_seconds") != expected_elapsed:
        raise QualificationError(f"{hardware_class} elapsed metrics are inconsistent")
    expected_realtime = round(statistics.median(realtime), 4)
    if metrics.get("audio_seconds_per_wall_second_median") != expected_realtime:
        raise QualificationError(f"{hardware_class} realtime metrics are inconsistent")


def _verify_resource_metrics(
    hardware_class: str,
    metrics: Mapping[str, Any],
    gpu_total_mb: int,
    ram_total_mb: int,
) -> None:
    gpu_used = metrics.get("peak_gpu_used_mb")
    ram_used = metrics.get("peak_host_ram_used_mb")
    if not isinstance(gpu_used, int) or not 0 < gpu_used <= gpu_total_mb:
        raise QualificationError(f"{hardware_class} GPU resource sampling is invalid")
    if not isinstance(ram_used, int) or not 0 < ram_used <= ram_total_mb:
        raise QualificationError(f"{hardware_class} RAM resource sampling is invalid")

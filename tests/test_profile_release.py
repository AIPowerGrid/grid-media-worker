# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bridge.profiles.profile import bundled_profile_path, load_profile
from bridge.profiles.qualification import QualificationError, qualify_reports
from bridge.profiles.release import finalize_profile
from bridge.profiles.state import profile_digest


def _private_key(tmp_path, password=None):
    path = tmp_path / "release-key.pem"
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=(
                serialization.BestAvailableEncryption(password)
                if password
                else serialization.NoEncryption()
            ),
        )
    )
    os.chmod(path, 0o600)
    return path


def _qualification_reports(tmp_path):
    profile = json.loads(bundled_profile_path().read_text(encoding="utf-8"))["profile"]
    specifications = {
        "minimum": (6144, "supported", profile["hardware"]["minimum_tier"]),
        "midrange": (24576, "recommended", profile["hardware"]["recommended_tier"]),
        "datacenter": (81920, "recommended", profile["hardware"]["recommended_tier"]),
    }
    reports = {}
    for hardware_class, (vram_mb, status, capability_tier) in specifications.items():
        device = f"GPU-{hardware_class}"
        results = [
            {
                "elapsed_seconds": elapsed,
                "output_bytes": 1024,
                "output_sha256": str(index + 1) * 64,
                "audio_seconds": 10.0,
                "sample_rate": 48000,
                "channels": 2,
            }
            for index, elapsed in enumerate((8.0, 9.0, 10.0))
        ]
        report = {
            "benchmark_version": 1,
            "created_at": "2026-07-16T12:00:00+00:00",
            "profile_id": profile["id"],
            "profile_version": profile["version"],
            "profile_digest": profile_digest(profile),
            "runtime_digest": profile["runtime"]["digest"],
            "recipe_root": profile["recipe"]["sha256"],
            "recipe_vault_root": None,
            "recommendation_status": status,
            "capability_tier": capability_tier,
            "runs": 3,
            "metrics": {
                "elapsed_seconds": {"min": 8.0, "median": 9.0, "max": 10.0},
                "audio_seconds_per_wall_second_median": 1.1111,
                "peak_gpu_used_mb": vram_mb - 512,
                "peak_host_ram_used_mb": 24000,
            },
            "privacy": "local-only; contains exact hardware inventory",
            "hardware": {
                "os": "linux",
                "architecture": "x86_64",
                "ram_mb": 262144,
                "disk_free_mb": 1048576,
                "accelerators": [
                    {
                        "vendor": "nvidia",
                        "name": f"private {hardware_class} GPU",
                        "memory_mb": vram_mb,
                        "driver_version": "575.57.08",
                        "runtime_version": "12.8",
                        "device_index": 0,
                        "device_uuid": device,
                    }
                ],
            },
            "selected_runtime_device": device,
            "canary_results": results,
        }
        path = tmp_path / f"{hardware_class}.json"
        path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        reports[hardware_class] = path
    return reports


def test_release_finalizer_binds_registered_root_and_verifies_signature(tmp_path):
    draft = json.loads(bundled_profile_path().read_text(encoding="utf-8"))
    root = draft["profile"]["recipe"]["sha256"]
    output = tmp_path / "active-profile.json"

    result = finalize_profile(
        bundled_profile_path(),
        output,
        _private_key(tmp_path),
        key_id="worker-profile-2026-01",
        recipe_vault_root="0x" + root,
        qualification_reports=_qualification_reports(tmp_path),
    )

    document = load_profile(
        output,
        trusted_keys={result["key_id"]: result["public_key_base64"]},
    )
    assert document.profile["status"] == "active"
    assert document.profile["recipe"]["onchain_root"] == "0x" + root
    evidence = document.profile["release_qualification"]["evidence"]
    assert evidence["manifest_sha256"] == result["qualification_manifest_sha256"]
    assert document.signature_verified is True
    assert result["profile_digest"] != root
    manifest = json.loads(
        (tmp_path / "active-profile.json.qualification.json").read_text(encoding="utf-8")
    )
    assert [item["class"] for item in manifest["reports"]] == [
        "minimum",
        "midrange",
        "datacenter",
    ]
    assert "hardware" not in json.dumps(manifest)
    assert "private minimum GPU" not in json.dumps(manifest)


def test_release_finalizer_rejects_unregistered_or_wrong_root(tmp_path):
    with pytest.raises(ValueError, match="must equal"):
        finalize_profile(
            bundled_profile_path(),
            tmp_path / "profile.json",
            _private_key(tmp_path),
            key_id="release",
            recipe_vault_root="00" * 32,
            qualification_reports=_qualification_reports(tmp_path),
        )


def test_release_finalizer_refuses_insecure_private_key_permissions(tmp_path):
    key = _private_key(tmp_path)
    if os.name == "nt":
        pytest.skip("POSIX mode check does not apply on Windows")
    os.chmod(key, 0o644)
    with pytest.raises(PermissionError, match="0600"):
        finalize_profile(
            bundled_profile_path(),
            tmp_path / "profile.json",
            key,
            key_id="release",
            recipe_vault_root=json.loads(
                bundled_profile_path().read_text(encoding="utf-8")
            )["profile"]["recipe"]["sha256"],
            qualification_reports=_qualification_reports(tmp_path),
        )


def test_release_finalizer_accepts_encrypted_private_key(tmp_path):
    password = b"correct horse battery staple"
    draft = json.loads(bundled_profile_path().read_text(encoding="utf-8"))
    result = finalize_profile(
        bundled_profile_path(),
        tmp_path / "profile.json",
        _private_key(tmp_path, password),
        key_id="encrypted-release",
        recipe_vault_root=draft["profile"]["recipe"]["sha256"],
        qualification_reports=_qualification_reports(tmp_path),
        private_key_password=password,
    )
    assert result["signature_verified"] is True


def test_release_qualification_requires_every_hardware_class(tmp_path):
    reports = _qualification_reports(tmp_path)
    reports.pop("datacenter")
    profile = load_profile(bundled_profile_path(), allow_unsigned_draft=True).profile

    with pytest.raises(QualificationError, match="missing datacenter"):
        qualify_reports(profile, reports)


def test_release_qualification_rejects_swapped_hardware_classes(tmp_path):
    reports = _qualification_reports(tmp_path)
    reports["minimum"], reports["midrange"] = reports["midrange"], reports["minimum"]
    profile = load_profile(bundled_profile_path(), allow_unsigned_draft=True).profile

    with pytest.raises(QualificationError, match="minimum report does not satisfy"):
        qualify_reports(profile, reports)


def test_release_qualification_rejects_mismatched_profile(tmp_path):
    reports = _qualification_reports(tmp_path)
    report = json.loads(reports["midrange"].read_text(encoding="utf-8"))
    report["profile_digest"] = "0" * 64
    reports["midrange"].write_text(json.dumps(report), encoding="utf-8")
    profile = load_profile(bundled_profile_path(), allow_unsigned_draft=True).profile

    with pytest.raises(QualificationError, match="mismatched profile_digest"):
        qualify_reports(profile, reports)


def test_release_qualification_requires_three_successful_runs(tmp_path):
    reports = _qualification_reports(tmp_path)
    report = json.loads(reports["datacenter"].read_text(encoding="utf-8"))
    report["runs"] = 2
    report["canary_results"] = report["canary_results"][:2]
    reports["datacenter"].write_text(json.dumps(report), encoding="utf-8")
    profile = load_profile(bundled_profile_path(), allow_unsigned_draft=True).profile

    with pytest.raises(QualificationError, match="at least 3"):
        qualify_reports(profile, reports)


def test_release_qualification_requires_resource_samples(tmp_path):
    reports = _qualification_reports(tmp_path)
    report = json.loads(reports["midrange"].read_text(encoding="utf-8"))
    report["metrics"]["peak_gpu_used_mb"] = None
    reports["midrange"].write_text(json.dumps(report), encoding="utf-8")
    profile = load_profile(bundled_profile_path(), allow_unsigned_draft=True).profile

    with pytest.raises(QualificationError, match="GPU resource sampling"):
        qualify_reports(profile, reports)

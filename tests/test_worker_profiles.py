# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bridge.profiles.profile import (
    ProfileSignatureError,
    ProfileValidationError,
    bundled_profile_path,
    canonical_profile_bytes,
    calculate_runtime_digest,
    load_bundled_trusted_keys,
    load_profile,
)


def _draft_envelope():
    return json.loads(bundled_profile_path().read_text(encoding="utf-8"))


def _signed_profile(tmp_path):
    envelope = _draft_envelope()
    envelope["profile"]["status"] = "active"
    envelope["profile"]["recipe"]["onchain_root"] = (
        "0x" + envelope["profile"]["recipe"]["sha256"]
    )
    envelope["profile"]["release_qualification"]["evidence"] = {
        "completed_at": "2026-07-16T12:00:00+00:00",
        "manifest_sha256": "1" * 64,
    }
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature = private_key.sign(canonical_profile_bytes(envelope["profile"]))
    envelope["signature"] = {
        "algorithm": "ed25519",
        "key_id": "test-release-key",
        "value": base64.b64encode(signature).decode("ascii"),
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    trusted = {"test-release-key": base64.b64encode(public_key).decode("ascii")}
    return path, trusted


def test_bundled_ace_step_profile_is_valid_draft():
    document = load_profile(bundled_profile_path(), allow_unsigned_draft=True)

    assert document.profile["id"] == "ace-step-v1.5-xl-turbo"
    assert document.profile["status"] == "draft"
    assert document.profile["release_qualification"]["scope"] == "public"
    assert document.signature_verified is False
    assert document.profile["recipe"]["onchain_root"] is None
    assert document.profile["release_qualification"]["evidence"] is None
    recipe = document.profile["recipe"]
    encoded = json.dumps(recipe["spec"], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == recipe["sha256"]
    assert calculate_runtime_digest(document.profile) == document.profile["runtime"]["digest"]
    assert "acemusic.ai" not in json.dumps(document.profile)


def test_unsigned_profile_fails_closed():
    with pytest.raises(ProfileSignatureError, match="unsigned"):
        load_profile(bundled_profile_path())


def test_operator_pilot_verification_key_is_publicly_bundled():
    keys = load_bundled_trusted_keys()

    assert keys["aipg-operator-pilot-2026-01"] == (
        "Ff2zYeMvDFjUZ23uA2vKqTYyFdUcaOQevBHADB9FXao="
    )


def test_active_profile_requires_release_evidence_and_recipe_root(tmp_path):
    envelope = _draft_envelope()
    envelope["profile"]["status"] = "active"
    path = tmp_path / "active-without-evidence.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="profile.(recipe|release_qualification)"):
        load_profile(path, allow_unsigned_draft=True)


def test_active_pilot_requires_evidence_but_not_recipe_vault_claim(tmp_path):
    envelope = _draft_envelope()
    envelope["profile"]["status"] = "active"
    envelope["profile"]["release_qualification"].update(
        {
            "scope": "pilot",
            "required_classes": ["midrange"],
            "evidence": {
                "completed_at": "2026-07-16T12:00:00+00:00",
                "manifest_sha256": "1" * 64,
            },
        }
    )
    path = tmp_path / "active-pilot.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ProfileSignatureError, match="unsigned"):
        load_profile(path)


def test_valid_ed25519_profile_is_accepted(tmp_path):
    path, trusted = _signed_profile(tmp_path)

    document = load_profile(path, trusted_keys=trusted)

    assert document.signature_verified is True
    assert document.key_id == "test-release-key"


def test_tampered_signed_profile_is_rejected(tmp_path):
    path, trusted = _signed_profile(tmp_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["profile"]["hardware"]["minimum"]["vram_mb"] = 1
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ProfileSignatureError, match="verification failed"):
        load_profile(path, trusted_keys=trusted)


def test_unknown_signing_key_is_rejected(tmp_path):
    path, _trusted = _signed_profile(tmp_path)

    with pytest.raises(ProfileSignatureError, match="not trusted"):
        load_profile(path, trusted_keys={})


def test_profile_rejects_path_traversal(tmp_path):
    envelope = _draft_envelope()
    envelope["profile"]["artifacts"][0]["destination"] = "../outside"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="destination"):
        load_profile(path, allow_unsigned_draft=True)


def test_profile_pins_all_large_model_artifacts():
    document = load_profile(bundled_profile_path(), allow_unsigned_draft=True)
    snapshots = [
        item
        for item in document.profile["artifacts"]
        if item["kind"] == "huggingface_snapshot"
    ]
    shared, xl = snapshots

    assert len(shared["revision"]) == 40
    assert len(xl["revision"]) == 40
    assert xl["source"].endswith("acestep-v15-xl-turbo")
    assert xl["destination"].endswith("/acestep-v15-xl-turbo")
    assert any(
        item["path"] == "Qwen3-Embedding-0.6B/model.safetensors"
        for item in shared["files"]
    )
    assert any(item["path"] == "vae/diffusion_pytorch_model.safetensors" for item in shared["files"])
    assert not any(item["path"].startswith("acestep-5Hz-lm-") for item in shared["files"])
    assert not any(item["path"].startswith("acestep-v15-turbo/") for item in shared["files"])
    assert len(xl["files"]) == 9
    assert sum(item["size"] for item in xl["files"] if item["path"].endswith(".safetensors")) > 19_000_000_000
    assert all(len(item["sha256"]) == 64 for snapshot in snapshots for item in snapshot["files"])


def test_profile_pins_self_contained_source_archive():
    document = load_profile(bundled_profile_path(), allow_unsigned_draft=True)
    source = next(
        item
        for item in document.profile["artifacts"]
        if item["id"] == document.profile["runtime"]["source_artifact"]
    )

    assert source["kind"] == "tar_archive"
    assert len(source["revision"]) == 40
    assert len(source["sha256"]) == 64
    assert source["size"] > 1_000_000
    assert source["unpacked_size"] > source["size"]
    assert source["strip_components"] == 1

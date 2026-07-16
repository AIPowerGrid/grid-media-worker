# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bridge.profiles.hardware import AcceleratorInfo, Recommendation
from bridge.profiles.installer import InstalledArtifact
from bridge.profiles.profile import (
    ProfileDocument,
    bundled_profile_path,
    canonical_profile_bytes,
    load_profile,
)
from bridge.profiles.state import (
    ProfileStateError,
    authoritative_capabilities,
    record_canary_pass,
    validated_install_state,
    write_install_state,
)


def _signed_document(tmp_path):
    envelope = json.loads(bundled_profile_path().read_text(encoding="utf-8"))
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
    envelope["signature"] = {
        "algorithm": "ed25519",
        "key_id": "test-release-key",
        "value": base64.b64encode(
            private_key.sign(canonical_profile_bytes(envelope["profile"]))
        ).decode("ascii"),
    }
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(envelope), encoding="utf-8")
    trusted = {"test-release-key": base64.b64encode(public_key).decode("ascii")}
    return load_profile(profile_path, trusted_keys=trusted)


def _recommendation():
    return Recommendation(
        "recommended",
        "audio.ace-step.standard",
        AcceleratorInfo("nvidia", "RTX", 24576, "575.1", "12.8", 2, "GPU-test"),
        ("ok",),
    )


def test_capabilities_unlock_only_after_canary(tmp_path):
    document = _signed_document(tmp_path)
    state_path = tmp_path / "state.json"
    write_install_state(
        state_path,
        document,
        _recommendation(),
        (InstalledArtifact("runtime", tmp_path, "verified", 28),),
    )

    with pytest.raises(ProfileStateError, match="has not passed"):
        authoritative_capabilities(state_path, document)

    record_canary_pass(
        state_path,
        document,
        {"output_sha256": "1" * 64, "audio_seconds": 10.0},
    )

    capabilities = authoritative_capabilities(state_path, document)
    assert capabilities == tuple(document.profile["capabilities_after_validation"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["runtime_device"] == "GPU-test"
    assert "RTX" not in json.dumps(state)
    assert validated_install_state(state_path, document)["runtime_device"] == "GPU-test"


def test_unsigned_draft_never_advertises_even_after_canary(tmp_path):
    document = load_profile(bundled_profile_path(), allow_unsigned_draft=True)
    state_path = tmp_path / "state.json"
    write_install_state(state_path, document, _recommendation(), ())
    record_canary_pass(state_path, document, {"audio_seconds": 10.0})

    with pytest.raises(ProfileStateError, match="active, signed"):
        authoritative_capabilities(state_path, document)


def test_profile_digest_change_invalidates_canary(tmp_path):
    document = _signed_document(tmp_path)
    state_path = tmp_path / "state.json"
    write_install_state(state_path, document, _recommendation(), ())
    record_canary_pass(state_path, document, {"audio_seconds": 10.0})
    changed = dict(document.profile)
    changed["version"] = "0.1.1"
    changed_document = ProfileDocument(
        changed,
        document.key_id,
        document.signature_verified,
        document.source,
    )

    with pytest.raises(ProfileStateError, match="version"):
        authoritative_capabilities(state_path, changed_document)


def test_install_resume_requires_private_gpu_binding(tmp_path):
    document = _signed_document(tmp_path)
    state_path = tmp_path / "state.json"
    write_install_state(state_path, document, _recommendation(), ())
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["runtime_device"] = None
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ProfileStateError, match="GPU binding"):
        validated_install_state(state_path, document)

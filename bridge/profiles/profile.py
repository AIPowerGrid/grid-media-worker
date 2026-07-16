# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validation and signature verification for Worker Profile V1."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator


class ProfileError(ValueError):
    """Base error for an unusable worker profile."""


class ProfileValidationError(ProfileError):
    """The profile does not conform to Worker Profile V1."""


class ProfileSignatureError(ProfileError):
    """The profile signature is missing, unknown, or invalid."""


@dataclass(frozen=True)
class ProfileDocument:
    """A schema-valid profile and its verified signing identity."""

    profile: Mapping[str, Any]
    key_id: str | None
    signature_verified: bool
    source: Path


def canonical_profile_bytes(profile: Mapping[str, Any]) -> bytes:
    """Return the one byte representation covered by the Ed25519 signature."""

    return json.dumps(
        profile,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def bundled_profile_path(name: str = "ace-step-v1.profile.json") -> Path:
    """Resolve a profile shipped with the worker package."""

    resource = files("bridge.profiles").joinpath(name)
    return Path(str(resource))


def load_profile(
    path: str | Path,
    *,
    trusted_keys: Mapping[str, str] | None = None,
    allow_unsigned_draft: bool = False,
) -> ProfileDocument:
    """Load, validate, and verify a worker-profile envelope.

    ``trusted_keys`` maps key IDs to base64-encoded raw Ed25519 public keys.
    Unsigned profiles are accepted only when they are explicitly marked draft
    and the caller opts into development mode.
    """

    source = Path(path).expanduser().resolve()
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileValidationError(f"cannot read profile {source}: {exc}") from exc

    _validate_envelope(envelope)
    profile = envelope["profile"]
    actual_runtime_digest = calculate_runtime_digest(profile)
    if profile["runtime"]["digest"] != actual_runtime_digest:
        raise ProfileValidationError(
            "profile.runtime.digest does not match the committed runtime inputs"
        )
    signature = envelope.get("signature")
    if signature is None:
        if allow_unsigned_draft and profile.get("status") == "draft":
            return ProfileDocument(profile, None, False, source)
        raise ProfileSignatureError("profile is unsigned")

    keys = dict(trusted_keys or load_bundled_trusted_keys())
    key_id = signature["key_id"]
    encoded_key = keys.get(key_id)
    if not encoded_key:
        raise ProfileSignatureError(f"profile signing key is not trusted: {key_id}")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(encoded_key, validate=True)
        )
        signature_bytes = base64.b64decode(signature["value"], validate=True)
        public_key.verify(signature_bytes, canonical_profile_bytes(profile))
    except (ValueError, InvalidSignature) as exc:
        raise ProfileSignatureError("profile signature verification failed") from exc

    return ProfileDocument(profile, key_id, True, source)


def load_bundled_trusted_keys() -> Mapping[str, str]:
    """Load release public keys bundled with this worker version."""

    resource = files("bridge.profiles").joinpath("trusted-keys.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    return data.get("keys", {})


def calculate_runtime_digest(profile: Mapping[str, Any]) -> str:
    """Commit source, dependency lock, model files, and governed recipe."""
    runtime = profile["runtime"]
    source = next(
        item for item in profile["artifacts"] if item["id"] == runtime["source_artifact"]
    )
    model_artifacts = [
        {
            "id": item["id"],
            "source": item["source"],
            "revision": item["revision"],
            "files": item["files"],
        }
        for item in profile["artifacts"]
        if item["kind"] == "huggingface_snapshot"
    ]
    source_commitment = {
        "id": source["id"],
        "kind": source["kind"],
        "url": source["source"],
        "revision": source["revision"],
    }
    for field in ("sha256", "size", "unpacked_size", "strip_components"):
        if field in source:
            source_commitment[field] = source[field]
    commitment = {
        "schema": "aipg-runtime-v1",
        "adapter": runtime["adapter"],
        "model": runtime["model"],
        "python": runtime["python"],
        "cuda": runtime["cuda"],
        "resource_policy": runtime["resource_policy"],
        "source": {**source_commitment, "lock_sha256": runtime["lock_sha256"]},
        "model_artifacts": model_artifacts,
        "recipe_sha256": profile["recipe"]["sha256"],
    }
    return hashlib.sha256(canonical_profile_bytes(commitment)).hexdigest()


def _validate_envelope(envelope: Any) -> None:
    schema_resource = files("bridge.profiles").joinpath("worker-profile-v1.schema.json")
    schema = json.loads(schema_resource.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(envelope),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path) or "<root>"
    raise ProfileValidationError(f"{location}: {first.message}")

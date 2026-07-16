# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable profile installation and canary state without hardware inventory."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .hardware import Recommendation
from .installer import InstalledArtifact
from .profile import ProfileDocument, canonical_profile_bytes

STATE_VERSION = 2


class ProfileStateError(ValueError):
    """Profile state is missing, stale, or not authoritative."""


def profile_digest(profile: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_profile_bytes(profile)).hexdigest()


def write_install_state(
    path: str | Path,
    document: ProfileDocument,
    recommendation: Recommendation,
    artifacts: Sequence[InstalledArtifact],
) -> Mapping[str, Any]:
    """Record a verified install. A reinstall clears prior canary authority."""

    state = {
        "state_version": STATE_VERSION,
        "profile_id": document.profile["id"],
        "profile_version": document.profile["version"],
        "profile_digest": profile_digest(document.profile),
        "signature_verified": document.signature_verified,
        "signing_key_id": document.key_id,
        "capability_tier": recommendation.capability_tier,
        "runtime_device": (
            recommendation.selected_accelerator.runtime_selector()
            if recommendation.selected_accelerator
            else None
        ),
        "runtime_adapter": document.profile["runtime"]["adapter"],
        "runtime_ready": True,
        "installed_at": _now(),
        "artifacts": [
            {
                "id": item.id,
                "status": item.status,
                "files_verified": item.files_verified,
            }
            for item in artifacts
        ],
        "canary": None,
        "capabilities": [],
    }
    _atomic_write(path, state)
    return state


def record_canary_pass(
    path: str | Path,
    document: ProfileDocument,
    result: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Unlock profile capabilities only for this exact installed profile."""

    state = load_state(path)
    _require_matching_profile(state, document)
    state["canary"] = {"passed": True, "completed_at": _now(), **dict(result)}
    state["capabilities"] = list(document.profile["capabilities_after_validation"])
    _atomic_write(path, state)
    return state


def authoritative_capabilities(
    path: str | Path,
    document: ProfileDocument,
) -> tuple[Mapping[str, Any], ...]:
    """Return capabilities only after signed-profile and canary validation."""

    if not document.signature_verified or document.profile["status"] != "active":
        raise ProfileStateError("only an active, signed profile can advertise")
    state = load_state(path)
    _require_matching_profile(state, document)
    if not state.get("signature_verified"):
        raise ProfileStateError("installation was not performed from a verified signature")
    if state.get("signing_key_id") != document.key_id:
        raise ProfileStateError("installed signing key does not match the profile")
    if not (state.get("canary") or {}).get("passed"):
        raise ProfileStateError("profile canary has not passed")
    expected = list(document.profile["capabilities_after_validation"])
    if state.get("capabilities") != expected:
        raise ProfileStateError("stored capabilities do not match the signed profile")
    return tuple(expected)


def load_state(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        state = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileStateError(f"cannot read profile state {source}: {exc}") from exc
    if not isinstance(state, dict) or state.get("state_version") != STATE_VERSION:
        raise ProfileStateError("unsupported profile state version")
    return state


def validated_install_state(
    path: str | Path,
    document: ProfileDocument,
) -> dict[str, Any]:
    """Return resumable local install state only when it matches this profile."""

    state = load_state(path)
    _require_matching_profile(state, document)
    if state.get("signature_verified") is not document.signature_verified:
        raise ProfileStateError("installed profile signature status does not match")
    if state.get("signing_key_id") != document.key_id:
        raise ProfileStateError("installed profile signing key does not match")
    if state.get("runtime_ready") is not True:
        raise ProfileStateError("installed profile runtime is not ready")
    if not isinstance(state.get("capability_tier"), str) or not state["capability_tier"]:
        raise ProfileStateError("installed profile capability tier is missing")
    if not isinstance(state.get("runtime_device"), str) or not state["runtime_device"]:
        raise ProfileStateError("installed profile GPU binding is missing")
    return state


def _require_matching_profile(
    state: Mapping[str, Any],
    document: ProfileDocument,
) -> None:
    if state.get("profile_id") != document.profile["id"]:
        raise ProfileStateError("installed profile ID does not match")
    if state.get("profile_version") != document.profile["version"]:
        raise ProfileStateError("installed profile version does not match")
    if state.get("profile_digest") != profile_digest(document.profile):
        raise ProfileStateError("installed profile digest does not match")


def _atomic_write(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

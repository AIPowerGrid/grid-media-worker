# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Translate authoritative profile state into a privacy-safe WS advertisement."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .profile import load_profile
from .state import authoritative_capabilities, load_state


@dataclass(frozen=True)
class ProfileAdvertisement:
    models: tuple[str, ...]
    job_types: tuple[str, ...]
    metadata: Mapping[str, Any]


def load_profile_advertisement(
    profile_path: str | Path,
    state_path: str | Path,
) -> ProfileAdvertisement:
    """Load only active, signed, canary-proven capabilities for registration."""

    document = load_profile(profile_path)
    capabilities = authoritative_capabilities(state_path, document)
    state = load_state(state_path)
    models = tuple(dict.fromkeys(item["model"] for item in capabilities))
    job_types = tuple(
        dict.fromkeys(
            job_type
            for item in capabilities
            for job_type in item["job_types"]
        )
    )
    canary = state["canary"]
    return ProfileAdvertisement(
        models=models,
        job_types=job_types,
        metadata={
            "id": document.profile["id"],
            "version": document.profile["version"],
            "digest": state["profile_digest"],
            "signing_key_id": document.key_id,
            "capability_tier": state["capability_tier"],
            "runtime_adapter": document.profile["runtime"]["adapter"],
            "runtime_digest": document.profile["runtime"]["digest"],
            "recipe_root": document.profile["recipe"]["sha256"],
            "canary_completed_at": canary["completed_at"],
            "canary_elapsed_seconds": canary.get("elapsed_seconds"),
        },
    )

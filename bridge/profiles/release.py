# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline-only finalization and Ed25519 signing for Worker Profile V1."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .profile import canonical_profile_bytes, load_profile
from .qualification import qualify_reports
from .state import profile_digest

KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def finalize_profile(
    source: str | Path,
    destination: str | Path,
    private_key_path: str | Path,
    *,
    key_id: str,
    recipe_vault_root: str,
    qualification_reports: Mapping[str, str | Path],
    force: bool = False,
    private_key_password: bytes | None = None,
) -> Mapping[str, Any]:
    """Promote one validated draft, bind its registered root, and sign it."""

    if not KEY_ID_RE.fullmatch(key_id):
        raise ValueError("release key ID must use 1-64 letters, numbers, dot, dash, or underscore")
    document = load_profile(source, allow_unsigned_draft=True)
    if document.profile["status"] != "draft" or document.signature_verified:
        raise ValueError("release input must be an unsigned draft profile")
    root = recipe_vault_root.removeprefix("0x").lower()
    if root != document.profile["recipe"]["sha256"]:
        raise ValueError("RecipeVault root must equal the canonical recipe SHA-256")
    qualification_manifest, qualification_digest = qualify_reports(
        document.profile,
        qualification_reports,
    )

    private = _load_private_key(private_key_path, password=private_key_password)
    public = private.public_key()
    source_envelope = json.loads(document.source.read_text(encoding="utf-8"))
    profile = json.loads(json.dumps(document.profile))
    profile["status"] = "active"
    onchain_root = "0x" + root
    profile["recipe"]["onchain_root"] = onchain_root
    profile["release_qualification"]["evidence"] = {
        "completed_at": qualification_manifest["completed_at"],
        "manifest_sha256": qualification_digest,
    }
    signature = private.sign(canonical_profile_bytes(profile))
    envelope = {
        "schema_version": source_envelope["schema_version"],
        "profile": profile,
        "signature": {
            "algorithm": "ed25519",
            "key_id": key_id,
            "value": base64.b64encode(signature).decode("ascii"),
        },
    }
    encoded_public = base64.b64encode(
        public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")

    target = Path(destination).expanduser()
    manifest_target = target.with_name(target.name + ".qualification.json")
    if not force:
        existing = next((path for path in (target, manifest_target) if path.exists()), None)
        if existing:
            raise FileExistsError(f"refusing to overwrite release output: {existing}")
    target.parent.mkdir(parents=True, exist_ok=True)
    profile_temporary = target.with_name(target.name + ".tmp")
    manifest_temporary = manifest_target.with_name(manifest_target.name + ".tmp")

    try:
        profile_temporary.write_text(
            json.dumps(envelope, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_temporary.write_text(
            json.dumps(qualification_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(profile_temporary, 0o644)
        os.chmod(manifest_temporary, 0o644)
        verified = load_profile(profile_temporary, trusted_keys={key_id: encoded_public})
        actual_manifest_digest = hashlib.sha256(
            canonical_profile_bytes(qualification_manifest)
        ).hexdigest()
        if actual_manifest_digest != qualification_digest:
            raise ValueError("qualification manifest digest changed before release")
        os.replace(manifest_temporary, manifest_target)
        os.replace(profile_temporary, target)
    finally:
        profile_temporary.unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)
    return {
        "signed_profile": str(target.resolve()),
        "key_id": key_id,
        "public_key_base64": encoded_public,
        "profile_digest": profile_digest(verified.profile),
        "recipe_vault_root": onchain_root,
        "qualification_manifest": str(manifest_target.resolve()),
        "qualification_manifest_sha256": qualification_digest,
        "signature_verified": verified.signature_verified,
    }


def _load_private_key(
    path: str | Path,
    *,
    password: bytes | None = None,
) -> Ed25519PrivateKey:
    source = Path(path).expanduser()
    if os.name != "nt" and source.stat().st_mode & 0o077:
        raise PermissionError("release private key permissions must be 0600")
    raw = source.read_bytes()
    try:
        key = serialization.load_pem_private_key(raw, password=password)
    except TypeError as exc:
        raise ValueError(
            "release-key password is missing or was supplied for an unencrypted key"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("release private key must be Ed25519 PEM")
    return key

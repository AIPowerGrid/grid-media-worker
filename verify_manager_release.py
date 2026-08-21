# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify a complete Grid media manager release payload offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


PAYLOADS = (
    "grid-media-manager-linux-x86_64",
    "grid-media-manager-windows-x86_64.exe",
    "grid-media-manager-release.spdx.json",
)
REQUIRED_CLASSES = ["minimum", "midrange", "datacenter"]
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
HEX_COMMIT = re.compile(r"[0-9a-f]{40}")
RELEASE_TAG = re.compile(r"manager-v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_release(root: Path) -> None:
    if not root.is_dir():
        raise ValueError(f"release directory not found: {root}")
    required = (*PAYLOADS, "manager-release.json", "SHA256SUMS")
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise ValueError(f"missing release assets: {', '.join(missing)}")

    checksums: dict[str, str] = {}
    for raw in (root / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        parts = raw.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"invalid SHA256SUMS line: {raw!r}")
        digest, name = parts
        name = name.lstrip("*")
        if name in checksums:
            raise ValueError(f"duplicate SHA256SUMS entry: {name}")
        if HEX_SHA256.fullmatch(digest) is None:
            raise ValueError(f"invalid SHA-256 digest for {name}")
        checksums[name] = digest
    if set(checksums) != set(PAYLOADS):
        raise ValueError("SHA256SUMS must cover exactly the manager binaries and SBOM")

    manifest = json.loads((root / "manager-release.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "aipg-manager-release-v1":
        raise ValueError("unsupported manager release manifest schema")
    if RELEASE_TAG.fullmatch(str(manifest.get("tag", ""))) is None:
        raise ValueError("release manifest has an invalid manager tag")
    if HEX_COMMIT.fullmatch(str(manifest.get("commit", ""))) is None:
        raise ValueError("release manifest has an invalid commit identity")

    profile = manifest.get("profile") or {}
    gates = {
        "status": profile.get("status") == "active",
        "signature": profile.get("signature_verified") is True,
        "signer": bool(profile.get("signing_key_id")),
        "scope": profile.get("qualification_scope") == "public",
        "classes": profile.get("qualification_required_classes") == REQUIRED_CLASSES,
        "recipe root": HEX_SHA256.fullmatch(
            str(profile.get("recipe_onchain_root", ""))
        )
        is not None,
        "qualification": HEX_SHA256.fullmatch(
            str(profile.get("qualification_manifest_sha256", ""))
        )
        is not None,
    }
    failed = [name for name, passed in gates.items() if not passed]
    if failed:
        raise ValueError("release profile failed gates: " + ", ".join(failed))

    assets = manifest.get("assets")
    if not isinstance(assets, list) or not all(isinstance(item, dict) for item in assets):
        raise ValueError("release manifest assets must be a list of objects")
    if len(assets) != len(PAYLOADS) or {item.get("name") for item in assets} != set(PAYLOADS):
        raise ValueError("release manifest assets do not match the required payload")
    for item in assets:
        name = item["name"]
        path = root / name
        digest = _sha256(path)
        if digest != checksums[name] or digest != item.get("sha256"):
            raise ValueError(f"checksum mismatch: {name}")
        if path.stat().st_size != item.get("bytes"):
            raise ValueError(f"size mismatch: {name}")

    sbom = json.loads(
        (root / "grid-media-manager-release.spdx.json").read_text(encoding="utf-8")
    )
    if not str(sbom.get("spdxVersion", "")).startswith("SPDX-"):
        raise ValueError("release SBOM is not SPDX JSON")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_directory", type=Path)
    args = parser.parse_args()
    verify_release(args.release_directory)
    print(f"Verified media manager release payload in {args.release_directory}")


if __name__ == "__main__":
    main()

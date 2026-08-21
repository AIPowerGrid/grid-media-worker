import hashlib
import json
from pathlib import Path

import pytest

from bridge.release_verifier import (
    PAYLOADS,
    QUALIFICATION_PAYLOADS,
    verify_qualification_release,
    verify_release,
)


def _release_payload(tmp_path: Path) -> Path:
    for name in PAYLOADS:
        content = b'{"spdxVersion":"SPDX-2.3"}' if name.endswith(".json") else name.encode()
        (tmp_path / name).write_bytes(content)
    assets = []
    checksum_lines = []
    for name in PAYLOADS:
        path = tmp_path / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assets.append({"name": name, "sha256": digest, "bytes": path.stat().st_size})
        checksum_lines.append(f"{digest}  {name}")
    manifest = {
        "schema": "aipg-manager-release-v1",
        "tag": "manager-v0.2.0",
        "commit": "a" * 40,
        "profile": {
            "status": "active",
            "signature_verified": True,
            "signing_key_id": "release-key",
            "qualification_scope": "public",
            "qualification_required_classes": ["minimum", "midrange", "datacenter"],
            "recipe_onchain_root": "b" * 64,
            "qualification_manifest_sha256": "c" * 64,
        },
        "assets": assets,
    }
    (tmp_path / "manager-release.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="ascii")
    return tmp_path


def _qualification_payload(tmp_path: Path) -> Path:
    for name in QUALIFICATION_PAYLOADS:
        content = b'{"spdxVersion":"SPDX-2.3"}' if name.endswith(".json") else name.encode()
        (tmp_path / name).write_bytes(content)
    assets = []
    checksum_lines = []
    for name in QUALIFICATION_PAYLOADS:
        path = tmp_path / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assets.append({"name": name, "sha256": digest, "bytes": path.stat().st_size})
        checksum_lines.append(f"{digest}  {name}")
    manifest = {
        "schema": "aipg-manager-qualification-v1",
        "tag": "manager-qualification-v0.2.0-preview.1",
        "commit": "a" * 40,
        "profile": {
            "status": "draft",
            "signature_verified": False,
            "signing_key_id": None,
            "qualification_scope": "public",
            "qualification_required_classes": ["minimum", "midrange", "datacenter"],
            "qualification_manifest_sha256": None,
        },
        "restrictions": {
            "capability_advertisement": False,
            "grid_enrollment": False,
            "purpose": "hardware_qualification_only",
        },
        "assets": assets,
    }
    (tmp_path / "manager-qualification.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (tmp_path / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="ascii"
    )
    return tmp_path


def test_complete_manager_release_verifies(tmp_path):
    verify_release(_release_payload(tmp_path))


def test_manager_release_rejects_missing_sbom(tmp_path):
    root = _release_payload(tmp_path)
    (root / "grid-media-manager-release.spdx.json").unlink()

    with pytest.raises(ValueError, match="missing release assets"):
        verify_release(root)


def test_manager_release_rejects_unsigned_profile(tmp_path):
    root = _release_payload(tmp_path)
    manifest_path = root / "manager-release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profile"]["signature_verified"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="signature"):
        verify_release(root)


def test_manager_release_rejects_manifest_checksum_disagreement(tmp_path):
    root = _release_payload(tmp_path)
    (root / "grid-media-manager-linux-x86_64").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_release(root)


def test_manager_release_rejects_malformed_asset_entry(tmp_path):
    root = _release_payload(tmp_path)
    manifest_path = root / "manager-release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0] = "not-an-object"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="list of objects"):
        verify_release(root)


def test_manager_release_rejects_duplicate_asset_entry(tmp_path):
    root = _release_payload(tmp_path)
    manifest_path = root / "manager-release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"].append(manifest["assets"][0])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="required payload"):
        verify_release(root)


def test_manager_release_rejects_invalid_release_identity(tmp_path):
    root = _release_payload(tmp_path)
    manifest_path = root / "manager-release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tag"] = "latest"
    manifest["commit"] = "main"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid manager tag"):
        verify_release(root)


def test_complete_qualification_release_verifies(tmp_path):
    verify_qualification_release(_qualification_payload(tmp_path))


def test_qualification_release_rejects_active_profile(tmp_path):
    root = _qualification_payload(tmp_path)
    manifest_path = root / "manager-qualification.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profile"]["status"] = "active"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="draft status"):
        verify_qualification_release(root)


def test_qualification_release_rejects_grid_enrollment(tmp_path):
    root = _qualification_payload(tmp_path)
    manifest_path = root / "manager-qualification.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["restrictions"]["grid_enrollment"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="restrictions"):
        verify_qualification_release(root)


def test_qualification_release_rejects_tampered_binary(tmp_path):
    root = _qualification_payload(tmp_path)
    (root / "grid-media-manager-linux-x86_64").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_qualification_release(root)

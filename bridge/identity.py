# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Worker identity keys, payout-wallet delegation, and signed receipts.

The payout key never belongs on a worker host. A wallet signs a reusable,
time-bounded delegation to a funds-less worker key; that worker key then signs
fresh registration payloads and per-job output commitments.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address

IDENTITY_VERSION = 1
DELEGATION_DOMAIN = "aipg-worker-delegation"
REGISTRATION_DOMAIN = "aipg-worker-registration"
JOB_DOMAIN = "aipg-job"
KEY_FILE_VERSION = 1
DELEGATION_FIELDS = frozenset(
    {
        "version",
        "chain_id",
        "audience",
        "delegation_id",
        "payout_wallet",
        "worker_signer",
        "worker_name",
        "issued_at",
        "expires_at",
    }
)


class WorkerIdentityError(ValueError):
    """Worker identity material is malformed, unsafe, or inconsistent."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def delegation_message(payload: Mapping[str, Any]) -> str:
    return f"{DELEGATION_DOMAIN}:v1:{canonical_json(payload)}"


def registration_message(payload: Mapping[str, Any]) -> str:
    return f"{REGISTRATION_DOMAIN}:v1:{canonical_json(payload)}"


def job_message(job_id: str, result_hash: str) -> str:
    return f"{JOB_DOMAIN}:{job_id}:{result_hash}"


def generate_worker_key(path: str | Path, *, force: bool = False) -> Mapping[str, Any]:
    destination = Path(path).expanduser()
    if destination.exists() and not force:
        raise WorkerIdentityError(f"worker key already exists: {destination}")
    account = Account.create()
    document = {
        "version": KEY_FILE_VERSION,
        "address": account.address.lower(),
        "private_key": account.key.hex(),
    }
    _atomic_private_write(destination, document)
    return {"version": KEY_FILE_VERSION, "address": account.address.lower()}


def import_worker_key(path: str | Path, private_key: str, *, force: bool = False) -> Mapping[str, Any]:
    destination = Path(path).expanduser()
    if destination.exists() and not force:
        raise WorkerIdentityError(f"worker key already exists: {destination}")
    try:
        account = Account.from_key(private_key.strip())
    except Exception as exc:
        raise WorkerIdentityError("invalid worker private key") from exc
    document = {
        "version": KEY_FILE_VERSION,
        "address": account.address.lower(),
        "private_key": account.key.hex(),
    }
    _atomic_private_write(destination, document)
    return {"version": KEY_FILE_VERSION, "address": account.address.lower()}


def load_worker_key(path: str | Path):
    source = Path(path).expanduser()
    _require_private_permissions(source)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerIdentityError(f"cannot read worker key {source}: {exc}") from exc
    if set(document) != {"version", "address", "private_key"} or document.get("version") != KEY_FILE_VERSION:
        raise WorkerIdentityError("unsupported worker key document")
    try:
        account = Account.from_key(document["private_key"])
    except Exception as exc:
        raise WorkerIdentityError("invalid worker private key") from exc
    if account.address.lower() != str(document["address"]).lower():
        raise WorkerIdentityError("worker key address does not match private key")
    return account


def create_delegation_request(
    *,
    worker_key_path: str | Path,
    payout_wallet: str,
    worker_name: str,
    chain_id: int,
    audience: str,
    valid_days: int = 90,
    now: int | None = None,
    delegation_id: str | None = None,
) -> Mapping[str, Any]:
    account = load_worker_key(worker_key_path)
    issued_at = int(now if now is not None else time.time())
    if not worker_name.strip() or worker_name != worker_name.strip():
        raise WorkerIdentityError("worker name must be non-empty and trimmed")
    if not 1 <= valid_days <= 365:
        raise WorkerIdentityError("delegation validity must be between 1 and 365 days")
    wallet = _address(payout_wallet, "payout wallet")
    payload = {
        "version": IDENTITY_VERSION,
        "chain_id": int(chain_id),
        "audience": _audience(audience),
        "delegation_id": delegation_id or secrets.token_hex(16),
        "payout_wallet": wallet,
        "worker_signer": account.address.lower(),
        "worker_name": worker_name,
        "issued_at": issued_at,
        "expires_at": issued_at + valid_days * 86400,
    }
    _validate_delegation_payload(payload)
    return {"payload": payload, "message": delegation_message(payload)}


def install_delegation_certificate(
    request: Mapping[str, Any],
    wallet_signature: str,
    destination: str | Path,
) -> Mapping[str, Any]:
    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        raise WorkerIdentityError("delegation request payload is missing")
    _validate_delegation_payload(payload)
    expected_message = delegation_message(payload)
    if request.get("message") != expected_message:
        raise WorkerIdentityError("delegation request message does not match its payload")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=expected_message), signature=wallet_signature
        )
    except Exception as exc:
        raise WorkerIdentityError("invalid payout-wallet signature") from exc
    if recovered.lower() != payload["payout_wallet"]:
        raise WorkerIdentityError("delegation was not signed by the payout wallet")
    certificate = {"payload": dict(payload), "signature": wallet_signature}
    _atomic_private_write(Path(destination).expanduser(), certificate)
    return certificate


def write_delegation_request(path: str | Path, request: Mapping[str, Any]) -> None:
    payload = request.get("payload")
    if not isinstance(payload, Mapping) or request.get("message") != delegation_message(payload):
        raise WorkerIdentityError("malformed delegation request")
    _validate_delegation_payload(payload)
    _atomic_private_write(Path(path).expanduser(), request)


def load_delegation_certificate(path: str | Path) -> Mapping[str, Any]:
    source = Path(path).expanduser()
    try:
        certificate = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerIdentityError(f"cannot read delegation certificate {source}: {exc}") from exc
    return verify_delegation_certificate(certificate)


def verify_delegation_certificate(certificate: Any) -> Mapping[str, Any]:
    """Verify one certificate without trusting its storage or transport."""
    if not isinstance(certificate, Mapping):
        raise WorkerIdentityError("malformed delegation certificate")
    if set(certificate) != {"payload", "signature"} or not isinstance(
        certificate["payload"], Mapping
    ):
        raise WorkerIdentityError("malformed delegation certificate")
    _validate_delegation_payload(certificate["payload"])
    try:
        recovered = Account.recover_message(
            encode_defunct(text=delegation_message(certificate["payload"])),
            signature=certificate["signature"],
        )
    except Exception as exc:
        raise WorkerIdentityError("invalid delegation certificate signature") from exc
    if recovered.lower() != certificate["payload"]["payout_wallet"]:
        raise WorkerIdentityError("delegation certificate signer is not the payout wallet")
    return {"payload": dict(certificate["payload"]), "signature": certificate["signature"]}


def install_enrollment_certificate(
    certificate: Any,
    destination: str | Path,
    *,
    worker_key_path: str | Path,
    worker_name: str,
    chain_id: int,
    audience: str,
) -> Mapping[str, Any]:
    """Verify a paired certificate targets this rig before storing it."""
    verified = verify_delegation_certificate(certificate)
    payload = verified["payload"]
    worker = load_worker_key(worker_key_path)
    if payload["worker_signer"] != worker.address.lower():
        raise WorkerIdentityError("paired delegation targets another worker key")
    if payload["worker_name"] != worker_name:
        raise WorkerIdentityError("paired delegation targets another worker name")
    if payload["chain_id"] != int(chain_id):
        raise WorkerIdentityError("paired delegation targets another chain")
    if payload["audience"] != _audience(audience):
        raise WorkerIdentityError("paired delegation targets another Core audience")
    _atomic_private_write(Path(destination).expanduser(), verified)
    return verified


def build_registration_proof(
    *,
    worker_key_path: str | Path,
    delegation_path: str | Path,
    worker_name: str,
    models: Sequence[str],
    job_types: Sequence[str],
    bridge_agent: str,
    profile_digest: str | None,
    profile_recipe_root: str | None = None,
    now: int | None = None,
    nonce: str | None = None,
) -> Mapping[str, Any]:
    account = load_worker_key(worker_key_path)
    certificate = load_delegation_certificate(delegation_path)
    delegation = certificate["payload"]
    if delegation["worker_signer"] != account.address.lower():
        raise WorkerIdentityError("delegation targets a different worker key")
    if delegation["worker_name"] != worker_name:
        raise WorkerIdentityError("delegation targets a different worker name")
    timestamp = int(now if now is not None else time.time())
    if timestamp >= int(delegation["expires_at"]):
        raise WorkerIdentityError("worker delegation has expired")
    payload = {
        "version": IDENTITY_VERSION,
        "timestamp": timestamp,
        "nonce": nonce or secrets.token_hex(16),
        "worker_signer": account.address.lower(),
        "worker_name": worker_name,
        "models": list(models),
        "job_types": list(job_types),
        "bridge_agent": bridge_agent,
        "profile_digest": profile_digest,
        "profile_recipe_root": profile_recipe_root,
    }
    signature = Account.sign_message(
        encode_defunct(text=registration_message(payload)), account.key
    ).signature.hex()
    return {
        "payload": payload,
        "signature": signature,
        "delegation": certificate,
    }


def sign_job_result(worker_key_path: str | Path, job_id: str, result_hash: str) -> str:
    account = load_worker_key(worker_key_path)
    return Account.sign_message(
        encode_defunct(text=job_message(str(job_id), result_hash)), account.key
    ).signature.hex()


def _validate_delegation_payload(payload: Mapping[str, Any]) -> None:
    if set(payload) != DELEGATION_FIELDS or payload.get("version") != IDENTITY_VERSION:
        raise WorkerIdentityError("unsupported delegation payload")
    if int(payload.get("chain_id", 0)) <= 0:
        raise WorkerIdentityError("delegation chain ID must be positive")
    _audience(payload.get("audience"))
    _address(payload.get("payout_wallet"), "payout wallet")
    _address(payload.get("worker_signer"), "worker signer")
    if not isinstance(payload.get("delegation_id"), str) or len(payload["delegation_id"]) != 32:
        raise WorkerIdentityError("delegation ID must be 16 random bytes encoded as hex")
    try:
        bytes.fromhex(payload["delegation_id"])
    except ValueError as exc:
        raise WorkerIdentityError("delegation ID must be hexadecimal") from exc
    if not isinstance(payload.get("worker_name"), str) or not payload["worker_name"].strip():
        raise WorkerIdentityError("delegation worker name is missing")
    issued_at = int(payload.get("issued_at", 0))
    expires_at = int(payload.get("expires_at", 0))
    if issued_at <= 0 or expires_at <= issued_at or expires_at - issued_at > 365 * 86400:
        raise WorkerIdentityError("delegation lifetime is invalid")


def _address(value: Any, label: str) -> str:
    if not isinstance(value, str) or not is_address(value):
        raise WorkerIdentityError(f"{label} is not an address")
    return value.lower()


def _audience(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise WorkerIdentityError("delegation audience is invalid")
    if "://" in value or "/" in value or any(ch.isspace() for ch in value):
        raise WorkerIdentityError("delegation audience must be a host name")
    return value.lower()


def _require_private_permissions(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise WorkerIdentityError(f"cannot access worker key {path}: {exc}") from exc
    if os.name != "nt" and mode & 0o077:
        raise WorkerIdentityError(f"worker key permissions must be 0600, got {mode:04o}")


def _atomic_private_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Crash-resumable console pairing for native worker credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import webbrowser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .identity import (
    generate_worker_key,
    install_enrollment_certificate,
    load_delegation_certificate,
    load_worker_key,
)

ENROLLMENT_STATE_VERSION = 1
CREDENTIAL_VERSION = 1
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class EnrollmentClientError(RuntimeError):
    """The manager could not complete or verify console enrollment."""


def grid_api_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EnrollmentClientError("Grid API URL must be HTTP(S)")
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise EnrollmentClientError("remote Grid enrollment requires HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EnrollmentClientError("Grid API URL must not contain credentials or query data")
    return value.rstrip("/")


async def connect_worker(
    *,
    grid_api_url: str,
    profile_id: str,
    worker_name: str,
    worker_key_path: str | Path,
    delegation_path: str | Path,
    credentials_path: str | Path,
    pending_path: str | Path,
    chain_id: int,
    audience: str,
    valid_days: int = 90,
    launch_browser: bool = True,
    timeout_seconds: int = 900,
    restart: bool = False,
    client: httpx.AsyncClient | None = None,
) -> Mapping[str, Any]:
    """Pair one rig through the Console and persist only verified credentials."""
    base = grid_api_base_url(grid_api_url)
    key_path = Path(worker_key_path).expanduser()
    delegation = Path(delegation_path).expanduser()
    credentials = Path(credentials_path).expanduser()
    pending = Path(pending_path).expanduser()

    if not key_path.exists():
        generate_worker_key(key_path)
    worker = load_worker_key(key_path)
    if restart and pending.exists():
        pending.unlink()
    existing = None
    if not pending.exists():
        existing = _existing_connection(
            credentials,
            delegation,
            worker.address.lower(),
            worker_name,
            base,
            chain_id,
            audience,
        )
        if existing and not restart:
            if int(existing["delegation_expires_at"]) <= int(time.time()):
                raise EnrollmentClientError(
                    "worker delegation has expired; rerun connect with --restart"
                )
            return _public_connection(existing)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30.0)
    try:
        state = _load_pending(pending) if pending.exists() else None
        if state is None:
            state = await _create_enrollment(
                http,
                base,
                profile_id=profile_id,
                worker_name=worker_name,
                worker_signer=worker.address.lower(),
                valid_days=valid_days,
                replace_existing=existing is not None,
            )
            _atomic_private_write(pending, state)
        _require_pending_matches(
            state,
            worker_signer=worker.address.lower(),
            worker_name=worker_name,
            profile_id=profile_id,
            grid_api_url=base,
        )
        if launch_browser:
            webbrowser.open(state["authorize_url"], new=2)

        deadline = min(
            time.monotonic() + max(30, timeout_seconds),
            time.monotonic() + max(1, int(state["expires_at"]) - int(time.time())),
        )
        stored_certificate = state.get("certificate")
        result = (
            {"status": "complete", "certificate": stored_certificate}
            if stored_certificate
            else await _poll_until_complete(http, state, deadline)
        )
        certificate = result.get("certificate")
        if not certificate:
            raise EnrollmentClientError("Core completed enrollment without a certificate")
        _install_or_verify_delegation(
            certificate,
            delegation,
            key_path=key_path,
            worker_name=worker_name,
            chain_id=chain_id,
            audience=audience,
            replace_existing=bool(state.get("replace_existing")),
        )
        if not stored_certificate:
            state = {**state, "certificate": certificate}
            _atomic_private_write(pending, state)
        credential = {
            "version": CREDENTIAL_VERSION,
            "grid_api_url": base,
            "api_key": state["api_key"],
            "enrollment_id": state["enrollment_id"],
            "worker_signer": worker.address.lower(),
            "worker_name": worker_name,
        }
        _write_or_verify_credentials(
            credentials,
            credential,
            replace_existing=bool(state.get("replace_existing")),
        )
        if result["status"] != "activated":
            activated = await _post_json(
                http,
                f"{base}/v1/workers/enrollments/{state['enrollment_id']}/ack",
                {"poll_token": state["poll_token"]},
            )
            if activated.get("status") != "activated":
                raise EnrollmentClientError("Core did not activate the worker credential")
        pending.unlink(missing_ok=True)
        return {
            "status": "connected",
            "worker_signer": worker.address.lower(),
            "payout_wallet": certificate["payload"]["payout_wallet"],
            "credentials_path": str(credentials),
            "delegation_path": str(delegation),
        }
    finally:
        if owns_client:
            await http.aclose()


def load_worker_credentials(path: str | Path) -> Mapping[str, Any]:
    source = Path(path).expanduser()
    _require_private_permissions(source)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentClientError(f"cannot read worker credentials {source}: {exc}") from exc
    required = {
        "version",
        "grid_api_url",
        "api_key",
        "enrollment_id",
        "worker_signer",
        "worker_name",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise EnrollmentClientError("worker credential document is malformed")
    if value["version"] != CREDENTIAL_VERSION:
        raise EnrollmentClientError("worker credential version is unsupported")
    grid_api_base_url(value["grid_api_url"])
    if not isinstance(value["api_key"], str) or not value["api_key"].startswith("grid_"):
        raise EnrollmentClientError("worker API credential is malformed")
    return value


async def _create_enrollment(
    http: httpx.AsyncClient,
    base: str,
    *,
    profile_id: str,
    worker_name: str,
    worker_signer: str,
    valid_days: int,
    replace_existing: bool,
) -> dict[str, Any]:
    api_key = "grid_" + secrets.token_urlsafe(24)
    poll_token = secrets.token_urlsafe(32)
    response = await _post_json(
        http,
        f"{base}/v1/workers/enrollments",
        {
            "worker_signer": worker_signer,
            "worker_name": worker_name,
            "profile_id": profile_id,
            "api_key": api_key,
            "poll_token_hash": hashlib.sha256(poll_token.encode()).hexdigest(),
            "valid_days": valid_days,
        },
    )
    return {
        "version": ENROLLMENT_STATE_VERSION,
        "grid_api_url": base,
        "profile_id": profile_id,
        "worker_name": worker_name,
        "worker_signer": worker_signer,
        "api_key": api_key,
        "poll_token": poll_token,
        "enrollment_id": response["enrollment_id"],
        "authorize_url": response["authorize_url"],
        "expires_at": int(response["expires_at"]),
        "poll_after_seconds": max(1, int(response.get("poll_after_seconds", 2))),
        "replace_existing": replace_existing,
    }


async def _poll_until_complete(
    http: httpx.AsyncClient,
    state: Mapping[str, Any],
    deadline: float,
) -> Mapping[str, Any]:
    url = (
        f"{state['grid_api_url']}/v1/workers/enrollments/"
        f"{state['enrollment_id']}/poll"
    )
    while time.monotonic() < deadline:
        result = await _post_json(http, url, {"poll_token": state["poll_token"]})
        if result.get("status") in {"complete", "activated"}:
            return result
        if result.get("status") not in {"pending", "prepared"}:
            raise EnrollmentClientError("Core returned an unknown enrollment state")
        await asyncio.sleep(state["poll_after_seconds"])
    raise EnrollmentClientError(
        f"worker enrollment timed out; reopen {state['authorize_url']} and rerun connect"
    )


async def _post_json(
    http: httpx.AsyncClient,
    url: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        response = await http.post(url, json=dict(payload))
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EnrollmentClientError(f"worker enrollment request failed: {exc}") from exc
    if not response.is_success:
        detail = body.get("detail") if isinstance(body, Mapping) else None
        raise EnrollmentClientError(
            f"worker enrollment request failed ({response.status_code}): "
            f"{detail or 'Core rejected the request'}"
        )
    if not isinstance(body, Mapping):
        raise EnrollmentClientError("Core returned malformed worker enrollment JSON")
    return body


def _load_pending(path: Path) -> Mapping[str, Any]:
    _require_private_permissions(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentClientError(f"cannot resume worker enrollment: {exc}") from exc
    if not isinstance(value, Mapping) or value.get("version") != ENROLLMENT_STATE_VERSION:
        raise EnrollmentClientError("pending worker enrollment is malformed")
    return value


def _require_pending_matches(state: Mapping[str, Any], **expected) -> None:
    for field, value in expected.items():
        if state.get(field) != value:
            raise EnrollmentClientError(
                f"pending worker enrollment targets another {field.replace('_', ' ')}; "
                "rerun with --restart"
            )


def _install_or_verify_delegation(
    certificate,
    destination: Path,
    *,
    key_path: Path,
    worker_name: str,
    chain_id: int,
    audience: str,
    replace_existing: bool,
) -> None:
    if destination.exists():
        existing = load_delegation_certificate(destination)
        if existing != certificate and not replace_existing:
            raise EnrollmentClientError("existing delegation differs from paired certificate")
        if existing == certificate:
            return
    install_enrollment_certificate(
        certificate,
        destination,
        worker_key_path=key_path,
        worker_name=worker_name,
        chain_id=chain_id,
        audience=audience,
    )


def _write_or_verify_credentials(
    path: Path,
    value: Mapping[str, Any],
    *,
    replace_existing: bool,
) -> None:
    if path.exists():
        existing = load_worker_credentials(path)
        if existing != value and not replace_existing:
            raise EnrollmentClientError("existing worker credentials differ from enrollment")
        if existing == value:
            return
    _atomic_private_write(path, value)


def _existing_connection(
    credentials_path: Path,
    delegation_path: Path,
    worker_signer: str,
    worker_name: str,
    grid_api_url: str,
    chain_id: int,
    audience: str,
) -> Mapping[str, Any] | None:
    if not credentials_path.exists() or not delegation_path.exists():
        return None
    credentials = load_worker_credentials(credentials_path)
    certificate = load_delegation_certificate(delegation_path)
    payload = certificate["payload"]
    if (
        credentials["worker_signer"] != worker_signer
        or credentials["worker_name"] != worker_name
        or credentials["grid_api_url"] != grid_api_url
        or payload["worker_signer"] != worker_signer
        or payload["worker_name"] != worker_name
        or payload["chain_id"] != chain_id
        or payload["audience"] != audience.lower()
    ):
        raise EnrollmentClientError("stored worker connection targets another rig")
    return {
        "status": "connected",
        "worker_signer": worker_signer,
        "payout_wallet": payload["payout_wallet"],
        "credentials_path": str(credentials_path),
        "delegation_path": str(delegation_path),
        "delegation_expires_at": payload["expires_at"],
    }


def _public_connection(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return {key: item for key, item in value.items() if key != "delegation_expires_at"}


def _atomic_private_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _require_private_permissions(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise EnrollmentClientError(f"cannot access private file {path}: {exc}") from exc
    if os.name != "nt" and mode & 0o077:
        raise EnrollmentClientError(f"private file permissions must be 0600, got {mode:04o}")

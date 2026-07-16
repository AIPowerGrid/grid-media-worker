from __future__ import annotations

import hashlib
import json
import os
import time

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct

from bridge.enrollment import (
    EnrollmentClientError,
    _existing_connection,
    connect_worker,
    grid_api_base_url,
)
from bridge.identity import delegation_message, load_delegation_certificate


@pytest.mark.asyncio
@respx.mock
async def test_console_pairing_installs_and_activates_private_credentials(
    tmp_path, monkeypatch
):
    wallet = Account.create()
    captured = {}

    def create_response(request):
        body = json.loads(request.content)
        captured["create"] = body
        return httpx.Response(
            200,
            json={
                "enrollment_id": "enrollment_abcdefghijklmnopqrstuvwxyz",
                "authorize_url": (
                    "https://console.example/dashboard/connect-worker/"
                    "enrollment_abcdefghijklmnopqrstuvwxyz"
                ),
                "expires_at": int(time.time()) + 900,
                "poll_after_seconds": 1,
            },
        )

    def poll_response(request):
        body = json.loads(request.content)
        captured["poll"] = body
        assert hashlib.sha256(body["poll_token"].encode()).hexdigest() == captured[
            "create"
        ]["poll_token_hash"]
        issued = int(time.time())
        payload = {
            "version": 1,
            "chain_id": 8453,
            "audience": "api.example",
            "delegation_id": "ab" * 16,
            "payout_wallet": wallet.address.lower(),
            "worker_signer": captured["create"]["worker_signer"],
            "worker_name": "audio-rig-1",
            "issued_at": issued,
            "expires_at": issued + 90 * 86400,
        }
        signature = Account.sign_message(
            encode_defunct(text=delegation_message(payload)), wallet.key
        ).signature.hex()
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "certificate": {"payload": payload, "signature": signature},
            },
        )

    def ack_response(request):
        captured["ack"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "activated"})

    respx.post("https://api.example/v1/workers/enrollments").mock(
        side_effect=create_response
    )
    respx.post(
        "https://api.example/v1/workers/enrollments/"
        "enrollment_abcdefghijklmnopqrstuvwxyz/poll"
    ).mock(side_effect=poll_response)
    respx.post(
        "https://api.example/v1/workers/enrollments/"
        "enrollment_abcdefghijklmnopqrstuvwxyz/ack"
    ).mock(side_effect=ack_response)
    opened = []
    monkeypatch.setattr(
        "bridge.enrollment.webbrowser.open", lambda url, new=0: opened.append((url, new))
    )

    key = tmp_path / "worker-key.json"
    delegation = tmp_path / "delegation.json"
    credentials = tmp_path / "credentials.json"
    pending = tmp_path / "pending.json"
    result = await connect_worker(
        grid_api_url="https://api.example",
        profile_id="ace-step-v1.5-turbo",
        worker_name="audio-rig-1",
        worker_key_path=key,
        delegation_path=delegation,
        credentials_path=credentials,
        pending_path=pending,
        chain_id=8453,
        audience="api.example",
    )

    assert result["status"] == "connected"
    assert result["payout_wallet"] == wallet.address.lower()
    assert opened[0][0].startswith("https://console.example/")
    assert not pending.exists()
    stored = json.loads(credentials.read_text())
    assert stored["api_key"] == captured["create"]["api_key"]
    assert stored["api_key"].startswith("grid_")
    assert captured["ack"] == captured["poll"]
    assert load_delegation_certificate(delegation)["payload"]["worker_name"] == "audio-rig-1"
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
        assert credentials.stat().st_mode & 0o777 == 0o600
        assert delegation.stat().st_mode & 0o777 == 0o600


def test_remote_enrollment_requires_https():
    with pytest.raises(EnrollmentClientError, match="requires HTTPS"):
        grid_api_base_url("http://api.example")
    assert grid_api_base_url("http://127.0.0.1:7002") == "http://127.0.0.1:7002"


def test_existing_connection_must_match_chain_and_audience(tmp_path):
    worker = Account.create()
    wallet = Account.create()
    issued = int(time.time())
    payload = {
        "version": 1,
        "chain_id": 8453,
        "audience": "api.example",
        "delegation_id": "ab" * 16,
        "payout_wallet": wallet.address.lower(),
        "worker_signer": worker.address.lower(),
        "worker_name": "audio-rig-1",
        "issued_at": issued,
        "expires_at": issued + 86400,
    }
    signature = Account.sign_message(
        encode_defunct(text=delegation_message(payload)), wallet.key
    ).signature.hex()
    delegation = tmp_path / "delegation.json"
    delegation.write_text(json.dumps({"payload": payload, "signature": signature}))
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "version": 1,
                "grid_api_url": "https://api.example",
                "api_key": "grid_" + "a" * 32,
                "enrollment_id": "enrollment_abcdefghijklmnopqrstuvwxyz",
                "worker_signer": worker.address.lower(),
                "worker_name": "audio-rig-1",
            }
        )
    )
    if os.name != "nt":
        delegation.chmod(0o600)
        credentials.chmod(0o600)

    with pytest.raises(EnrollmentClientError, match="another rig"):
        _existing_connection(
            credentials,
            delegation,
            worker.address.lower(),
            "audio-rig-1",
            "https://api.example",
            1,
            "api.example",
        )
    with pytest.raises(EnrollmentClientError, match="another rig"):
        _existing_connection(
            credentials,
            delegation,
            worker.address.lower(),
            "audio-rig-1",
            "https://api.example",
            8453,
            "other.example",
        )


@pytest.mark.asyncio
@respx.mock
async def test_pairing_retries_activation_after_ack_failure(tmp_path, monkeypatch):
    wallet = Account.create()
    captured = {}
    enrollment_id = "enrollment_abcdefghijklmnopqrstuvwxyz"

    def create_response(request):
        captured["create"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "enrollment_id": enrollment_id,
                "authorize_url": f"https://console.example/connect/{enrollment_id}",
                "expires_at": int(time.time()) + 900,
                "poll_after_seconds": 1,
            },
        )

    def poll_response(_request):
        issued = int(time.time())
        payload = {
            "version": 1,
            "chain_id": 8453,
            "audience": "api.example",
            "delegation_id": "cd" * 16,
            "payout_wallet": wallet.address.lower(),
            "worker_signer": captured["create"]["worker_signer"],
            "worker_name": "audio-rig-1",
            "issued_at": issued,
            "expires_at": issued + 90 * 86400,
        }
        signature = Account.sign_message(
            encode_defunct(text=delegation_message(payload)), wallet.key
        ).signature.hex()
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "certificate": {"payload": payload, "signature": signature},
            },
        )

    ack_attempts = 0

    def ack_response(_request):
        nonlocal ack_attempts
        ack_attempts += 1
        if ack_attempts == 1:
            return httpx.Response(503, json={"detail": "temporary failure"})
        return httpx.Response(200, json={"status": "activated"})

    create_route = respx.post("https://api.example/v1/workers/enrollments").mock(
        side_effect=create_response
    )
    poll_route = respx.post(
        f"https://api.example/v1/workers/enrollments/{enrollment_id}/poll"
    ).mock(side_effect=poll_response)
    respx.post(
        f"https://api.example/v1/workers/enrollments/{enrollment_id}/ack"
    ).mock(side_effect=ack_response)
    monkeypatch.setattr("bridge.enrollment.webbrowser.open", lambda *_args, **_kw: None)

    paths = {
        "worker_key_path": tmp_path / "worker-key.json",
        "delegation_path": tmp_path / "delegation.json",
        "credentials_path": tmp_path / "credentials.json",
        "pending_path": tmp_path / "pending.json",
    }
    kwargs = {
        "grid_api_url": "https://api.example",
        "profile_id": "ace-step-v1.5-turbo",
        "worker_name": "audio-rig-1",
        "chain_id": 8453,
        "audience": "api.example",
        "launch_browser": False,
        **paths,
    }
    with pytest.raises(EnrollmentClientError, match="temporary failure"):
        await connect_worker(**kwargs)
    assert paths["pending_path"].exists()
    assert paths["credentials_path"].exists()

    result = await connect_worker(**kwargs)
    assert result["status"] == "connected"
    assert not paths["pending_path"].exists()
    assert create_route.call_count == 1
    assert poll_route.call_count == 1
    assert ack_attempts == 2


@pytest.mark.asyncio
@respx.mock
async def test_restart_rotates_local_worker_credentials(tmp_path, monkeypatch):
    wallet = Account.create()
    enrollment_ids = [
        "enrollment_abcdefghijklmnopqrstuvwxyz1",
        "enrollment_abcdefghijklmnopqrstuvwxyz2",
    ]
    creates = []

    def create_response(request):
        creates.append(json.loads(request.content))
        enrollment_id = enrollment_ids[len(creates) - 1]
        return httpx.Response(
            200,
            json={
                "enrollment_id": enrollment_id,
                "authorize_url": f"https://console.example/connect/{enrollment_id}",
                "expires_at": int(time.time()) + 900,
                "poll_after_seconds": 1,
            },
        )

    def certificate(index):
        issued = int(time.time())
        payload = {
            "version": 1,
            "chain_id": 8453,
            "audience": "api.example",
            "delegation_id": f"{index + 1:02x}" * 16,
            "payout_wallet": wallet.address.lower(),
            "worker_signer": creates[index]["worker_signer"],
            "worker_name": "audio-rig-1",
            "issued_at": issued,
            "expires_at": issued + 90 * 86400,
        }
        signature = Account.sign_message(
            encode_defunct(text=delegation_message(payload)), wallet.key
        ).signature.hex()
        return {"payload": payload, "signature": signature}

    respx.post("https://api.example/v1/workers/enrollments").mock(
        side_effect=create_response
    )
    for index, enrollment_id in enumerate(enrollment_ids):
        respx.post(
            f"https://api.example/v1/workers/enrollments/{enrollment_id}/poll"
        ).mock(
            side_effect=lambda _request, index=index: httpx.Response(
                200,
                json={"status": "complete", "certificate": certificate(index)},
            )
        )
        respx.post(
            f"https://api.example/v1/workers/enrollments/{enrollment_id}/ack"
        ).mock(return_value=httpx.Response(200, json={"status": "activated"}))
    monkeypatch.setattr("bridge.enrollment.webbrowser.open", lambda *_args, **_kw: None)

    paths = {
        "worker_key_path": tmp_path / "worker-key.json",
        "delegation_path": tmp_path / "delegation.json",
        "credentials_path": tmp_path / "credentials.json",
        "pending_path": tmp_path / "pending.json",
    }
    kwargs = {
        "grid_api_url": "https://api.example",
        "profile_id": "ace-step-v1.5-turbo",
        "worker_name": "audio-rig-1",
        "chain_id": 8453,
        "audience": "api.example",
        "launch_browser": False,
        **paths,
    }
    await connect_worker(**kwargs)
    first_credential = json.loads(paths["credentials_path"].read_text())
    first_certificate = json.loads(paths["delegation_path"].read_text())

    await connect_worker(**kwargs, restart=True)
    second_credential = json.loads(paths["credentials_path"].read_text())
    second_certificate = json.loads(paths["delegation_path"].read_text())

    assert len(creates) == 2
    assert first_credential["api_key"] != second_credential["api_key"]
    assert first_certificate["payload"]["delegation_id"] != second_certificate[
        "payload"
    ]["delegation_id"]
    assert not paths["pending_path"].exists()

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from bridge.config import Settings
from bridge.identity import (
    create_delegation_request,
    delegation_message,
    generate_worker_key,
    install_delegation_certificate,
)
from bridge.ws_worker import WSWorker, grid_ws_url, media_result_hash, resolve_output_seeds


def test_grid_ws_url_rejects_plaintext_outside_loopback(monkeypatch):
    monkeypatch.setattr(Settings, "GRID_STREAMING_URL", "")
    monkeypatch.setattr(Settings, "GRID_API_URL", "http://grid.example")
    monkeypatch.setattr(Settings, "GRID_WS_INSECURE", False)

    with pytest.raises(RuntimeError, match="refusing plaintext"):
        grid_ws_url()


def test_grid_ws_url_allows_loopback_or_explicit_insecure_mode(monkeypatch):
    monkeypatch.setattr(Settings, "GRID_STREAMING_URL", "")
    monkeypatch.setattr(Settings, "GRID_WS_INSECURE", False)
    monkeypatch.setattr(Settings, "GRID_API_URL", "http://127.0.0.1:8000")
    assert grid_ws_url() == "ws://127.0.0.1:8000/v1/workers/ws"

    monkeypatch.setattr(Settings, "GRID_API_URL", "http://grid.test")
    monkeypatch.setattr(Settings, "GRID_WS_INSECURE", True)
    assert grid_ws_url() == "ws://grid.test/v1/workers/ws"


def test_resolve_output_seeds_preserves_explicit_seed():
    assert resolve_output_seeds({"seed": 0}, 3) == [0, 1, 2]
    assert resolve_output_seeds({"seed": "42"}, 2) == [42, 43]


def test_resolve_output_seeds_preserves_seed_list():
    assert resolve_output_seeds({"seed": 9, "seeds": [5, "6"]}, 2) == [5, 6]


def test_resolve_output_seeds_rejects_invalid_seed():
    with pytest.raises(ValueError):
        resolve_output_seeds({"seed": -1}, 1)


@pytest.mark.asyncio
async def test_registration_payload_exposes_only_coarse_profile_metadata(tmp_path, monkeypatch):
    key_path = tmp_path / "worker-key.json"
    delegation_path = tmp_path / "delegation.json"
    generate_worker_key(key_path)
    wallet = Account.from_key("0x" + "44" * 32)
    request = create_delegation_request(
        worker_key_path=key_path,
        payout_wallet=wallet.address,
        worker_name=Settings.GRID_WORKER_NAME,
        chain_id=8453,
        audience="api.aipowergrid.io",
    )
    signature = Account.sign_message(
        encode_defunct(text=delegation_message(request["payload"])), wallet.key
    ).signature.hex()
    install_delegation_certificate(request, signature, delegation_path)
    monkeypatch.setattr(Settings, "GRID_WORKER_KEY_PATH", str(key_path))
    monkeypatch.setattr(Settings, "GRID_WORKER_DELEGATION_PATH", str(delegation_path))
    worker = WSWorker()
    worker.models = ["ace-step-v1.5-turbo"]
    worker.job_types = ["audio"]
    worker.profile_metadata = {
        "id": "ace-step-v1.5-turbo",
        "version": "0.1.0",
        "digest": "a" * 64,
        "signing_key_id": "release-key",
        "capability_tier": "audio.ace-step.standard",
        "runtime_adapter": "ace-step-1.5-api",
        "runtime_digest": "c" * 64,
        "recipe_root": "b" * 64,
        "canary_completed_at": "2026-07-15T00:00:00+00:00",
        "canary_elapsed_seconds": 8.5,
    }
    try:
        payload = worker.registration_payload()
    finally:
        await worker.comfy.aclose()

    assert payload["models"] == ["ace-step-v1.5-turbo"]
    assert payload["job_types"] == ["audio"]
    assert payload["worker_profile"]["capability_tier"] == "audio.ace-step.standard"
    assert payload["worker_identity"]["payload"]["profile_digest"] == "a" * 64
    assert payload["worker_identity"]["payload"]["profile_recipe_root"] == "b" * 64
    assert "accelerator" not in str(payload)
    assert "ram" not in str(payload)
    assert "private_key" not in str(payload)


def test_media_result_hash_orders_outputs_by_index():
    ordered = [
        {"index": 0, "sha256": "a" * 64},
        {"index": 1, "sha256": "b" * 64},
    ]
    assert media_result_hash(list(reversed(ordered))) == media_result_hash(ordered)

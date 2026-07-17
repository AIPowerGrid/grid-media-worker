from __future__ import annotations

import json
import os

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from bridge.identity import (
    WorkerIdentityError,
    build_registration_proof,
    create_delegation_request,
    delegation_message,
    generate_worker_key,
    install_delegation_certificate,
    job_message,
    load_worker_key,
    registration_message,
    sign_job_result,
)


def _delegated_identity(tmp_path, *, worker_name="audio-rig"):
    key_path = tmp_path / "worker-key.json"
    delegation_path = tmp_path / "delegation.json"
    generate_worker_key(key_path)
    wallet = Account.from_key("0x" + "11" * 32)
    request = create_delegation_request(
        worker_key_path=key_path,
        payout_wallet=wallet.address,
        worker_name=worker_name,
        chain_id=8453,
        audience="api.aipowergrid.io",
        now=1_800_000_000,
        delegation_id="ab" * 16,
    )
    signature = Account.sign_message(
        encode_defunct(text=delegation_message(request["payload"])), wallet.key
    ).signature.hex()
    install_delegation_certificate(request, signature, delegation_path)
    return key_path, delegation_path, wallet


def test_delegation_and_registration_prove_both_keys(tmp_path):
    key_path, delegation_path, wallet = _delegated_identity(tmp_path)
    proof = build_registration_proof(
        worker_key_path=key_path,
        delegation_path=delegation_path,
        worker_name="audio-rig",
        models=["ace-step-v1.5-xl-turbo"],
        job_types=["audio"],
        bridge_agent="comfy-bridge/ws:1",
        profile_digest="a" * 64,
        profile_recipe_root="b" * 64,
        now=1_800_000_100,
        nonce="cd" * 16,
    )

    recovered_wallet = Account.recover_message(
        encode_defunct(text=delegation_message(proof["delegation"]["payload"])),
        signature=proof["delegation"]["signature"],
    )
    recovered_worker = Account.recover_message(
        encode_defunct(text=registration_message(proof["payload"])),
        signature=proof["signature"],
    )
    assert recovered_wallet.lower() == wallet.address.lower()
    assert recovered_worker.lower() == proof["payload"]["worker_signer"]
    assert "private_key" not in json.dumps(proof)


def test_job_receipt_uses_core_domain(tmp_path):
    key_path = tmp_path / "worker-key.json"
    generated = generate_worker_key(key_path)
    signature = sign_job_result(key_path, "job-1", "f" * 64)
    recovered = Account.recover_message(
        encode_defunct(text=job_message("job-1", "f" * 64)), signature=signature
    )
    assert recovered.lower() == generated["address"]


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_key_file_rejects_group_or_world_access(tmp_path):
    key_path = tmp_path / "worker-key.json"
    generate_worker_key(key_path)
    key_path.chmod(0o644)
    with pytest.raises(WorkerIdentityError, match="0600"):
        load_worker_key(key_path)


def test_delegation_rejects_wrong_wallet_signature(tmp_path):
    key_path = tmp_path / "worker-key.json"
    generate_worker_key(key_path)
    wallet = Account.from_key("0x" + "22" * 32)
    attacker = Account.from_key("0x" + "33" * 32)
    request = create_delegation_request(
        worker_key_path=key_path,
        payout_wallet=wallet.address,
        worker_name="audio-rig",
        chain_id=8453,
        audience="api.aipowergrid.io",
        now=1_800_000_000,
    )
    signature = Account.sign_message(
        encode_defunct(text=delegation_message(request["payload"])), attacker.key
    ).signature.hex()
    with pytest.raises(WorkerIdentityError, match="payout wallet"):
        install_delegation_certificate(request, signature, tmp_path / "delegation.json")


def test_registration_rejects_worker_name_mismatch(tmp_path):
    key_path, delegation_path, _wallet = _delegated_identity(tmp_path)
    with pytest.raises(WorkerIdentityError, match="different worker name"):
        build_registration_proof(
            worker_key_path=key_path,
            delegation_path=delegation_path,
            worker_name="renamed-rig",
            models=["ace-step-v1.5-xl-turbo"],
            job_types=["audio"],
            bridge_agent="comfy-bridge/ws:1",
            profile_digest="a" * 64,
            profile_recipe_root="b" * 64,
            now=1_800_000_100,
        )

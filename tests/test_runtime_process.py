from __future__ import annotations

import json

import pytest

from bridge.profiles.profile import bundled_profile_path
from bridge.runtime_process import RuntimeProcessError, build_runtime_process_spec


def _profile():
    return json.loads(bundled_profile_path().read_text(encoding="utf-8"))["profile"]


def _runtime_tree(tmp_path, profile):
    root = tmp_path / "runtimes" / "ace-step-1.5"
    entrypoint = root / ".venv" / "bin" / "acestep-api"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "checkpoints").mkdir()
    return root


def test_runtime_spec_is_loopback_pinned_and_uses_verified_models(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_API_KEY", "must-not-reach-model-runtime")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-model-runtime")
    profile = _profile()
    root = _runtime_tree(tmp_path, profile)
    spec = build_runtime_process_spec(
        profile,
        tmp_path,
        api_url="http://127.0.0.1:8001",
        api_key="local-secret",
        capability_tier=profile["hardware"]["minimum_tier"],
        runtime_device="GPU-test",
    )
    assert spec.command == (str(root / ".venv" / "bin" / "acestep-api"),)
    assert spec.environment["ACESTEP_API_HOST"] == "127.0.0.1"
    assert spec.environment["ACESTEP_CONFIG_PATH"] == "acestep-v15-turbo"
    assert spec.environment["ACESTEP_CHECKPOINTS_DIR"] == str(root / "checkpoints")
    assert spec.environment["ACESTEP_INIT_LLM"] == "false"
    assert spec.environment["HF_HUB_OFFLINE"] == "1"
    assert spec.environment["TRANSFORMERS_OFFLINE"] == "1"
    assert spec.environment["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert profile["runtime"]["resource_policy"] == "upstream-vram-auto-v1"
    assert "ACESTEP_OFFLOAD_TO_CPU" not in spec.environment
    assert spec.environment["CUDA_VISIBLE_DEVICES"] == "GPU-test"
    assert "GRID_API_KEY" not in spec.environment
    assert "AWS_SECRET_ACCESS_KEY" not in spec.environment
    assert spec.environment.get("PATH")


def test_runtime_spec_rejects_remote_api(tmp_path):
    profile = _profile()
    _runtime_tree(tmp_path, profile)
    with pytest.raises(RuntimeProcessError, match="loopback"):
        build_runtime_process_spec(
            profile,
            tmp_path,
            api_url="https://api.acemusic.ai",
            api_key="secret",
            capability_tier=profile["hardware"]["recommended_tier"],
        )


def test_runtime_spec_rejects_unknown_resource_policy(tmp_path):
    profile = _profile()
    profile["runtime"]["resource_policy"] = "unsafe-override"
    _runtime_tree(tmp_path, profile)

    with pytest.raises(RuntimeProcessError, match="resource policy"):
        build_runtime_process_spec(
            profile,
            tmp_path,
            api_url="http://127.0.0.1:8001",
            api_key="secret",
            capability_tier=profile["hardware"]["minimum_tier"],
        )


def test_runtime_spec_clears_unbound_inherited_gpu_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "attacker-controlled")
    profile = _profile()
    _runtime_tree(tmp_path, profile)
    spec = build_runtime_process_spec(
        profile,
        tmp_path,
        api_url="http://127.0.0.1:8001",
        api_key="secret",
        capability_tier=profile["hardware"]["recommended_tier"],
    )
    assert "CUDA_VISIBLE_DEVICES" not in spec.environment

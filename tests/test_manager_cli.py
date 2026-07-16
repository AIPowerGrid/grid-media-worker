from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bridge import manager_cli
from bridge.manager_cli import main
from bridge.profiles.canary import CanaryResult
from bridge.profiles.hardware import AcceleratorInfo, HardwareSnapshot
from bridge.profiles.profile import ProfileDocument, bundled_profile_path, load_profile


def test_inspect_reports_core_allowlist_digest(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["grid-media-manager", "--allow-unsigned-draft", "inspect"],
    )

    main()
    inspected = json.loads(capsys.readouterr().out)

    assert len(inspected["profile_digest"]) == 64
    assert inspected["profile_digest"] != inspected["runtime_digest"]


def test_identity_generate_and_show_through_cli(tmp_path, monkeypatch, capsys):
    key_path = tmp_path / "worker-key.json"
    delegation_path = tmp_path / "delegation.json"

    monkeypatch.setattr(
        "sys.argv",
        [
            "grid-media-manager",
            "identity",
            "--key",
            str(key_path),
            "--delegation",
            str(delegation_path),
            "generate",
        ],
    )
    main()
    generated = json.loads(capsys.readouterr().out)

    assert generated["address"].startswith("0x")
    if os.name != "nt":
        assert key_path.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(
        "sys.argv",
        [
            "grid-media-manager",
            "identity",
            "--key",
            str(key_path),
            "--delegation",
            str(delegation_path),
            "show",
        ],
    )
    main()
    shown = json.loads(capsys.readouterr().out)

    assert shown == {"worker_signer": generated["address"], "delegated": False}


def test_default_worker_name_is_stable_and_funds_less(tmp_path):
    key_path = tmp_path / "worker-key.json"

    first = manager_cli._resolved_worker_name(None, key_path)
    second = manager_cli._resolved_worker_name(None, key_path)

    assert first == second
    assert first.startswith("ace-step-")
    assert len(first) == len("ace-step-") + 12


def test_console_install_progress_is_throttled_and_uses_stderr_format():
    stream = io.StringIO()
    progress = manager_cli._ConsoleInstallProgress(stream)

    progress("model", 0, 1024 * 1024)
    progress("model", 1024, 1024 * 1024)
    progress("model", 64 * 1024, 1024 * 1024)
    progress("model", 1024 * 1024, 1024 * 1024)

    lines = stream.getvalue().splitlines()
    assert lines == [
        "[install] model: 0 B / 1.0 MiB (0%)",
        "[install] model: 64.0 KiB / 1.0 MiB (6%)",
        "[install] model: 1.0 MiB / 1.0 MiB (100%)",
    ]


@pytest.mark.asyncio
async def test_setup_orchestrates_install_canary_pair_and_clean_exit(
    tmp_path, monkeypatch, capsys,
):
    draft = load_profile(bundled_profile_path(), allow_unsigned_draft=True)
    profile = json.loads(json.dumps(draft.profile))
    profile["status"] = "active"
    document = ProfileDocument(profile, "test-release", True, draft.source)
    accelerator = AcceleratorInfo(
        "nvidia", "RTX", 24576, "580.1", "12.8", 0, "GPU-test",
    )
    snapshot = HardwareSnapshot(
        "linux", "x86_64", 65536, 131072, (accelerator,),
    )
    state = {
        "capability_tier": profile["hardware"]["recommended_tier"],
        "runtime_device": "GPU-test",
        "profile_digest": "a" * 64,
        "canary": None,
    }
    installed = AsyncMock(return_value=state)
    started = AsyncMock(return_value=SimpleNamespace(returncode=None))
    waited = AsyncMock()
    stopped = AsyncMock()
    connected = AsyncMock(return_value={"status": "connected"})
    result = CanaryResult(8.0, 100, "b" * 64, 10.0, 48000, 2)

    monkeypatch.setattr(manager_cli, "load_profile", lambda *_a, **_k: document)
    monkeypatch.setattr(manager_cli, "detect_hardware", lambda *_a, **_k: snapshot)
    monkeypatch.setattr(manager_cli, "_install_profile", installed)
    monkeypatch.setattr(manager_cli, "_verify_profile_install", lambda *_a, **_k: None)
    monkeypatch.setattr(manager_cli, "build_runtime_process_spec", lambda *_a, **_k: object())
    monkeypatch.setattr(manager_cli, "start_runtime", started)
    monkeypatch.setattr(manager_cli, "wait_runtime_ready", waited)
    monkeypatch.setattr(manager_cli, "stop_runtime", stopped)
    monkeypatch.setattr(
        manager_cli,
        "run_profile_benchmark",
        AsyncMock(
            return_value=(
                {"canary_results": [result.as_state()]},
                {"metrics": {"elapsed_seconds": {"median": 8.0}}},
            )
        ),
    )
    monkeypatch.setattr(manager_cli, "write_benchmark_report", lambda *_a, **_k: None)
    monkeypatch.setattr(
        manager_cli,
        "record_canary_pass",
        lambda *_a, **_k: {**state, "canary": {"passed": True}},
    )
    monkeypatch.setattr(manager_cli, "authoritative_capabilities", lambda *_a, **_k: ())
    monkeypatch.setattr(manager_cli, "connect_worker", connected)

    args = manager_cli._parser().parse_args(
        [
            "setup",
            "--install-root", str(tmp_path / "install"),
            "--state", str(tmp_path / "state.json"),
            "--worker-name", "audio-test-rig",
            "--key", str(tmp_path / "worker-key.json"),
            "--delegation", str(tmp_path / "delegation.json"),
            "--credentials", str(tmp_path / "credentials.json"),
            "--pending", str(tmp_path / "pending.json"),
            "--benchmark-out", str(tmp_path / "benchmark.json"),
            "--exit-after-setup",
            "--no-browser",
        ]
    )
    await manager_cli._run(args)

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready"
    assert output["worker_name"] == "audio-test-rig"
    installed.assert_awaited_once()
    started.assert_awaited_once()
    waited.assert_awaited_once()
    connected.assert_awaited_once()
    assert connected.await_args.kwargs["worker_name"] == "audio-test-rig"
    stopped.assert_awaited_once()

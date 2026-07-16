# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Supervise the pinned local runtime without invoking a shell."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .audio_runtime import AudioRuntimeError, check_ace_step_runtime, local_runtime_base_url


class RuntimeProcessError(RuntimeError):
    """The managed runtime could not be launched or did not become ready."""


RUNTIME_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "CUDA_HOME",
        "CUDA_PATH",
        "CUDA_VISIBLE_DEVICES",
        "DYLD_LIBRARY_PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "LOCALAPPDATA",
        "NVIDIA_DRIVER_CAPABILITIES",
        "NVIDIA_VISIBLE_DEVICES",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SystemRoot",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TORCH_HOME",
        "TRANSFORMERS_CACHE",
        "USERPROFILE",
        "WINDIR",
    }
)


@dataclass(frozen=True)
class RuntimeProcessSpec:
    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]


def build_runtime_process_spec(
    profile: Mapping[str, Any],
    install_root: str | Path,
    *,
    api_url: str,
    api_key: str,
    capability_tier: str,
    runtime_device: str | None = None,
) -> RuntimeProcessSpec:
    try:
        base = local_runtime_base_url(api_url)
    except AudioRuntimeError as exc:
        raise RuntimeProcessError(str(exc)) from exc
    parsed = urlparse(base)
    runtime = profile["runtime"]
    if runtime.get("resource_policy") != "upstream-vram-auto-v1":
        raise RuntimeProcessError("ACE-Step runtime resource policy is unsupported")
    source = next(
        item for item in profile["artifacts"] if item["id"] == runtime["source_artifact"]
    )
    root = _contained(Path(install_root).expanduser().resolve(), source["destination"])
    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    entrypoint = root / ".venv" / scripts / f"acestep-api{suffix}"
    if not entrypoint.is_file():
        raise RuntimeProcessError(f"ACE-Step runtime entrypoint is missing: {entrypoint}")
    checkpoints = root / "checkpoints"
    if not checkpoints.is_dir():
        raise RuntimeProcessError(f"ACE-Step checkpoints are missing: {checkpoints}")
    # The model runtime is third-party code. Do not give it Grid keys, cloud
    # credentials, payout configuration, proxy credentials, or unrelated app
    # secrets merely because the manager inherited them.
    environment = {
        key: value for key, value in os.environ.items() if key in RUNTIME_ENV_ALLOWLIST
    }
    environment.update(
        {
            "ACESTEP_API_HOST": parsed.hostname or "127.0.0.1",
            "ACESTEP_API_PORT": str(parsed.port or 8001),
            "ACESTEP_API_KEY": api_key,
            "ACESTEP_API_WORKERS": "1",
            "ACESTEP_CHECKPOINTS_DIR": str(checkpoints),
            "ACESTEP_CONFIG_PATH": runtime["model"],
            "ACESTEP_INIT_LLM": "false",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    if runtime_device:
        environment["CUDA_VISIBLE_DEVICES"] = runtime_device
    else:
        environment.pop("CUDA_VISIBLE_DEVICES", None)
    return RuntimeProcessSpec((str(entrypoint),), root, environment)


async def start_runtime(spec: RuntimeProcessSpec):
    return await asyncio.create_subprocess_exec(
        *spec.command,
        cwd=spec.cwd,
        env=dict(spec.environment),
    )


async def wait_runtime_ready(
    process,
    *,
    api_url: str,
    runtime_model: str,
    api_key: str,
    timeout_seconds: float = 600.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error = "not started"
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise RuntimeProcessError(
                f"ACE-Step runtime exited before readiness (code {process.returncode})"
            )
        try:
            await check_ace_step_runtime(api_url, runtime_model, api_key=api_key)
            return
        except (AudioRuntimeError, OSError) as exc:
            last_error = str(exc)
            await asyncio.sleep(2)
    raise RuntimeProcessError(f"ACE-Step runtime readiness timed out: {last_error}")


async def stop_runtime(process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=15)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _contained(root: Path, relative: str) -> Path:
    destination = (root / relative).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise RuntimeProcessError("runtime source escaped the install root") from exc
    return destination

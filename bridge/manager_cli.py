# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command-line manager for signed media-worker profiles."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import TextIO

from .config import Settings
from .enrollment import connect_worker, load_worker_credentials
from .identity import (
    create_delegation_request,
    generate_worker_key,
    import_worker_key,
    install_delegation_certificate,
    load_delegation_certificate,
    load_worker_key,
    write_delegation_request,
)
from .profiles.canary import run_ace_step_canary
from .profiles.benchmark import run_profile_benchmark, write_benchmark_report
from .profiles.hardware import detect_hardware, evaluate_hardware
from .profiles.installer import ProfileInstaller
from .profiles.profile import bundled_profile_path, load_profile
from .profiles.state import (
    ProfileStateError,
    authoritative_capabilities,
    load_state,
    profile_digest,
    record_canary_pass,
    validated_install_state,
    write_install_state,
)
from .runtime_process import (
    build_runtime_process_spec,
    start_runtime,
    stop_runtime,
    wait_runtime_ready,
)
from .comfyui_detect import detect_comfyui

DEFAULT_ROOT = Path.home() / ".aipg" / "media-worker"
DEFAULT_CREDENTIALS = DEFAULT_ROOT / "worker-credentials.json"
DEFAULT_ENROLLMENT = DEFAULT_ROOT / "worker-enrollment.json"


class _ConsoleInstallProgress:
    """Render bounded human progress without contaminating JSON stdout."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr
        self._last_update: dict[str, tuple[int, int]] = {}

    def __call__(self, label: str, completed: int, total: int) -> None:
        percent = min(100, int(completed * 100 / total)) if total else 0
        previous = self._last_update.get(label)
        if previous is not None:
            previous_completed, previous_percent = previous
            if completed == total and previous_completed == total:
                return
            if (
                completed >= previous_completed
                and completed != total
                and percent < previous_percent + 5
            ):
                return
        self._last_update[label] = (completed, percent)
        print(
            f"[install] {label}: {_format_bytes(completed)} / "
            f"{_format_bytes(total)} ({percent}%)",
            file=self._stream,
            flush=True,
        )


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def main() -> None:
    parser = _parser()
    args = parser.parse_args(["ui"] if len(sys.argv) == 1 else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return
    try:
        if args.command == "ui":
            from .web.manager import run_manager_ui

            run_manager_ui(args)
            return
        asyncio.run(_run(args))
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


async def _run(args: argparse.Namespace) -> None:
    if args.command == "identity":
        _run_identity(args)
        return
    document = load_profile(
        args.profile,
        allow_unsigned_draft=args.allow_unsigned_draft,
    )
    if args.command == "inspect":
        print(
            json.dumps(
                {
                    "id": document.profile["id"],
                    "version": document.profile["version"],
                    "status": document.profile["status"],
                    "signature_verified": document.signature_verified,
                    "signing_key_id": document.key_id,
                    "profile_digest": profile_digest(document.profile),
                    "model_revision": document.profile["model"]["revision"],
                    "runtime_digest": document.profile["runtime"]["digest"],
                    "recipe_sha256": document.profile["recipe"]["sha256"],
                    "recipe_onchain_root": document.profile["recipe"]["onchain_root"],
                    "qualification_policy_version": document.profile[
                        "release_qualification"
                    ]["policy_version"],
                    "qualification_scope": document.profile["release_qualification"][
                        "scope"
                    ],
                    "qualification_required_classes": document.profile[
                        "release_qualification"
                    ]["required_classes"],
                    "qualification_runs_per_class": document.profile[
                        "release_qualification"
                    ]["runs_per_class"],
                    "qualification_manifest_sha256": (
                        document.profile["release_qualification"]["evidence"] or {}
                    ).get("manifest_sha256"),
                    "artifact_count": len(document.profile["artifacts"]),
                    "runtime_tools": ProfileInstaller.runtime_tool_status(document.profile),
                },
                indent=2,
            )
        )
        return
    if args.command == "connect":
        worker_name = _resolved_worker_name(args.worker_name, args.key)
        result = await connect_worker(
            grid_api_url=args.grid_url,
            profile_id=document.profile["id"],
            worker_name=worker_name,
            worker_key_path=args.key,
            delegation_path=args.delegation,
            credentials_path=args.credentials,
            pending_path=args.pending,
            chain_id=args.chain_id,
            audience=args.audience,
            valid_days=args.valid_days,
            launch_browser=not args.no_browser,
            timeout_seconds=args.timeout,
            restart=args.restart,
        )
        print(json.dumps(result, indent=2))
        return
    if args.command == "setup" and (
        document.profile["status"] != "active" or not document.signature_verified
    ):
        raise RuntimeError(
            "one-command setup requires an active signed release; use the lower-level "
            "development commands for an unsigned draft"
        )

    state_path = getattr(args, "state", None) or Path(
        getattr(args, "install_root", DEFAULT_ROOT)
    ) / "profile-state.json"
    runtime_state = None
    accelerator_selector = getattr(args, "gpu", None)
    if args.command in {"canary", "benchmark", "serve"}:
        runtime_state = load_state(state_path)
        accelerator_selector = runtime_state.get("runtime_device")
    elif args.command == "setup" and accelerator_selector is None:
        try:
            accelerator_selector = load_state(state_path).get("runtime_device")
        except ProfileStateError:
            pass

    snapshot = detect_hardware(getattr(args, "install_root", DEFAULT_ROOT))
    recommendation = evaluate_hardware(
        snapshot,
        document.profile,
        accelerator_selector=accelerator_selector,
    )
    if args.command == "recommend":
        print(
            json.dumps(
                {
                    "status": recommendation.status,
                    "capability_tier": recommendation.capability_tier,
                    "selected_accelerator": (
                        {
                            "index": recommendation.selected_accelerator.device_index,
                            "uuid": recommendation.selected_accelerator.device_uuid,
                            "name": recommendation.selected_accelerator.name,
                        }
                        if recommendation.selected_accelerator
                        else None
                    ),
                    "reasons": recommendation.reasons,
                    "warnings": recommendation.warnings,
                },
                indent=2,
            )
        )
        return
    if recommendation.status == "unsupported":
        raise RuntimeError("host is unsupported: " + "; ".join(recommendation.reasons))

    if args.command == "install":
        state = await _install_profile(args, document, recommendation, state_path)
        print(json.dumps(state, indent=2))
        return
    if args.command == "setup":
        worker_name = _resolved_worker_name(args.worker_name, args.key)
        expected_device = recommendation.selected_accelerator.runtime_selector()
        try:
            state = validated_install_state(state_path, document)
        except ProfileStateError:
            state = await _install_profile(args, document, recommendation, state_path)
        if state.get("runtime_device") != expected_device:
            state = await _install_profile(args, document, recommendation, state_path)
        _verify_profile_install(args, document)

        process = None
        api_key = Settings.ACE_STEP_API_KEY or secrets.token_urlsafe(32)
        try:
            spec = build_runtime_process_spec(
                document.profile,
                args.install_root,
                api_url=args.ace_url,
                api_key=api_key,
                capability_tier=state["capability_tier"],
                runtime_device=state["runtime_device"],
            )
            process = await start_runtime(spec)
            await wait_runtime_ready(
                process,
                api_url=args.ace_url,
                runtime_model=document.profile["runtime"]["model"],
                api_key=api_key,
            )
            if not (state.get("canary") or {}).get("passed"):
                async def run_once():
                    return await run_ace_step_canary(
                        args.ace_url,
                        document.profile,
                        api_key=api_key,
                    )

                private, public = await run_profile_benchmark(
                    document.profile,
                    snapshot,
                    recommendation,
                    run_once,
                    runs=args.benchmark_runs,
                )
                _verify_profile_install(args, document)
                write_benchmark_report(args.benchmark_out, private)
                if args.public_benchmark_out:
                    write_benchmark_report(args.public_benchmark_out, public)
                state = record_canary_pass(
                    state_path,
                    document,
                    private["canary_results"][-1],
                )

            authoritative_capabilities(state_path, document)
            pairing = await connect_worker(
                grid_api_url=args.grid_url,
                profile_id=document.profile["id"],
                worker_name=worker_name,
                worker_key_path=args.key,
                delegation_path=args.delegation,
                credentials_path=args.credentials,
                pending_path=args.pending,
                chain_id=args.chain_id,
                audience=args.audience,
                valid_days=args.valid_days,
                launch_browser=not args.no_browser,
                timeout_seconds=args.timeout,
                restart=args.restart,
            )
            summary = {
                "status": "ready" if args.exit_after_setup else "serving",
                "worker_name": worker_name,
                "capability_tier": state["capability_tier"],
                "profile_digest": state["profile_digest"],
                "pairing": pairing,
                "benchmark_report": (
                    str(args.benchmark_out) if args.benchmark_out.exists() else None
                ),
            }
            print(json.dumps(summary, indent=2), flush=True)
            if args.exit_after_setup:
                return

            credentials = load_worker_credentials(args.credentials)
            Settings.GRID_API_KEY = credentials["api_key"]
            Settings.GRID_API_URL = credentials["grid_api_url"]
            Settings.GRID_WORKER_NAME = worker_name
            Settings.GRID_WORKER_KEY_PATH = str(args.key)
            Settings.GRID_WORKER_DELEGATION_PATH = str(args.delegation)
            Settings.GRID_PROFILE_PATH = str(args.profile)
            Settings.GRID_PROFILE_STATE_PATH = str(state_path)
            Settings.ACE_STEP_API_URL = args.ace_url
            Settings.ACE_STEP_API_KEY = api_key
            Settings.validate()
            from .ws_worker import run_ws_worker

            await run_ws_worker()
        finally:
            if process is not None:
                await stop_runtime(process)
        return
    if args.command == "canary":
        _verify_profile_install(args, document)
        process = None
        api_key = Settings.ACE_STEP_API_KEY or secrets.token_urlsafe(32)
        try:
            if args.launch_runtime:
                state = runtime_state or load_state(state_path)
                spec = build_runtime_process_spec(
                    document.profile,
                    args.install_root,
                    api_url=args.ace_url,
                    api_key=api_key,
                    capability_tier=state["capability_tier"],
                    runtime_device=state.get("runtime_device"),
                )
                process = await start_runtime(spec)
                await wait_runtime_ready(
                    process,
                    api_url=args.ace_url,
                    runtime_model=document.profile["runtime"]["model"],
                    api_key=api_key,
                )
            result = await run_ace_step_canary(
                args.ace_url,
                document.profile,
                api_key=api_key,
            )
            _verify_profile_install(args, document)
            state = record_canary_pass(state_path, document, result.as_state())
            print(json.dumps(state, indent=2))
        finally:
            if process is not None:
                await stop_runtime(process)
        return
    if args.command == "benchmark":
        _verify_profile_install(args, document)
        state = runtime_state or load_state(state_path)
        process = None
        api_key = Settings.ACE_STEP_API_KEY or secrets.token_urlsafe(32)
        try:
            spec = build_runtime_process_spec(
                document.profile,
                args.install_root,
                api_url=args.ace_url,
                api_key=api_key,
                capability_tier=state["capability_tier"],
                runtime_device=state.get("runtime_device"),
            )
            process = await start_runtime(spec)
            await wait_runtime_ready(
                process,
                api_url=args.ace_url,
                runtime_model=document.profile["runtime"]["model"],
                api_key=api_key,
            )

            async def run_once():
                return await run_ace_step_canary(
                    args.ace_url,
                    document.profile,
                    api_key=api_key,
                )

            private, public = await run_profile_benchmark(
                document.profile,
                snapshot,
                recommendation,
                run_once,
                runs=args.runs,
            )
            _verify_profile_install(args, document)
            write_benchmark_report(args.out, private)
            if args.public_out:
                write_benchmark_report(args.public_out, public)
            record_canary_pass(
                state_path,
                document,
                private["canary_results"][-1],
            )
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "private_report": str(args.out),
                        "public_report": str(args.public_out) if args.public_out else None,
                        **public,
                    },
                    indent=2,
                )
            )
        finally:
            if process is not None:
                await stop_runtime(process)
        return
    if args.command == "serve":
        _verify_profile_install(args, document)
        state = runtime_state or load_state(state_path)
        authoritative_capabilities(state_path, document)
        if not Settings.GRID_API_KEY:
            credentials = load_worker_credentials(args.credentials)
            Settings.GRID_API_KEY = credentials["api_key"]
            Settings.GRID_API_URL = credentials["grid_api_url"]
            Settings.GRID_WORKER_NAME = credentials["worker_name"]
        Settings.GRID_WORKER_KEY_PATH = str(args.key)
        Settings.GRID_WORKER_DELEGATION_PATH = str(args.delegation)
        api_key = Settings.ACE_STEP_API_KEY or secrets.token_urlsafe(32)
        spec = build_runtime_process_spec(
            document.profile,
            args.install_root,
            api_url=args.ace_url,
            api_key=api_key,
            capability_tier=state["capability_tier"],
            runtime_device=state.get("runtime_device"),
        )
        process = await start_runtime(spec)
        try:
            await wait_runtime_ready(
                process,
                api_url=args.ace_url,
                runtime_model=document.profile["runtime"]["model"],
                api_key=api_key,
            )
            _verify_profile_install(args, document)
            Settings.GRID_PROFILE_PATH = str(args.profile)
            Settings.GRID_PROFILE_STATE_PATH = str(state_path)
            Settings.ACE_STEP_API_URL = args.ace_url
            Settings.ACE_STEP_API_KEY = api_key
            Settings.validate()
            from .ws_worker import run_ws_worker

            await run_ws_worker()
        finally:
            await stop_runtime(process)
        return


def _run_identity(args: argparse.Namespace) -> None:
    if not args.identity_command:
        raise RuntimeError("choose an identity command")
    if args.identity_command == "generate":
        print(json.dumps(generate_worker_key(args.key, force=args.force), indent=2))
        return
    if args.identity_command == "import":
        source = Path(args.source).expanduser()
        raw = source.read_text(encoding="utf-8").strip()
        try:
            value = json.loads(raw)
            private_key = value["private_key"]
        except (json.JSONDecodeError, KeyError, TypeError):
            private_key = raw
        print(json.dumps(import_worker_key(args.key, private_key, force=args.force), indent=2))
        return
    if args.identity_command == "request":
        request = create_delegation_request(
            worker_key_path=args.key,
            payout_wallet=args.payout_wallet,
            worker_name=args.worker_name,
            chain_id=args.chain_id,
            audience=args.audience,
            valid_days=args.valid_days,
        )
        write_delegation_request(args.out, request)
        print(json.dumps({**request, "request_path": str(args.out)}, indent=2))
        return
    if args.identity_command == "install":
        request = json.loads(Path(args.request).expanduser().read_text(encoding="utf-8"))
        signature = args.signature
        if args.signature_file:
            signature = Path(args.signature_file).expanduser().read_text(encoding="utf-8").strip()
        if not signature:
            raise RuntimeError("provide --signature or --signature-file")
        certificate = install_delegation_certificate(request, signature, args.delegation)
        print(
            json.dumps(
                {
                    "delegation_path": str(args.delegation),
                    "payout_wallet": certificate["payload"]["payout_wallet"],
                    "worker_signer": certificate["payload"]["worker_signer"],
                    "expires_at": certificate["payload"]["expires_at"],
                },
                indent=2,
            )
        )
        return
    if args.identity_command == "show":
        account = load_worker_key(args.key)
        value = {"worker_signer": account.address.lower(), "delegated": False}
        if Path(args.delegation).expanduser().exists():
            certificate = load_delegation_certificate(args.delegation)
            value.update(
                {
                    "delegated": True,
                    "payout_wallet": certificate["payload"]["payout_wallet"],
                    "worker_name": certificate["payload"]["worker_name"],
                    "expires_at": certificate["payload"]["expires_at"],
                }
            )
        print(json.dumps(value, indent=2))
        return
    raise RuntimeError(f"unknown identity command: {args.identity_command}")


async def _install_profile(args, document, recommendation, state_path):
    comfyui_root = args.comfyui_root
    needs_comfyui = any(
        str(item["destination"]).startswith("comfyui/")
        for item in document.profile["artifacts"]
    )
    if needs_comfyui and comfyui_root is None:
        detected = detect_comfyui()
        comfyui_root = Path(detected.base_path) if detected.base_path else None
    if needs_comfyui and comfyui_root is None:
        raise RuntimeError(
            "ComfyUI was not found; pass --comfyui-root after installing it"
        )
    installer = ProfileInstaller(
        args.install_root,
        comfyui_root=comfyui_root,
        progress=_ConsoleInstallProgress(),
    )
    installer.require_runtime_tools(document.profile)
    artifacts = await installer.install(document.profile)
    await installer.setup_runtime(document.profile)
    installer.verify_installed(document.profile)
    return write_install_state(state_path, document, recommendation, artifacts)


def _verify_profile_install(args, document) -> None:
    ProfileInstaller(getattr(args, "install_root", DEFAULT_ROOT)).verify_installed(
        document.profile
    )


def _resolved_worker_name(requested: str | None, key_path: str | Path) -> str:
    if requested:
        value = requested.strip()
        if value != requested or not value or len(value) > 120:
            raise RuntimeError("worker name must be trimmed and at most 120 characters")
        return value
    path = Path(key_path).expanduser()
    if not path.exists():
        generate_worker_key(path)
    address = load_worker_key(path).address.lower()
    return f"ace-step-{address[2:14]}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grid-media-manager")
    parser.add_argument(
        "--profile",
        type=Path,
        default=bundled_profile_path(),
        help="signed Worker Profile V1 envelope",
    )
    parser.add_argument(
        "--allow-unsigned-draft",
        action="store_true",
        help="development only; unsigned drafts cannot advertise capabilities",
    )
    subparsers = parser.add_subparsers(dest="command")
    ui = subparsers.add_parser("ui")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8791)
    ui.add_argument("--install-root", type=Path, default=DEFAULT_ROOT)
    ui.add_argument("--state", type=Path)
    ui.add_argument("--grid-url", default=Settings.GRID_API_URL)
    ui.add_argument("--key", type=Path, default=Path(Settings.GRID_WORKER_KEY_PATH))
    ui.add_argument(
        "--delegation", type=Path, default=Path(Settings.GRID_WORKER_DELEGATION_PATH)
    )
    ui.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    ui.add_argument("--pending", type=Path, default=DEFAULT_ENROLLMENT)
    ui.add_argument("--no-browser", action="store_true")
    subparsers.add_parser("inspect")
    recommend = subparsers.add_parser("recommend")
    recommend.add_argument(
        "--gpu",
        help="NVIDIA GPU index, UUID, or unique exact name (defaults to most VRAM)",
    )

    connect = subparsers.add_parser("connect")
    connect.add_argument("--grid-url", default=Settings.GRID_API_URL)
    connect.add_argument("--worker-name")
    connect.add_argument("--key", type=Path, default=Path(Settings.GRID_WORKER_KEY_PATH))
    connect.add_argument(
        "--delegation", type=Path, default=Path(Settings.GRID_WORKER_DELEGATION_PATH)
    )
    connect.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    connect.add_argument("--pending", type=Path, default=DEFAULT_ENROLLMENT)
    connect.add_argument("--chain-id", type=int, default=Settings.GRID_WORKER_IDENTITY_CHAIN_ID)
    connect.add_argument("--audience", default=Settings.GRID_WORKER_IDENTITY_AUDIENCE)
    connect.add_argument("--valid-days", type=int, default=90)
    connect.add_argument("--timeout", type=int, default=900)
    connect.add_argument("--no-browser", action="store_true")
    connect.add_argument("--restart", action="store_true")

    identity = subparsers.add_parser("identity")
    identity.add_argument("--key", type=Path, default=Path(Settings.GRID_WORKER_KEY_PATH))
    identity.add_argument(
        "--delegation", type=Path, default=Path(Settings.GRID_WORKER_DELEGATION_PATH)
    )
    identity_commands = identity.add_subparsers(dest="identity_command")
    generate = identity_commands.add_parser("generate")
    generate.add_argument("--force", action="store_true")
    key_import = identity_commands.add_parser("import")
    key_import.add_argument("--source", type=Path, required=True)
    key_import.add_argument("--force", action="store_true")
    request = identity_commands.add_parser("request")
    request.add_argument("--payout-wallet", required=True)
    request.add_argument("--worker-name", default=Settings.GRID_WORKER_NAME)
    request.add_argument("--chain-id", type=int, default=Settings.GRID_WORKER_IDENTITY_CHAIN_ID)
    request.add_argument("--audience", default=Settings.GRID_WORKER_IDENTITY_AUDIENCE)
    request.add_argument("--valid-days", type=int, default=90)
    request.add_argument("--out", type=Path, default=DEFAULT_ROOT / "delegation-request.json")
    install_identity = identity_commands.add_parser("install")
    install_identity.add_argument("--request", type=Path, required=True)
    install_identity.add_argument("--signature")
    install_identity.add_argument("--signature-file", type=Path)
    identity_commands.add_parser("show")

    install = subparsers.add_parser("install")
    install.add_argument("--install-root", type=Path, default=DEFAULT_ROOT)
    install.add_argument("--comfyui-root", type=Path)
    install.add_argument("--state", type=Path)
    install.add_argument(
        "--gpu",
        help="NVIDIA GPU index, UUID, or unique exact name (defaults to most VRAM)",
    )

    canary = subparsers.add_parser("canary")
    canary.add_argument("--install-root", type=Path, default=DEFAULT_ROOT)
    canary.add_argument("--state", type=Path)
    canary.add_argument("--ace-url", default="http://127.0.0.1:8001")
    canary.add_argument("--launch-runtime", action="store_true")

    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--install-root", type=Path, default=DEFAULT_ROOT)
    benchmark.add_argument("--state", type=Path)
    benchmark.add_argument("--ace-url", default="http://127.0.0.1:8001")
    benchmark.add_argument("--runs", type=int, default=3)
    benchmark.add_argument("--out", type=Path, default=DEFAULT_ROOT / "benchmark-local.json")
    benchmark.add_argument("--public-out", type=Path)

    setup = subparsers.add_parser("setup")
    setup.add_argument("--install-root", type=Path, default=DEFAULT_ROOT)
    setup.add_argument("--state", type=Path)
    setup.add_argument("--comfyui-root", type=Path)
    setup.add_argument("--gpu", help="NVIDIA GPU index, UUID, or unique exact name")
    setup.add_argument("--ace-url", default="http://127.0.0.1:8001")
    setup.add_argument("--grid-url", default=Settings.GRID_API_URL)
    setup.add_argument("--worker-name")
    setup.add_argument("--key", type=Path, default=Path(Settings.GRID_WORKER_KEY_PATH))
    setup.add_argument(
        "--delegation", type=Path, default=Path(Settings.GRID_WORKER_DELEGATION_PATH)
    )
    setup.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    setup.add_argument("--pending", type=Path, default=DEFAULT_ENROLLMENT)
    setup.add_argument("--chain-id", type=int, default=Settings.GRID_WORKER_IDENTITY_CHAIN_ID)
    setup.add_argument("--audience", default=Settings.GRID_WORKER_IDENTITY_AUDIENCE)
    setup.add_argument("--valid-days", type=int, default=90)
    setup.add_argument("--timeout", type=int, default=900)
    setup.add_argument("--benchmark-runs", type=int, default=1)
    setup.add_argument("--benchmark-out", type=Path, default=DEFAULT_ROOT / "benchmark-local.json")
    setup.add_argument("--public-benchmark-out", type=Path)
    setup.add_argument("--no-browser", action="store_true")
    setup.add_argument("--restart", action="store_true")
    setup.add_argument(
        "--exit-after-setup",
        action="store_true",
        help="install, validate, and pair without entering the worker loop",
    )

    serve = subparsers.add_parser("serve")
    serve.add_argument("--install-root", type=Path, default=DEFAULT_ROOT)
    serve.add_argument("--state", type=Path)
    serve.add_argument("--ace-url", default="http://127.0.0.1:8001")
    serve.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    serve.add_argument("--key", type=Path, default=Path(Settings.GRID_WORKER_KEY_PATH))
    serve.add_argument(
        "--delegation", type=Path, default=Path(Settings.GRID_WORKER_DELEGATION_PATH)
    )
    return parser


if __name__ == "__main__":
    main()

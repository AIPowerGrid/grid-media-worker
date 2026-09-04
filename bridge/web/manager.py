# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Loopback-only control surface for the standalone media manager."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..capacity import effective_concurrency, load_schedule, validate_schedule
from ..enrollment import (
    EnrollmentClientError,
    grid_api_base_url,
    load_worker_credentials,
)
from ..identity import (
    load_delegation_certificate,
    load_worker_key,
)
from ..profiles.hardware import detect_hardware, evaluate_hardware
from ..profiles.profile import load_profile
from ..profiles.state import (
    ProfileStateError,
    profile_digest,
    validated_install_state,
)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SESSION_COOKIE = "aipg_manager_session"
MAX_LOG_LINES = 240
MAX_LOG_LINE = 1000
CAPACITY_FILE_NAME = "capacity.json"
CAPACITY_MODES = frozenset({"always", "paused", "maintenance"})
MAINTENANCE_DAYS = frozenset({"daily", "mon-fri", "sat-sun"})
_SECRET_PATTERNS = (
    re.compile(r"\bgrid_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,}\]]+"),
)
logger = logging.getLogger(__name__)


class GridCanaryError(RuntimeError):
    """Bounded operator-facing failure from the Core connectivity canary."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ManagerWebConfig:
    profile: Path
    allow_unsigned_draft: bool
    install_root: Path
    state: Path
    credentials: Path
    pending: Path
    key: Path
    delegation: Path
    grid_url: str
    host: str
    port: int
    launch_browser: bool

    @property
    def origin(self) -> str:
        host = "[::1]" if self.host == "::1" else self.host
        return f"http://{host}:{self.port}"


class ManagerProcessController:
    """Run the reviewed manager commands without a shell and retain safe logs."""

    def __init__(self, config: ManagerWebConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.action: str | None = None
        self.started_at: float | None = None
        self.returncode: int | None = None
        self.error: str | None = None
        self._stopping = False
        self._lock = asyncio.Lock()
        self._logs: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_LINES)
        self._tasks: set[asyncio.Task[Any]] = set()

    def snapshot(self) -> dict[str, Any]:
        running = self.process is not None and self.process.returncode is None
        return {
            "running": running,
            "action": self.action,
            "started_at": self.started_at,
            "returncode": self.returncode,
            "error": self.error,
            "logs": list(self._logs),
        }

    async def start(self, action: str) -> None:
        if action not in {"setup", "serve", "install", "canary", "connect"}:
            raise ValueError("unsupported manager action")
        async with self._lock:
            if self.process is not None and self.process.returncode is None:
                raise RuntimeError("a manager operation is already running")
            command = self._command(action)
            env = self._environment()
            self.error = None
            self.returncode = None
            self.action = action
            self.started_at = time.time()
            self._stopping = False
            self._logs.clear()
            self._append_log("system", f"Starting {action}")
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._track(asyncio.create_task(self._read_stream(self.process.stdout, "out")))
            self._track(asyncio.create_task(self._read_stream(self.process.stderr, "err")))
            self._track(asyncio.create_task(self._wait_for_exit(self.process)))

    def _environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "GRID_CAPACITY_FILE": str(_capacity_path(self.config)),
        }

    async def stop(self) -> None:
        async with self._lock:
            process = self.process
            if process is None or process.returncode is not None:
                return
            self._stopping = True
            self._append_log("system", "Stopping manager process")
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        await self.stop()
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _command(self, action: str) -> list[str]:
        command = _manager_invocation()
        command.extend(["--profile", str(self.config.profile)])
        if self.config.allow_unsigned_draft:
            command.append("--allow-unsigned-draft")
        command.append(action)
        if action in {"setup", "install", "canary", "serve"}:
            command.extend(
                [
                    "--install-root",
                    str(self.config.install_root),
                    "--state",
                    str(self.config.state),
                ]
            )
        if action in {"setup", "serve", "connect"}:
            command.extend(
                [
                    "--credentials",
                    str(self.config.credentials),
                    "--key",
                    str(self.config.key),
                    "--delegation",
                    str(self.config.delegation),
                ]
            )
        if action in {"setup", "connect"}:
            command.extend(
                [
                    "--grid-url",
                    self.config.grid_url,
                    "--pending",
                    str(self.config.pending),
                ]
            )
        if action == "canary":
            command.append("--launch-runtime")
        return command

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        channel: str,
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            self._append_log(channel, line.decode("utf-8", errors="replace").rstrip())

    async def _wait_for_exit(self, process: asyncio.subprocess.Process) -> None:
        returncode = await process.wait()
        self.returncode = returncode
        if self._stopping:
            self._append_log("system", "Manager stopped")
        elif returncode == 0:
            self._append_log("system", "Operation completed")
        else:
            self.error = f"manager exited with status {returncode}"
            self._append_log("system", self.error)

    def _append_log(self, channel: str, message: str) -> None:
        safe = _redact_log(message)[:MAX_LOG_LINE]
        if safe:
            self._logs.append(
                {"timestamp": int(time.time()), "channel": channel, "message": safe}
            )

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def create_manager_app(
    config: ManagerWebConfig,
    controller: ManagerProcessController,
    session_token: str,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await controller.close()

    app = FastAPI(
        title="AI Power Grid Worker Manager",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    grid_cache: dict[str, Any] = {
        "credential_stamp": None,
        "expires_at": 0.0,
        "value": None,
    }
    grid_canary_cache: dict[str, Any] = {
        "credential_stamp": None,
        "value": None,
    }
    grid_canary_lock = asyncio.Lock()

    @app.middleware("http")
    async def local_session_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/"):
            response = await call_next(request)
            return _secure_headers(response)
        if path == "/bootstrap":
            if not secrets.compare_digest(
                request.query_params.get("token", ""), session_token
            ):
                return _secure_headers(JSONResponse({"detail": "forbidden"}, 403))
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                session_token,
                httponly=True,
                samesite="strict",
                secure=False,
                max_age=12 * 60 * 60,
            )
            return _secure_headers(response)
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if not secrets.compare_digest(cookie, session_token):
            if request.method in {"GET", "HEAD"} and path == "/":
                response = templates.TemplateResponse(
                    request=request,
                    name="manager_session_required.html",
                    status_code=403,
                )
                return _secure_headers(response)
            return _secure_headers(JSONResponse({"detail": "local session required"}, 403))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != config.origin:
                return _secure_headers(JSONResponse({"detail": "invalid origin"}, 403))
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return _secure_headers(JSONResponse({"detail": "JSON required"}, 415))
        response = await call_next(request)
        return _secure_headers(response)

    @app.get("/", response_class=HTMLResponse)
    async def manager_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="manager.html",
            context={"origin": config.origin},
        )

    @app.get("/api/manager/status")
    async def manager_status():
        result = _manager_status(config, controller)
        credential_stamp = (
            config.credentials.stat().st_mtime_ns
            if config.credentials.exists()
            else None
        )
        if credential_stamp != grid_canary_cache["credential_stamp"]:
            grid_canary_cache.update(
                {"credential_stamp": credential_stamp, "value": None}
            )
        now = time.monotonic()
        if (
            credential_stamp != grid_cache["credential_stamp"]
            or now >= grid_cache["expires_at"]
        ):
            grid_cache.update(
                {
                    "credential_stamp": credential_stamp,
                    "expires_at": now + 30.0,
                    "value": await _worker_grid_status(config),
                }
            )
        result["grid"] = grid_cache["value"]
        result["grid_canary"] = grid_canary_cache["value"]
        return result

    @app.post("/api/manager/grid-canary")
    async def manager_grid_canary(request: Request):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(payload, dict) or payload:
            raise HTTPException(400, "Grid canary accepts no parameters")
        if grid_canary_lock.locked():
            raise HTTPException(409, "a Grid connectivity test is already running")
        async with grid_canary_lock:
            try:
                result = await _worker_grid_canary(config)
            except GridCanaryError as exc:
                raise HTTPException(exc.status_code, exc.detail) from exc
            credential_stamp = (
                config.credentials.stat().st_mtime_ns
                if config.credentials.exists()
                else None
            )
            grid_canary_cache.update(
                {"credential_stamp": credential_stamp, "value": result}
            )
        return {"ok": result["status"] == "passed", "canary": result}

    @app.post("/api/manager/action")
    async def manager_action(request: Request):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(400, "invalid JSON") from exc
        action = payload.get("action") if isinstance(payload, dict) else None
        if action == "stop":
            await controller.stop()
            return {"ok": True}
        try:
            await controller.start(str(action))
        except ValueError as exc:
            raise HTTPException(400, "unsupported manager action") from exc
        except RuntimeError as exc:
            raise HTTPException(409, "a manager operation is already running") from exc
        return {"ok": True, "action": action}

    @app.post("/api/manager/capacity")
    async def manager_capacity(request: Request):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(400, "invalid JSON") from exc
        try:
            schedule = _capacity_schedule(payload)
            _write_capacity(config, schedule)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            logger.warning("Manager capacity persistence failed", exc_info=exc)
            raise HTTPException(500, "capacity settings could not be saved") from exc
        return {"ok": True, "capacity": _capacity_status(config)}

    return app


def run_manager_ui(args: Any) -> None:
    host = str(args.host).strip().lower()
    if host not in LOOPBACK_HOSTS:
        raise RuntimeError("manager UI may bind only to a loopback host")
    if not 1 <= int(args.port) <= 65535:
        raise RuntimeError("manager UI port must be between 1 and 65535")
    install_root = Path(args.install_root).expanduser().resolve()
    config = ManagerWebConfig(
        profile=Path(args.profile).expanduser().resolve(),
        allow_unsigned_draft=bool(args.allow_unsigned_draft),
        install_root=install_root,
        state=Path(args.state or install_root / "profile-state.json").expanduser().resolve(),
        credentials=Path(args.credentials).expanduser().resolve(),
        pending=Path(args.pending).expanduser().resolve(),
        key=Path(args.key).expanduser().resolve(),
        delegation=Path(args.delegation).expanduser().resolve(),
        grid_url=str(args.grid_url),
        host=host,
        port=int(args.port),
        launch_browser=not bool(args.no_browser),
    )
    session_token = secrets.token_urlsafe(32)
    controller = ManagerProcessController(config)
    app = create_manager_app(config, controller, session_token)
    bootstrap_url = f"{config.origin}/bootstrap?token={session_token}"
    print(f"AI Power Grid Worker Manager: {config.origin}", flush=True)
    if config.launch_browser:
        timer = threading.Timer(0.6, webbrowser.open, args=(bootstrap_url,))
        timer.daemon = True
        timer.start()
    else:
        print(f"Open once to establish the local session: {bootstrap_url}", flush=True)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")


def _manager_status(
    config: ManagerWebConfig,
    controller: ManagerProcessController,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": {"available": False, "error": None},
        "hardware": {"status": "unknown", "reasons": [], "warnings": []},
        "installation": {"valid": False, "canary_passed": False, "error": None},
        "identity": {"worker_signer": None, "connected": False, "payout_wallet": None},
        "capacity": _capacity_status(config),
        "process": controller.snapshot(),
        "ready": False,
    }
    try:
        document = load_profile(
            config.profile,
            allow_unsigned_draft=config.allow_unsigned_draft,
        )
    except (OSError, ValueError) as exc:
        logger.warning("Manager profile inspection failed", exc_info=exc)
        result["profile"]["error"] = "Profile is unavailable or invalid"
        return result

    profile = document.profile
    result["profile"] = {
        "available": True,
        "id": profile["id"],
        "display_name": profile["display_name"],
        "version": profile["version"],
        "status": profile["status"],
        "signature_verified": document.signature_verified,
        "digest": profile_digest(profile),
        "recipe_root": profile["recipe"]["onchain_root"],
        "error": None,
    }
    state = None
    try:
        state = validated_install_state(config.state, document)
        result["installation"] = {
            "valid": True,
            "canary_passed": bool((state.get("canary") or {}).get("passed")),
            "capability_tier": state.get("capability_tier"),
            "installed_at": state.get("installed_at"),
            "canary": state.get("canary"),
            "error": None,
        }
    except ProfileStateError as exc:
        if config.state.exists():
            logger.warning("Manager installation-state inspection failed", exc_info=exc)
        result["installation"]["error"] = "Installation is incomplete or invalid"

    try:
        snapshot = detect_hardware(config.install_root)
        recommendation = evaluate_hardware(
            snapshot,
            profile,
            accelerator_selector=state.get("runtime_device") if state else None,
        )
        accelerator = recommendation.selected_accelerator
        result["hardware"] = {
            "status": recommendation.status,
            "capability_tier": recommendation.capability_tier,
            "gpu": (
                {
                    "name": accelerator.name,
                    "vram_mb": accelerator.memory_mb,
                    "driver": accelerator.driver_version,
                    "index": accelerator.device_index,
                }
                if accelerator
                else None
            ),
            "ram_mb": snapshot.ram_mb,
            "disk_free_mb": snapshot.disk_free_mb,
            "reasons": list(recommendation.reasons),
            "warnings": list(recommendation.warnings),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Manager hardware detection failed", exc_info=exc)
        result["hardware"]["reasons"] = ["Hardware detection failed"]

    try:
        if config.key.exists():
            result["identity"]["worker_signer"] = load_worker_key(config.key).address.lower()
        if config.delegation.exists():
            certificate = load_delegation_certificate(config.delegation)
            result["identity"]["payout_wallet"] = certificate["payload"]["payout_wallet"]
        if config.credentials.exists():
            credentials = load_worker_credentials(config.credentials)
            result["identity"]["connected"] = True
            result["identity"]["worker_name"] = credentials["worker_name"]
    except (OSError, ValueError, EnrollmentClientError) as exc:
        logger.warning("Manager identity inspection failed", exc_info=exc)
        result["identity"]["error"] = "Worker identity is unavailable or invalid"

    result["ready"] = bool(
        document.signature_verified
        and profile["status"] == "active"
        and result["installation"]["valid"]
        and result["installation"]["canary_passed"]
        and result["identity"]["connected"]
    )
    return result


async def _worker_grid_status(
    config: ManagerWebConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Read the exact bound rig's redacted operational status from Core."""
    if not config.credentials.exists():
        return None
    try:
        credentials = load_worker_credentials(config.credentials)
        base = grid_api_base_url(str(credentials["grid_api_url"]))
        api = base if base.endswith("/v1") else f"{base}/v1"
        api_key = str(credentials["api_key"])
        worker_name = str(credentials["worker_name"])
    except (KeyError, OSError, ValueError, EnrollmentClientError):
        return {"available": False}

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.get(
            f"{api}/workers/self",
            headers={"apikey": api_key},
        )
        if response.status_code != 200:
            return {"available": False}
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return {"available": False}
    finally:
        if owns_client:
            await http.aclose()

    worker = data.get("worker") if isinstance(data, dict) else None
    payout = data.get("payout") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("schema") != "aipg.worker.self.v1"
        or not isinstance(worker, dict)
        or worker.get("name") != worker_name
        or not isinstance(payout, dict)
    ):
        return {"available": False}
    try:
        jobs_completed = max(0, int(worker.get("jobs_completed") or 0))
        den_recorded = max(0.0, float(worker.get("den_recorded") or 0.0))
    except (TypeError, ValueError):
        return {"available": False}
    if not math.isfinite(den_recorded):
        return {"available": False}
    return {
        "available": True,
        "worker": {
            "online": worker.get("online") if isinstance(worker.get("online"), bool) else None,
            "maintenance": bool(worker.get("maintenance")),
            "models": [
                item for item in (worker.get("models") or [])
                if isinstance(item, str)
            ][:32],
            "job_types": [
                item for item in (worker.get("job_types") or [])
                if isinstance(item, str)
            ][:8],
            "jobs_completed": jobs_completed,
            "den_recorded": den_recorded,
        },
        "payout": {
            "scope": "account",
            "wallet_configured": bool(payout.get("wallet_configured")),
            "latest_status": str(payout.get("latest_status") or ""),
            "last_paid_at": payout.get("last_paid_at")
            if isinstance(payout.get("last_paid_at"), str)
            else None,
        },
    }


async def _worker_grid_canary(
    config: ManagerWebConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Run Core's exact-worker media canary without exposing rig credentials."""
    if not config.credentials.exists():
        raise GridCanaryError(409, "Connect this worker before running a Grid test.")
    try:
        credentials = load_worker_credentials(config.credentials)
        base = grid_api_base_url(str(credentials["grid_api_url"]))
        api = base if base.endswith("/v1") else f"{base}/v1"
        api_key = str(credentials["api_key"])
        worker_name = str(credentials["worker_name"])
    except (KeyError, OSError, ValueError, EnrollmentClientError) as exc:
        raise GridCanaryError(409, "Worker credentials are unavailable or invalid.") from exc

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=920.0)
    try:
        response = await http.post(
            f"{api}/workers/self/canary",
            headers={"apikey": api_key},
            json={},
        )
    except httpx.HTTPError as exc:
        raise GridCanaryError(503, "Grid connectivity test is unavailable.") from exc
    finally:
        if owns_client:
            await http.aclose()

    errors = {
        401: "Worker credential was rejected by the Grid.",
        403: "This worker credential cannot run a Grid test.",
        404: "Grid connectivity testing is not available on Core yet.",
        409: "The worker must be online before running a Grid test.",
        429: "A Grid test ran recently. Try again in a few minutes.",
    }
    if response.status_code != 200:
        raise GridCanaryError(
            response.status_code if response.status_code in errors else 502,
            errors.get(response.status_code, "Grid connectivity test failed."),
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise GridCanaryError(502, "Grid returned an invalid canary result.") from exc

    latency_ms = result.get("latency_ms") if isinstance(result, dict) else None
    model = result.get("model") if isinstance(result, dict) else None
    modality = result.get("modality") if isinstance(result, dict) else None
    reason = result.get("reason") if isinstance(result, dict) else None
    valid = (
        isinstance(result, dict)
        and result.get("schema") == "aipg.worker.canary.v1"
        and result.get("status") in {"passed", "failed"}
        and result.get("worker_name") == worker_name
        and isinstance(model, str)
        and 0 < len(model) <= 256
        and modality in {"image", "video", "audio"}
        and isinstance(latency_ms, int)
        and not isinstance(latency_ms, bool)
        and latency_ms >= 0
        and isinstance(reason, str)
        and 0 < len(reason) <= 64
        and result.get("proof_scope")
        == "hard_targeted_connectivity_and_media_output"
        and result.get("quality_claim") == "none"
        and result.get("economic_effect") == "none"
    )
    if not valid:
        raise GridCanaryError(502, "Grid returned an invalid canary result.")
    return {
        "status": result["status"],
        "worker_name": worker_name,
        "model": model,
        "modality": modality,
        "latency_ms": latency_ms,
        "reason": reason,
        "proof_scope": "hard_targeted_connectivity_and_media_output",
        "quality_claim": "none",
        "economic_effect": "none",
    }


def _capacity_path(config: ManagerWebConfig) -> Path:
    return config.install_root / CAPACITY_FILE_NAME


def _capacity_schedule(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("capacity settings must be an object")
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in CAPACITY_MODES:
        raise ValueError("capacity mode is invalid")
    if mode == "always":
        return ""
    if mode == "paused":
        return validate_schedule('[{"days":"daily","concurrency":0}]')

    days = str(payload.get("days") or "").strip().lower()
    start = str(payload.get("start") or "").strip()
    end = str(payload.get("end") or "").strip()
    if days not in MAINTENANCE_DAYS:
        raise ValueError("maintenance days are invalid")
    if start == end:
        raise ValueError("maintenance start and end must differ")
    return validate_schedule(
        json.dumps(
            [{"days": days, "start": start, "end": end, "concurrency": 0}],
            separators=(",", ":"),
        )
    )


def _write_capacity(config: ManagerWebConfig, schedule: str) -> None:
    canonical = validate_schedule(schedule)
    config.install_root.mkdir(parents=True, exist_ok=True)
    path = _capacity_path(config)
    if path.exists() and path.is_symlink():
        raise ValueError("capacity schedule file must not be a symlink")
    temporary = config.install_root / f".{CAPACITY_FILE_NAME}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _capacity_status(config: ManagerWebConfig) -> dict[str, Any]:
    path = _capacity_path(config)
    try:
        schedule = load_schedule("", str(path))
        windows = json.loads(schedule) if schedule else []
        mode = "always"
        days = "daily"
        start = "02:00"
        end = "04:00"
        if windows:
            window = windows[0]
            days = window.get("days", days)
            start = window.get("start", start)
            end = window.get("end", end)
            mode = (
                "paused"
                if "start" not in window and "end" not in window
                else "maintenance"
            )
        concurrency = effective_concurrency(schedule)
        return {
            "mode": mode,
            "days": days,
            "start": start,
            "end": end,
            "max_concurrency": 1,
            "effective_concurrency": concurrency,
            "accepting_jobs": bool(concurrency),
            "error": None,
        }
    except (OSError, UnicodeError, ValueError) as exc:
        logger.warning("Manager capacity inspection failed", exc_info=exc)
        return {
            "mode": "invalid",
            "days": "daily",
            "start": "02:00",
            "end": "04:00",
            "max_concurrency": 1,
            "effective_concurrency": 0,
            "accepting_jobs": False,
            "error": "Capacity settings are invalid; new work is paused",
        }


def _manager_invocation() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "bridge.manager_cli"]


def _redact_log(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[redacted]",
            redacted,
        )
    return redacted


def _secure_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

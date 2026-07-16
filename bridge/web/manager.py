# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Loopback-only control surface for the standalone media manager."""

from __future__ import annotations

import asyncio
import json
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

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..enrollment import EnrollmentClientError, load_worker_credentials
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
_SECRET_PATTERNS = (
    re.compile(r"\bgrid_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,}\]]+"),
)


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
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
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
        return _manager_status(config, controller)

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
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "action": action}

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
        "process": controller.snapshot(),
        "ready": False,
    }
    try:
        document = load_profile(
            config.profile,
            allow_unsigned_draft=config.allow_unsigned_draft,
        )
    except (OSError, ValueError) as exc:
        result["profile"]["error"] = str(exc)
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
        result["installation"]["error"] = str(exc)

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
        result["hardware"]["reasons"] = [str(exc)]

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
        result["identity"]["error"] = str(exc)

    result["ready"] = bool(
        document.signature_verified
        and profile["status"] == "active"
        and result["installation"]["valid"]
        and result["installation"]["canary_passed"]
        and result["identity"]["connected"]
    )
    return result


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

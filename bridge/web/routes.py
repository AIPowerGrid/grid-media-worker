import logging
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import Settings
from ..capacity import validate_max_concurrency, validate_schedule
from ..comfyui_detect import (
    check_comfyui_url,
    detect_comfyui,
    install_comfyui_via_cli,
    validated_comfyui_url,
)
from ..model_mapper import ModelMapper
from .app import app, templates, worker_state, start_worker, stop_worker

logger = logging.getLogger(__name__)

ENV_PATH = Path.cwd() / ".env"
_PERSISTED_SETTINGS = frozenset(
    {
        "COMFYUI_BASE_PATH",
        "COMFYUI_URL",
        "GRID_API_KEY",
        "GRID_BATCH_SIZE",
        "GRID_MAX_PIXELS",
        "GRID_MODEL",
        "GRID_NSFW",
        "GRID_SCHEDULE",
        "GRID_THREADS",
        "GRID_WORKER_NAME",
        "WORKFLOW_FILE",
    }
)
_JSON_POST_PATHS = frozenset(
    {
        "/api/setup/check-url",
        "/api/setup/inventory",
        "/api/setup/install-comfyui",
        "/api/setup/complete",
        "/api/settings",
    }
)


def _secure_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.middleware("http")
async def local_control_guard(request: Request, call_next):
    """Reject cross-origin and non-JSON browser mutations on the local UI."""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected_origin = str(request.base_url).rstrip("/")
        if request.headers.get("origin") != expected_origin:
            return _secure_headers(JSONResponse({"detail": "invalid origin"}, status_code=403))
        if (
            request.url.path in _JSON_POST_PATHS
            and request.headers.get("content-type", "").split(";", 1)[0]
            != "application/json"
        ):
            return _secure_headers(JSONResponse({"detail": "JSON required"}, status_code=415))
    return _secure_headers(await call_next(request))


# ---------------------------------------------------------------------------
# Middleware: redirect to setup if not configured
# ---------------------------------------------------------------------------
@app.middleware("http")
async def setup_guard(request: Request, call_next):
    path = request.url.path
    # Allow static files, API routes, and setup pages through always
    if (
        path.startswith("/static")
        or path.startswith("/api/")
        or path.startswith("/setup")
    ):
        return await call_next(request)
    # If not configured, redirect to setup
    if not worker_state["setup_complete"]:
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------
@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    detection = detect_comfyui()
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={"detection": detection},
    )


@app.post("/api/setup/detect")
async def api_detect():
    """Run ComfyUI detection and return results."""
    detection = detect_comfyui()
    return {
        "found": detection.found,
        "base_path": detection.base_path,
        "url": detection.url,
        "comfy_cli_available": detection.comfy_cli_available,
        "comfy_cli_path": detection.comfy_cli_path,
        "comfy_cli_workspace": detection.comfy_cli_workspace,
        "methods": detection.methods,
    }


@app.post("/api/setup/check-url")
async def api_check_url(request: Request):
    """Check if a ComfyUI URL is reachable."""
    body = await request.json()
    url = body.get("url", "")
    reachable = await check_comfyui_url(url)
    return {"url": url, "reachable": reachable}


@app.post("/api/setup/inventory")
async def api_setup_inventory(request: Request):
    """Inspect a local ComfyUI instance without changing worker state."""
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"url"}:
        return JSONResponse({"detail": "Invalid inventory request"}, status_code=400)
    try:
        url = validated_comfyui_url(str(body["url"]))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "Invalid ComfyUI URL"}, status_code=400)

    mapper = ModelMapper()
    await mapper.initialize(url)
    advertised = []
    bridge = worker_state.get("bridge")
    if bridge is not None:
        advertised = list(getattr(bridge, "models", []) or [])
    return {
        "detected": bool(mapper.available_files),
        "weight_count": len(mapper.available_files),
        "capabilities": mapper.capability_report(),
        "advertised": advertised,
    }


@app.post("/api/setup/install-comfyui")
async def api_install_comfyui(request: Request):
    """Install ComfyUI via comfy-cli."""
    body = await request.json()
    if not isinstance(body, dict) or body:
        return JSONResponse({"ok": False, "error": "Unsupported install option"}, status_code=400)
    result = install_comfyui_via_cli()
    return result


@app.post("/api/setup/complete")
async def api_complete_setup(request: Request):
    """Save config and start the worker."""
    try:
        form = _validated_settings_form(await request.json())
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid worker settings"}, status_code=400)

    # Build .env content, preserving any existing keys not in the form
    env_lines = _read_existing_env()
    for key, value in form.items():
        if value is not None and value != "":
            env_lines[key] = value

    _write_env(env_lines)

    # Reload settings in memory
    _reload_settings(form)

    worker_state["setup_complete"] = True

    # Start worker
    if Settings.GRID_API_KEY:
        await start_worker()

    logger.info("Setup complete. Worker starting.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "worker_running": worker_state["running"],
            "worker_error": worker_state.get("error"),
        },
    )


@app.get("/api/status")
async def api_status():
    return {
        "worker_running": worker_state["running"],
        "worker_error": worker_state.get("error"),
        "config": {
            "has_api_key": bool(Settings.GRID_API_KEY),
            "worker_name": Settings.GRID_WORKER_NAME,
            "comfyui_url": Settings.COMFYUI_URL,
            "models": Settings.GRID_MODELS,
            "nsfw": Settings.NSFW,
            "max_pixels": Settings.MAX_PIXELS,
            "max_concurrency": Settings.THREADS,
            "schedule": Settings.GRID_SCHEDULE,
        },
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": {
                "GRID_API_KEY": Settings.GRID_API_KEY,
                "GRID_WORKER_NAME": Settings.GRID_WORKER_NAME,
                "COMFYUI_URL": Settings.COMFYUI_URL,
                "GRID_MODEL": Settings._GRID_MODELS_RAW,
                "WORKFLOW_FILE": Settings.WORKFLOW_FILE or "",
                "GRID_NSFW": str(Settings.NSFW).lower(),
                "GRID_MAX_PIXELS": str(Settings.MAX_PIXELS),
                "GRID_BATCH_SIZE": str(Settings.BATCH_SIZE),
                "GRID_THREADS": str(Settings.THREADS),
                "GRID_SCHEDULE": Settings.GRID_SCHEDULE,
            },
        },
    )


@app.post("/api/settings")
async def save_settings(request: Request):
    """Save settings to .env and update in-memory config."""
    try:
        form = _validated_settings_form(await request.json())
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid worker settings"}, status_code=400)

    env_lines = _read_existing_env()
    for key, value in form.items():
        if value is not None and value != "":
            env_lines[key] = value
        elif key in env_lines:
            del env_lines[key]

    _write_env(env_lines)
    _reload_settings(form)

    logger.info(f"Settings saved to {ENV_PATH}")
    return {"ok": True, "message": "Restart worker to apply all changes."}


@app.post("/api/worker/restart")
async def restart_worker():
    """Stop and restart the worker with current config."""
    await stop_worker()
    await start_worker()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _read_existing_env() -> dict:
    """Read existing .env into an ordered dict."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _write_env(values: dict) -> None:
    content = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    ENV_PATH.write_text(content)
    if os.name != "nt":
        os.chmod(ENV_PATH, 0o600)


def _validated_settings_form(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Settings payload must be an object")
    unknown = set(value) - _PERSISTED_SETTINGS
    if unknown:
        raise ValueError("Settings payload contains unsupported fields")

    form: dict[str, str] = {}
    for key, raw in value.items():
        text = str(raw) if raw is not None else ""
        if len(text) > 4096 or "\n" in text or "\r" in text:
            raise ValueError(f"Invalid value for {key}")
        form[key] = text

    if form.get("COMFYUI_URL"):
        form["COMFYUI_URL"] = validated_comfyui_url(form["COMFYUI_URL"])
    if "GRID_NSFW" in form and form["GRID_NSFW"].lower() not in {"true", "false"}:
        raise ValueError("GRID_NSFW must be true or false")
    if "GRID_SCHEDULE" in form:
        form["GRID_SCHEDULE"] = validate_schedule(form["GRID_SCHEDULE"])
    if "GRID_THREADS" in form:
        form["GRID_THREADS"] = str(validate_max_concurrency(form["GRID_THREADS"]))
    for key, lower, upper in (
        ("GRID_BATCH_SIZE", 1, 16),
        ("GRID_MAX_PIXELS", 1, 134_217_728),
    ):
        if key in form and form[key]:
            try:
                number = int(form[key])
            except ValueError as exc:
                raise ValueError(f"{key} must be an integer") from exc
            if not lower <= number <= upper:
                raise ValueError(f"{key} must be between {lower} and {upper}")
            form[key] = str(number)
    return form


def _reload_settings(form: dict):
    """Update Settings class attributes from form data."""
    if "GRID_API_KEY" in form:
        Settings.GRID_API_KEY = form["GRID_API_KEY"]
    if "GRID_WORKER_NAME" in form:
        Settings.GRID_WORKER_NAME = form["GRID_WORKER_NAME"]
    if "COMFYUI_URL" in form:
        Settings.COMFYUI_URL = form["COMFYUI_URL"]
    if "GRID_MODEL" in form:
        Settings._GRID_MODELS_RAW = form["GRID_MODEL"]
        Settings.GRID_MODELS = [
            m.strip() for m in form["GRID_MODEL"].split(",") if m.strip()
        ]
    if "GRID_NSFW" in form:
        Settings.NSFW = form["GRID_NSFW"].lower() == "true"
    if "GRID_MAX_PIXELS" in form:
        Settings.MAX_PIXELS = int(form["GRID_MAX_PIXELS"])
    if "WORKFLOW_FILE" in form:
        Settings.WORKFLOW_FILE = form["WORKFLOW_FILE"] or None
    if "GRID_BATCH_SIZE" in form:
        Settings.BATCH_SIZE = int(form["GRID_BATCH_SIZE"])
    if "GRID_THREADS" in form:
        Settings.THREADS = int(form["GRID_THREADS"])
    if "GRID_SCHEDULE" in form:
        Settings.GRID_SCHEDULE = form["GRID_SCHEDULE"]
    if "COMFYUI_BASE_PATH" in form:
        os.environ["COMFYUI_BASE_PATH"] = form["COMFYUI_BASE_PATH"]

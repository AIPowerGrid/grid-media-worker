# bridge/web — control UI (FastAPI)

## Purpose

Local management surfaces for both worker paths. The legacy `comfy-bridge` app
on port 7860 configures an existing ComfyUI worker. The standalone manager UI
on loopback port 8791 presents signed-profile recommendation, installation,
canary, wallet-pairing, and worker process state.

## Ownership

- `app.py` — the FastAPI app, lifespan, and `worker_state` (running/error/task/bridge).
  `_run_worker` selects WS vs legacy transport by `Settings.GRID_WS`; `start_worker`/`stop_worker`
  manage the background task.
- `routes.py` — HTTP routes + JSON `/api/*` endpoints (setup detect/install/check/complete,
  status, settings save, worker restart) and the `setup_guard` redirect middleware. Reads/writes
  `.env` and mutates `Settings` in place.
- `manager.py` — isolated manager app factory and shell-free lifecycle process
  controller. A random bootstrap token establishes an HttpOnly local session;
  mutating routes additionally require the exact loopback Origin and JSON.
- `templates/manager_session_required.html` — safe recovery page for a missing
  local bootstrap session; API callers continue to receive a JSON 403.
- `templates/` — Jinja2 pages (base, setup, dashboard, settings). `static/` — CSS,
  manager JavaScript, and the canonical inference-worker logo/favicon assets.

## Local Contracts

- This is the only place that persists config: it writes `.env` and updates `Settings`
  attributes live. Config still flows through `Settings`; do not read env here directly.
- Settings changes apply to the in-memory `Settings` immediately but a worker restart
  (`/api/worker/restart`) is required for the worker to pick them up.
- Both UIs bind loopback by default. The manager UI rejects non-loopback binds,
  has no remote-access mode, redacts retained logs, and never exposes worker API
  credentials. Do not weaken these boundaries for convenience.
- Manager actions execute the same reviewed CLI commands as the terminal path;
  do not duplicate install, canary, enrollment, or serving policy in HTTP routes.
- An explicitly allowed unsigned preview may install and rerun its local audio
  canary from the UI, but it must never connect or serve a Grid capability.

## Work Guidance

- New manager controls must be deterministic commands with no arbitrary
  arguments, paths, URLs, or shell strings supplied by the browser.

## Verification

- `pytest -q tests/test_manager_web.py tests/test_manager_cli.py`.

## Child DOX Index

- None — leaf.

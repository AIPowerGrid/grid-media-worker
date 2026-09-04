# bridge/web — control UI (FastAPI)

## Purpose

Local management surfaces for Grid workers. The `comfy-bridge` app on port 7860
configures an existing ComfyUI worker. The standalone manager UI
on loopback port 8791 presents signed-profile recommendation, installation,
canary, wallet-pairing, and worker process state.

## Ownership

- `app.py` — the FastAPI app, lifespan, and `worker_state`
  (running/error/task/bridge). `_run_worker` supervises the Grid WebSocket transport and retries
  the full startup sequence after local-runtime/preflight failures;
  `start_worker`/`stop_worker` manage the background task.
- `routes.py` — HTTP routes + JSON `/api/*` endpoints (setup detect/install/check/complete,
  status, settings save, worker restart) and the `setup_guard` redirect middleware. Reads/writes
  `.env` and mutates `Settings` in place.
- `manager.py` — isolated manager app factory and shell-free lifecycle process
  controller. A random bootstrap token establishes an HttpOnly local session;
  mutating routes additionally require the exact loopback Origin and JSON. It
  proxies the exact-worker Core canary with the local rig credential and
  returns only a schema-validated summary.
- `templates/manager_session_required.html` — safe recovery page for a missing
  local bootstrap session; API callers continue to receive a JSON 403.
- `templates/` — Jinja2 pages (base, setup, dashboard, settings). `static/` — CSS,
  manager JavaScript, and the canonical inference-worker logo/favicon assets.

## Local Contracts

- This is the only place that persists config: it writes `.env` and updates `Settings`
  attributes live. Config still flows through `Settings`; do not read env here directly.
- Settings changes apply to the in-memory `Settings` immediately but a worker restart
  (`/api/worker/restart`) is required for the worker to pick them up.
- Both UIs are loopback-only and reject untrusted Host values. Browser mutations
  require the exact local Origin; JSON-bearing routes reject other content
  types. Use an SSH tunnel for remote operation rather than widening the bind.
  The manager additionally requires its bootstrap session, redacts retained
  logs, and never exposes worker API credentials. Do not weaken these
  boundaries for convenience.
- The legacy setup UI may invoke only the server-discovered `comfy-cli` binary.
  Browser-supplied executables and environment keys are forbidden, and its
  automated install target is fixed at `~/ComfyUI`; custom locations are a
  terminal-only operator action. Dependency installation is also terminal-only;
  the browser never invokes pip or a remote shell installer.
- Render persisted values into JavaScript with Jinja `tojson`; never interpolate
  environment or exception text into executable script literals.
- The ComfyUI inventory is descriptive only. Keep the state progression
  explicit: detected, compatible, qualified, then advertised. Never label a
  local workflow as qualified or advertised before its runtime gate passes.
- The UI exposes the current one-job media limit and a bounded local-time
  `GRID_SCHEDULE`. Matching windows may pause (`concurrency: 0`) or accept one
  job (`concurrency: 1`). Do not permit values above one until Core supports
  multiple claim slots for one signed worker identity.
- The standalone manager persists only three bounded availability modes:
  always available, paused, or one daily/weekday/weekend maintenance window.
  It writes a local `0600` capacity file that the serving child watches. A
  pause must drain an active render before disconnecting; never implement it by
  terminating the serving process.
- The standalone manager reads Core's `GET /v1/workers/self` only with its
  locally stored rig credential and renders the exact bound worker's
  online/jobs/den state plus a redacted account-level payout lifecycle. It must
  never return
  the API key, broaden that key to account reads, enumerate sibling workers, or
  expose payout addresses, balances, amounts, or transaction hashes.
  Cache that remote status for 30 seconds, invalidating immediately when the
  local credential file changes; the five-second local process poll must not
  become five-second Core database traffic.
- The manager's Grid-canary action is available only for an online exact-bound
  worker. It accepts no browser parameters, serializes requests, validates
  worker name, modality, proof scope, and zero economic/quality effect, and
  invalidates the retained result when credentials change. Never expose the
  worker credential or Core's output digest to the browser.
- Browser dependencies must be exact-versioned and integrity-pinned; do not
  restore floating CDN ranges on a page that handles worker credentials.
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

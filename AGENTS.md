# DOX framework

- DOX is a hierarchy of AGENTS.md files that carry the durable contracts for this repo.
- Agents must follow the DOX chain on every edit.

## Core Contract

- AGENTS.md files are binding work contracts for their subtrees.
- Any work product must stay understandable from the nearest AGENTS.md plus every parent above it.

## Read Before Editing

1. Read this root AGENTS.md.
2. Identify every path you expect to touch.
3. Walk from repo root to each target, reading every AGENTS.md on the way.
4. The nearest AGENTS.md is the local contract; parents hold repo-wide rules.
5. If docs conflict, the closer doc controls local detail, but no child may weaken DOX.

Do not rely on memory — re-read the applicable chain in-session before editing.

## Update After Editing

Every meaningful change requires a DOX pass before the task is done. Update the closest
owning AGENTS.md when a change affects: purpose/scope/ownership; durable structure,
contracts, or workflows; inputs/outputs/permissions/side-effects; or the Child DOX Index.
Remove stale text immediately. Refresh affected parent and child indexes.

## Style

Concise, current, operational. Stable contracts, not diary entries. Broad rules in parents,
concrete detail in children. Delete stale notes instead of explaining history.

---

# grid-media-worker — ComfyUI → AI Power Grid worker

## Purpose

Runs a local ComfyUI install as a grid GPU worker. Receives image/video jobs from the
grid, renders them by templating a ComfyUI workflow graph and posting it to ComfyUI's
`/prompt` API, streams progress/preview frames upstream, and returns the outputs.
Ships a FastAPI control UI (setup wizard + dashboard) on port 7860. Console script:
`comfy-bridge` (entry point `bridge.cli:main`).

## Ownership

- `bridge/` — the worker package: transport, model→workflow mapping, graph templating,
  control UI. Owned in its own AGENTS.md.
- `workflows/` — ComfyUI graph JSON templates the worker fills per job. Owned in its own AGENTS.md.
- `tests/` — pytest suite (`respx` HTTP mocking, `pytest-asyncio`). Covers `api_client`,
  `workflow`, `utils`, preview.
- Top-level loose files (`*.json`, `enhanced_reference.json`, `*.html`, `prepare_release.py`,
  `workflow_git_export.py`, `check_connections.py`) are sample workflows, the model
  reference, and dev/release helpers — not part of the worker runtime.

## Local Contracts

- **Inherit org engineering standards:** aipg-documentation/engineering-standards/
  (core + git + the matching language file — Python).
- **WebSocket is the working transport.** `bridge/ws_worker.py` opens a persistent WebSocket to
  `/v1/workers/ws` (derived from `GRID_API_URL`) for push dispatch + presigned R2 PUT. The
  legacy poll loop (`bridge/bridge.py`, `/v2/generate/pop` → `/submit`) targets the RETIRED `/v2`
  queue and no longer functions server-side — do not treat it as a working default even though
  `GRID_WS` still defaults `false` in config. All new work targets the WS path (`GRID_WS=true`).
- **Recipe-executor dispatch (primary):** the grid resolves a job's `recipe_spec` server-side
  and pushes the graph; `bridge/workflow.py:build_recipe_workflow` runs that core-resolved spec
  directly (binding only supplied source images into declared slots). This is the forward
  dispatch mode — "add a recipe in core → it runs here" — preferred over local model→workflow
  mapping.
- **Advertise only what you can serve:** a model is advertised only when its workflow file is
  resolvable. With `WORKFLOW_FILE` set, models are derived from the checkpoint files in those
  graphs via the local model reference; unresolved files are not advertised.
- **No standing storage creds on the worker.** WS path uploads outputs to grid-issued presigned
  R2 URLs from the job message. The legacy path returns base64 if no R2 URL is present.
- **All config is env-driven** through `bridge/config.py` (`Settings`); the UI persists changes
  to `.env`. `GRID_API_KEY` is required.

## Work Guidance

- New model support → add the grid-name→workflow mapping in `bridge/model_mapper.py` and the
  graph file under `workflows/`; verify it resolves before advertising.
- New job parameter → template it in `bridge/workflow.py` for BOTH graph shapes (ComfyUI native
  `nodes`/`widgets_values` and the API-export `class_type`/`inputs` form).
- Prefer the explicit `_bridge` metadata block in a workflow over heuristic node detection.

## Verification

- `pytest tests/` — CI runs it on Python 3.10–3.12 (`.github/workflows/test.yml`).

## Child DOX Index

- [bridge/AGENTS.md](bridge/AGENTS.md) — worker package: transport, mapping, templating, UI.
- [workflows/AGENTS.md](workflows/AGENTS.md) — ComfyUI graph JSON templates.
- [tests/AGENTS.md](tests/AGENTS.md) — worker transport, workflow, and utility
  tests.

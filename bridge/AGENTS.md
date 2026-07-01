# bridge — the worker package

## Purpose

The worker runtime: connect to the grid, map grid model names to local ComfyUI workflows,
template the workflow per job, drive ComfyUI, relay progress/previews, and return outputs.

## Ownership

- **Transport (forward, the working path):** `ws_worker.py` — persistent WebSocket to
  `/v1/workers/ws` (derived from `GRID_API_URL`). Registers (`apikey`/`name`/`models`/`job_types`),
  receives `job` messages, renders, uploads each output to its presigned R2 slot, replies `done`
  with seeds + sha256 receipts. Enable with `GRID_WS=true`.
- **Transport (legacy, RETIRED server-side):** `bridge.py` (`ComfyUIBridge`) — poll loop
  `/v2/generate/pop` → render → R2-or-base64 → `/v2/generate/submit`. The grid's `/v2` queue is
  gone, so this path no longer functions; `api_client.py` is its HTTP client. `_view_url` (in
  `bridge.py`) builds ComfyUI `/view` URLs (keep `subfolder`+`type` — WAN videos land in
  subfolders) and is still reused by the WS path.
- **Mapping:** `model_mapper.py` — grid model name → workflow filename (`DEFAULT_WORKFLOW_MAP`
  + img2img map), and checkpoint-file → grid-name resolution via the local model reference.
- **Templating:** `workflow.py` — two paths. `build_recipe_workflow(job, payload)` executes a
  core-resolved `recipe_spec` the grid pushes (binds supplied source images into declared slots
  only; never invents structure) — the primary dispatch mode. `build_workflow(job)` is the local
  fallback: loads the mapped graph and fills prompt/seed/dimensions/batch/output-prefix, handling
  both graph shapes and the `_bridge` block.
- **Config:** `config.py` (`Settings`) — env reads + `.env` loading; the single config surface.
- **Detection/UI:** `comfyui_detect.py` (find/install ComfyUI for the wizard); `web/` — control
  UI, owned in its own AGENTS.md.
- `utils.py` — seed + media encoding helpers. `cli.py` — console entry; launches the web app.

## Local Contracts

- Both transports share `build_workflow`, `model_mapper`, and `Settings` — keep payload
  adaptation in the transport layer, not in `workflow.py`.
- The worker never holds storage credentials (WS uploads to presigned slots; see root contract).
- Progress/preview relay is best-effort and throttled; a dropped frame must never fail a job.
- `cli.main` starts the FastAPI app; the worker runs as a background task inside its lifespan,
  selected by `Settings.GRID_WS`. There is no separate worker-only entry point.

## Work Guidance

- New job parameter → template in `workflow.py` for BOTH the ComfyUI native (`type` +
  `widgets_values`) and API-export (`class_type` + `inputs`) node forms.
- Adding a model → mapping in `model_mapper.py` + graph under `../workflows/`; advertise only
  what resolves (root contract).
- Config → add to `Settings`; do not scatter `os.getenv` elsewhere.

## Verification

- `pytest ../tests/` (api_client, workflow, utils, preview).

## Child DOX Index

- [web/AGENTS.md](web/AGENTS.md) — FastAPI setup wizard + dashboard control UI.

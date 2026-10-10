# bridge — the worker package

## Purpose

The worker runtime: connect to the grid, map grid model names to local ComfyUI workflows,
template the workflow per job, drive ComfyUI, relay progress/previews, and return outputs.

## Ownership

- **Transport:** `ws_worker.py` — persistent WebSocket to
  `/v1/workers/ws` (derived from `GRID_API_URL`). Registers (`apikey`/`name`/`models`/`job_types`),
  receives `job` messages, renders, uploads each output to its presigned R2 slot, replies `done`
  with seeds + SHA-256 receipts. `_view_url` preserves ComfyUI `subfolder` and
  `type` values when retrieving outputs. Video collection accepts native
  `videos`/`video` entries and Video Helper Suite's legacy `gifs` key, which may
  contain MP4 output. A completed prompt without a supported output fails
  immediately; a bounded timeout interrupts a genuinely stuck prompt.
- **Candidate video recovery:** `render_journal.py` - private SQLite execution
  identity and MP4 byte cache for marked, single-video recipe jobs. Persist
  the preassigned prompt ID and submitting state before the sole `/prompt`
  POST. ComfyUI accepts that ID but does not deduplicate submissions. Unknown
  acceptance can only observe the saved ID; never POST again or interrupt
  a shared runtime. Recover cached bytes before upload/DONE retries.
- **Mapping:** `model_mapper.py` — grid model name → workflow filename (`DEFAULT_WORKFLOW_MAP`
  + img2img map), and checkpoint-file → grid-name resolution via the local model reference.
- **Templating:** `workflow.py` — two paths. `build_recipe_workflow(job, payload)` executes a
  core-resolved `recipe_spec` the grid pushes (binds supplied source images into declared slots
  only; never invents structure) — the primary dispatch mode. `build_workflow(job)` is the local
  fallback: loads the mapped graph and fills prompt/seed/dimensions/batch/output-prefix, handling
  both graph shapes and the `_bridge` block.
  `recipe_image_bindings` maps at most sixteen source indices to unique declared
  `LoadImage.inputs.image` slots. Validate all bindings before fetching and refuse
  unbound image nodes. Repeated source URLs reuse one upload; downloads are streamed
  under the 12 MiB per-image limit. Preserve the supplied graph.
  Bind the actual ComfyUI upload filename, including collision renames; reject
  returned paths, unexpected subfolders and non-input identities.
- **Config:** `config.py` (`Settings`) — env reads + `.env` loading; the single config surface.
- **Detection/UI:** `comfyui_detect.py` (find/install ComfyUI for the wizard); `web/` — control
  UI and local capability inventory, owned in its own AGENTS.md. Inventory is descriptive;
  runtime gates remain authoritative for qualification and advertisement.
- **Managed profiles:** `profiles/` - signed declarative manifests, artifact
  commitments, local hardware detection, and recommendation. Owned in its own
  AGENTS.md.
- `utils.py` — seed + media encoding helpers. `cli.py` — console entry; launches the web app.
- `image_output.py` - validates/re-encodes PNG, WebP, and JPEG outputs to match
  signed upload content types; receipts hash the uploaded bytes.
- `loras.py` - source/sink-checked recipe LoRA injection, local safetensors
  resolution, and bounded CivitAI downloads. New downloads require a provider
  SHA256 commitment plus safetensors validation and atomic file promotion.
- `manager_cli.py` - `grid-media-manager` profile lifecycle, worker identity,
  runtime supervision, serve commands, and loopback manager-UI entry point.
- `enrollment.py` - crash-resumable Console pairing. The candidate worker API
  key and poll token originate locally and remain in a `0600` pending file;
  returned delegation certificates must match signer, name, chain, and audience
  before credentials are promoted and ACKed.
- `audio_runtime.py` / `runtime_process.py` - constrained loopback ACE-Step API
  execution and shell-free child-process supervision.
- `identity.py` - funds-less worker key, payout-wallet delegation, registration
  proof, and signed job receipts.
- `release_verifier.py` - offline verification for exact manager binaries,
  aggregate checksums, signed-profile release gates, benchmark-only
  qualification restrictions, and SPDX SBOM metadata.

## Local Contracts

- Keep transport payload adaptation in `ws_worker.py`, not `workflow.py`.
- Registration advertises `recipe-image-bindings-v1` for Core's multi-reference
  dispatch compatibility check. This does not attest execution or model fidelity.
  Multiple sources without explicit bindings fail, never fall back to one image.
- No worker release yet advertises `async-video-resume-v1`. A marked video
  requires `GRID_COMFYUI_STATE_DIR`, a loopback runtime, one MP4 upload slot,
  one Core-assigned seed and a governed ComfyUI recipe; LoRAs and batches are
  not supported in this candidate path. Legacy requests are unchanged.
  The ComfyUI submission/polling client ignores ambient HTTP proxies; validate
  its actual base URL, not a changed Settings value, before durable execution.
  The future Core reconnect delivery must set top-level `resume: true` without
  changing the immutable payload. Missing local render identity on a resume
  fails uncertain before graph construction; never treat lost state as a new
  render. This worker guard does not implement Core's authorization handoff.
- The candidate journal is namespaced to Core's account-owned worker UUID from
  the authenticated ready frame, worker name, Grid endpoint and ComfyUI endpoint.
  Missing/invalid worker IDs fail closed. Reconnect resets the local journal
  handle so its owner is rechecked. API-key rotation within the same Core worker
  identity does not invalidate the cache; changing identity/name/endpoints
  requires operator reconciliation, never deletion to rerender. Keep each GPU's
  state separate.
  POSIX requires operator-owned `0700` directories and `0600` regular files;
  Windows ACL/crash qualification remains a release gate. No raw prompt,
  graph, credential, secret-derived identifier or upload URL is stored in the
  journal. Generated MP4s are private user content, not public validator evidence.
- Bind both the exact submitted graph hash and a pre-submission exact-number
  normalized hash. ComfyUI FLOAT validation changes `24` to `24.0`; only safe
  integral floats normalize. Booleans, strings, fractions, different nodes,
  paths and parameters must still conflict. Old rows never infer a missing
  normalized commitment from returned history.
- Cache expected byte hash/size before atomic rename and fsync. Enforce
  256 KiB commitments, 4 MiB observations, 256 MiB per MP4, 1 GiB aggregate
  cache and 1024 tombstones. ACK retains identity/bytes; there is no automatic
  pruning yet. At capacity, stop/reconcile rather than forgetting a render.
  Retention and Core-bound continuation are required before advertisement.
- A definite backend rejection/execution or invalid completed-output failure
  may send a generic error. Unknown transport, cache or delivery failures close
  the WebSocket without a terminal failure/refund request. Core owns financial
  expiry and must independently authorize/settle the original execution.
- Native ComfyUI SaveVideo uses an `images` envelope for MP4 output. Video
  collection recognizes it without image re-encoding; the candidate cache
  still validates MP4 magic and bounds. This is not codec, quality or fidelity
  certification; live qualification must inspect the actual video/audio.
- Requested recipe LoRAs must resolve and inject or the job fails before
  rendering. Missing injection maps never silently drop a requested modifier.
  The `done.loras` list reports filenames, not model-fidelity proof. Downloads
  require explicit operator `LORA_DIR`, numeric IDs, approved HTTPS origins,
  finite strengths, at most five adapters, and `LORA_MAX_DOWNLOAD_BYTES`.
  `CIVITAI_TOKEN` is a header only on civitai.com, never a query parameter or
  CDN credential. Existing operator-installed safetensors remain trusted local
  inputs; this is not a validator certification of their contents.
- Image bytes must decode and match the upload slot's content type. Convert
  every image before uploading the first; format failures cannot be relabelled
  as successful images. Audio/video bytes are unchanged.
- Recipe image batching must include `EmptyFlux2LatentImage`, not only the
  older empty-latent classes. Preserve the input graph and verify exact output
  count before upload; this does not enable Core's public batch gate.
- Recipe image batches render independently at batch size one for each assigned
  seed. Native ComfyUI batches consume one RNG stream and cannot truthfully be
  labelled seed + index. Only graphs with one concrete RandomNoise, KSampler,
  or KSamplerAdvanced seed and supported empty-latent batch nodes are accepted;
  linked/ambiguous seed graphs fail closed. All outputs must finish before any
  upload. Single outputs, video/audio, and legacy local templates are unchanged.
- Grid `n` is the output-count authority and overrides the legacy `batch_size`
  adapter field. Upload slots must match before rendering. Rendered output count
  must match before any upload or signed `done`; partial or extra batches fail
  locally rather than uploading a subset or silently dropping extras. Core must
  still independently verify all requested outputs before charging/rewarding.
- The worker never holds storage credentials (WS uploads to presigned slots; see root contract).
- Worker credentials may cross only `wss://` outside loopback. Plaintext remote
  WebSockets require the explicit development-only `GRID_WS_INSECURE` override.
- Progress/preview relay is best-effort and throttled; a dropped frame must never fail a job.
- `cli.main` starts the FastAPI app; the WebSocket worker runs as a background
  task inside its lifespan. There is no separate worker-only entry point.

## Work Guidance

- New job parameter → template in `workflow.py` for BOTH the ComfyUI native (`type` +
  `widgets_values`) and API-export (`class_type` + `inputs`) node forms.
- Adding a model → mapping in `model_mapper.py` + graph under `../workflows/`; advertise only
  what resolves (root contract).
- Config → add to `Settings`; do not scatter `os.getenv` elsewhere.
- Legacy ComfyUI startup retries an empty weight inventory before selecting
  automatic candidates. An empty inventory never authorizes registration.
  Default workflows resolve relative to the installation; explicit paths win.
- The startup pricing check is advisory, includes priced aliases, and stays
  silent when the public price book cannot be read. It is not an admission gate.
- Managed-profile mode requires an active signed profile, matching install
  state, and a passed runtime-specific canary. The profile's capabilities
  replace manual model/job-type declarations; direct ACE-Step readiness replaces
  generic ComfyUI preflight.
- Grid enrollment also requires an active signed profile. An unsigned
  qualification draft may install and benchmark locally, but it cannot enroll,
  advertise, set up, or serve a Grid capability.
- Managed ACE-Step processes run with model-hub offline mode and may launch only
  after the pinned source and exact checkpoint tree revalidate locally.
- Managed capabilities remain registered only while the supervised runtime
  process is alive and its loopback health/model-inventory checks remain
  healthy. Runtime exit cancels the worker WebSocket immediately; sustained
  health loss also withdraws the capability so the service supervisor can
  restart the complete runtime/worker pair.
- Every WebSocket worker, including legacy ComfyUI services outside the managed
  profile supervisor, proves local runtime health before registration and
  withdraws its Grid connection after sustained health-check failure.
- Retired model identities are filtered before advertisement, including trusted
  overrides and signed profiles. A stale local reference must not resurrect a
  network capability that Core has retired.
- Managed third-party runtimes run at warning log level so request prompts and
  lyrics are not persisted in service journals.
- Local control surfaces return stable error classes, not raw exception or
  subprocess output. Detailed diagnostics remain in local process logs.
- The local profile canary proves only pinned runtime readiness. The manager's
  separate Grid test uses the rig-only credential server-side, accepts no
  browser-selected model or payload, and displays only Core's bounded,
  economically inert exact-worker result.
- Job failures sent to Core are generic and never contain local paths or raw
  backend exception text; operators diagnose the detailed local log entry.
- Media capacity is one simultaneous job per signed worker identity. A bounded
  local-time schedule may pause new claims; a transition to paused drains the
  active job before disconnecting. `GRID_THREADS` values above one fail closed
  until Core has an explicit multi-slot worker protocol.
- A pending enrollment remains authoritative until Core activation is ACKed.
  Existing credential files must not short-circuit a pending ACK retry.

## Verification

- `pytest ../tests/` (api_client, workflow, utils, preview).

## Child DOX Index

- [web/AGENTS.md](web/AGENTS.md) — FastAPI setup wizard + dashboard control UI.
- [profiles/AGENTS.md](profiles/AGENTS.md) - signed install profiles and local
  compatibility evaluation.

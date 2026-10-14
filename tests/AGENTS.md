# Media Worker Tests

## Purpose

Pytest coverage for workflow templating, utility functions, preview handling,
and the WebSocket worker.

## Ownership

- `test_video_startup.py` - normal-run capability gating, exact Core model/root/
  contract, complete shared source modes, missing nodes/files, private POSIX
  state, loopback/TLS origin rules and bounded read-only HTTP. Worker keys must
  reach Core only; no redirects, ambient proxies or unpaid render. Flags/private
  state alone, signed-profile overrides and blind trust cannot declare support.
  Mocked inventories do not establish model fidelity or public qualification.
  Loader tests cover legacy list choices and ComfyUI V3 `COMBO.options`; missing
  options or a string default must not substitute for an installed weight.
  Distinct NVFP4 cases require all four actual component filenames in every
  selected recipe/source inventory; BF16 substitution and missing local NVFP4
  reject. The read-only HTTP path retains credential separation and defaults.
  POSIX state/HTTP startup cases run only on POSIX hosts. Schema parsing,
  configuration bypasses and explicit non-POSIX rejection remain covered on
  Windows; a skip never grants Windows video capability or ACL qualification.
  Transient read/network/deadline tests require bounded full-check retries with
  credentials confined to Core and no render or early capability. Bad inventory
  still rejects after transient recovery; cancellation cannot retry. These
  synthetic faults do not explain or reproduce every native startup timeout.
  Targeted inventory tests require only governed nodes, full weight/source
  coverage, strict response names and aggregate bounds. Invalid Core contracts
  cannot trigger local reads; there is no full-inventory fallback.
- `test_operator_rough_edges.py` - cold-start inventory/discovery, portable
  port-conflict diagnostics, install-relative defaults, advertised status, and
  advisory price-book aliases/fetch failures.
- `test_bridge_web.py` - legacy loopback bridge dashboard/settings rendering,
  same-origin and trusted-executable boundaries, URL/config validation, plus
  startup-failure supervision against the installed FastAPI/Starlette API.
- `test_capacity.py` - bounded schedule validation, local-time and overnight
  evaluation, plus the fail-closed one-job media protocol limit.
- `test_ws_worker.py` - WebSocket registration, dispatch, result, failure
  behavior, and runtime-health capability withdrawal. Batch regressions check
  authoritative `n` despite conflicting legacy `batch_size`, exact upload slots
  before rendering, and exact generated count before upload/signing; partial
  and extra outputs must not become a successful result.
  Recipe batches also prove independent arbitrary seeds, unique output prefixes,
  input preservation, and no uploads when a later render fails.
- `test_render_journal.py` - actual local SQLite multi-process submit-once
  guards, private state, bounds, immutable request/runtime identity, retained
  byte integrity, rename/commit crash recovery, expired/uncertain acceptance,
  exact-number graph normalization and native SaveVideo output. Ignore
  LoadVideo input previews without downloading or delivering them; retain
  fail-closed checks for missing, unknown, temporary and extra real outputs.
  Mocked HTTP and WS cases prove lost-reply/restart recovery and re-upload after PUT/DONE
  failure without a second render. These do not prove Core reconnect routing,
  R2 settlement, Windows ACLs or GPU fidelity. Default advertisement stays off;
  the separate opt-in startup test covers compatibility only.
  Expired already-armed work can recover one exact completed history without
  rebuilding, reposting or changing creation time. Unproven history reads once
  without download/polling; the real WS path refuses closed/unknown Core holds
  before inspecting or delivering even a completed native result.
- `test_render_retention.py` - strict Core-origin/schema/worker/ID binding,
  response bounds, no redirect/proxy credentials, refused preflight, bounded
  closed history, legacy closure migration, pending-file preservation and
  periodic lost-ACK recovery. A real child process exits after SQLite closure
  commits but before unlink; cleanup resumes safely. HTTP/Core/GPU remain
  fixtures, not joint paid qualification.
- `test_model_mapper.py` - model-reference resolution, capability inventory,
  and retired-capability advertisement guards.
- `test_worker_identity.py` - worker-key custody, payout-wallet delegation,
  fresh registration proofs, and job-receipt signing.
- `test_worker_profiles.py` - schema/signature fail-closed behavior and pinned
  ACE-Step profile commitments.
- `test_profile_release.py` - one-class private pilot and three-class public
  qualification, offline finalization, public RecipeVault-root binding,
  private-key permissions, and self-verifying Ed25519 output.
- `test_manager_release.py` - exact manager and benchmark-only qualification
  binary/SBOM payloads, aggregate checksums, signed public-profile gates,
  explicit no-enrollment restrictions, and tamper rejection.
- `test_hardware_profiles.py` - local detection, privacy-safe summaries, and
  authoritative minimum/midrange/datacenter qualification-class fixtures.
- `test_profile_installer.py` - resumable artifact download, constrained source
  archive extraction, commitment checks, and filesystem-containment behavior.
- `test_profile_canary.py` - real ACE-Step API lifecycle parsing and WAV quality gates.
- `test_profile_benchmark.py` - repeatable measurements, local resource sampling,
  and removal of exact hardware from shareable evidence.
- `test_audio_runtime.py` - local-only ACE-Step readiness, constrained job
  requests, output validation, and same-origin download enforcement.
- `test_runtime_process.py` - shell-free runtime launch specification,
  checkpoint binding, loopback enforcement, and low-VRAM offload policy.
- `test_manager_cli.py` - executable command parsing, local worker-key
  lifecycle, and unsigned-draft enrollment denial.
- `test_manager_web.py` - loopback manager session/origin controls, private
  status projection, bounded persistent capacity controls, parameter-free
  exact-worker Grid canary proxy, explicit media capability-state rendering,
  shell-free action commands, and log redaction.
- `test_enrollment.py` - TLS URL policy, zero-copy Console pairing, private file
  promotion, chain/audience binding, and ACK failure recovery without issuing a
  second credential.
- `test_profile_state.py` - digest/signature/canary authority required before advertisement.
- `test_workflow.py` - ComfyUI workflow mutation and parameter mapping.
- `test_loras.py` - recipe injection/source preservation, unsafe names/strengths,
  missing maps, local resolution, bounded/hash-verified safetensors downloads,
  atomic cleanup, redirect origins, and credential separation from CDN requests.
- `test_image_output.py` - PNG/WebP/JPEG conversion and fail-closed invalid data.
  WebSocket tests prove uploaded-byte receipts and loaded-LoRA reporting.
- `test_recipe_workflow.py` - Core-resolved recipe output materialization and
  cache-busting contracts, plus batch-size binding for SD/SD3/Flux2 latent
  nodes without modifying the supplied recipe.
  Independent image-output binding rejects missing, linked, ambiguous seed
  layouts and unsupported batch nodes rather than issuing misleading receipts.
  Timed-image tests cover exact slot/index correspondence, URL deduplication,
  graph preservation, invalid/unbound slots and streamed source-size limits.
- `test_source_media.py` - role/slot/unit/digest validation before downloads,
  source graph preservation, exact byte counts/hashes, R2-only reads, redirect
  denial and actual collision-renamed ComfyUI filenames. These mocked transfers
  are not joint GPU/R2 or paid lifecycle evidence.
  Appearance cases cover three distinct source roles, fixed profile units,
  refusal of opening-image reinterpretation and actual renamed PNG binding.
  Startup cases require the exact Ingredients capacity and complete source/node/
  weight inventory before adding its new feature; fixtures are not native proof.
  Core's optional `test_ltx25_appearance.py` handoff points
  `MEDIA_VIDEO_WORKER_SOURCE` at this reviewed checkout and uses this test
  `.venv` to run the real adapter against synthetic two/three-role Core plans.
  Mocked HTTP proves byte/slot compatibility, not GPU, live R2 or paid execution.
- `image_compare/` - manual/historical image metadata comparison fixtures.

## Local Contracts

- Tests must not contact production or use real Grid keys, R2 credentials,
  prompts, or payout wallets.
- Keep WebSocket tests authoritative for the supported `/v1/workers/ws` path.
- Use tiny synthetic fixtures and bound decoded/base64 data to avoid hiding
  memory-amplification failures.
- Image comparison fixtures are not private validator golden outputs or proof
  of model fidelity.

## Work Guidance

- Add a regression test for each dispatch schema, recipe, upload, reconnect,
  timeout, cancellation, and error-classification change.
- Keep ComfyUI execution itself behind an explicit integration-test boundary.

## Verification

- Run `pytest -q` from the repository root.
- For workflow/transport changes, also run a local ComfyUI job through the
  WebSocket path without production credentials.

## Child DOX Index

No child guides are currently required; this file owns `tests/`.

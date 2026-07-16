# Media Worker Tests

## Purpose

Pytest coverage for API compatibility helpers, workflow templating, utility
functions, preview handling, and the current WebSocket worker.

## Ownership

- `test_ws_worker.py` - WebSocket registration, dispatch, result, and failure
  behavior.
- `test_worker_identity.py` - worker-key custody, payout-wallet delegation,
  fresh registration proofs, and job-receipt signing.
- `test_worker_profiles.py` - schema/signature fail-closed behavior and pinned
  ACE-Step profile commitments.
- `test_profile_release.py` - one-class private pilot and three-class public
  qualification, offline finalization, public RecipeVault-root binding,
  private-key permissions, and self-verifying Ed25519 output.
- `test_hardware_profiles.py` - local detection, privacy-safe summaries, and
  minimum/midrange/datacenter recommendation fixtures.
- `test_profile_installer.py` - resumable artifact download, constrained source
  archive extraction, commitment checks, and filesystem-containment behavior.
- `test_profile_canary.py` - real ACE-Step API lifecycle parsing and WAV quality gates.
- `test_profile_benchmark.py` - repeatable measurements, local resource sampling,
  and removal of exact hardware from shareable evidence.
- `test_audio_runtime.py` - local-only ACE-Step readiness, constrained job
  requests, output validation, and same-origin download enforcement.
- `test_runtime_process.py` - shell-free runtime launch specification,
  checkpoint binding, loopback enforcement, and low-VRAM offload policy.
- `test_manager_cli.py` - executable command parsing and local worker-key lifecycle.
- `test_manager_web.py` - loopback manager session/origin controls, private
  status projection, shell-free action commands, and log redaction.
- `test_enrollment.py` - TLS URL policy, zero-copy Console pairing, private file
  promotion, chain/audience binding, and ACK failure recovery without issuing a
  second credential.
- `test_profile_state.py` - digest/signature/canary authority required before advertisement.
- `test_workflow.py` - ComfyUI workflow mutation and parameter mapping.
- `test_api_client*.py` - legacy/client response and preview handling.
- `image_compare/` - manual/historical image metadata comparison fixtures.

## Local Contracts

- Tests must not contact production or use real Grid keys, R2 credentials,
  prompts, or payout wallets.
- Keep WebSocket tests authoritative for the supported `/v1/workers/ws` path;
  legacy poll-client tests do not make `/v2` supported again.
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

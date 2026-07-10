# Media Worker Tests

## Purpose

Pytest coverage for API compatibility helpers, workflow templating, utility
functions, preview handling, and the current WebSocket worker.

## Ownership

- `test_ws_worker.py` - WebSocket registration, dispatch, result, and failure
  behavior.
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

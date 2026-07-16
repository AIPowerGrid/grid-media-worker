# Worker profiles

## Purpose

Defines the signed, declarative installation contract used to turn a host into a
verified media worker. Profiles describe compatibility, pinned artifacts,
runtime identity, governed recipe identity, and the capabilities unlocked by a
successful canary.

## Ownership

- `worker-profile-v1.schema.json` - strict JSON Schema for signed envelopes.
- `ace-step-v1.profile.json` - pinned ACE-Step 1.5 launch profile and constrained
  local API recipe. It remains a draft until it is signed with a production
  release key and its Grid audio recipe is accepted.
- `profile.py` - schema loading, canonicalization, runtime-digest derivation,
  and Ed25519 verification.
- `hardware.py` - private local hardware inventory and profile recommendation.
- `installer.py` - resumable, hash-verified HTTP/Hugging Face downloads,
  constrained source-archive extraction, offline source/model-tree
  revalidation, and pinned Git checkouts with destination containment.
- `state.py` - privacy-safe install/canary state and authoritative capability gate.
- `canary.py` - deterministic ACE-Step API generation and WAV validation.
- `benchmark.py` - repeatable canary measurements with separate private and
  privacy-safe report formats.
- `qualification.py` - offline recomputation of minimum, midrange, and
  datacenter benchmark evidence plus a privacy-safe manifest commitment.
- `release.py` - offline final-profile promotion, RecipeVault-root binding, and
  qualification binding and Ed25519 signing; it is not imported by the public
  manager runtime.
- `advertisement.py` - signed state to coarse WebSocket capability metadata.

## Local Contracts

- Qualification scope is explicit. `pilot` means one exact, privately operated
  hardware class and may not be published by release CI. `public` requires the
  minimum/midrange/datacenter matrix plus a RecipeVault provenance root.

- Remote profiles are untrusted until both schema validation and signature
  verification pass. Draft/unsigned profiles are development-only and require
  an explicit caller override.
- Profiles are declarative. Never add shell strings or general-purpose script
  execution to the schema.
- Do not promote upstream workflows that call hosted generation services. The
  ACE-Step profile executes only the pinned local loopback runtime.
- Artifact URLs and revisions are immutable; model weights carry SHA-256 and
  byte-size commitments. The runtime digest binds the source revision,
  dependency lock, model snapshot, and recipe. Never use `latest`, a floating
  branch, or an unpinned container tag.
- A per-file artifact `source` override is only for a runtime-effective file
  that must differ from the model repository snapshot. It must be immutable
  HTTPS content with an exact byte size and SHA-256 commitment, and its
  destination must remain inside the declared model tree.
- Managed runtime launch is offline for model hubs. Revalidate the retained
  source archive and exact checkpoint tree before canary, benchmark, or serving;
  uncommitted files and post-install drift fail closed.
- Full hardware inventory stays local. Only a coarse capability tier and
  measured canary performance may be advertised after validation.
- A profile's advertised capabilities are unavailable until its canary passes.
- An active release requires distinct private reports for every hardware class
  declared by its qualification policy; only their privacy-safe manifest hash
  is bound into the signed profile.
- A managed profile also requires the payout wallet to delegate to a local,
  funds-less worker signer. The payout private key must never be copied to the
  worker host; see `bridge/identity.py`.

## Verification

- `pytest -q tests/test_worker_profiles.py tests/test_hardware_profiles.py tests/test_profile_installer.py tests/test_profile_canary.py tests/test_profile_state.py`
- `python -m bridge.manager_cli --allow-unsigned-draft inspect`

## Child DOX Index

None - leaf.

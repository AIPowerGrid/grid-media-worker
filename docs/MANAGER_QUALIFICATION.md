# Media Manager Qualification Cohort

The first public media-manager profile serves ACE-Step 1.5 XL Turbo audio. Its
release stays closed until the exact profile is exercised three times in each
hardware class and its recipe commitment is registered in RecipeVault.

This is release testing, not paid Grid work. It does not create rewards, den,
stake, or a promise of future earnings.

## Hardware Needed

The manager derives the class from the signed profile and the selected GPU. For
the current draft profile, the target matrix is:

| Class | Selected GPU VRAM | Other requirements |
| --- | ---: | --- |
| `minimum` | 12 GB through less than 20 GB | At least 32 GB RAM, 48 GB free disk, supported NVIDIA driver |
| `midrange` | 20 GB through less than 80 GB | At least 64 GB RAM, 64 GB free disk, recommended NVIDIA driver |
| `datacenter` | 80 GB or more | At least 64 GB RAM, 64 GB free disk, recommended NVIDIA driver |

The table is orientation only. `grid-media-manager recommend` is authoritative;
it evaluates the bundled profile, operating system, driver, CUDA compatibility,
RAM, disk, and the exact selected GPU. A host that does not map to a class cannot
produce release evidence.

## Check A Candidate

Use the provenance-attested qualification prerelease when one is available. It
is benchmark-only: it cannot enroll with the Grid or advertise capabilities.
Download the binary and `SHA256SUMS` from the same exact
`manager-qualification-v*` release, verify both checksum and GitHub provenance,
then run:

```bash
sha256sum --check --ignore-missing SHA256SUMS
gh attestation verify grid-media-manager-linux-x86_64 \
  --repo AIPowerGrid/grid-media-worker
chmod +x grid-media-manager-linux-x86_64
./grid-media-manager-linux-x86_64 \
  --allow-unsigned-draft recommend --gpu GPU-REPLACE-WITH-NVIDIA-UUID
```

Windows operators use `grid-media-manager-windows-x86_64.exe` and verify its
line in `SHA256SUMS` with a local SHA-256 tool plus the same `gh attestation
verify` command.

When no qualification prerelease exists, use a clean checkout of `main`:

```bash
uv sync --frozen --extra test --extra release --python 3.12
uv run --frozen python -m bridge.manager_cli \
  --allow-unsigned-draft recommend --gpu GPU-REPLACE-WITH-NVIDIA-UUID
```

The JSON output must report `supported` or `recommended`, a non-null
`qualification_class`, and the expected run count. Do not assign the class by
hand.

## Produce Evidence

Use a dedicated state file for this profile and GPU. Installation is resumable
and verifies every pinned runtime and model artifact before execution.

```bash
CLASS=minimum # use exactly the class returned by recommend
GPU=GPU-REPLACE-WITH-NVIDIA-UUID
ROOT="$HOME/.aipg/media-worker-qualification"
MANAGER="uv run --frozen python -m bridge.manager_cli"

$MANAGER \
  --allow-unsigned-draft install \
  --install-root "$ROOT/install" \
  --state "$ROOT/state-$CLASS.json" \
  --gpu "$GPU"

$MANAGER \
  --allow-unsigned-draft benchmark \
  --install-root "$ROOT/install" \
  --state "$ROOT/state-$CLASS.json" \
  --runs 3 \
  --out "$ROOT/$CLASS-private.json" \
  --public-out "$ROOT/$CLASS-public.json"
```

For a downloaded Linux qualification binary, set
`MANAGER=./grid-media-manager-linux-x86_64` instead. On Windows, invoke the
`.exe` directly with the same arguments.

The benchmark launches the pinned local runtime, runs the profile canary three
times, samples GPU and host memory, validates the output, and shuts the runtime
down. It does not connect the candidate to the Grid.

## Submit Safely

1. Open the **Media manager qualification** issue form in this repository.
2. Attach only the generated `*-public.json` report.
3. Keep `*-private.json` local. It contains the exact GPU inventory and UUID.
4. A maintainer will review the public profile commitments and arrange a private,
   encrypted transfer for the private report if the class is still needed.

Never paste worker API keys, payout-wallet signatures, private benchmark reports,
release keys, environment files, or service logs into an issue or chat.

## Maintainer Acceptance

Before a report can contribute to the release, maintainers must verify that:

- it matches the current draft profile ID, version, profile digest, runtime
  digest, and recipe root;
- the manager-derived class matches the class claimed by the operator;
- all three canaries and resource samples pass the offline verifier;
- the three required reports come from distinct hardware-class runs;
- the exact recipe SHA-256 is confirmed in the Base mainnet RecipeVault;
- the final profile is signed offline and the public key is reviewed before it
  is added to the manager bundle.

Only then may a `manager-v*` tag assemble a draft release. Platform signing,
supervised staging, checksums, SPDX SBOM verification, and provenance review are
still required before that draft is published.

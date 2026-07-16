# Grid Media Worker

Run image, video, 3D, and managed audio generation for AI Power Grid. The
current production path connects an existing ComfyUI installation. The ACE-Step
audio manager is a signed-profile release candidate and is not approved on the
live Grid yet.

## Runtime Paths

| Path | Status | Runtime |
|---|---|---|
| ComfyUI image/video/3D | Current | Operator-managed ComfyUI workflows |
| ACE-Step 1.5 audio | Draft | Manager-installed, pinned local API runtime |
| Legacy `/v2` polling | Retired | Do not use |

Both current paths use `/v1/workers/ws` for push dispatch and upload outputs
through short-lived, presigned URLs. Workers never receive Grid storage keys.

## ComfyUI Worker

Requirements: Python 3.9+, a running ComfyUI instance, and a Grid API key from
[the developer console](https://console.aipowergrid.io/dashboard/api-key).

```bash
git clone https://github.com/AIPowerGrid/grid-media-worker.git
cd grid-media-worker
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Set at least:

```ini
GRID_API_KEY=your-grid-api-key
GRID_WORKER_NAME=your-worker-name
GRID_API_URL=https://api.aipowergrid.io
GRID_WS=true
COMFYUI_URL=http://127.0.0.1:8188
```

Start ComfyUI, then run:

```bash
comfy-bridge
```

The bridge advertises only models it can resolve and serve. `GRID_MODEL` can
restrict that list, but it cannot make a missing workflow or checkpoint valid.

## ACE-Step Worker Profile V1

The standalone `grid-media-manager` detects hardware locally, installs exact
artifacts, launches the loopback-only ACE-Step API, runs an audio canary, and
starts the Grid worker. Its embedded `uv` installs the profile's Python runtime;
the pinned ACE-Step source is a hash-verified archive, so system Git is not
required.

The bundled profile commits to:

- supported OS, architecture, NVIDIA driver, VRAM, RAM, and disk tiers;
- the ACE-Step source commit, exact managed Python 3.12.13 runtime, and
  `uv.lock` hash;
- every model file's revision, size, and SHA-256;
- a derived runtime digest over source, lock, model files, and recipe;
- a pinned upstream VRAM-auto resource policy, with language-model loading
  forced off for this DiT-only profile;
- a constrained DiT-only local audio request template and recipe root;
- capabilities that remain unavailable until a signed profile passes canary.

The conservative driver gate follows NVIDIA's
[CUDA 12.8 toolkit floor](https://docs.nvidia.com/cuda/archive/12.8.0/cuda-toolkit-release-notes/index.html):
Linux 570.26+ or Windows 570.65+. Older 525/528 drivers can provide broad CUDA
12.x minor-version compatibility, but this profile does not claim that fallback
until ACE-Step is qualified on it.

V1 forces every language-model-assisted request mode off. ACE-Step's upstream
presence check nevertheless requires its default 1.7B LM files before loading
the DiT, so the complete immutable 28-file runtime model tree is pinned rather
than allowing an automatic runtime download. Twenty-six files come from the
exact model revision; two Python model definitions come from the exact ACE-Step
source commit because the runtime overlays those definitions before loading.
Every file still carries an exact size and SHA-256 commitment. The tree is about
10.09 GB (9.40 GiB), while the pinned Linux dependency environment measures
about 8.28 GB allocated. A complete install is therefore about 17.2 GiB before
operating headroom. V1 requires 24 GiB free, keeps managed Python and the `uv`
cache under the install root, and retains a 32 GiB recommended floor. The
runtime forces Hugging Face and Transformers offline and revalidates the source
and exact model tree before serving.

The checked-in profile is deliberately `draft` and unsigned. These commands are
for development evaluation only; an unsigned profile can install and test, but
cannot advertise or serve managed capabilities:

```bash
grid-media-manager --allow-unsigned-draft inspect
grid-media-manager --allow-unsigned-draft recommend
grid-media-manager --allow-unsigned-draft install
grid-media-manager --allow-unsigned-draft canary --launch-runtime
```

On a multi-GPU host, select the card by index or NVIDIA UUID during install;
the private install state binds future canary, benchmark, and serving processes
to that exact device:

```bash
grid-media-manager --allow-unsigned-draft recommend --gpu 0
grid-media-manager --allow-unsigned-draft install --gpu 0
```

Release qualification uses a repeatable three-run benchmark. The local report
contains exact hardware diagnostics and is written with private permissions;
the optional shareable report contains only profile commitments, the coarse
tier, and measured performance:

```bash
grid-media-manager --allow-unsigned-draft benchmark \
  --runs 3 \
  --public-out ~/.aipg/media-worker/benchmark-public.json
```

For release evidence, use a separate state and report name on each selected
GPU. The install root may be shared: verified artifacts resume instead of being
downloaded again.

```bash
CLASS=midrange
GPU=GPU-REPLACE-WITH-NVIDIA-UUID
ROOT="$HOME/.aipg/media-worker"

grid-media-manager --allow-unsigned-draft install \
  --install-root "$ROOT" \
  --state "$ROOT/state-$CLASS.json" \
  --gpu "$GPU"
grid-media-manager --allow-unsigned-draft benchmark \
  --install-root "$ROOT" \
  --state "$ROOT/state-$CLASS.json" \
  --runs 3 \
  --out "$ROOT/$CLASS-private.json" \
  --public-out "$ROOT/$CLASS-public.json"
```

Use `CLASS=minimum`, `midrange`, or `datacenter` only for the matching release
machine. The offline signer recomputes the class from the private report and
rejects labels that do not match the selected GPU and profile recommendation.

After benchmark approval and the hardware-wallet RecipeVault transaction, use
the separate offline signer. It is intentionally not bundled into the manager:

```bash
openssl genpkey -algorithm ED25519 -aes-256-cbc \
  -out /offline/worker-profile-release.pem
chmod 600 /offline/worker-profile-release.pem
python profile_release.py \
  --profile bridge/profiles/ace-step-v1.profile.json \
  --private-key /offline/worker-profile-release.pem \
  --ask-pass \
  --key-id worker-profile-2026-01 \
  --qualification minimum=/offline/reports/minimum-private.json \
  --qualification midrange=/offline/reports/midrange-private.json \
  --qualification datacenter=/offline/reports/datacenter-private.json \
  --recipe-vault-root 0xRECIPE_ROOT \
  --out /offline/ace-step-v1.active.json
```

The signer recomputes every private report against the draft policy, requires
three successful runs on distinct minimum, midrange, and datacenter hardware,
and writes a privacy-safe `.qualification.json` sidecar. Only the canonical
sidecar hash is embedded in the signed profile; GPU names, UUIDs, RAM, and disk
inventory remain offline.

Review the emitted public key and profile digest before adding only that public
key to `trusted-keys.json`. Never place the release private key in this repo, a
CI secret, the manager bundle, or a worker host.

An active release will use the same commands without
`--allow-unsigned-draft`. The primary operator path is one command:

```bash
grid-media-manager
```

With no arguments, the executable opens the local Worker Manager at
`http://127.0.0.1:8791`. The UI shows release verification, the selected GPU,
install and canary state, payout-wallet delegation, and redacted process logs.
Its setup button runs the same fail-closed CLI lifecycle described below; the UI
cannot enable an unsigned profile or advertise a capability before validation.
It uses a one-time local browser session, requires exact same-origin JSON for
controls, and rejects non-loopback binds.

The equivalent terminal command is:

```bash
grid-media-manager setup
```

`setup` recommends and binds one GPU, resumes verified installation with
throttled download progress on stderr, launches
ACE-Step, benchmarks the canary, opens wallet pairing, and then enters the Grid
worker loop. With no `--worker-name`, it derives a stable unique name from the
funds-less rig signer. On a multi-GPU machine, pass `--gpu 0` or a displayed
NVIDIA UUID; otherwise the manager selects the supported card with the most
VRAM. `--exit-after-setup` performs every step except entering the long-running
worker loop.

The separate `inspect`, `recommend`, `install`, `canary`, `benchmark`,
`connect`, and `serve` commands remain available for qualification, diagnostics,
and recovery.

## Worker Identity

Each rig uses a funds-less secp256k1 worker key. The payout wallet signs a
time-bounded delegation to that key; the payout private key never belongs on
the worker host. Core resolves the actual payout wallet from the API-key
account, verifies the wallet delegation, consumes a one-use registration nonce,
and then accepts job receipts from the delegated signer.

The primary `setup` command performs pairing automatically. To pair or rotate a
rig separately without copying keys or signatures:

```bash
grid-media-manager connect --worker-name my-audio-rig
```

The manager creates its worker signer and final worker-only API credential
locally, opens a short-lived Console link, and waits. Sign in with Google or a
wallet, connect the payout wallet, review the rig identity, and sign the exact
delegation message. Core stores only the API-key hash and grants only
`worker.connect`; it cannot return the plaintext credential to the browser.
Rerun `connect` with `--restart` to rotate a rig connection. Core activates the
new credential only after the manager verifies and ACKs it, then revokes the
prior credential for that account and rig name.

The manual identity commands below are the offline/recovery path:

```bash
grid-media-manager identity generate
grid-media-manager identity request --payout-wallet 0xYOUR_WALLET
grid-media-manager identity install \
  --request ~/.aipg/media-worker/delegation-request.json \
  --signature 0xWALLET_SIGNATURE
grid-media-manager identity show
```

Never paste a payout-wallet private key into this manager. The payout wallet
signs in its own wallet UI and remains separate from the funds-less rig key.

## Security Boundaries

- Managed ACE-Step accepts only a loopback runtime URL; hosted generation is rejected.
- The ACE-Step subprocess receives a narrow environment without Grid or cloud
  credentials. It still runs as the operator's OS user; signed profiles authorize
  pinned code and artifacts, but do not provide an OS/container sandbox.
- Artifact paths are contained and downloads are size/hash verified before promotion.
- Multi-GPU recommendations are bound by NVIDIA UUID to the runtime process; an
  inherited `CUDA_VISIBLE_DEVICES` cannot silently select another card.
- Profile signatures protect release policy for honest operators; they are not proof
  that an adversarial worker executed the claimed model.
- Media output hashes are signed provenance receipts, not fidelity proofs. Validators
  must fetch/sample/re-execute outputs before quality evidence affects economics.
- Core audio charging remains dark until real hardware benchmarks set the price peg.

## Development

```bash
pip install -e '.[test]'
pytest -q
```

Build and smoke-test the standalone manager:

```bash
uv sync --frozen --extra test --extra release
uv run --frozen --extra release pyinstaller --clean --noconfirm grid-media-manager.spec
./dist/grid-media-manager --help
./dist/grid-media-manager --allow-unsigned-draft inspect
```

The V1 release workflow builds Linux x86_64 and Windows x86_64 artifacts with
checksums and GitHub provenance attestations, matching the NVIDIA profile.
Linux V1 requires Ubuntu 22.04 or another x86_64 distribution with glibc 2.35
or newer. The runtime bundle excludes Tk and the offline profile-signing and
qualification tools. A `manager-v*` tag can assemble only a draft release, and
it fails unless the bundled profile is active, signature-verified,
RecipeVault-bound, and qualified on the required hardware classes. The draft
includes `manager-release.json` so `aipowergrid.io/run` can eventually consume
one reviewed platform/download contract. Public Windows distribution still
requires code signing before that draft may be promoted. Apple/MPS needs its
own measured profile rather than inheriting NVIDIA assumptions.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

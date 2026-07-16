import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # The local setup UI is private by default. Operators who place it behind
    # their own authenticated proxy may opt into a wider bind explicitly.
    BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1").strip()
    BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "7860"))
    GRID_API_KEY = os.getenv("GRID_API_KEY", "")
    _GRID_MODELS_RAW = os.getenv("GRID_MODEL", "")
    GRID_MODELS = [m.strip() for m in _GRID_MODELS_RAW.split(",") if m.strip()]
    GRID_WORKER_NAME = os.getenv("GRID_WORKER_NAME", "ComfyUI-Bridge-Worker")
    COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
    # Modalities this worker advertises. Default image+video; a TRELLIS box sets
    # GRID_JOB_TYPES=3d (and GRID_MODEL=TRELLIS2). The grid routes by (model, job_type).
    _GRID_JOB_TYPES_RAW = os.getenv("GRID_JOB_TYPES", "image,video")
    GRID_JOB_TYPES = [t.strip() for t in _GRID_JOB_TYPES_RAW.split(",") if t.strip()]
    # Recipe-served models (e.g. TRELLIS2) have no local workflow/checkpoint — the
    # grid sends the graph (recipe_spec). Set GRID_TRUST_MODELS=true to advertise
    # GRID_MODEL verbatim, skipping the local-workflow servability gate.
    GRID_TRUST_MODELS = os.getenv("GRID_TRUST_MODELS", "false").lower() in ("1", "true", "yes")
    # Preflight: before advertising a model, fetch its recipe from the grid, check the
    # nodes+files are present in ComfyUI, and SMOKE-RUN it. Advertise only what actually
    # runs E2E. Supersedes GRID_TRUST_MODELS (earned trust vs blind trust).
    GRID_PREFLIGHT = os.getenv("GRID_PREFLIGHT", "false").lower() in ("1", "true", "yes")
    # Clean base (no /api) — the new prod nginx serves /v2 directly and the
    # WS worker derives /v1/workers/ws from this. A legacy /api tail is
    # tolerated and stripped where needed.
    GRID_API_URL = os.getenv("GRID_API_URL", "https://api.aipowergrid.io")
    # WS endpoint: auto-derived api.*->ws.* (DNS-only host that bypasses
    # Cloudflare's WS resets). Override with GRID_STREAMING_URL. That endpoint
    # serves the Cloudflare Origin cert, verified via the bundled CF Origin CA
    # (certs/cloudflare_origin_root.pem) — no Let's Encrypt. Optional overrides:
    GRID_STREAMING_URL = os.getenv("GRID_STREAMING_URL", "")
    GRID_WS_CA = os.getenv("GRID_WS_CA", "")
    GRID_WS_INSECURE = os.getenv("GRID_WS_INSECURE", "false").lower() in ("1", "true", "yes")
    # v2 WebSocket worker protocol (push dispatch + presigned R2 uploads).
    # Off by default until the v2 API is the production default deployment.
    GRID_WS = os.getenv("GRID_WS", "false").lower() == "true"
    # Optional signed managed profile. When set, models/job types come only from
    # matching signed install state whose canary passed; GRID_MODEL/JOB_TYPES do
    # not override it. The profile path is intentionally opt-in while V1 is draft.
    GRID_PROFILE_PATH = os.getenv("GRID_PROFILE_PATH", "").strip()
    GRID_PROFILE_STATE_PATH = os.getenv(
        "GRID_PROFILE_STATE_PATH",
        os.path.expanduser("~/.aipg/media-worker/profile-state.json"),
    ).strip()
    GRID_WORKER_KEY_PATH = os.getenv(
        "GRID_WORKER_KEY_PATH",
        os.path.expanduser("~/.aipg/media-worker/worker-key.json"),
    ).strip()
    GRID_WORKER_DELEGATION_PATH = os.getenv(
        "GRID_WORKER_DELEGATION_PATH",
        os.path.expanduser("~/.aipg/media-worker/delegation.json"),
    ).strip()
    GRID_WORKER_IDENTITY_AUDIENCE = os.getenv(
        "GRID_WORKER_IDENTITY_AUDIENCE", "api.aipowergrid.io"
    ).strip().lower()
    GRID_WORKER_IDENTITY_CHAIN_ID = int(os.getenv("GRID_WORKER_IDENTITY_CHAIN_ID", "8453"))
    ACE_STEP_API_URL = os.getenv("ACE_STEP_API_URL", "http://127.0.0.1:8001").strip()
    ACE_STEP_API_KEY = os.getenv("ACE_STEP_API_KEY", "").strip()
    # Keep this below Core's worker receive and client response deadlines.
    ACE_STEP_JOB_TIMEOUT = int(os.getenv("ACE_STEP_JOB_TIMEOUT", "1800"))
    NSFW = os.getenv("GRID_NSFW", "false").lower() == "true"
    THREADS = int(os.getenv("GRID_THREADS", "1"))
    MAX_PIXELS = int(os.getenv("GRID_MAX_PIXELS", "20971520"))
    WORKFLOW_DIR = os.getenv("WORKFLOW_DIR", os.path.join(os.getcwd(), "workflows"))
    WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", None)
    GRID_IMAGE_MODEL_REFERENCE_REPOSITORY_PATH = os.getenv("GRID_IMAGE_MODEL_REFERENCE_REPOSITORY_PATH")
    BATCH_SIZE = int(os.getenv("GRID_BATCH_SIZE", "4"))  # Native ComfyUI batch size

    @classmethod
    def validate(cls):
        if not cls.GRID_API_KEY:
            raise RuntimeError("GRID_API_KEY environment variable is required.")

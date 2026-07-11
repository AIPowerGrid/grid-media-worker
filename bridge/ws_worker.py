"""Grid v2 WebSocket worker — the unified worker protocol.

Replaces the legacy poll loop (/v2/generate/pop → submit) with a persistent
WebSocket to the grid API:

  register  {apikey, name, models[], job_types:["image","video"], bridge_agent}
  ← job     {id, job_type, model, payload, upload:[{put_url, key, content_type}]}
  → progress{id, pct, preview_b64?}
  → done    {id, results:[{index, seed, sha256}]}
  ← ack     {id, den}

Outputs upload directly to R2 via the presigned PUT slots in the job message —
this worker never holds storage credentials. Enable with GRID_WS=true until
the v2 API is the default deployment, after which this becomes the default.
"""

import asyncio
import base64
import hashlib
import json
import os
import ssl
import logging
import random
import time

import httpx

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

from .bridge import _view_url
from .config import Settings
from .model_mapper import initialize_model_mapper, get_horde_models
try:
    from .model_mapper import is_servable
except ImportError:  # older worker forks lack the servability gate — advertise as-is
    def is_servable(_m):
        return (True, "")
from .workflow import build_workflow

logger = logging.getLogger(__name__)

BRIDGE_AGENT = "comfy-bridge/ws:1"
RECONNECT_DELAY_S = 5
PROGRESS_INTERVAL = 2.0
PREVIEW_INTERVAL = 1.5
MAX_SEED = 2**53 - 1

# 3D mesh outputs: TRELLIS's Trellis2ExportTrimesh writes the file to ComfyUI's
# output dir and returns the path as a String output — it registers NOTHING in
# /history outputs (unlike SaveImage/VHS). So for job_type=3d the worker reads the
# newest mesh file from the output dir. Requires COMFYUI_OUTPUT_DIR (the bridge is
# colocated with its ComfyUI). Jobs are serialized per worker, so "newest since
# the prompt started" is unambiguous.
_MESH_EXTS = (".glb", ".gltf", ".ply", ".obj", ".stl", ".3mf")
COMFYUI_OUTPUT_DIR = os.getenv("COMFYUI_OUTPUT_DIR", "").strip()


def grid_ws_url() -> str:
    """Derive the worker WS URL from GRID_API_URL (handles legacy /api tails).

    Auto-maps an `api.*` host to `ws.*`: the public grid serves the persistent
    worker WebSocket on a DNS-only `ws.` host (bypasses Cloudflare, which resets
    long-lived WS). Zero operator config; GRID_STREAMING_URL overrides.
    """
    base = getattr(Settings, "GRID_STREAMING_URL", "") or Settings.GRID_API_URL
    if not getattr(Settings, "GRID_STREAMING_URL", ""):
        for scheme in ("https://", "http://"):
            if base.startswith(scheme + "api."):
                base = scheme + "ws." + base[len(scheme) + 4:]
                break
    url = base.rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    url = url.replace("https://", "wss://").replace("http://", "ws://")
    return f"{url}/v1/workers/ws"


def grid_ws_ssl(url: str):
    """SSL context for the worker WS: trust system CAs + the bundled Cloudflare
    Origin CA so the DNS-only ws.* endpoint verifies without Let's Encrypt.
    None for plain ws://. GRID_WS_CA overrides; GRID_WS_INSECURE disables verify."""
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    ca = getattr(Settings, "GRID_WS_CA", "") or os.path.join(
        os.path.dirname(__file__), "certs", "cloudflare_origin_root.pem"
    )
    try:
        if ca and os.path.exists(ca):
            ctx.load_verify_locations(ca)
    except Exception as e:
        logger.warning(f"could not load WS CA '{ca}': {e}")
    if getattr(Settings, "GRID_WS_INSECURE", False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logger.warning("GRID_WS_INSECURE=1 — WS certificate verification DISABLED")
    return ctx


def _coerce_seed(value):
    if value is None or value == "":
        return None
    seed = int(value)
    if seed < 0 or seed > MAX_SEED:
        raise ValueError(f"seed must be between 0 and {MAX_SEED}")
    return seed


def resolve_output_seeds(payload: dict, n: int) -> list[int]:
    """Preserve grid/client seeds; only randomize when the grid omitted them."""
    count = max(int(n or 1), 1)
    provided = payload.get("seeds")
    if isinstance(provided, list) and len(provided) >= count:
        seeds = [_coerce_seed(v) for v in provided[:count]]
        if all(v is not None for v in seeds):
            return [int(v) for v in seeds]

    base = _coerce_seed(payload.get("seed"))
    if base is not None:
        return [(base + i) % (MAX_SEED + 1) for i in range(count)]

    return [random.randint(0, MAX_SEED) for _ in range(count)]


class WSWorker:
    def __init__(self):
        self.comfy = httpx.AsyncClient(base_url=Settings.COMFYUI_URL, timeout=300)
        self.models: list[str] = []

    async def run(self):
        if websockets is None:
            raise RuntimeError("websockets package required for GRID_WS mode")
        await initialize_model_mapper(Settings.COMFYUI_URL)
        # Advertise-only-what-you-can-serve gate. Applies to an explicit
        # GRID_MODEL override too — a worker must never advertise a model whose
        # workflow is missing or whose weights aren't loaded in ComfyUI (that's
        # what made this box advertise LTX-2.3 and 502 every job).
        candidates = Settings.GRID_MODELS or get_horde_models()
        self.models = []
        for m in candidates:
            if Settings.GRID_TRUST_MODELS:
                self.models.append(m)  # recipe-served: grid supplies the graph, no local workflow
                continue
            ok, reason = is_servable(m)
            if ok:
                self.models.append(m)
            else:
                logger.warning(f"Refusing to advertise '{m}': {reason}")
        if not self.models:
            raise RuntimeError(
                "No servable models — every candidate is missing its workflow or "
                "ComfyUI weights. Install the model files (and a mapped workflow), "
                "then restart. Candidates were: %s" % candidates
            )
        logger.info(f"WS worker advertising servable models: {self.models}")

        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"WS session ended: {e} — reconnecting in {RECONNECT_DELAY_S}s")
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def _session(self):
        url = grid_ws_url()
        logger.info(f"Connecting to {url} ...")
        async with websockets.connect(url, ssl=grid_ws_ssl(url), ping_interval=30, ping_timeout=10) as ws:
            await ws.send(json.dumps({
                "apikey": Settings.GRID_API_KEY,
                "name": Settings.GRID_WORKER_NAME,
                "models": self.models,
                "job_types": Settings.GRID_JOB_TYPES,
                "bridge_agent": BRIDGE_AGENT,
            }))
            ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if ready.get("type") != "ready":
                raise RuntimeError(f"Registration rejected: {ready}")
            logger.info(f"Registered as worker {ready.get('worker_id')}")

            while True:
                msg = json.loads(await ws.recv())
                mtype = msg.get("type")
                if mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif mtype == "job":
                    await self._handle_job(ws, msg)
                elif mtype == "ack":
                    logger.info(f"Job {msg.get('id')} acked, den={msg.get('den')}")
                elif mtype == "error":
                    logger.error(f"Server error: {msg.get('message')}")

    # ── Job handling ──────────────────────────────────────────────────

    async def _handle_job(self, ws, msg: dict):
        job_id = msg["id"]
        payload = dict(msg.get("payload", {}))
        upload_slots = msg.get("upload", [])
        n = int(payload.get("n", 1) or 1)
        logger.info(f"Job {job_id}: {msg.get('job_type')} model={msg['model']} n={n}")

        try:
            await self._generate_and_upload(ws, msg, payload, upload_slots, n)
        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            await ws.send(json.dumps({
                "type": "error", "id": job_id, "message": str(e)[:300],
            }))

    async def _generate_and_upload(self, ws, msg, payload, upload_slots, n):
        job_id = msg["id"]

        # Adapt the v2 payload to the shape build_workflow expects. The grid is
        # the seed authority; preserve provided seeds and randomize only as a
        # defensive fallback for older cores.
        seeds = resolve_output_seeds(payload, n)
        payload.setdefault("batch_size", n)
        payload["seeds"] = seeds
        payload["seed"] = seeds[0]
        bridge_job = {"id": job_id, "model": msg["model"], "payload": payload}

        job_type = msg.get("job_type", "image")
        started_at = time.time()
        workflow = await build_workflow(bridge_job)
        resp = await self.comfy.post("/prompt", json={"prompt": workflow})
        if resp.status_code != 200:
            raise RuntimeError(f"ComfyUI rejected workflow: {resp.text[:200]}")
        prompt_id = resp.json().get("prompt_id")
        if not prompt_id:
            raise RuntimeError("No prompt_id from ComfyUI")

        progress_task = asyncio.create_task(self._relay_progress(ws, job_id, prompt_id))
        try:
            media_items = await self._collect_outputs(prompt_id, job_type, started_at)
        finally:
            progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass

        # Upload each output to its presigned slot, hash for the receipt.
        results = []
        async with httpx.AsyncClient(timeout=120) as client:
            for i, (media_bytes, media_type, filename) in enumerate(media_items):
                if i >= len(upload_slots):
                    logger.warning(f"More outputs than upload slots ({len(media_items)} > {len(upload_slots)}); dropping extras")
                    break
                slot = upload_slots[i]
                r = await client.put(
                    slot["put_url"], content=media_bytes,
                    headers={"Content-Type": slot["content_type"]},
                )
                r.raise_for_status()
                results.append({
                    "index": i,
                    "seed": int(seeds[i] if i < len(seeds) else seeds[0]),
                    "sha256": hashlib.sha256(media_bytes).hexdigest(),
                })
                logger.info(f"Uploaded output {i + 1}/{len(media_items)} ({len(media_bytes)} bytes)")

        if not results:
            raise RuntimeError("Generation produced no outputs")

        await ws.send(json.dumps({"type": "done", "id": job_id, "results": results}))

    async def _collect_outputs(self, prompt_id: str, job_type: str = "image", started_at: float = 0.0):
        """Poll ComfyUI history until the prompt finishes; return its outputs.

        Image/video outputs are registered in /history and fetched via /view. 3D
        mesh outputs are NOT in /history (TRELLIS's export node only writes to disk),
        so for job_type=3d we read the newest mesh file from COMFYUI_OUTPUT_DIR once
        the prompt completes."""
        media_items = []
        while True:
            hist = await self.comfy.get(f"/history/{prompt_id}")
            hist.raise_for_status()
            data = hist.json().get(prompt_id, {})
            outputs = data.get("outputs", {})
            # image/video: registered in history
            for node_data in outputs.values():
                for video_info in node_data.get("videos", []):
                    r = await self.comfy.get(_view_url(video_info))
                    r.raise_for_status()
                    media_items.append((r.content, "video", video_info["filename"]))
                for img_info in node_data.get("images", []):
                    r = await self.comfy.get(_view_url(img_info))
                    r.raise_for_status()
                    media_items.append((r.content, "image", img_info["filename"]))
            if media_items:
                return media_items
            # 3D: history carries no mesh; once the prompt is DONE, read from disk
            status_done = bool(data.get("status", {}).get("completed")) or bool(outputs)
            if job_type == "3d" and status_done:
                mesh = self._read_newest_mesh(started_at)
                if mesh:
                    return [mesh]
                raise RuntimeError(
                    "3D job finished but no mesh found in COMFYUI_OUTPUT_DIR "
                    f"({COMFYUI_OUTPUT_DIR or 'unset!'})")
            await asyncio.sleep(1)

    def _read_newest_mesh(self, since_ts: float):
        """Newest mesh file written to the ComfyUI output dir since the job started."""
        if not COMFYUI_OUTPUT_DIR or not os.path.isdir(COMFYUI_OUTPUT_DIR):
            return None
        newest, newest_mtime = None, since_ts - 2  # small slack for clock skew
        for root, _dirs, files in os.walk(COMFYUI_OUTPUT_DIR):
            for fn in files:
                if not fn.lower().endswith(_MESH_EXTS):
                    continue
                p = os.path.join(root, fn)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m >= newest_mtime:
                    newest, newest_mtime = p, m
        if not newest:
            return None
        with open(newest, "rb") as f:
            data = f.read()
        return (data, "3d", os.path.basename(newest))

    async def _relay_progress(self, ws, job_id: str, prompt_id: str):
        """Forward ComfyUI progress + preview frames as v2 progress messages."""
        if websockets is None:
            return
        comfy_ws = Settings.COMFYUI_URL.replace("http://", "ws://").replace("https://", "wss://")
        comfy_ws = f"{comfy_ws}/ws?clientId={prompt_id}"
        last_progress, last_preview, pct = 0.0, 0.0, 0
        try:
            async with websockets.connect(comfy_ws) as cws:
                async for message in cws:
                    now = time.time()
                    if isinstance(message, bytes):
                        if now - last_preview < PREVIEW_INTERVAL or len(message) < 8:
                            continue
                        if int.from_bytes(message[0:4], "big") != 1:
                            continue
                        last_preview = now
                        await ws.send(json.dumps({
                            "type": "progress", "id": job_id, "pct": pct,
                            "preview_b64": base64.b64encode(message[8:]).decode(),
                        }))
                        continue
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") != "progress":
                        continue
                    p = data.get("data", {})
                    if p.get("max", 0) > 0:
                        pct = int(100 * p.get("value", 0) / p["max"])
                        if now - last_progress >= PROGRESS_INTERVAL:
                            last_progress = now
                            await ws.send(json.dumps({"type": "progress", "id": job_id, "pct": pct}))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"progress relay for {prompt_id} ended: {e}")


async def run_ws_worker():
    worker = WSWorker()
    try:
        await worker.run()
    finally:
        await worker.comfy.aclose()

"""Worker preflight — prove a model runs end-to-end before advertising it.

Three tiers, cheap→definitive, each catching a real failure we've hit:
  1. NODE check   — every recipe node_type is registered in ComfyUI (/object_info).
  2. FILE check   — every model file the recipe references is present (best-effort;
                    some packs load weights internally and expose no filename).
  3. SMOKE run    — actually queue the recipe graph with a canary image + minimum
                    steps and require it to COMPLETE without a ComfyUI exec error.

A model advertises only if its preflight passes. Replaces "does the file exist"
(is_servable) and blind GRID_TRUST_MODELS with earned trust.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# 128x128 white circle on black — a minimal "subject" so bg-removal/shape has input.
_CANARY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAABaUlEQVR42u3dQWEEMQwEQTEQf4wmcRjy2IwtVTPYqWdycrWilQkAABAAAAIAQAAACAAAAQAgAAAEAIAAABAAAAIAQAAApDp/D0Bg9Ocwavbu90vUnunvZKht09/GUDunv4ehlq8fNyjrZw3K9FmGsn7WoKyfNSjrZw3K+lmDsn7WoKyfNQAwHeC838MAZ0pPApxZAQCweP3vDABMBDhzAwBg8fpfGAAAAMD6QQMAAAAAGAJwNgUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAvYi+sDwAAAACjAPxnHAAAALIA7fcBALYDtN+IAdgO0H4nDGA7QLsVEQdo11LiAO1eEIDtAO1mXBygXU2MA7S7oXGAdjk3DtBuR8cB2vX0OMCFDKkRvKCRXL+9IXOWvyGTZbjhw70jBuB/GW77WG9JAvgM44nv8p4wAAACAEAAAAgAAAEAIAAABACAAAAQAAACAEAAAAjAvH42c7+4D8v9XQAAAABJRU5ErkJggg=="


async def preflight_model(model: str, grid_api_url: str, api_key: str,
                          comfy_url: str, timeout: float = 240.0) -> tuple[bool, str]:
    """Return (ok, reason). Fetches the recipe from the grid, checks nodes+files
    against ComfyUI, then smoke-runs it. Any failure → (False, why)."""
    # Recipe fetch is a plain HTTPS GET → use the PUBLIC api.* host. GRID_API_URL is
    # often ws.* (the WS-bypass endpoint that serves a Cloudflare Origin cert httpx's
    # default CA won't trust); swap it to api.* which has a normal public cert.
    grid = grid_api_url.rstrip("/").replace("//ws.", "//api.")
    comfy = comfy_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{grid}/v1/models/{model}/recipe", headers={"apikey": api_key})
            if r.status_code == 404:
                return False, "no recipe serves this model (grid 404)"
            if r.status_code != 200:
                return False, f"recipe fetch failed (grid {r.status_code})"
            recs = r.json().get("recipes", [])
            if not recs:
                return False, "grid returned no recipe"
            rec = recs[0]

            oi = (await c.get(f"{comfy}/object_info")).json()

        # tier 1: nodes
        avail_nodes = set(oi.keys())
        missing = [n for n in rec.get("node_types", []) if n not in avail_nodes]
        if missing:
            return False, f"missing ComfyUI nodes: {missing}"

        # tier 2: files (best-effort — gather every combo-list option across loaders)
        avail_files = set()
        for spec in oi.values():
            for grp in ("required", "optional"):
                for opt in (spec.get("input", {}).get(grp, {}) or {}).values():
                    if isinstance(opt, list) and opt and isinstance(opt[0], list):
                        avail_files.update(x for x in opt[0] if isinstance(x, str))
        missing_files = [f for f in rec.get("model_files", []) if f not in avail_files]
        if missing_files:
            return False, f"missing model files: {missing_files}"

        # tier 3: smoke run
        return await _smoke_run(rec, comfy, timeout)
    except Exception as e:
        return False, f"preflight error: {type(e).__name__}: {e}"


async def _smoke_run(rec: dict, comfy: str, timeout: float) -> tuple[bool, str]:
    """Queue the recipe graph with a canary image and wait for it to complete
    without a ComfyUI execution error."""
    import copy
    spec = copy.deepcopy(rec["spec"])
    async with httpx.AsyncClient(timeout=60) as c:
        # bind the canary image to the recipe's image slot(s), if any
        paths = rec.get("image_paths")
        if paths:
            up = await c.post(f"{comfy}/upload/image",
                              files={"image": ("canary.png", _png_bytes(), "image/png")},
                              data={"overwrite": "true"})
            up.raise_for_status()
            fn = up.json()["name"]
            for p in (paths if isinstance(paths, list) else [paths]):
                nid, _, key = p.partition(".inputs.")
                if nid in spec and key:
                    spec[nid].setdefault("inputs", {})[key] = fn

        q = await c.post(f"{comfy}/prompt", json={"prompt": spec, "client_id": "preflight"})
        if q.status_code != 200:
            return False, f"ComfyUI rejected the graph: {q.text[:180]}"
        pid = q.json().get("prompt_id")
        if not pid:
            return False, "no prompt_id from ComfyUI"

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            h = (await c.get(f"{comfy}/history/{pid}")).json().get(pid, {})
            st = h.get("status", {})
            if st.get("status_str") == "error":
                msgs = st.get("messages", [])
                err = next((m[1] for m in msgs if m[0] == "execution_error"), {})
                detail = err.get("exception_message", "") or str(err)[:180]
                return False, f"smoke run failed: {detail[:180]}"
            if st.get("completed") or h.get("outputs"):
                return True, "ok (smoke run completed)"
            await asyncio.sleep(2)
        return False, f"smoke run timed out ({int(timeout)}s)"


def _png_bytes():
    import base64
    return base64.b64decode(_CANARY_PNG_B64)

"""Best-effort price-book check for advertised models.

Registering and selling are separate gates: a worker may advertise any name,
but Core dispatches paid jobs only for models in the public price book
(GET /v1/pricing) and rejects unpriced work before dispatch. A worker
advertising an unlisted name therefore shows as online and earns nothing,
with no signal anywhere — this module supplies that signal as a startup
warning. It must never block or fail registration: any fetch problem returns
None and the caller stays silent.
"""
import logging

import httpx

logger = logging.getLogger(__name__)


async def fetch_priced_model_names(grid_api_url: str, timeout: float = 5.0):
    """Lowercased model names from the public price book, or None on any error."""
    # Same host swap as preflight: GRID_API_URL is often the ws.* WS-bypass
    # endpoint whose cert httpx's default CA won't trust; pricing is a plain
    # public GET on api.*.
    base = (grid_api_url or "").rstrip("/").replace("//ws.", "//api.")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base}/v1/pricing")
            r.raise_for_status()
            models = (r.json().get("price_book", {}) or {}).get("models", [])
            return {str(m.get("model", "")).lower() for m in models if m.get("model")}
    except Exception as e:  # noqa: BLE001 — advisory only, never fatal
        logger.debug("price-book check skipped: %s", e)
        return None


def unsellable_names(advertised, priced_names) -> list:
    """Advertised names absent from the price book (case-insensitive)."""
    if priced_names is None:
        return []
    return [m for m in advertised if m.lower() not in priced_names]

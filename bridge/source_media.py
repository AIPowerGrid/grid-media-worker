# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exact-byte, role-bound input media for Core-governed video recipes."""

import asyncio
import hashlib
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings

LIMITS = {"reference_audio": (12 * 1024 * 1024, "audio/wav", "wav", "LoadAudio", "audio"),
          "reference_video": (32 * 1024 * 1024, "video/mp4", "mp4", "LoadVideo", "file"),
          "reference_image": (12 * 1024 * 1024, "image/png", "png", "LoadImage", "image")}
R2_HOST = re.compile(r"[0-9a-f]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com")
LONG_SOURCE_FEATURE = "recipe-media-sources-625-v1"


def validate_bindings(workflow: dict, payload: dict) -> list[tuple[dict, dict]]:
    """Validate the entire source contract before any image/media fetch."""
    bindings, sources = payload.get("recipe_source_bindings"), payload.get("source_media")
    appearance = payload.get("appearance_profile")
    if bindings is None and sources is None and appearance is None:
        return []  # Legacy recipes are unchanged.
    try:
        if (not isinstance(bindings, list) or not isinstance(sources, list)
                or not 1 <= len(bindings) == len(sources) <= 3):
            raise ValueError
        if appearance is not None and (
                appearance != "ingredients-dev-v1" or payload.get("frames") != 121
                or payload.get("width") != 768 or payload.get("height") != 448
                or payload.get("steps") != 40 or payload.get("cfg_scale") != 2
                or payload.get("source_image_url") or payload.get("source_image_urls")
                or payload.get("recipe_image_inputs") or payload.get("recipe_image_bindings")
                or any(isinstance(node, dict) and node.get("class_type") == "LoadImage" and name != "reference_image"
                       for name, node in workflow.items())):
            raise ValueError
        if (type(payload.get("frames")) is not int or not 9 <= payload["frames"] <= 625
                or payload["frames"] % 8 != 1 or type(payload.get("fps")) is not int):
            raise ValueError
        features = payload.get("required_media_features")
        if payload["frames"] > 337 and (not isinstance(features, list) or LONG_SOURCE_FEATURE not in features):
            raise ValueError
        expected = {name for name, node in workflow.items()
                    if isinstance(node, dict) and node.get("class_type") in {"LoadAudio", "LoadVideo"}}
        if appearance:
            expected.add("reference_image")
        bound, roles = set(), set()
        for binding, source in zip(bindings, sources):
            if (not isinstance(binding, dict) or set(binding) != {"role", "path"}
                    or not isinstance(source, dict) or source.get("role") != binding["role"]
                    or binding["role"] not in LIMITS or binding["role"] in roles):
                raise ValueError
            role = binding["role"]
            if role == "reference_image" and (not appearance or binding["path"] != "reference_image.inputs.image"):
                raise ValueError
            limit, mime, _ext, kind, field = LIMITS[role]
            parts = binding["path"].split(".")
            if (len(parts) != 3 or parts[1:] != ["inputs", field] or parts[0] not in expected
                    or parts[0] in bound or workflow[parts[0]]["class_type"] != kind
                    or not isinstance(workflow[parts[0]].get("inputs"), dict)
                    or field not in workflow[parts[0]]["inputs"]):
                raise ValueError
            if (source.get("content_type") != mime or type(source.get("bytes")) is not int
                    or not 0 < source["bytes"] <= limit or not isinstance(source.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                    or not isinstance(source.get("url"), str) or len(source["url"]) > 4096):
                raise ValueError
            url = urlsplit(source["url"])
            # Core emits private presigned R2 reads. Do not accept redirects,
            # arbitrary origins, embedded credentials or proxy interception.
            if (url.scheme != "https" or not R2_HOST.fullmatch(url.hostname or "")
                    or url.port not in {None, 443} or url.username or url.password or url.fragment):
                raise ValueError
            units = {} if role == "reference_image" else {"frames": payload["frames"], "fps": 24}
            if role == "reference_audio":
                units.update(sample_rate=48_000, channels=2, samples=payload["frames"] * 2000)
            else:
                units.update(width=payload["width"], height=payload["height"])
            if (set(source) != {"role", "sha256", "bytes", "content_type", "url", *units}
                    or any(type(source[key]) is not int or source[key] != value for key, value in units.items())):
                raise ValueError
            roles.add(role)
            bound.add(parts[0])
        if bound != expected or "reference_audio" not in roles or payload.get("fps") != 24:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("Invalid governed video source binding contract") from None
    return list(zip(bindings, sources))


async def download_source(source: dict) -> str:
    """Verify the entire source before giving it to the loopback decoder."""
    return await download_committed_source(source, transport_role=source["role"])


async def download_committed_source(source: dict[str, Any], *, transport_role: str) -> str:
    """Transfer a validated commitment without changing its semantic source role."""
    try:
        limit, mime, ext, _kind, _field = LIMITS[transport_role]
        if (not isinstance(source.get("url"), str) or not 0 < len(source["url"]) <= 4096
                or type(source.get("bytes")) is not int or not 0 < source["bytes"] <= limit
                or source.get("content_type") != mime or not isinstance(source.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])):
            raise ValueError
        url = urlsplit(source["url"])
        if (url.scheme != "https" or not R2_HOST.fullmatch(url.hostname or "")
                or url.port not in {None, 443} or url.username or url.password or url.fragment):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("Invalid committed source transfer") from None
    base = urlsplit(Settings.COMFYUI_URL)
    if (base.scheme != "http" or base.hostname not in {"127.0.0.1", "::1"}
            or base.username or base.password or base.query or base.fragment
            or base.path not in {"", "/"}):
        raise RuntimeError("Source-conditioned video requires a loopback ComfyUI runtime")
    async def fetch():
        async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
            content = bytearray()
            async with client.stream("GET", source["url"]) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > min(source["bytes"], limit):
                        raise RuntimeError("Video source exceeds its committed byte count")
                    content.extend(chunk)
            if len(content) != source["bytes"] or hashlib.sha256(content).hexdigest() != source["sha256"]:
                raise RuntimeError("Video source does not match its committed bytes")
            filename = f"src_{uuid.uuid4().hex}.{ext}"
            response = await client.post(Settings.COMFYUI_URL.rstrip("/") + "/upload/image",
                                         files={"image": (filename, bytes(content), mime)},
                                         data={"type": "input", "overwrite": "false"})
            response.raise_for_status()
            uploaded = response.json()
            name = uploaded.get("name") if isinstance(uploaded, dict) else None
            if (not isinstance(name, str) or not name or len(name) > 255
                    or not name.endswith(f".{ext}") or any(char in name for char in ("/", "\\", "\x00"))
                    or uploaded.get("subfolder", "") or uploaded.get("type") != "input"):
                raise RuntimeError("ComfyUI returned an invalid source media identity")
            return name
    try:
        return await asyncio.wait_for(fetch(), timeout=60)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
        raise RuntimeError("Video source transfer failed before rendering") from None

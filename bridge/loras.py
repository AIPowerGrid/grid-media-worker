# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resolve recipe LoRAs without accepting caller-selected URLs or file paths."""

import asyncio
import copy
import hashlib
import math
import os
import re
import tempfile
from pathlib import Path

import httpx
from safetensors import safe_open

from .config import Settings

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


async def _inventory(comfy: httpx.AsyncClient) -> list[str]:
    response = await comfy.get("/object_info/LoraLoader")
    response.raise_for_status()
    return response.json()["LoraLoader"]["input"]["required"]["lora_name"][0]


def _checked_url(value: str) -> httpx.URL:
    url = httpx.URL(value)
    host = url.host.lower()
    allowed = host == "civitai.com" or host.endswith(
        (".civitai.com", ".r2.cloudflarestorage.com")
    )
    if (
        url.scheme != "https"
        or url.port not in {None, 443}
        or url.userinfo
        or not allowed
    ):
        raise ValueError("LoRA download origin is not allowed")
    return url


async def _download(
    client: httpx.AsyncClient, url: str, destination: Path, limit: int
) -> str:
    """Stream into a private temp file; authorize only CivitAI, never its CDN."""
    target = _checked_url(url)
    for _ in range(6):
        headers = (
            {"Authorization": f"Bearer {Settings.CIVITAI_TOKEN}"}
            if Settings.CIVITAI_TOKEN and target.host == "civitai.com"
            else {}
        )
        async with client.stream("GET", target, headers=headers) as response:
            if response.is_redirect:
                target = _checked_url(str(target.join(response.headers["location"])))
                continue
            response.raise_for_status()
            declared = int(response.headers.get("content-length", "0"))
            if declared > limit:
                raise ValueError("LoRA download exceeds configured size limit")
            digest, size = hashlib.sha256(), 0
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("LoRA download exceeds configured size limit")
                    output.write(chunk)
                    digest.update(chunk)
            if size == 0:
                raise ValueError("LoRA download was empty")
            return digest.hexdigest()
    raise ValueError("Too many LoRA download redirects")


async def _json(client: httpx.AsyncClient, path: str) -> dict:
    headers = (
        {"Authorization": f"Bearer {Settings.CIVITAI_TOKEN}"}
        if Settings.CIVITAI_TOKEN
        else {}
    )
    async with client.stream(
        "GET", "https://civitai.com" + path, headers=headers
    ) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("LoRA metadata exceeds size limit")
    import json

    return json.loads(data)


async def resolve_lora(comfy: httpx.AsyncClient, entry: dict) -> str:
    name = str(entry.get("name", "")).strip()
    if not _NAME.fullmatch(name):
        raise ValueError("LoRA name must be an ID or simple filename")
    available = await _inventory(comfy)
    for candidate in (
        name,
        name + ".safetensors",
        f"grid_{name}_{name}.safetensors" if entry.get("is_version") else "",
    ):
        if candidate.endswith(".safetensors") and candidate in available:
            return candidate
    if not name.isascii() or not name.isdecimal() or not Settings.LORA_DIR:
        raise ValueError(
            "LoRA unavailable locally; downloads need a numeric CivitAI ID and LORA_DIR"
        )
    root = Path(Settings.LORA_DIR).expanduser().resolve(strict=True)
    if not root.is_dir() or Settings.LORA_MAX_DOWNLOAD_BYTES <= 0:
        raise ValueError("Invalid LoRA download configuration")
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
        if entry.get("is_version"):
            version = await _json(client, f"/api/v1/model-versions/{name}")
        else:
            model = await _json(client, f"/api/v1/models/{name}")
            version = model["modelVersions"][0]
        version_id = str(version["id"])
        if not version_id.isascii() or not version_id.isdecimal():
            raise ValueError("Invalid CivitAI version ID")
        cached = f"grid_{name}_{version_id}.safetensors"
        if cached in available:
            return cached
        files = [
            f
            for f in version.get("files", [])
            if f.get("type") == "Model" and f.get("name", "").endswith(".safetensors")
        ]
        files.sort(key=lambda f: not f.get("primary", False))
        if not files:
            raise ValueError("No safetensors LoRA file available")
        file = files[0]
        expected = str(file.get("hashes", {}).get("SHA256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("LoRA download requires a SHA256 commitment")
        # New cache names bind bytes; legacy operator-installed files stay usable.
        filename = f"grid_{name}_{version_id}_{expected[:16]}.safetensors"
        if filename in available:
            return filename
        fd, temp_name = tempfile.mkstemp(prefix=".grid-lora-", dir=root)
        os.close(fd)
        temporary = Path(temp_name)
        try:
            digest = await asyncio.wait_for(
                _download(
                    client,
                    file["downloadUrl"],
                    temporary,
                    Settings.LORA_MAX_DOWNLOAD_BYTES,
                ),
                timeout=120,
            )
            if digest != expected:
                raise ValueError("LoRA download hash mismatch")
            with safe_open(str(temporary), framework="numpy") as tensors:
                if not tensors.keys():
                    raise ValueError("LoRA has no tensors")
            os.replace(temporary, root / filename)
        finally:
            temporary.unlink(missing_ok=True)
    for _ in range(20):
        if filename in await _inventory(comfy):
            return filename
        await asyncio.sleep(0.5)
    raise RuntimeError("ComfyUI did not register the downloaded LoRA")


async def apply_recipe_loras(
    comfy: httpx.AsyncClient, workflow: dict, inject: dict, entries: list
) -> tuple[dict, list[str]]:
    if not inject or not isinstance(entries, list) or not 1 <= len(entries) <= 5:
        raise ValueError(
            "Requested LoRAs require a recipe injection map and at most five entries"
        )
    graph = copy.deepcopy(workflow)
    for source in ("model_src", "clip_src"):
        ref = inject[source]
        if (
            not isinstance(ref, list)
            or len(ref) != 2
            or ref[0] not in graph
            or type(ref[1]) is not int
            or ref[1] < 0
        ):
            raise ValueError("Invalid LoRA injection source")
    for sinks in ("model_sinks", "clip_sinks"):
        if not inject.get(sinks):
            raise ValueError("Missing LoRA injection sinks")
        for node, field in inject[sinks]:
            if graph[node]["inputs"][field] != inject[sinks.replace("sinks", "src")]:
                raise ValueError(
                    "LoRA injection sink does not match the approved source"
                )
    normalized = []
    for entry in entries:
        strengths = [float(entry.get(key, 1.0)) for key in ("model", "clip")]
        if any(not math.isfinite(v) or not -2 <= v <= 2 for v in strengths):
            raise ValueError("LoRA strength must be finite and between -2 and 2")
        normalized.append((entry, strengths))
    loaded = [await resolve_lora(comfy, entry) for entry, _ in normalized]
    model_ref, clip_ref = inject["model_src"], inject["clip_src"]
    for index, (filename, (_, strengths)) in enumerate(zip(loaded, normalized)):
        node_id = f"grid_lora_{index}"
        if node_id in graph:
            raise ValueError("LoRA injection node collision")
        graph[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": filename,
                "strength_model": strengths[0],
                "strength_clip": strengths[1],
                "model": model_ref,
                "clip": clip_ref,
            },
        }
        model_ref, clip_ref = [node_id, 0], [node_id, 1]
    for sinks, ref in (("model_sinks", model_ref), ("clip_sinks", clip_ref)):
        for node, field in inject[sinks]:
            graph[node]["inputs"][field] = ref
    return graph, loaded

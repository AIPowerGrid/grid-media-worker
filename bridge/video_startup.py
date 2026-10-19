# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in video protocol compatibility, not GPU fidelity or public approval."""

import asyncio
import json
import logging
import os
import re
from urllib.parse import quote, urlsplit

import httpx

from .render_journal import RenderUncertain, prepare_state_directory
from .render_retention import status_url
from .source_media import LONG_SOURCE_FEATURE

APPEARANCE_MODEL = "LTX-2.5 Dev BF16 Ingredients"
NVFP4_MODEL = "LTX-2.5 Distilled NVFP4"
NVFP4_FILES = frozenset({
    "ltx-2.5-22b-distilled-transformer-nvfp4.safetensors",
    "gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
    "ltx-2.5-video-vae-bf16.safetensors",
    "ltx-2.5-audio-vae-bf16.safetensors",
})
MODELS = {"LTX-2.5 Distilled BF16": "distilled", NVFP4_MODEL: "distilled", "LTX-2.5 Dev BF16": "dev", APPEARANCE_MODEL: "dev"}
MAX_RECIPE_BYTES = 512 * 1024
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
STARTUP_DEADLINE_SECONDS = 30
STARTUP_MAX_ATTEMPTS = 3
STARTUP_RETRY_SECONDS = 2
logger = logging.getLogger(__name__)


class VideoStartupError(RuntimeError):
    """No capability may be declared until the startup requirements validate."""


def _strings(value, *, limit: int) -> set[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= limit
        or any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in value)
        or len(set(value)) != len(value)
    ):
        raise VideoStartupError("Invalid governed video inventory")
    return set(value)


async def _read_json(client: httpx.AsyncClient, url: str, *, limit: int, headers=None):
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            raise VideoStartupError("Video startup inventory is unavailable")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > limit:
                raise VideoStartupError("Video startup inventory exceeds its limit")
            content.extend(chunk)
    return json.loads(content), len(content)


def _require_files(requirements: dict, inventory: dict) -> None:
    nodes = _strings(requirements.get("node_types"), limit=128)
    files = _strings(requirements.get("model_files"), limit=32)
    if nodes - inventory.keys():
        raise VideoStartupError("A governed video node is missing from ComfyUI")
    # Check only recipe classes; unrelated nodes cannot satisfy a weight.
    available = set()
    for name in nodes:
        definition = inventory[name]
        if not isinstance(definition, dict):
            raise VideoStartupError("Invalid local video node inventory")
        for group in ("required", "optional"):
            inputs = definition.get("input", {}).get(group, {})
            if not isinstance(inputs, dict):
                raise VideoStartupError("Invalid local video node inputs")
            for value in inputs.values():
                if not isinstance(value, list) or not value:
                    continue
                choices = value[0]
                # ComfyUI V3 uses COMBO metadata instead of a first-item choice list.
                if (
                    choices == "COMBO"
                    and len(value) >= 2
                    and isinstance(value[1], dict)
                ):
                    choices = value[1].get("options")
                if isinstance(choices, list):
                    available.update(item for item in choices if isinstance(item, str))
    if files - available:
        raise VideoStartupError("A governed video weight is missing from ComfyUI")


def _recipe_requirements(value: dict, model: str, modes: list[str]) -> list[dict]:
    if not isinstance(value, dict) or value.get("model") != model:
        raise VideoStartupError("Core returned a different video model")
    records = value.get("recipes")
    if not isinstance(records, list) or not 1 <= len(records) <= 4:
        raise VideoStartupError("Core returned no bounded governed video recipes")
    roots = set()
    requirements = []
    for record in records:
        if not isinstance(record, dict) or record.get("job_type") != "video":
            raise VideoStartupError("Core returned a non-video recipe")
        root = record.get("recipe_root")
        if (
            not isinstance(root, str)
            or not re.fullmatch(r"0x[0-9a-f]{64}", root)
            or root in roots
        ):
            raise VideoStartupError("Core returned an ambiguous video recipe identity")
        roots.add(root)
        contract = record.get("video_contract")
        if (
            not isinstance(contract, dict)
            or contract.get("schema") != "aipg.ltx-video.v1"
            or contract.get("variant") != MODELS[model]
        ):
            raise VideoStartupError("Core lacks the matching governed video contract")
        supported = contract.get("source_modes")
        appearance = model == APPEARANCE_MODEL
        if appearance:
            if (contract.get("appearance_profile") != "ingredients-dev-v1"
                    or type(contract.get("max_frames")) is not int or contract["max_frames"] != 121
                    or type(contract.get("max_pixels")) is not int or contract["max_pixels"] != 768 * 448
                    or type(contract.get("max_keyframes")) is not int or contract["max_keyframes"] != 0
                    or set(modes) != {"audio", "video"}):
                raise VideoStartupError("Core lacks the exact Ingredients profile and complete source modes")
        elif contract.get("appearance_profile") is not None:
            raise VideoStartupError("Appearance profile does not match the explicit worker model")
        if (
            not isinstance(supported, list)
            or len(supported) > 2
            or any(mode not in ("audio", "video") for mode in supported)
            or len(set(supported)) != len(supported)
            or set(modes) - set(supported)
        ):
            raise VideoStartupError(
                "Core does not govern the requested video source modes"
            )
        # Core's current source feature covers both roles, not a per-mode subset.
        if modes and set(modes) != set(supported):
            raise VideoStartupError(
                "The shared source capability requires every governed source mode"
            )
        requirements.append(record)
        sources = record.get("source_requirements")
        if not isinstance(sources, dict) or set(sources) != set(supported):
            raise VideoStartupError("Core lacks the governed video source requirements")
        for mode in modes:
            source = sources[mode]
            if not isinstance(source, dict):
                raise VideoStartupError("Invalid governed video source requirements")
            requirements.append(source)
            if appearance:
                nodes = _strings(source.get("node_types"), limit=128)
                files = _strings(source.get("model_files"), limit=32)
                if (not {"LoadImage", "RepeatImageBatch", "LTXICLoRALoaderModelOnly", "LTXAddVideoICLoRAGuide"} <= nodes
                        or not {"ltx-2.5-22b-dev-transformer-bf16.safetensors",
                                "ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors"} <= files
                        or mode == "video" and "LTXAddVideoICLoRAGuideAdvanced" not in nodes):
                    raise VideoStartupError("Core lacks the governed Ingredients node and weight inventory")
    for required in requirements:
        _strings(required.get("node_types"), limit=128)
        files = _strings(required.get("model_files"), limit=32)
        if model == NVFP4_MODEL and (
            not NVFP4_FILES <= files
            or "ltx-2.5-22b-distilled-transformer-bf16.safetensors" in files
        ):
            raise VideoStartupError("Core lacks the exact governed NVFP4 component inventory")
    return requirements


async def _read_inventory(client: httpx.AsyncClient, origin: str, requirements: list[dict]):
    names = set().union(*(set(required["node_types"]) for required in requirements))
    if len(names) > 128 or any(
        name in (".", "..") or any(char in name for char in "/\\?#")
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        for name in names
    ):
        raise VideoStartupError("Invalid governed video node names")
    inventory = {}
    remaining = MAX_INVENTORY_BYTES
    for name in sorted(names):
        if remaining <= 0:
            raise VideoStartupError("Video startup inventory exceeds its limit")
        node, size = await _read_json(
            client, origin.rstrip("/") + "/object_info/" + quote(name, safe=""),
            limit=remaining,
        )
        remaining -= size
        if not isinstance(node, dict) or set(node) != {name} or not isinstance(node[name], dict):
            raise VideoStartupError("A governed video node is missing or invalid")
        inventory.update(node)
    return inventory


async def qualify_video_startup(
    *,
    models: list[str],
    job_types: list[str],
    source_modes: list[str],
    grid_api_url: str,
    api_key: str,
    comfy_url: str,
    state_dir: str,
) -> tuple[str, ...]:
    """Read-only inventory plus private state; never submit an unpaid smoke job."""
    if os.name != "posix":
        raise VideoStartupError(
            "Durable video requires separately qualified POSIX state"
        )
    if (
        not isinstance(models, list)
        or len(models) != 1
        or not isinstance(models[0], str)
        or models[0] not in MODELS
        or job_types != ["video"]
        or not state_dir
        or not api_key
    ):
        raise VideoStartupError(
            "Durable video requires one explicit LTX model, video-only jobs and private state"
        )
    if (
        not isinstance(source_modes, list)
        or len(source_modes) > 2
        or any(mode not in ("audio", "video") for mode in source_modes)
        or len(set(source_modes)) != len(source_modes)
    ):
        raise VideoStartupError("Video source modes must be unique audio/video values")
    origin = urlsplit(comfy_url)
    if (
        origin.scheme not in ("http", "https")
        or origin.hostname not in ("127.0.0.1", "localhost", "::1")
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise VideoStartupError(
            "Durable video requires the actual private loopback ComfyUI origin"
        )
    try:
        base = status_url(grid_api_url).removesuffix("/v1/workers/self/media-jobs")
        await asyncio.to_thread(prepare_state_directory, state_dir)

        async def read():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=3),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                records, _ = await _read_json(
                    client,
                    f"{base}/v1/models/{quote(models[0], safe='')}/recipe",
                    limit=MAX_RECIPE_BYTES,
                    headers={"apikey": api_key},
                )
                requirements = _recipe_requirements(records, models[0], source_modes)
                inventory = await _read_inventory(client, comfy_url, requirements)
                for required in requirements:
                    _require_files(required, inventory)

        for attempt in range(STARTUP_MAX_ATTEMPTS):
            try:
                await asyncio.wait_for(read(), timeout=STARTUP_DEADLINE_SECONDS)
                break
            except (httpx.TimeoutException, httpx.NetworkError, asyncio.TimeoutError):
                if attempt + 1 == STARTUP_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "Governed video startup read unavailable; retrying verification (%s/%s)",
                    attempt + 2, STARTUP_MAX_ATTEMPTS,
                )
                await asyncio.sleep(STARTUP_RETRY_SECONDS)
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        OSError,
        asyncio.TimeoutError,
        RenderUncertain,
    ) as exc:
        raise VideoStartupError(
            "Durable video startup could not verify its private requirements"
        ) from exc
    return ("async-video-resume-v1",) + (
        ("recipe-media-sources-v1",) if source_modes else ()
    ) + ((LONG_SOURCE_FEATURE,) if source_modes and models == ["LTX-2.5 Distilled BF16"] else ()) + (
        ("recipe-appearance-reference-v1",) if models == [APPEARANCE_MODEL] else ())

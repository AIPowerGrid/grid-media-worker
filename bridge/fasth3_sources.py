# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Disabled FastH3 qualification adapter; never grants Grid/runtime admission."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from fractions import Fraction
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .source_media import R2_HOST, download_committed_source

SCHEMA = "aipg.fasth3-sources.v1"
FEATURE = "recipe-fasth3-sources-v1"
MODEL = "FastH3 Omni NVFP4 QAD"
# Core's reviewed qualification graph, excluding _grid metadata. Normalize only
# validated request fields/role additions; every other node/input stays pinned.
BASE_GRAPH_SHA256 = "dfba8880c99927f7a828184f3ea88cedfe08a576c7ae676ac6b7b70b47ebed95"
ROLES = {
    "appearance_image": (
        "appearance",
        "LoadImage",
        "image",
        "reference_image",
        "image/png",
    ),
    "opening_image": ("opening", "LoadImage", "image", "reference_image", "image/png"),
    "conditioning_audio": (
        "reference_sound",
        "LoadAudio",
        "audio",
        "reference_audio",
        "audio/wav",
    ),
    "soundtrack": ("soundtrack", "LoadAudio", "audio", "reference_audio", "audio/wav"),
}


def identifies_fasth3(model: object, payload: dict[str, Any]) -> bool:
    """Recognize candidates even if their source marker was dropped or renamed."""
    graph = payload.get("recipe_spec")
    schema = payload.get("recipe_source_schema")
    return (
        isinstance(model, str)
        and model.strip().lower() == MODEL.lower()
        or isinstance(schema, str)
        and schema.startswith("aipg.fasth3-")
        or isinstance(graph, dict)
        and any(
            isinstance(node, dict)
            and (
                isinstance(node.get("class_type"), str)
                and "MiniMaxH3" in node["class_type"]
                or isinstance(node.get("inputs"), dict)
                and node["inputs"].get("unet_name")
                == "fasth3_omniref_qad_nvfp4_comfy.safetensors"
            )
            for node in graph.values()
        )
    )


def validate_bindings(
    workflow: dict[str, Any], payload: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Reject the entire candidate before fetching the first source."""
    try:
        if not isinstance(workflow, dict) or not 15 <= len(workflow) <= 19:
            raise ValueError
        execution = payload["execution"]
        bindings, sources = payload["recipe_source_bindings"], payload["source_media"]
        if (
            payload.get("recipe_source_schema") != SCHEMA
            or payload.get("recipe_engine") != "comfyui"
            or payload.get("required_media_features") != [FEATURE]
            or type(payload.get("n")) is not int
            or payload["n"] != 1
            or not isinstance(execution, dict)
            or not isinstance(bindings, list)
            or not isinstance(sources, list)
            or not 0 <= len(bindings) == len(sources) <= 3
            or any(
                payload.get(key)
                for key in (
                    "source_image_url",
                    "source_image_urls",
                    "recipe_image_inputs",
                    "recipe_image_bindings",
                    "appearance_profile",
                    "loras",
                )
            )
        ):
            raise ValueError
        frames, width, height, seed = (
            execution[key] for key in ("frames", "width", "height", "seed")
        )
        if (
            any(type(value) is not int for value in (frames, width, height, seed))
            or not 124 <= frames <= 362
            or frames % 17 != 5
            or not 0 <= seed < 2**53
            or (width, height) not in {(1344, 768), (768, 1344), (768, 768)}
            or type(execution.get("appearance_image")) is not bool
            or type(execution.get("opening_image")) is not bool
            or execution.get("audio_mode") not in {"generated", "reference", "preserve"}
            or type(execution.get("fps")) is not int
            or execution["fps"] != 24
        ):
            raise ValueError
        duration = Fraction(frames, 24)
        if (
            not isinstance(execution.get("prompt"), str)
            or not execution["prompt"].strip()
            or len(execution["prompt"].encode()) > 8000
            or type(execution.get("seconds")) not in (int, float)
            or not math.isclose(
                execution["seconds"], float(duration), rel_tol=0, abs_tol=1e-9
            )
            or type(execution.get("steps")) is not int
            or execution["steps"] != 8
            or type(execution.get("cfg")) not in (int, float)
            or execution["cfg"] != 1
            or execution.get("audio") is not True
            or execution.get("sampler") != "res_multistep"
            or execution.get("scheduler") != "simple"
            or any(
                type(execution.get(key)) is not int
                for key in (
                    "duration_numerator",
                    "duration_denominator",
                    "output_audio_sample_rate",
                    "output_audio_channels",
                )
            )
            or (execution["duration_numerator"], execution["duration_denominator"])
            != (duration.numerator, duration.denominator)
            or execution["output_audio_channels"] != 2
            or execution["output_audio_sample_rate"]
            != (48_000 if execution["audio_mode"] == "preserve" else 32_000)
        ):
            raise ValueError
        roles = []
        if execution["appearance_image"]:
            roles.append("appearance_image")
        if execution["audio_mode"] == "reference":
            roles.append("conditioning_audio")
        if execution["opening_image"]:
            roles.append("opening_image")
        if execution["audio_mode"] == "preserve":
            roles.append("soundtrack")
        if execution["audio_mode"] != "generated" and not (
            execution["appearance_image"] or execution["opening_image"]
        ):
            raise ValueError
        if len(roles) != len(bindings):
            raise ValueError
        available = set()
        if execution["appearance_image"]:
            available.add(("Picture", "1"))
        if execution["audio_mode"] == "reference":
            available.add(("Audio", "1"))
        if any(
            label not in available
            for label in re.findall(
                r"<(Picture|Audio|Video)\s*(\d+)>", execution["prompt"]
            )
        ):
            raise ValueError
        expected_nodes = {ROLES[role][0] for role in roles}
        actual_nodes = {
            name
            for name, node in workflow.items()
            if isinstance(node, dict)
            and node.get("class_type") in {"LoadImage", "LoadAudio", "LoadVideo"}
        }
        if actual_nodes != expected_nodes:
            raise ValueError
        conditioning = workflow["conditioning"]
        if (
            conditioning["class_type"] != "MiniMaxH3ReferenceToVideo"
            or any(
                conditioning["inputs"].get(key) != value
                for key, value in {
                    "width": width,
                    "height": height,
                    "length": frames,
                    "prompt": execution["prompt"],
                }.items()
            )
            or type(workflow["noise"]["inputs"].get("noise_seed")) is not int
            or workflow["noise"]["inputs"].get("noise_seed") != seed
        ):
            raise ValueError
        condition_inputs = conditioning["inputs"]
        if (
            ("ref_images.ref_image_0" in condition_inputs)
            != ("appearance_image" in roles)
            or ("ref_audios.ref_audio_0" in condition_inputs)
            != ("conditioning_audio" in roles)
            or any(
                type(condition_inputs.get(key)) is not int
                for key in ("width", "height", "length")
            )
        ):
            raise ValueError
        if not _same_json(
            condition_inputs.get("ref_images.ref_image_0"),
            ["appearance", 0] if "appearance_image" in roles else None,
        ) or not _same_json(
            condition_inputs.get("ref_audios.ref_audio_0"),
            ["reference_sound", 0] if "conditioning_audio" in roles else None,
        ):
            raise ValueError
        expected_audio = (
            ["soundtrack", 0] if "soundtrack" in roles else ["decode_audio", 0]
        )
        if (
            not _same_json(workflow["video"]["inputs"].get("audio"), expected_audio)
            or workflow["video"]["inputs"].get("fps") != 24
        ):
            raise ValueError
        guide_needed = "opening_image" in roles or "soundtrack" in roles
        if ("guide" in workflow) != guide_needed:
            raise ValueError
        if guide_needed:
            guide = workflow["guide"]
            if (
                guide["class_type"] != "MiniMaxH3AddGuide"
                or guide["inputs"].get("frame_idx") != 0
                or guide["inputs"].get("positive") != ["conditioning", 0]
                or guide["inputs"].get("latent") != ["conditioning", 1]
                or guide["inputs"].get("image")
                != (["opening", 0] if "opening_image" in roles else None)
                or guide["inputs"].get("audio")
                != (["soundtrack", 0] if "soundtrack" in roles else None)
            ):
                raise ValueError
        if not _same_json(
            workflow["guider"]["inputs"].get("conditioning"),
            ["guide", 0] if guide_needed else ["conditioning", 0],
        ):
            raise ValueError
        _validate_graph_pin(workflow, roles)
        for role, binding, source in zip(roles, bindings, sources):
            node, kind, field, _transport, mime = ROLES[role]
            if (
                binding != {"role": role, "path": f"{node}.inputs.{field}"}
                or not isinstance(source, dict)
                or source.get("role") != role
                or workflow[node]["class_type"] != kind
                or field not in workflow[node]["inputs"]
                or source.get("content_type") != mime
                or type(source.get("bytes")) is not int
                or not 0 < source["bytes"] <= 12 * 1024 * 1024
                or not isinstance(source.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                or not isinstance(source.get("url"), str)
                or not 0 < len(source["url"]) <= 4096
            ):
                raise ValueError
            url = urlsplit(source["url"])
            if (
                url.scheme != "https"
                or not R2_HOST.fullmatch(url.hostname or "")
                or url.port not in {None, 443}
                or url.username
                or url.password
                or url.fragment
            ):
                raise ValueError
            if role in {"appearance_image", "opening_image"}:
                units = {key: source.get(key) for key in ("width", "height")}
                if (
                    any(
                        type(value) is not int or not 32 <= value <= 2048
                        for value in units.values()
                    )
                    or role == "opening_image"
                    and units != {"width": width, "height": height}
                ):
                    raise ValueError
            else:
                samples = source.get("samples")
                if type(samples) is not int or not 96_000 <= samples <= 724_000:
                    raise ValueError
                units = {"sample_rate": 48_000, "channels": 2, "samples": samples}
                if role == "soundtrack":
                    units.update(samples=frames * 2000, frames=frames, fps=24)
                elif samples > 720_000:
                    raise ValueError
            if set(source) != {
                "role",
                "sha256",
                "bytes",
                "content_type",
                "url",
                *units,
            } or any(
                type(source[key]) is not int or source[key] != value
                for key, value in units.items()
            ):
                raise ValueError
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ):
        raise RuntimeError("Invalid FastH3 qualification source contract") from None
    return list(zip(bindings, sources))


def _same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def _validate_graph_pin(workflow: dict[str, Any], roles: list[str]) -> None:
    normalized = copy.deepcopy(workflow)
    for role in roles:
        node, kind, field, _transport, _mime = ROLES[role]
        placeholder = {
            "appearance_image": "grid_appearance.png",
            "opening_image": "grid_opening.png",
            "conditioning_audio": "grid_reference.wav",
            "soundtrack": "grid_soundtrack.wav",
        }[role]
        if normalized.pop(node) != {"class_type": kind, "inputs": {field: placeholder}}:
            raise ValueError
    inputs = normalized["conditioning"]["inputs"]
    inputs.update(prompt="", width=1344, height=768, length=124)
    inputs.pop("ref_images.ref_image_0", None)
    inputs.pop("ref_audios.ref_audio_0", None)
    normalized["noise"]["inputs"]["noise_seed"] = 29
    if "opening_image" in roles or "soundtrack" in roles:
        guide = {
            "positive": ["conditioning", 0],
            "latent": ["conditioning", 1],
            "frame_idx": 0,
        }
        if "opening_image" in roles:
            guide.update(image=["opening", 0], vae=["video_vae", 0])
        if "soundtrack" in roles:
            guide.update(audio=["soundtrack", 0], audio_vae=["audio_vae", 0])
            normalized["video"]["inputs"]["audio"] = ["decode_audio", 0]
            if "decode_audio" in normalized:
                raise ValueError
            normalized["decode_audio"] = {
                "class_type": "VAEDecodeAudio",
                "inputs": {"samples": ["sample", 0], "vae": ["audio_vae", 0]},
            }
        if not _same_json(
            normalized.pop("guide"),
            {"class_type": "MiniMaxH3AddGuide", "inputs": guide},
        ):
            raise ValueError
        normalized["guider"]["inputs"]["conditioning"] = ["conditioning", 0]
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if hashlib.sha256(encoded).hexdigest() != BASE_GRAPH_SHA256:
        raise ValueError


async def build_candidate_workflow(
    job_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Bind a private candidate for qualification; normal WS dispatch rejects H3."""
    try:
        if str(UUID(job_id)) != job_id:
            raise ValueError
        graph = payload["recipe_spec"]
        if not isinstance(graph, dict):
            raise TypeError
        workflow: dict[str, Any] = copy.deepcopy(graph)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("Invalid FastH3 qualification job identity") from None
    pairs = validate_bindings(workflow, payload)
    for binding, source in pairs:
        node, _inputs, field = binding["path"].split(".")
        name = await download_committed_source(
            source, transport_role=ROLES[binding["role"]][3]
        )
        workflow[node]["inputs"][field] = name
    workflow["save"]["inputs"]["filename_prefix"] = f"grid_{job_id}"
    return workflow

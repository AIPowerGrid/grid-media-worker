# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Synthetic candidate handoff only: no weights, GPU, real storage or billing."""

import asyncio
import copy
import hashlib
from fractions import Fraction
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from bridge import fasth3_sources as h3
from bridge import source_media
from bridge.render_journal import RenderUncertain
from bridge.ws_worker import WSWorker

JOB = "a0666ffe-da9b-4e92-93c1-abf9301e0558"
URL = (
    "https://"
    + "a" * 32
    + ".r2.cloudflarestorage.com/private/source?signature=synthetic"
)


def node(kind, **inputs):
    return {"class_type": kind, "inputs": inputs}


def candidate(
    appearance=True,
    opening=True,
    mode="preserve",
    frames=124,
    canvas=(1344, 768),
    seed=29,
):
    width, height = canvas
    duration = Fraction(frames, 24)
    execution = {
        "prompt": "A sunny synthetic park.",
        "frames": frames,
        "width": width,
        "height": height,
        "seed": seed,
        "fps": 24,
        "steps": 8,
        "cfg": 1.0,
        "sampler": "res_multistep",
        "scheduler": "simple",
        "audio": True,
        "appearance_image": appearance,
        "opening_image": opening,
        "audio_mode": mode,
        "seconds": float(duration),
        "duration_numerator": duration.numerator,
        "duration_denominator": duration.denominator,
        "output_audio_sample_rate": 48_000 if mode == "preserve" else 32_000,
        "output_audio_channels": 2,
    }
    graph = {
        "model": node(
            "UNETLoader",
            unet_name="fasth3_omniref_qad_nvfp4_comfy.safetensors",
            weight_dtype="default",
        ),
        "encoder": node(
            "CLIPLoader",
            clip_name="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            type="minimax",
            device="default",
        ),
        "video_vae": node(
            "VAELoader", vae_name="minimax_h3_video_vae_fp16.safetensors"
        ),
        "audio_vae": node(
            "VAELoader", vae_name="minimax_h3_audio_vae_fp32.safetensors"
        ),
        "conditioning": node(
            "MiniMaxH3ReferenceToVideo",
            clip=["encoder", 0],
            vae=["video_vae", 0],
            audio_vae=["audio_vae", 0],
            prompt=execution["prompt"],
            width=width,
            height=height,
            length=frames,
            ref_image_size="match",
        ),
        "shift": node(
            "MiniMaxH3SigmaShift", model=["model", 0], shift_video=12.0, shift_audio=3.0
        ),
        "sampler": node("KSamplerSelect", sampler_name="res_multistep"),
        "sigmas": node(
            "BasicScheduler",
            model=["shift", 0],
            scheduler="simple",
            steps=8,
            denoise=1.0,
        ),
        "guider": node(
            "BasicGuider", model=["shift", 0], conditioning=["conditioning", 0]
        ),
        "noise": node("RandomNoise", noise_seed=seed),
        "sample": node(
            "SamplerCustomAdvanced",
            noise=["noise", 0],
            guider=["guider", 0],
            sampler=["sampler", 0],
            sigmas=["sigmas", 0],
            latent_image=["conditioning", 1],
        ),
        "decode_video": node("VAEDecode", samples=["sample", 0], vae=["video_vae", 0]),
        "decode_audio": node(
            "VAEDecodeAudio", samples=["sample", 0], vae=["audio_vae", 0]
        ),
        "video": node(
            "CreateVideo", images=["decode_video", 0], fps=24, audio=["decode_audio", 0]
        ),
        "save": node(
            "SaveVideo",
            video=["video", 0],
            filename_prefix="fasth3_qualification",
            format="auto",
            codec="h264",
        ),
    }
    roles = (["appearance_image"] if appearance else []) + (
        ["conditioning_audio"] if mode == "reference" else []
    )
    roles += (["opening_image"] if opening else []) + (
        ["soundtrack"] if mode == "preserve" else []
    )
    bindings, sources = [], []
    for role in roles:
        name, kind, field, _transport, mime = h3.ROLES[role]
        placeholder = {
            "appearance_image": "grid_appearance.png",
            "opening_image": "grid_opening.png",
            "conditioning_audio": "grid_reference.wav",
            "soundtrack": "grid_soundtrack.wav",
        }[role]
        graph[name] = node(kind, **{field: placeholder})
        bindings.append({"role": role, "path": f"{name}.inputs.{field}"})
        units = (
            {"width": 64, "height": 96}
            if role == "appearance_image"
            else {"width": width, "height": height}
        )
        if mime == "audio/wav":
            units = {"sample_rate": 48_000, "channels": 2, "samples": 96_000}
            if role == "soundtrack":
                units.update(samples=frames * 2000, frames=frames, fps=24)
        raw = role.encode()
        sources.append(
            dict(
                role=role,
                url=URL + "&role=" + role,
                sha256=hashlib.sha256(raw).hexdigest(),
                bytes=len(raw),
                content_type=mime,
                **units,
            )
        )
    if appearance:
        graph["conditioning"]["inputs"]["ref_images.ref_image_0"] = ["appearance", 0]
    if mode == "reference":
        graph["conditioning"]["inputs"]["ref_audios.ref_audio_0"] = [
            "reference_sound",
            0,
        ]
    if opening or mode == "preserve":
        guide = {
            "positive": ["conditioning", 0],
            "latent": ["conditioning", 1],
            "frame_idx": 0,
        }
        if opening:
            guide.update(image=["opening", 0], vae=["video_vae", 0])
        if mode == "preserve":
            guide.update(audio=["soundtrack", 0], audio_vae=["audio_vae", 0])
            graph["video"]["inputs"]["audio"] = ["soundtrack", 0]
            del graph["decode_audio"]
        graph["guide"] = node("MiniMaxH3AddGuide", **guide)
        graph["guider"]["inputs"]["conditioning"] = ["guide", 0]
    return {
        "recipe_source_schema": h3.SCHEMA,
        "recipe_engine": "comfyui",
        "required_media_features": [h3.FEATURE],
        "n": 1,
        "execution": execution,
        "recipe_spec": graph,
        "recipe_source_bindings": bindings,
        "source_media": sources,
    }


MODES = [
    (False, False, "generated"),
    (True, False, "generated"),
    (False, True, "generated"),
    (True, True, "generated"),
    (True, False, "reference"),
    (False, True, "reference"),
    (True, True, "reference"),
    (True, False, "preserve"),
    (False, True, "preserve"),
    (True, True, "preserve"),
]


@pytest.mark.parametrize("appearance,opening,mode", MODES)
@pytest.mark.parametrize(
    "frames,canvas,seed",
    [(124, (1344, 768), 29), (362, (768, 1344), 0), (243, (768, 768), 2**53 - 1)],
)
@pytest.mark.asyncio
async def test_all_candidate_modes_bind_only_exact_slots_without_mutating_input(
    monkeypatch, appearance, opening, mode, frames, canvas, seed
):
    body = candidate(appearance, opening, mode, frames, canvas, seed)
    original = copy.deepcopy(body)
    transfer = AsyncMock(
        side_effect=lambda source, **_kwargs: (
            source["role"] + " (1)." + ("png" if "image" in source["role"] else "wav")
        )
    )
    monkeypatch.setattr(h3, "download_committed_source", transfer)
    graph = await h3.build_candidate_workflow(JOB, body)
    expected = copy.deepcopy(body["recipe_spec"])
    expected["save"]["inputs"]["filename_prefix"] = "grid_" + JOB
    for binding, source in zip(body["recipe_source_bindings"], body["source_media"]):
        name, _, field = binding["path"].split(".")
        expected[name]["inputs"][field] = (
            source["role"] + " (1)." + ("png" if "image" in source["role"] else "wav")
        )
    assert graph == expected
    assert body == original
    assert transfer.await_count == len(body["source_media"])
    for call, source in zip(transfer.await_args_list, body["source_media"]):
        assert call.args == (source,)
        assert call.kwargs == {"transport_role": h3.ROLES[source["role"]][3]}


@pytest.mark.parametrize(
    "path,value",
    [
        (("recipe_source_schema",), "aipg.ltx-sources.v1"),
        (("required_media_features",), []),
        (("n",), True),
        (("execution", "frames"), 125),
        (("execution", "seconds"), 5),
        (("execution", "duration_numerator"), 1),
        (("execution", "fps"), True),
        (("execution", "steps"), 9),
        (("execution", "cfg"), True),
        (("execution", "output_audio_sample_rate"), 32_000),
        (("execution", "prompt"), " "),
        (("execution", "prompt"), "x" * 8001),
        (("execution", "audio_mode"), "reference"),
        (("execution", "appearance_image"), False),
        (("source_image_url",), URL),
        (("loras",), ["extra"]),
        (("recipe_source_bindings",), []),
        (("source_media",), []),
        (("source_media", 2, "samples"), 247_999),
        (("source_media", 2, "bytes"), True),
        (("source_media", 2, "content_type"), "video/mp4"),
        (("source_media", 2, "sha256"), "bad"),
        (("source_media", 2, "url"), "https://127.0.0.1/private"),
        (("source_media", 2, "url"), URL + "#fragment"),
        (
            ("source_media", 2, "url"),
            "https://" + "a" * 32 + ".r2.cloudflarestorage.com:bad/private",
        ),
        (("source_media", 1, "width"), 64),
        (("source_media", 0, "height"), 4096),
        (("recipe_spec", "model", "inputs", "unet_name"), "substitute.safetensors"),
        (("recipe_spec", "shift", "inputs", "shift_audio"), 12.0),
        (("recipe_spec", "sigmas", "inputs", "steps"), 99),
        (("recipe_spec", "sample", "inputs", "guider"), ["conditioning", 0]),
        (
            ("recipe_spec", "conditioning", "inputs", "ref_images.ref_image_0"),
            ["opening", 0],
        ),
        (
            ("recipe_spec", "conditioning", "inputs", "ref_audios.ref_audio_0"),
            ["soundtrack", 0],
        ),
        (
            ("recipe_spec", "conditioning", "inputs", "ref_images.ref_image_1"),
            ["appearance", 0],
        ),
        (("recipe_spec", "guider", "inputs", "conditioning"), ["conditioning", 0]),
        (("recipe_spec", "guide", "inputs", "vae"), ["audio_vae", 0]),
        (("recipe_spec", "guide", "inputs", "frame_idx"), False),
        (("recipe_spec", "video", "inputs", "audio"), ["decode_audio", 0]),
        (("recipe_spec", "save", "inputs", "codec"), "other"),
        (("recipe_spec", "extra"), {"class_type": "UnreviewedPlugin", "inputs": {}}),
    ],
)
@pytest.mark.asyncio
async def test_any_invalid_role_or_graph_rejects_before_first_fetch(
    monkeypatch, path, value
):
    body = candidate()
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    transfer = AsyncMock()
    monkeypatch.setattr(h3, "download_committed_source", transfer)
    with pytest.raises(RuntimeError, match="qualification source contract"):
        await h3.build_candidate_workflow(JOB, body)
    transfer.assert_not_awaited()


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_missing_or_reordered_pair_never_passes_a_short_zip(missing):
    body = candidate()
    body["source_media"].pop(missing)
    body["recipe_source_bindings"].pop(missing)
    with pytest.raises(RuntimeError, match="source contract"):
        h3.validate_bindings(body["recipe_spec"], body)


@pytest.mark.parametrize("identity", [None, "bad", JOB.upper(), "../../output"])
@pytest.mark.asyncio
async def test_job_identity_is_canonical_before_source_transfer(monkeypatch, identity):
    transfer = AsyncMock()
    monkeypatch.setattr(h3, "download_committed_source", transfer)
    with pytest.raises(RuntimeError, match="job identity"):
        await h3.build_candidate_workflow(identity, candidate())
    transfer.assert_not_awaited()


@pytest.mark.parametrize("marker", ["model", "schema", "graph"])
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.asyncio
async def test_normal_dispatch_denies_h3_without_rendering_or_terminal_error(
    marker, durable
):
    worker, ws = WSWorker(), AsyncMock()
    worker._generate_durable_video = AsyncMock()
    body = candidate()
    if marker != "schema":
        body.pop("recipe_source_schema")
    if marker != "graph":
        body.pop("recipe_spec")
    if durable:
        body["_grid_media_job_version"] = 1
    try:
        assert h3.FEATURE not in worker.registration_payload()["media_features"]
        with pytest.raises(RenderUncertain, match="not qualified"):
            await worker._handle_job(
                ws,
                {
                    "id": JOB,
                    "model": h3.MODEL if marker == "model" else "other",
                    "payload": body,
                },
            )
        ws.close.assert_awaited_once_with(
            code=1011, reason="FastH3 runtime is not qualified"
        )
        ws.send.assert_not_awaited()
        worker._generate_durable_video.assert_not_awaited()
    finally:
        await worker.comfy.aclose()


@pytest.mark.parametrize(
    "change",
    [
        {"url": None},
        {"url": "http://127.0.0.1/private"},
        {"url": "https://bad/private"},
        {"url": URL + "#bad"},
        {"bytes": 0},
        {"bytes": True},
        {"sha256": "bad"},
    ],
)
@pytest.mark.asyncio
@respx.mock
async def test_shared_transfer_validates_before_network(change):
    source = candidate()["source_media"][0]
    source.update(change)
    with pytest.raises(RuntimeError, match="Invalid committed source transfer"):
        await source_media.download_committed_source(
            source, transport_role="reference_image"
        )
    assert not respx.calls


@pytest.mark.asyncio
@respx.mock
async def test_shared_transfer_keeps_semantic_role_and_uses_actual_collision_name(
    monkeypatch,
):
    monkeypatch.setattr(source_media.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    body = candidate()
    for source in body["source_media"]:
        respx.get(source["url"]).mock(
            return_value=httpx.Response(200, content=source["role"].encode())
        )
    uploads = respx.post("http://127.0.0.1:8188/upload/image").mock(
        side_effect=[
            httpx.Response(200, json={"name": "appearance (1).png", "type": "input"}),
            httpx.Response(200, json={"name": "opening (2).png", "type": "input"}),
            httpx.Response(200, json={"name": "soundtrack (3).wav", "type": "input"}),
        ]
    )
    original = copy.deepcopy(body)
    result = await h3.build_candidate_workflow(JOB, body)
    assert result["appearance"]["inputs"]["image"] == "appearance (1).png"
    assert result["opening"]["inputs"]["image"] == "opening (2).png"
    assert result["soundtrack"]["inputs"]["audio"] == "soundtrack (3).wav"
    assert body == original
    assert uploads.call_count == 3
    assert all("authorization" not in call.request.headers for call in respx.calls)


@pytest.mark.asyncio
async def test_cancelled_source_transfer_cannot_continue_or_return_a_graph(monkeypatch):
    transfer = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(h3, "download_committed_source", transfer)
    with pytest.raises(asyncio.CancelledError):
        await h3.build_candidate_workflow(JOB, candidate())
    assert transfer.await_count == 1


@pytest.mark.parametrize(
    "kind", ["MiniMaxH3ImageToVideo", "EmptyMiniMaxH3LatentAV", "MiniMaxH3AddGuide"]
)
def test_other_native_h3_nodes_cannot_bypass_the_closed_runtime(kind):
    assert h3.identifies_fasth3("unrecognized", {"recipe_spec": {"1": node(kind)}})


def test_checkpoint_only_candidate_is_also_recognized():
    assert h3.identifies_fasth3(
        "unrecognized",
        {
            "recipe_spec": {
                "1": node(
                    "UNETLoader", unet_name="fasth3_omniref_qad_nvfp4_comfy.safetensors"
                )
            }
        },
    )


@pytest.mark.parametrize("case", ["seed", "reference-index", "guide-index"])
def test_bool_is_not_a_graph_number_even_when_python_equality_would_allow_it(case):
    body = candidate(seed=0)
    graph = body["recipe_spec"]
    if case == "seed":
        graph["noise"]["inputs"]["noise_seed"] = False
    elif case == "reference-index":
        graph["conditioning"]["inputs"]["ref_images.ref_image_0"] = [
            "appearance",
            False,
        ]
    else:
        graph["guide"]["inputs"]["frame_idx"] = False
    with pytest.raises(RuntimeError, match="source contract"):
        h3.validate_bindings(graph, body)


@pytest.mark.parametrize("key", ["ref_images.ref_image_0", "ref_audios.ref_audio_0"])
def test_an_extra_null_source_slot_is_not_the_pinned_no_source_graph(key):
    body = candidate(False, False, "generated")
    body["recipe_spec"]["conditioning"]["inputs"][key] = None
    with pytest.raises(RuntimeError, match="source contract"):
        h3.validate_bindings(body["recipe_spec"], body)

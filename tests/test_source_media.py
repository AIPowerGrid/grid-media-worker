# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import hashlib
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from bridge import source_media, workflow

URL = "https://" + "a" * 32 + ".r2.cloudflarestorage.com/bucket/source.wav?signature=test"


def payload():
    content = b"synthetic bounded bytes"
    return {"frames": 9, "fps": 24, "width": 256, "height": 256,
            "recipe_spec": {"sound": {"class_type": "LoadAudio", "inputs": {"audio": "unbound.wav"}}},
            "recipe_source_bindings": [{"role": "reference_audio", "path": "sound.inputs.audio"}],
            "source_media": [{"role": "reference_audio", "url": URL, "sha256": hashlib.sha256(content).hexdigest(),
                              "bytes": len(content), "content_type": "audio/wav", "frames": 9, "fps": 24,
                              "sample_rate": 48_000, "samples": 18_000, "channels": 2}]}


@pytest.mark.asyncio
async def test_recipe_binds_actual_audio_upload_name_without_changing_core_graph(monkeypatch):
    body = payload()
    original = copy.deepcopy(body)
    upload = AsyncMock(return_value="source (1).wav")
    monkeypatch.setattr(workflow, "download_source", upload)
    graph = await workflow.build_recipe_workflow({"id": "test"}, body)
    assert graph["sound"]["inputs"]["audio"] == "source (1).wav"
    assert body == original
    upload.assert_awaited_once_with(body["source_media"][0])


@pytest.mark.parametrize("change", [
    {"url": "http://127.0.0.1/private"}, {"url": "https://attacker.invalid/private"},
    {"url": "https://" + "a" * 32 + ".r2.cloudflarestorage.com.attacker.invalid/a"},
    {"url": "https://user:secret@" + "a" * 32 + ".r2.cloudflarestorage.com/a"},
    {"sha256": "bad"}, {"bytes": True}, {"samples": 1}, {"fps": True},
    {"extra": "private"}, {"content_type": "video/mp4"},
])
def test_invalid_contract_rejects_before_fetch(change):
    body = payload()
    body["source_media"][0].update(change)
    with pytest.raises(RuntimeError, match="binding contract"):
        source_media.validate_bindings(body["recipe_spec"], body)


@pytest.mark.asyncio
async def test_invalid_media_binding_fails_before_even_image_download(monkeypatch):
    body = payload()
    body["recipe_spec"]["extra"] = {"class_type": "LoadVideo", "inputs": {"file": "unbound.mp4"}}
    body["source_image_url"] = "https://image.invalid/private.png"
    body["recipe_image_inputs"] = "image.inputs.image"
    image = AsyncMock()
    monkeypatch.setattr(workflow, "download_image", image)
    with pytest.raises(RuntimeError):
        await workflow.build_recipe_workflow({"id": "test"}, body)
    image.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_bindings_never_silently_discards_source():
    body = payload()
    del body["recipe_source_bindings"]
    with pytest.raises(RuntimeError):
        await workflow.build_recipe_workflow({"id": "test"}, body)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("content", [b"wrong same byte count!!", b"", b"too long" * 20])
async def test_wrong_truncated_or_extra_bytes_never_reach_comfy(monkeypatch, content):
    monkeypatch.setattr(source_media.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    respx.get(URL).mock(return_value=httpx.Response(200, content=content))
    with pytest.raises(RuntimeError):
        await source_media.download_source(payload()["source_media"][0])
    assert len(respx.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_download_hashes_bytes_and_binds_collision_name(monkeypatch):
    monkeypatch.setattr(source_media.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"synthetic bounded bytes"))
    upload = respx.post("http://127.0.0.1:8188/upload/image").mock(
        return_value=httpx.Response(200, json={"name": "collision (1).wav", "subfolder": "", "type": "input"}))
    assert await source_media.download_source(payload()["source_media"][0]) == "collision (1).wav"
    assert b"audio/wav" in upload.calls[0].request.content
    assert b"overwrite" in upload.calls[0].request.content
    assert "authorization" not in upload.calls[0].request.headers


@pytest.mark.asyncio
@respx.mock
async def test_redirect_is_not_followed(monkeypatch):
    monkeypatch.setattr(source_media.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}))
    with pytest.raises(RuntimeError, match="transfer failed"):
        await source_media.download_source(payload()["source_media"][0])
    assert len(respx.calls) == 1


def appearance_payload():
    body = payload()
    body.update(frames=121, width=768, height=448, steps=40, cfg_scale=2,
                appearance_profile="ingredients-dev-v1")
    body["source_media"][0].update(frames=121, samples=242_000)
    body["recipe_spec"]["reference_image"] = {"class_type": "LoadImage", "inputs": {"image": "unbound.png"}}
    body["recipe_source_bindings"].insert(0, {"role": "reference_image", "path": "reference_image.inputs.image"})
    body["source_media"].insert(0, {"role": "reference_image", "url": URL.replace(".wav", ".png"),
                                  "sha256": "a" * 64, "bytes": 10, "content_type": "image/png",
                                  "width": 768, "height": 448})
    return body


@pytest.mark.asyncio
async def test_appearance_binds_only_its_separate_slot_and_keeps_core_graph(monkeypatch):
    body = appearance_payload()
    original = copy.deepcopy(body)
    upload = AsyncMock(side_effect=["appearance (1).png", "sound (1).wav"])
    monkeypatch.setattr(workflow, "download_source", upload)
    graph = await workflow.build_recipe_workflow({"id": "test"}, body)
    assert graph["reference_image"]["inputs"]["image"] == "appearance (1).png"
    assert graph["sound"]["inputs"]["audio"] == "sound (1).wav"
    assert body == original
    assert upload.await_count == 2


def test_combined_appearance_audio_video_accepts_three_distinct_roles():
    body = appearance_payload()
    body["recipe_spec"]["source_video"] = {"class_type": "LoadVideo", "inputs": {"file": "unbound.mp4"}}
    body["recipe_source_bindings"].append({"role": "reference_video", "path": "source_video.inputs.file"})
    body["source_media"].append({"role": "reference_video", "url": URL.replace(".wav", ".mp4"),
                                 "sha256": "b" * 64, "bytes": 10, "content_type": "video/mp4",
                                 "frames": 121, "fps": 24, "width": 768, "height": 448})
    assert len(source_media.validate_bindings(body["recipe_spec"], body)) == 3


@pytest.mark.parametrize("change", [
    {"appearance_profile": None}, {"appearance_profile": "unknown"}, {"frames": 81},
    {"width": 448}, {"steps": 8}, {"cfg_scale": 1}, {"source_image_url": "private"},
    {"recipe_image_inputs": "reference_image.inputs.image"},
])
def test_appearance_cannot_downgrade_or_be_used_as_an_opening_image(change):
    body = appearance_payload()
    body.update(change)
    with pytest.raises(RuntimeError, match="binding contract"):
        source_media.validate_bindings(body["recipe_spec"], body)


@pytest.mark.parametrize("change", [{"role": "reference_video"}, {"width": True}, {"frames": 121},
                                   {"content_type": "image/jpeg"}, {"sha256": "bad"}])
def test_bad_appearance_commitments_reject_before_transfer(change):
    body = appearance_payload()
    body["source_media"][0].update(change)
    with pytest.raises(RuntimeError, match="binding contract"):
        source_media.validate_bindings(body["recipe_spec"], body)


@pytest.mark.asyncio
@respx.mock
async def test_appearance_download_checks_hash_and_binds_actual_png_name(monkeypatch):
    monkeypatch.setattr(source_media.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    content = b"synthetic PNG bytes"
    source = appearance_payload()["source_media"][0]
    source.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    respx.get(source["url"]).mock(return_value=httpx.Response(200, content=content))
    upload = respx.post("http://127.0.0.1:8188/upload/image").mock(return_value=httpx.Response(
        200, json={"name": "appearance (1).png", "subfolder": "", "type": "input"}))
    assert await source_media.download_source(source) == "appearance (1).png"
    assert b"image/png" in upload.calls[0].request.content
    assert "authorization" not in upload.calls[0].request.headers

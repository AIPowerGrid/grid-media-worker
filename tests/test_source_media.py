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

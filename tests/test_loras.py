# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import hashlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from safetensors import SafetensorError

from bridge import loras
from bridge.config import Settings


def inventory(names):
    return {"LoraLoader": {"input": {"required": {"lora_name": [names]}}}}


def recipe():
    graph = {
        "model": {"inputs": {}},
        "clip": {"inputs": {}},
        "sample": {"inputs": {"model": ["model", 0], "clip": ["clip", 0]}},
    }
    inject = {
        "model_src": ["model", 0],
        "clip_src": ["clip", 0],
        "model_sinks": [["sample", "model"]],
        "clip_sinks": [["sample", "clip"]],
    }
    return graph, inject


@pytest.mark.asyncio
async def test_local_lora_injection_preserves_input_and_chains(monkeypatch):
    resolver = AsyncMock(side_effect=["first.safetensors", "second.safetensors"])
    monkeypatch.setattr(loras, "resolve_lora", resolver)
    graph, inject = recipe()
    original = copy.deepcopy(graph)
    result, loaded = await loras.apply_recipe_loras(
        None,
        graph,
        inject,
        [
            {"name": "first", "model": 0.5},
            {"name": "second", "clip": -1},
        ],
    )
    assert graph == original
    assert loaded == ["first.safetensors", "second.safetensors"]
    assert result["sample"]["inputs"] == {
        "model": ["grid_lora_1", 0],
        "clip": ["grid_lora_1", 1],
    }
    assert result["grid_lora_1"]["inputs"]["model"] == ["grid_lora_0", 0]
    assert result["grid_lora_0"]["inputs"]["strength_model"] == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 3, -3])
async def test_invalid_strength_rejected_before_download(monkeypatch, value):
    resolve = AsyncMock()
    monkeypatch.setattr(loras, "resolve_lora", resolve)
    graph, inject = recipe()
    with pytest.raises(ValueError):
        await loras.apply_recipe_loras(
            None, graph, inject, [{"name": "1", "model": value}]
        )
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_or_mismatched_injection_rejected(monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(loras, "resolve_lora", resolve)
    graph, inject = recipe()
    with pytest.raises(ValueError):
        await loras.apply_recipe_loras(None, graph, None, [{"name": "1"}])
    graph["sample"]["inputs"]["model"] = ["unexpected", 0]
    with pytest.raises(ValueError):
        await loras.apply_recipe_loras(None, graph, inject, [{"name": "1"}])
    resolve.assert_not_awaited()


@pytest.mark.asyncio
@respx.mock
async def test_local_safetensors_needs_no_download(monkeypatch):
    respx.get("http://comfy/object_info/LoraLoader").respond(
        200, json=inventory(["style.safetensors"])
    )
    monkeypatch.setattr(Settings, "LORA_DIR", "")
    async with httpx.AsyncClient(base_url="http://comfy") as client:
        assert (
            await loras.resolve_lora(client, {"name": "style"}) == "style.safetensors"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../x", "/tmp/x", "https://evil/file", "x\\y"])
async def test_untrusted_names_rejected_before_network(name):
    async with httpx.AsyncClient(base_url="http://comfy") as client:
        with pytest.raises(ValueError):
            await loras.resolve_lora(client, {"name": name})


@pytest.mark.parametrize(
    "url",
    [
        "http://civitai.com/file",
        "https://civitai.com.evil/file",
        "https://127.0.0.1/file",
        "https://user:pass@civitai.com/file",
        "https://civitai.com:8080/file",
    ],
)
def test_download_origin_gate(url):
    with pytest.raises(ValueError):
        loras._checked_url(url)


def tiny_tensor():
    header = json.dumps(
        {"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}
    ).encode()
    header += b" " * (-len(header) % 8)
    return len(header).to_bytes(8, "little") + header + b"\0" * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "hash", "html", "oversize", "redirect"])
@pytest.mark.respx(assert_all_called=False)
async def test_verified_download_atomic_and_bounded(
    monkeypatch, tmp_path, respx_mock, fault
):
    monkeypatch.setattr(Settings, "LORA_DIR", str(tmp_path))
    monkeypatch.setattr(Settings, "CIVITAI_TOKEN", "synthetic-test-token")
    data = b"<html>login</html>" if fault == "html" else tiny_tensor()
    expected = "a" * 64 if fault == "hash" else hashlib.sha256(data).hexdigest()
    filename = f"grid_123_123_{expected[:16]}.safetensors"
    monkeypatch.setattr(
        Settings, "LORA_MAX_DOWNLOAD_BYTES", 1 if fault == "oversize" else 1024
    )
    inv = respx_mock.get("http://comfy/object_info/LoraLoader").mock(
        side_effect=[
            httpx.Response(200, json=inventory([])),
            httpx.Response(200, json=inventory([filename])),
        ]
    )
    meta = respx_mock.get("https://civitai.com/api/v1/model-versions/123").respond(
        200,
        json={
            "id": 123,
            "files": [
                {
                    "type": "Model",
                    "name": "lora.safetensors",
                    "hashes": {"SHA256": expected},
                    "downloadUrl": "https://civitai.com/api/download/models/123",
                }
            ],
        },
    )
    download = respx_mock.get("https://civitai.com/api/download/models/123").respond(
        302,
        headers={
            "location": "https://127.0.0.1/private"
            if fault == "redirect"
            else "https://bucket.r2.cloudflarestorage.com/lora",
        },
    )
    cdn = respx_mock.get("https://bucket.r2.cloudflarestorage.com/lora").respond(
        200, content=data
    )
    async with httpx.AsyncClient(base_url="http://comfy") as client:
        if fault:
            with pytest.raises((ValueError, SafetensorError)):
                await loras.resolve_lora(client, {"name": "123", "is_version": True})
            assert list(tmp_path.iterdir()) == []
        else:
            assert (
                await loras.resolve_lora(client, {"name": "123", "is_version": True})
                == filename
            )
            assert (tmp_path / filename).read_bytes() == data
            assert inv.call_count == 2
    assert (
        meta.calls.last.request.headers["authorization"]
        == "Bearer synthetic-test-token"
    )
    assert (
        download.calls.last.request.headers["authorization"]
        == "Bearer synthetic-test-token"
    )
    if cdn.called:
        assert "authorization" not in cdn.calls.last.request.headers

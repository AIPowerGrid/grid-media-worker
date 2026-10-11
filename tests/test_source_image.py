from __future__ import annotations

import httpx
import pytest
import respx

from bridge import workflow


@pytest.mark.asyncio
@respx.mock
async def test_source_image_upload_stays_in_memory(monkeypatch):
    monkeypatch.setattr(workflow.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    respx.get("https://media.example/source.png").mock(
        return_value=httpx.Response(200, content=b"small-png")
    )
    upload = respx.post("http://127.0.0.1:8188/upload/image").mock(
        return_value=httpx.Response(200, json={"name": "source.png"})
    )

    result = await workflow.download_image(
        "https://media.example/source.png", "source.png"
    )

    assert result == "source.png"
    assert b"small-png" in upload.calls[0].request.content


@pytest.mark.asyncio
@respx.mock
async def test_source_image_download_is_bounded(monkeypatch):
    monkeypatch.setattr(workflow, "MAX_SOURCE_IMAGE_BYTES", 3)
    respx.get("https://media.example/large.png").mock(
        return_value=httpx.Response(200, content=b"four")
    )

    with pytest.raises(RuntimeError, match="exceeds 12 MB"):
        await workflow.download_image(
            "https://media.example/large.png", "source.png"
        )


@pytest.mark.asyncio
@respx.mock
async def test_source_image_returns_actual_backend_name_after_collision(monkeypatch):
    monkeypatch.setattr(workflow.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    respx.get("https://media.example/source.png").mock(return_value=httpx.Response(200, content=b"small-png"))
    respx.post("http://127.0.0.1:8188/upload/image").mock(return_value=httpx.Response(200, json={"name": "source (1).png"}))
    assert await workflow.download_image("https://media.example/source.png", "source.png") == "source (1).png"


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("identity", [{}, {"name": "../outside.png"}, {"name": "a/b.png"},
                                    {"name": "source.png", "subfolder": "unexpected"},
                                    {"name": "source.png", "type": "output"}])
async def test_source_upload_identity_rejects_unsafe_paths(monkeypatch, identity):
    monkeypatch.setattr(workflow.Settings, "COMFYUI_URL", "http://127.0.0.1:8188")
    respx.get("https://media.example/source.png").mock(return_value=httpx.Response(200, content=b"small-png"))
    respx.post("http://127.0.0.1:8188/upload/image").mock(return_value=httpx.Response(200, json=identity))
    with pytest.raises(RuntimeError, match="invalid source image identity"):
        await workflow.download_image("https://media.example/source.png", "source.png")

from __future__ import annotations

import io
import math
import struct
import wave

import httpx
import pytest
import respx

from bridge import audio_runtime
from bridge.audio_runtime import (
    AudioRuntimeError,
    check_ace_step_runtime,
    generate_ace_step_audio,
)


def _wav(seconds=10, sample_rate=8000):
    output = io.BytesIO()
    samples = [int(1000 * math.sin(2 * math.pi * 220 * i / sample_rate)) for i in range(seconds * sample_rate)]
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return output.getvalue()


def _recipe():
    return {
        "adapter": "ace-step-api-audio-v1",
        "fixed": {
            "audio_format": "wav",
            "batch_size": 1,
            "model": "acestep-v15-turbo",
            "sample_mode": False,
            "thinking": False,
            "use_cot_caption": False,
            "use_cot_language": False,
            "use_format": False,
            "use_random_seed": False,
        },
        "limits": {
            "audio_duration": [10, 300],
            "inference_steps": [1, 20],
            "lyrics_chars": 20000,
            "prompt_chars": 2000,
        },
        "variables": ["prompt", "lyrics", "audio_duration", "inference_steps", "seed"],
    }


@pytest.mark.asyncio
@respx.mock
async def test_readiness_requires_the_pinned_runtime_model():
    respx.get("http://127.0.0.1:8001/health").mock(
        return_value=httpx.Response(200, json={"data": {"status": "ok"}})
    )
    respx.get("http://127.0.0.1:8001/v1/model_inventory").mock(
        return_value=httpx.Response(200, json={"data": {"models": [{"name": "acestep-v15-turbo"}]}})
    )
    await check_ace_step_runtime("http://127.0.0.1:8001", "acestep-v15-turbo")


@pytest.mark.asyncio
@respx.mock
async def test_readiness_rejects_openai_model_list_shape_cleanly():
    respx.get("http://127.0.0.1:8001/health").mock(
        return_value=httpx.Response(200, json={"data": {"status": "ok"}})
    )
    respx.get("http://127.0.0.1:8001/v1/model_inventory").mock(
        return_value=httpx.Response(200, json={"object": "list", "data": []})
    )
    with pytest.raises(AudioRuntimeError, match="inventory response is malformed"):
        await check_ace_step_runtime("http://127.0.0.1:8001", "acestep-v15-turbo")


@pytest.mark.asyncio
@respx.mock
async def test_generation_uses_only_the_constrained_local_recipe():
    audio = _wav()
    release = respx.post("http://127.0.0.1:8001/release_task").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"task_id": "task-1"}})
    )
    respx.post("http://127.0.0.1:8001/query_result").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"task_id": "task-1", "status": 1, "result": [{"status": 1, "file": "/v1/audio?path=one.wav"}]}]},
        )
    )
    respx.get("http://127.0.0.1:8001/v1/audio?path=one.wav").mock(
        return_value=httpx.Response(200, content=audio)
    )

    result = await generate_ace_step_audio(
        "http://127.0.0.1:8001",
        {"prompt": "clean electronic pulse", "lyrics": "", "seconds": 10, "inference_steps": 8, "seed": 42},
        _recipe(),
        poll_interval=0,
    )
    sent = release.calls[0].request.content
    assert b'"model":"acestep-v15-turbo"' in sent
    assert b'"sample_mode":false' in sent
    assert b'"thinking":false' in sent
    assert b'"use_cot_caption":false' in sent
    assert b'"use_cot_language":false' in sent
    assert b'"use_format":false' in sent
    assert b'"use_random_seed":false' in sent
    assert result.content == audio
    assert result.seconds == 10


@pytest.mark.asyncio
async def test_runtime_rejects_non_loopback_urls():
    with pytest.raises(AudioRuntimeError, match="loopback"):
        await generate_ace_step_audio(
            "https://api.acemusic.ai", {"prompt": "x", "seconds": 10, "seed": 1}, _recipe()
        )


@pytest.mark.asyncio
@respx.mock
async def test_runtime_rejects_external_output_urls():
    respx.post("http://127.0.0.1:8001/release_task").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"task_id": "task-1"}})
    )
    respx.post("http://127.0.0.1:8001/query_result").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"task_id": "task-1", "status": 1, "result": [{"status": 1, "file": "https://evil.example/audio.wav"}]}]},
        )
    )
    with pytest.raises(AudioRuntimeError, match="non-local"):
        await generate_ace_step_audio(
            "http://127.0.0.1:8001",
            {"prompt": "clean pulse", "lyrics": "", "seconds": 10, "seed": 42},
            _recipe(),
            poll_interval=0,
        )


@pytest.mark.asyncio
@respx.mock
async def test_audio_download_is_bounded_while_streaming(monkeypatch):
    monkeypatch.setattr(audio_runtime, "MAX_AUDIO_BYTES", 32)
    respx.post("http://127.0.0.1:8001/release_task").mock(
        return_value=httpx.Response(
            200, json={"code": 200, "data": {"task_id": "task-large"}}
        )
    )
    respx.post("http://127.0.0.1:8001/query_result").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "task_id": "task-large",
                        "status": 1,
                        "result": [{"status": 1, "file": "/large.wav"}],
                    }
                ]
            },
        )
    )
    respx.get("http://127.0.0.1:8001/large.wav").mock(
        return_value=httpx.Response(200, content=b"x" * 33)
    )
    with pytest.raises(AudioRuntimeError, match="safety bound"):
        await generate_ace_step_audio(
            "http://127.0.0.1:8001",
            {
                "prompt": "clean pulse",
                "lyrics": "",
                "seconds": 10,
                "seed": 42,
            },
            _recipe(),
            poll_interval=0,
        )

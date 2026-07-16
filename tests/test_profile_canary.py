# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import io
import json
import struct
import wave

import httpx
import pytest
import respx

from bridge.profiles import canary as canary_module
from bridge.profiles.canary import CanaryError, run_ace_step_canary
from bridge.profiles.profile import bundled_profile_path, load_profile


def _profile():
    return load_profile(bundled_profile_path(), allow_unsigned_draft=True).profile


def _wav(seconds=10, sample_rate=8000):
    output = io.BytesIO()
    samples = [1000 if index % 2 else -1000 for index in range(seconds * sample_rate)]
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return output.getvalue()


@pytest.mark.asyncio
@respx.mock
async def test_canary_generates_and_validates_audio():
    audio = _wav()
    respx.get("http://127.0.0.1:8001/health").mock(
        return_value=httpx.Response(200, json={"data": {"status": "ok"}})
    )
    release = respx.post("http://127.0.0.1:8001/release_task").mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"task_id": "canary-1"}},
        )
    )
    query = respx.post("http://127.0.0.1:8001/query_result").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"data": [{"task_id": "canary-1", "status": 0}]},
            ),
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "task_id": "canary-1",
                            "status": 1,
                            "result": json.dumps(
                                [
                                    {
                                        "status": 1,
                                        "file": "/v1/audio?path=canary.wav",
                                    }
                                ]
                            ),
                        }
                    ]
                },
            ),
        ]
    )
    respx.get("http://127.0.0.1:8001/v1/audio?path=canary.wav").mock(
        return_value=httpx.Response(200, content=audio)
    )

    result = await run_ace_step_canary(
        "http://127.0.0.1:8001",
        _profile(),
        poll_interval=0,
    )

    request = json.loads(release.calls[0].request.content)
    assert request["model"] == "acestep-v15-turbo"
    assert request["sample_mode"] is False
    assert request["thinking"] is False
    assert request["use_cot_caption"] is False
    assert request["use_cot_language"] is False
    assert request["use_format"] is False
    assert request["use_random_seed"] is False
    assert request["seed"] == 15012026
    assert query.call_count == 2
    assert result.audio_seconds == 10
    assert result.sample_rate == 8000
    assert result.channels == 1
    assert result.output_bytes == len(audio)


@pytest.mark.asyncio
@respx.mock
async def test_canary_rejects_silent_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0" * 160000)

    respx.get("http://127.0.0.1:8001/health").mock(
        return_value=httpx.Response(200, json={"data": {"status": "ok"}})
    )
    respx.post("http://127.0.0.1:8001/release_task").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"task_id": "c"}})
    )
    respx.post("http://127.0.0.1:8001/query_result").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "task_id": "c",
                        "status": 1,
                        "result": [{"status": 1, "file": "/audio.wav"}],
                    }
                ]
            },
        )
    )
    respx.get("http://127.0.0.1:8001/audio.wav").mock(
        return_value=httpx.Response(200, content=output.getvalue())
    )

    with pytest.raises(CanaryError, match="no signal"):
        await run_ace_step_canary("http://127.0.0.1:8001", _profile(), poll_interval=0)


@pytest.mark.asyncio
@respx.mock
async def test_canary_rejects_terminal_generation_failure():
    respx.get("http://127.0.0.1:8001/health").mock(
        return_value=httpx.Response(200, json={"data": {"status": "ok"}})
    )
    respx.post("http://127.0.0.1:8001/release_task").mock(
        return_value=httpx.Response(200, json={"code": 200, "data": {"task_id": "c"}})
    )
    respx.post("http://127.0.0.1:8001/query_result").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"task_id": "c", "status": 2}]},
        )
    )

    with pytest.raises(CanaryError, match="generation failure"):
        await run_ace_step_canary("http://127.0.0.1:8001", _profile(), poll_interval=0)


@pytest.mark.asyncio
@respx.mock
async def test_canary_download_is_bounded_while_streaming(monkeypatch):
    monkeypatch.setattr(canary_module, "MAX_CANARY_BYTES", 32)
    respx.get("http://127.0.0.1:8001/health").mock(
        return_value=httpx.Response(200, json={"data": {"status": "ok"}})
    )
    respx.post("http://127.0.0.1:8001/release_task").mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"task_id": "canary-large"}},
        )
    )
    respx.post("http://127.0.0.1:8001/query_result").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "task_id": "canary-large",
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

    with pytest.raises(CanaryError, match="safety bound"):
        await run_ace_step_canary(
            "http://127.0.0.1:8001",
            _profile(),
            poll_interval=0,
        )

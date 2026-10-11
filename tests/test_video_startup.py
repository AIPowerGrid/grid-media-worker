# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Governed startup compatibility; mocked inventories are not GPU qualification."""

import asyncio
import copy
import os
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from bridge.config import Settings
from bridge import video_startup as startup
from bridge.ws_worker import WSWorker

MODEL = "LTX-2.5 Distilled BF16"
CORE = "https://grid.test"
COMFY = "http://127.0.0.1:8188"
RECIPE_URL = f"{CORE}/v1/models/{quote(MODEL, safe='')}/recipe"
POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="Durable video startup requires qualified POSIX state"
)


@pytest.fixture
def inventories():
    requirements = {
        "node_types": ["ModelLoader", "AudioDecoder"],
        "model_files": ["video.safetensors"],
    }
    audio = {
        "node_types": ["ModelLoader", "AudioEncoder"],
        "model_files": ["video.safetensors"],
    }
    video = {
        "node_types": ["ModelLoader", "AudioEncoder", "UnionLoader"],
        "model_files": ["video.safetensors", "union.safetensors"],
    }
    record = {
        **requirements,
        "job_type": "video",
        "recipe_root": "0x" + "a" * 64,
        "video_contract": {
            "schema": "aipg.ltx-video.v1",
            "variant": "distilled",
            "source_modes": ["audio", "video"],
        },
        "source_requirements": {"audio": audio, "video": video},
    }
    info = {
        name: {"input": {"required": {"file": [files]}}}
        for name, files in (
            ("ModelLoader", ["video.safetensors"]),
            ("AudioDecoder", []),
            ("AudioEncoder", []),
            ("UnionLoader", ["union.safetensors"]),
        )
    }
    info["UnionLoader"]["input"]["required"]["file"] = [
        "COMBO",
        {"multiselect": False, "options": ["union.safetensors"]},
    ]
    return {"model": MODEL, "recipes": [record]}, info


@pytest.mark.parametrize(
    "definition",
    [
        [["video.safetensors"]],
        ["COMBO", {"options": ["video.safetensors"], "multiselect": False}],
    ],
)
def test_legacy_and_v3_loader_choices(definition):
    startup._require_files(
        {"node_types": ["ModelLoader"], "model_files": ["video.safetensors"]},
        {"ModelLoader": {"input": {"required": {"file": definition}}}},
    )


@pytest.mark.parametrize(
    "definition",
    [
        ["COMBO"],
        ["COMBO", {}],
        ["COMBO", {"options": "video.safetensors"}],
        ["COMBO", {"options": []}],
        ["STRING", {"default": "video.safetensors"}],
    ],
)
def test_missing_or_non_combo_choices_cannot_supply_a_weight(definition):
    with pytest.raises(startup.VideoStartupError, match="weight"):
        startup._require_files(
            {"node_types": ["ModelLoader"], "model_files": ["video.safetensors"]},
            {"ModelLoader": {"input": {"required": {"file": definition}}}},
        )


def options(tmp_path, **overrides):
    return {
        "models": [MODEL],
        "job_types": ["video"],
        "source_modes": ["audio", "video"],
        "grid_api_url": CORE,
        "api_key": "synthetic-worker-key",
        "comfy_url": COMFY,
        "state_dir": str(tmp_path / "private-state"),
        **overrides,
    }


@pytest.mark.asyncio
@POSIX_ONLY
@pytest.mark.parametrize(
    "modes,expected",
    [
        ([], ("async-video-resume-v1",)),
        (["audio", "video"], ("async-video-resume-v1", "recipe-media-sources-v1")),
    ],
)
async def test_read_only_startup_checks_exact_model_and_separates_credentials(
    tmp_path,
    inventories,
    modes,
    expected,
    monkeypatch,
):
    records, info = inventories
    original = copy.deepcopy(inventories)
    real_client = httpx.AsyncClient
    client_options = []

    def client(**kwargs):
        client_options.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(startup.httpx, "AsyncClient", client)
    with respx.mock() as routes:
        recipe = routes.get(RECIPE_URL).respond(200, json=records)
        local = routes.get(COMFY + "/object_info").respond(200, json=info)
        assert (
            await startup.qualify_video_startup(
                **options(tmp_path, source_modes=modes),
            )
            == expected
        )
        assert len(routes.calls) == 2
        assert all(call.request.method == "GET" for call in routes.calls)
        assert recipe.calls[0].request.headers["apikey"] == "synthetic-worker-key"
        assert "apikey" not in local.calls[0].request.headers
        assert "authorization" not in local.calls[0].request.headers
    assert client_options[0]["follow_redirects"] is False
    assert client_options[0]["trust_env"] is False
    assert inventories == original
    assert not (tmp_path / "private-state" / "renders.sqlite3").exists()
    assert (tmp_path / "private-state").stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
@POSIX_ONLY
@pytest.mark.parametrize(
    "overrides",
    [
        {"models": []},
        {"models": [MODEL, MODEL]},
        {"models": ["LTX-2.3"]},
        {"job_types": ["video", "image"]},
        {"state_dir": ""},
        {"api_key": ""},
        {"source_modes": ["other"]},
        {"source_modes": ["audio", "audio"]},
        {"comfy_url": "http://remote.test:8188"},
        {"comfy_url": "http://user:password@localhost:8188"},
        {"comfy_url": COMFY + "/nested"},
        {"comfy_url": COMFY + "?credential=bad"},
        {"grid_api_url": "http://remote.test"},
        {"grid_api_url": "https://user:password@grid.test"},
    ],
)
async def test_invalid_configuration_never_contacts_core_or_comfy(tmp_path, overrides):
    with respx.mock() as routes:
        with pytest.raises(startup.VideoStartupError):
            await startup.qualify_video_startup(**options(tmp_path, **overrides))
        assert not routes.calls


@pytest.mark.asyncio
@POSIX_ONLY
async def test_private_state_gate_before_http(tmp_path):
    state = tmp_path / "private-state"
    state.mkdir(mode=0o755)
    with respx.mock() as routes:
        with pytest.raises(startup.VideoStartupError):
            await startup.qualify_video_startup(**options(tmp_path))
        assert not routes.calls


@pytest.mark.asyncio
async def test_non_posix_gate_before_state_or_http(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "os", SimpleNamespace(name="nt"))
    with respx.mock() as routes:
        with pytest.raises(startup.VideoStartupError, match="POSIX"):
            await startup.qualify_video_startup(**options(tmp_path))
        assert not routes.calls
    assert not (tmp_path / "private-state").exists()


@pytest.mark.asyncio
@POSIX_ONLY
@pytest.mark.parametrize(
    "missing", ["node", "weight", "misplaced-weight", "source-contract"]
)
async def test_missing_source_requirements_do_not_advertise(
    tmp_path, inventories, missing
):
    records, info = inventories
    if missing == "node":
        del info["UnionLoader"]
    elif missing == "source-contract":
        del records["recipes"][0]["source_requirements"]["video"]
    else:
        info["UnionLoader"]["input"]["required"]["file"] = [[]]
        if missing == "misplaced-weight":
            info["UnrelatedLoader"] = {
                "input": {"required": {"file": [["union.safetensors"]]}}
            }
    with respx.mock() as routes:
        routes.get(RECIPE_URL).respond(200, json=records)
        routes.get(COMFY + "/object_info").respond(200, json=info)
        with pytest.raises(startup.VideoStartupError):
            await startup.qualify_video_startup(**options(tmp_path))


@pytest.mark.asyncio
@POSIX_ONLY
async def test_source_free_worker_does_not_need_union_inventory(tmp_path, inventories):
    records, info = inventories
    del info["UnionLoader"]
    del info["AudioEncoder"]
    with respx.mock() as routes:
        routes.get(RECIPE_URL).respond(200, json=records)
        routes.get(COMFY + "/object_info").respond(200, json=info)
        assert await startup.qualify_video_startup(
            **options(tmp_path, source_modes=[]),
        ) == ("async-video-resume-v1",)


@pytest.mark.asyncio
@POSIX_ONLY
@pytest.mark.parametrize(
    "change", ["model", "schema", "variant", "duplicate-root", "partial-mode"]
)
async def test_governed_identity_and_complete_source_feature(
    tmp_path, inventories, change
):
    records, info = inventories
    args = options(tmp_path)
    if change == "model":
        records["model"] = "different-model"
    elif change == "schema":
        records["recipes"][0]["video_contract"]["schema"] = "unknown"
    elif change == "variant":
        records["recipes"][0]["video_contract"]["variant"] = "dev"
    elif change == "duplicate-root":
        records["recipes"].append(copy.deepcopy(records["recipes"][0]))
    else:
        args["source_modes"] = ["audio"]
    with respx.mock() as routes:
        routes.get(RECIPE_URL).respond(200, json=records)
        routes.get(COMFY + "/object_info").respond(200, json=info)
        with pytest.raises(startup.VideoStartupError):
            await startup.qualify_video_startup(**args)


@pytest.mark.asyncio
@POSIX_ONLY
@pytest.mark.parametrize("failure", ["redirect", "status", "size", "json", "deadline"])
async def test_bounded_inventory_failures(tmp_path, inventories, monkeypatch, failure):
    records, info = inventories
    if failure == "size":
        monkeypatch.setattr(startup, "MAX_RECIPE_BYTES", 10)
    if failure == "deadline":
        monkeypatch.setattr(startup, "STARTUP_DEADLINE_SECONDS", 0.01)

    async def late(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=records)

    with respx.mock(assert_all_called=False) as routes:
        recipe = routes.get(RECIPE_URL)
        if failure == "deadline":
            recipe.mock(side_effect=late)
        elif failure == "redirect":
            recipe.respond(302, headers={"location": "https://other.test"})
        elif failure == "status":
            recipe.respond(401)
        elif failure == "json":
            recipe.respond(200, content=b"{")
        else:
            recipe.respond(200, json=records)
        local = routes.get(COMFY + "/object_info").respond(200, json=info)
        with pytest.raises(startup.VideoStartupError):
            await startup.qualify_video_startup(**options(tmp_path))
        assert len(routes.calls) <= 1
        assert not local.called


@pytest.fixture
def video_settings(monkeypatch, tmp_path):
    for name, value in {
        "GRID_ASYNC_VIDEO": True,
        "GRID_VIDEO_SOURCE_MODES": ["audio", "video"],
        "GRID_PROFILE_PATH": "",
        "GRID_TRUST_MODELS": False,
        "GRID_PREFLIGHT": False,
        "GRID_MODELS": [MODEL],
        "GRID_JOB_TYPES": ["video"],
        "GRID_SCHEDULE": "",
        "THREADS": 1,
        "GRID_API_URL": CORE,
        "GRID_API_KEY": "synthetic-worker-key",
        "GRID_COMFYUI_STATE_DIR": str(tmp_path / "private-state"),
        "COMFYUI_URL": COMFY,
    }.items():
        monkeypatch.setattr(Settings, name, value)
    import bridge.ws_worker as ws

    monkeypatch.setattr(ws, "fetch_priced_model_names", AsyncMock(return_value=None))
    return ws


@pytest.mark.asyncio
@POSIX_ONLY
async def test_normal_run_advertises_only_after_verified_startup(
    video_settings, inventories, monkeypatch
):
    records, info = inventories
    worker = WSWorker()
    health = AsyncMock()
    monkeypatch.setattr(worker, "_check_runtime_health", health)
    monkeypatch.setattr(worker, "_wait_until_available", AsyncMock())
    payloads = []

    async def session():
        payloads.append(worker.registration_payload())
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_session", session)
    try:
        assert worker.registration_payload()["media_features"] == [
            "recipe-image-bindings-v1"
        ]
        with respx.mock() as routes:
            routes.get(RECIPE_URL).respond(200, json=records)
            routes.get(COMFY + "/object_info").respond(200, json=info)
            with pytest.raises(asyncio.CancelledError):
                await worker.run()
        assert payloads[0]["models"] == [MODEL]
        assert payloads[0]["job_types"] == ["video"]
        assert payloads[0]["media_features"] == [
            "recipe-image-bindings-v1",
            "async-video-resume-v1",
            "recipe-media-sources-v1",
        ]
        health.assert_awaited_once()
    finally:
        await worker.comfy.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bypass",
    ["trust", "profile", "flag-off", pytest.param("bad-inventory", marks=POSIX_ONLY)],
)
async def test_normal_run_rejects_bypasses_before_registration(
    video_settings,
    inventories,
    monkeypatch,
    bypass,
):
    if bypass == "trust":
        monkeypatch.setattr(Settings, "GRID_TRUST_MODELS", True)
    elif bypass == "profile":
        monkeypatch.setattr(Settings, "GRID_PROFILE_PATH", "unsigned-draft.json")
    elif bypass == "flag-off":
        monkeypatch.setattr(Settings, "GRID_ASYNC_VIDEO", False)
    records, info = inventories
    del info["UnionLoader"]
    worker = WSWorker()
    session = AsyncMock()
    monkeypatch.setattr(worker, "_session", session)
    monkeypatch.setattr(worker, "_check_runtime_health", AsyncMock())
    try:
        with respx.mock() as routes:
            if bypass == "bad-inventory":
                routes.get(RECIPE_URL).respond(200, json=records)
                routes.get(COMFY + "/object_info").respond(200, json=info)
            with pytest.raises(RuntimeError):
                await worker.run()
        session.assert_not_awaited()
        assert worker.registration_payload()["media_features"] == [
            "recipe-image-bindings-v1"
        ]
    finally:
        await worker.comfy.aclose()

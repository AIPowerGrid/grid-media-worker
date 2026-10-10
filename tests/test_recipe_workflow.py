import copy
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response

from bridge.workflow import build_recipe_workflow, download_image, recipe_image_output


def image_payload():
    return {
        "recipe_spec": {
            "first": {"class_type": "LoadImage", "inputs": {"image": "unbound.png"}},
            "last": {"class_type": "LoadImage", "inputs": {"image": "unbound.png"}},
        },
        "source_image_url": "https://source.invalid/first.png",
        "source_image_urls": ["https://source.invalid/first.png", "https://source.invalid/last.png"],
        "recipe_image_bindings": [
            {"image_index": 0, "path": "first.inputs.image"},
            {"image_index": 1, "path": "last.inputs.image"},
        ],
        "recipe_image_inputs": "first.inputs.image",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("same_image", [False, True])
async def test_recipe_timed_images_use_exact_bindings_and_deduplicate_uploads(monkeypatch, same_image):
    payload = image_payload()
    if same_image:
        payload["source_image_urls"][1] = payload["source_image_urls"][0]
    before = copy.deepcopy(payload)
    download = AsyncMock(side_effect=lambda _url, filename: filename)
    monkeypatch.setattr("bridge.workflow.download_image", download)
    result = await build_recipe_workflow({"id": "pins-test"}, payload)
    assert result["first"]["inputs"]["image"] == "src_pins-test_0.png"
    assert result["last"]["inputs"]["image"] == f"src_pins-test_{0 if same_image else 1}.png"
    assert download.await_count == (1 if same_image else 2)
    assert payload == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"source_image_urls": []}, {"source_image_urls": ["https://source.invalid/first.png"]},
    {"source_image_urls": [None, None]}, {"source_image_urls": "not-a-list"},
    {"recipe_image_bindings": []},
    {"recipe_image_bindings": [{"image_index": False, "path": "first.inputs.image"}, {"image_index": 1, "path": "last.inputs.image"}]},
    {"recipe_image_bindings": [{"image_index": 0, "path": "first.inputs.image"}, {"image_index": 1, "path": "first.inputs.image"}]},
    {"recipe_image_bindings": [{"image_index": 0, "path": "first.inputs.image"}, {"image_index": 1, "path": "last.inputs.filename"}]},
    {"recipe_image_bindings": [{"image_index": 0, "path": "first.inputs.image"}, {"image_index": 2, "path": "last.inputs.image"}]},
    {"recipe_image_bindings": [{"image_index": 0, "path": "first.inputs.image", "extra": 1}, {"image_index": 1, "path": "last.inputs.image"}]},
])
async def test_malformed_timed_image_contract_fails_before_download(monkeypatch, change):
    payload = {**image_payload(), **change}
    download = AsyncMock()
    monkeypatch.setattr("bridge.workflow.download_image", download)
    with pytest.raises(RuntimeError):
        await build_recipe_workflow({"id": "bad-pins"}, payload)
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_binding_never_falls_back_to_one_image(monkeypatch):
    payload = image_payload()
    del payload["recipe_image_bindings"]
    download = AsyncMock()
    monkeypatch.setattr("bridge.workflow.download_image", download)
    with pytest.raises(RuntimeError, match="explicit image bindings"):
        await build_recipe_workflow({"id": "bad-pins"}, payload)
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_image_node_rejects_before_download(monkeypatch):
    payload = image_payload()
    payload["recipe_spec"]["extra"] = {"class_type": "LoadImage", "inputs": {"image": "unbound.png"}}
    download = AsyncMock()
    monkeypatch.setattr("bridge.workflow.download_image", download)
    with pytest.raises(RuntimeError, match="unbound LoadImage"):
        await build_recipe_workflow({"id": "bad-pins"}, payload)
    download.assert_not_awaited()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("content", [b"", b"x" * 9])
async def test_source_download_is_bounded_before_comfy_upload(monkeypatch, content):
    monkeypatch.setattr("bridge.workflow.MAX_SOURCE_IMAGE_BYTES", 8)
    respx.get("https://source.invalid/image.png").mock(return_value=Response(200, content=content))
    with pytest.raises(RuntimeError, match="empty or exceeds"):
        await download_image("https://source.invalid/image.png", "source.png")
    assert len(respx.calls) == 1


@pytest.mark.asyncio
async def test_recipe_binds_actual_comfy_filename_after_collision(monkeypatch):
    payload = image_payload()
    download = AsyncMock(side_effect=["first (1).png", "last (1).png"])
    monkeypatch.setattr("bridge.workflow.download_image", download)
    result = await build_recipe_workflow({"id": "retry"}, payload)
    assert result["first"]["inputs"]["image"] == "first (1).png"
    assert result["last"]["inputs"]["image"] == "last (1).png"


@pytest.mark.asyncio
async def test_recipe_outputs_get_job_unique_filename_prefix():
    spec = {
        "1": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["2", 0], "filename_prefix": "video/LTX"},
        },
        "2": {"class_type": "CreateVideo", "inputs": {}},
    }
    payload = {"recipe_spec": spec, "recipe_engine": "comfyui"}

    workflow = await build_recipe_workflow({"id": "job-123"}, payload)

    assert workflow["1"]["inputs"]["filename_prefix"] == "grid_job-123"
    assert spec["1"]["inputs"]["filename_prefix"] == "video/LTX"


@pytest.mark.asyncio
@pytest.mark.parametrize("latent_type", ["EmptyLatentImage", "EmptySD3LatentImage", "EmptyFlux2LatentImage"])
async def test_recipe_batch_sets_empty_latent_count_without_mutating_spec(latent_type):
    spec = {"latent": {"class_type": latent_type, "inputs": {"batch_size": 1}}}
    result = await build_recipe_workflow(
        {"id": "batch-test"}, {"recipe_spec": spec, "batch_size": 2}
    )
    assert result["latent"]["inputs"]["batch_size"] == 2
    assert spec["latent"]["inputs"]["batch_size"] == 1


@pytest.mark.parametrize("kind,field", [("RandomNoise", "noise_seed"), ("KSampler", "seed"), ("KSamplerAdvanced", "noise_seed")])
def test_recipe_independent_seed_and_single_output(kind, field):
    spec = {
        "noise": {"class_type": kind, "inputs": {field: 42}},
        "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {"batch_size": 3}},
        "save": {"class_type": "SaveImage", "inputs": {"filename_prefix": "grid_job"}},
    }
    result = recipe_image_output(spec, 99, 1)
    assert result["noise"]["inputs"][field] == 99
    assert result["latent"]["inputs"]["batch_size"] == 1
    assert result["save"]["inputs"]["filename_prefix"] == "grid_job_1"
    assert spec["noise"]["inputs"][field] == 42
    assert spec["latent"]["inputs"]["batch_size"] == 3


@pytest.mark.parametrize("spec", [
    {},
    {"noise": {"class_type": "UnknownNoise", "inputs": {"seed": 42}}},
    {"noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": ["primitive", 0]}}},
    {"a": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
     "b": {"class_type": "KSampler", "inputs": {"seed": 1}}},
    {"noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
     "batch": {"class_type": "UnknownBatch", "inputs": {"batch_size": 2}}},
])
def test_recipe_batch_rejects_unsupported_or_ambiguous_seed_graphs(spec):
    with pytest.raises(RuntimeError):
        recipe_image_output(spec, 99, 0)

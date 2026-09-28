import pytest

from bridge.workflow import build_recipe_workflow


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

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

import pytest

from bridge.workflow import build_recipe_workflow, recipe_image_output


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

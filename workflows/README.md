# ComfyUI Bridge Workflow Templates

> **Adding a NEW model? Don't start here.** These `_bridge` workflow templates are
> the legacy worker-side path. New image/video models are added as **governed recipes**
> in grid-core — see `grid-core/docs/architecture/RECIPE_DISPATCH.md`. The recipe flow
> auto-detects the node map for you and adds clamp/enum governance the `_bridge` format
> never had. This file is kept only for the models still on the legacy path.

This directory contains workflow templates for the ComfyUI Bridge. Each workflow file is a JSON representation of a ComfyUI workflow.

## How to Use

1. Create a JSON file with your ComfyUI workflow
2. Use placeholder values that will be replaced by the bridge:
   - `POSITIVE_PROMPT_PLACEHOLDER` - Will be replaced with the user's prompt
   - `NEGATIVE_PROMPT_PLACEHOLDER` - Will be replaced with the user's negative prompt
3. Run the bridge with your selected workflow file:
   ```
   python start_bridge.py --workflow your_workflow.json
   ```
   
   Alternatively, you can set the workflow file in your `.env` file:
   ```
   WORKFLOW_FILE=your_workflow.json
   ```

## Dynamic Values

The bridge will automatically update the following values in your workflow:

- `seed` - Replaced with the job's seed value
- `steps` - Replaced with the job's steps value
- `cfg` - Replaced with the job's CFG scale value
- `sampler_name` - Replaced with the job's sampler name (mapped to ComfyUI compatibility)
- `width` and `height` - Replaced with the job's requested dimensions
- `filename_prefix` - Replaced with a job-specific ID for the output file

## Available Workflow Templates

The repository includes several pre-made templates:

- `flux2_klein_4b_api.json` / `flux2_klein_4b_image_edit.json` - FLUX.2 Klein 4B (txt2img / img2img)
- `flux1.dev.json` - FLUX.1 dev
- `flux_kontext_dev_basic_api.json` - FLUX Kontext dev
- `sdxl.json` / `sdxl1.json` - SDXL Base 1.0 (+ refiner)
- `turbovision.json` - TurboVision XL
- `Chroma_final.json` - Chroma
- `wan2_2_t2v_14b.json` - Wan 2.2 text-to-video 14B

Note: a template being present only makes the model *servable* by your rig;
whether it is *sellable* on the Grid is decided by the price book and the
on-chain model/recipe registry (see the main README's "Which models can earn").

## How to Get a Workflow File

1. Create your workflow in ComfyUI
2. Click the "Save" button to download the workflow as a JSON file
3. Copy the JSON file to this directory
4. Run the bridge with your selected workflow

## Special Node Requirements

For the bridge to properly update your workflow, it should contain:

1. A `KSampler` node (for updating sampling parameters)
2. `CLIPTextEncode` nodes with the placeholders mentioned above
3. An `EmptyLatentImage` node (for setting dimensions)
4. A `SaveImage` node (for saving the output with the correct filename)

The bridge will try to find these nodes by their class type, regardless of the node IDs used in your workflow. 
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from bridge.model_mapper import ModelMapper, is_retired_model


def test_legacy_krea_models_are_retired_from_default_mapping():
    assert is_retired_model("flux.1-krea-dev")
    assert is_retired_model("Flux.1-Krea-dev Uncensored (fp8+CLIP+VAE)")
    assert is_retired_model("flux1_krea_dev_fp8_scaled")
    assert not is_retired_model("Krea 2 Turbo")
    assert all(
        not is_retired_model(model_name)
        for model_name in ModelMapper.DEFAULT_WORKFLOW_MAP
    )


def test_stale_reference_cannot_restore_retired_krea(tmp_path, monkeypatch):
    workflow = tmp_path / "legacy.json"
    workflow.write_text(
        '{"1":{"class_type":"UNETLoader","inputs":'
        '{"unet_name":"flux1-krea-dev_fp8_scaled.safetensors"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("bridge.model_mapper.Settings.WORKFLOW_DIR", str(tmp_path))
    monkeypatch.setattr("bridge.model_mapper.Settings.WORKFLOW_FILE", workflow.name)

    mapper = ModelMapper()
    mapper.reference_file_to_grid_name = {
        "flux1-krea-dev_fp8_scaled.safetensors": "flux.1-krea-dev"
    }
    mapper._build_workflow_map_from_env()

    assert "flux.1-krea-dev" not in mapper.workflow_map

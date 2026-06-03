"""Tests for Task 027 model-density loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from batch_runner.sizing.model_density import (
    DEFAULT_DENSITY_PATH,
    ModelDensityError,
    UnknownModelError,
    UnknownOutputRatioError,
    input_tpm_per_ptu,
    load_density_snapshot,
    output_ratio_provenance,
    output_to_input_ratio,
)


def test_density_yaml_is_valid():
    with DEFAULT_DENSITY_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert data["guide_publication_date"] == "2026-05"
    assert data["learn_url"].startswith("https://learn.microsoft.com/")


def test_input_tpm_per_ptu_guide_table_core_entries():
    assert input_tpm_per_ptu("gpt-5.2") == 3400
    assert input_tpm_per_ptu("gpt-5.2-codex") == 3400
    assert input_tpm_per_ptu("gpt-5.3-codex") == 3400
    assert input_tpm_per_ptu("gpt-5.1") == 4750
    assert input_tpm_per_ptu("gpt-5.1-codex") == 4750
    assert input_tpm_per_ptu("gpt-5") == 4750
    assert input_tpm_per_ptu("gpt-4.1") == 3000
    assert input_tpm_per_ptu("gpt-4o") == 2500


def test_input_tpm_per_ptu_reference_entries():
    assert input_tpm_per_ptu("gpt-5.4") == 2400
    assert input_tpm_per_ptu("gpt-5.5") == 1200
    assert input_tpm_per_ptu("gpt-5-mini") == 23750
    assert input_tpm_per_ptu("gpt-4o-mini") == 37000


def test_explicit_output_ratios_match_guide_examples():
    assert output_to_input_ratio("gpt-5") == 8.0
    assert output_ratio_provenance("gpt-5") == "official_spec"
    assert output_to_input_ratio("gpt-4.1") == 4.0
    assert output_ratio_provenance("gpt-4.1") == "official_spec"


def test_gpt_5_2_and_5_1_ratio_is_operational_inference():
    assert output_to_input_ratio("gpt-5.2") == 8.0
    assert output_ratio_provenance("gpt-5.2") == "operational_inference"
    assert output_to_input_ratio("gpt-5.1") == 8.0
    assert output_ratio_provenance("gpt-5.1") == "operational_inference"


def test_gpt_4o_unspecified_ratio_uses_neutral_weight():
    assert output_to_input_ratio("gpt-4o") == 1.0
    assert output_ratio_provenance("gpt-4o") == "unspecified"


def test_unknown_model_rejected():
    with pytest.raises(UnknownModelError):
        input_tpm_per_ptu("gpt-missing")
    with pytest.raises(UnknownModelError):
        output_to_input_ratio("gpt-missing")


def test_known_reference_model_without_ratio_rejected():
    with pytest.raises(UnknownOutputRatioError):
        output_to_input_ratio("gpt-5.4")


def test_snapshot_mappings_are_readonly():
    snapshot = load_density_snapshot()
    with pytest.raises(TypeError):
        snapshot.input_tpm_per_ptu["gpt-new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.output_to_input_token_ratio["gpt-new"] = 1.0  # type: ignore[index]


def test_operational_inference_label_is_present_in_yaml_text():
    text = DEFAULT_DENSITY_PATH.read_text(encoding="utf-8")
    assert "# operational inference" in text


def test_loader_rejects_ratio_for_unknown_model(tmp_path: Path):
    path = tmp_path / "bad-density.yaml"
    path.write_text(
        "\n".join(
            [
                'guide_publication_date: "2026-05"',
                'guide_source: "test"',
                'learn_url: "https://learn.microsoft.com/example"',
                "input_tpm_per_ptu:",
                "  gpt-5.2: 3400",
                "output_to_input_token_ratio:",
                "  explicit:",
                "    gpt-other: 8",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelDensityError):
        load_density_snapshot(path)

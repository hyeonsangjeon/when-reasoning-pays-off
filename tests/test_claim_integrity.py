"""Mutation tests for the current public-claim contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import check_claim_integrity
from scripts.check_claim_integrity import (
    DEFAULT_CONTRACT,
    DEFAULT_README,
    END_MARKER,
    PAUSE_MARKER,
    REPO_ROOT,
    RESUME_MARKER,
    START_MARKER,
    ClaimIntegrityError,
    _decimal,
    _resolve_reference,
    check_readme,
    load_contract,
    render_readme_block,
    replace_readme_block,
    run_check,
    validate_contract,
)


def _contract() -> dict:
    return load_contract(DEFAULT_CONTRACT)


def _claim(contract: dict, claim_id: str) -> dict:
    return next(item for item in contract["claims"] if item["claim_id"] == claim_id)


def _comparison(contract: dict, claim_id: str) -> dict:
    return next(
        item for item in contract["comparisons"] if item["claim_id"] == claim_id
    )


def test_current_contract_and_readme_pass():
    measurements, comparisons = run_check()
    assert measurements >= 9
    assert comparisons >= 2


def test_contract_schema_is_valid_and_accepts_contract():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (REPO_ROOT / "schemas/public_claim_contract.v1.schema.json").read_text()
    )
    jsonschema.Draft7Validator.check_schema(schema)
    jsonschema.validate(_contract(), schema)


def test_mutated_value_fails():
    contract = _contract()
    claim = _claim(contract, "short-factual-cost-xhigh")
    claim["value"] = 0.00598
    claim["format"] = "$0.005980"
    with pytest.raises(ClaimIntegrityError, match="analysis source"):
        validate_contract(REPO_ROOT, contract)


def test_minimal_current_cohort_fails():
    contract = _contract()
    claim = _claim(contract, "short-factual-cost-none")
    claim["effort"] = "minimal"
    with pytest.raises(ClaimIntegrityError, match="approved slot"):
        validate_contract(REPO_ROOT, contract)


def test_legacy_headline_fails():
    contract = _contract()
    rendered = render_readme_block(contract).replace("1.02x", "7.6x")
    with pytest.raises(ClaimIntegrityError, match="legacy current headline"):
        check_readme(rendered, contract)


@pytest.mark.parametrize(
    "legacy",
    ["7.6x", "7.6×", "4 -> 311", "4 → 311", "~4 to ~311", "~311"],
)
def test_legacy_headline_outside_marker_fails(legacy: str):
    contract = _contract()
    readme = DEFAULT_README.read_text(encoding="utf-8") + f"\nCurrent: {legacy}\n"
    with pytest.raises(ClaimIntegrityError, match="legacy current headline"):
        check_readme(readme, contract)


def test_effort_only_attribution_for_model_change_fails():
    contract = _contract()
    comparison = _comparison(contract, "multi-step-baseline-to-none-quality")
    comparison["changed_dimensions"] = ["effort"]
    comparison["causal_attribution"] = "descriptive"
    with pytest.raises(ClaimIntegrityError, match="changed_dimensions"):
        validate_contract(REPO_ROOT, contract)


def test_missing_source_fails():
    contract = _contract()
    _claim(contract, "short-factual-cost-none")["source"]["path"] = (
        "benchmarks/99-missing/analysis.json"
    )
    with pytest.raises(ClaimIntegrityError, match="evidence paths"):
        validate_contract(REPO_ROOT, contract)


def test_wrong_selector_fails():
    contract = _contract()
    _claim(contract, "short-factual-cost-none")["source"]["selector"]["field"] = (
        "not_a_metric"
    )
    with pytest.raises(ClaimIntegrityError, match="selector contradicts"):
        validate_contract(REPO_ROOT, contract)


def test_readme_marker_is_exact_contract_render():
    contract = _contract()
    readme = DEFAULT_README.read_text(encoding="utf-8")
    check_readme(readme, contract)


def test_duplicate_selector_rows_fail(tmp_path: Path):
    claim = _claim(_contract(), "short-factual-cost-none")
    source = json.loads((REPO_ROOT / claim["source"]["path"]).read_text())
    source["cell_stats"].append(copy.deepcopy(source["cell_stats"][1]))
    local = tmp_path / "analysis.json"
    local.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ClaimIntegrityError, match="resolved 2 rows"):
        _resolve_reference(
            tmp_path,
            {
                "path": "analysis.json",
                "selector": claim["source"]["selector"],
            },
            label="duplicate fixture",
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_claim_values_fail(value: float):
    with pytest.raises(ClaimIntegrityError, match="finite"):
        _decimal(value, label="mutation")


@pytest.mark.parametrize(
    "marker", [START_MARKER, PAUSE_MARKER, RESUME_MARKER, END_MARKER]
)
def test_missing_and_duplicate_markers_fail(marker: str):
    contract = _contract()
    rendered = render_readme_block(contract)
    with pytest.raises(ClaimIntegrityError, match="exactly one"):
        check_readme(rendered.replace(marker, ""), contract)
    with pytest.raises(ClaimIntegrityError, match="exactly one"):
        check_readme(rendered + "\n" + marker, contract)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (START_MARKER, PAUSE_MARKER),
        (PAUSE_MARKER, RESUME_MARKER),
        (RESUME_MARKER, END_MARKER),
    ],
)
def test_out_of_order_markers_fail(first: str, second: str):
    contract = _contract()
    rendered = (
        render_readme_block(contract)
        .replace(first, "<!-- placeholder -->", 1)
        .replace(second, first, 1)
        .replace("<!-- placeholder -->", second, 1)
    )
    with pytest.raises(ClaimIntegrityError, match="out of order"):
        check_readme(rendered, contract)


@pytest.mark.parametrize("region", [0, 1])
@pytest.mark.parametrize(
    ("original", "mutation"),
    [("1.02x", "1.03x"), ("GPT-5.2 short-factual cohort", "All workloads")],
)
def test_front_and_detailed_claims_require_exact_values_and_scope(
    region: int, original: str, mutation: str
):
    contract = _contract()
    regions = render_readme_block(contract).split(RESUME_MARKER)
    regions[region] = regions[region].replace(original, mutation, 1)
    with pytest.raises(ClaimIntegrityError, match="contract render"):
        check_readme(RESUME_MARKER.join(regions), contract)


def test_render_preserves_readme_layout():
    readme = DEFAULT_README.read_text(encoding="utf-8")
    assert replace_readme_block(readme, render_readme_block(_contract())) == readme


def test_render_write_preserves_unmanaged_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gap = "\n\n## Quick start\n\nKeep these commands and contracts unchanged.\n\n"
    rendered = render_readme_block(_contract())
    expected = (
        "Introduction\n\n"
        + rendered.replace(
            f"{PAUSE_MARKER}\n\n{RESUME_MARKER}",
            f"{PAUSE_MARKER}{gap}{RESUME_MARKER}",
        )
        + "\n\nFurther documentation\n"
    )
    readme = tmp_path / "README.md"
    readme.write_text(expected.replace("1.02x", "1.03x"), encoding="utf-8")
    monkeypatch.setattr(check_claim_integrity, "DEFAULT_README", readme)

    assert check_claim_integrity.main(["render", "--write"]) == 0
    assert readme.read_text(encoding="utf-8") == expected
    check_readme(expected, _contract())


def test_render_write_refuses_missing_gap_marker_without_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original = render_readme_block(_contract()).replace(RESUME_MARKER, "")
    readme = tmp_path / "README.md"
    readme.write_text(original, encoding="utf-8")
    monkeypatch.setattr(check_claim_integrity, "DEFAULT_README", readme)

    assert check_claim_integrity.main(["render", "--write"]) == 1
    assert readme.read_text(encoding="utf-8") == original


def test_ratio_format_must_match_rounded_derived_value():
    contract = _contract()
    _comparison(contract, "short-factual-cost-none-to-xhigh")["format"] = "1.01x"
    with pytest.raises(ClaimIntegrityError, match="rounded derived ratio"):
        validate_contract(REPO_ROOT, contract)


def test_unregistered_measurement_format_cannot_be_fabricated():
    contract = _contract()
    _claim(contract, "short-factual-quality-xhigh")["format"] = "99.0"
    with pytest.raises(ClaimIntegrityError, match="format does not match"):
        validate_contract(REPO_ROOT, contract)


def test_selector_cannot_use_another_effort_row():
    contract = _contract()
    claim = _claim(contract, "short-factual-cost-xhigh")
    claim["source"]["selector"]["where"]["effort"] = "none"
    claim["chart_cross_check"]["selector"]["where"]["effort"] = "none"
    with pytest.raises(ClaimIntegrityError, match="selector contradicts"):
        validate_contract(REPO_ROOT, contract)


def test_contract_cannot_reference_itself_as_evidence():
    contract = _contract()
    claim = _claim(contract, "short-factual-cost-none")
    claim["source"]["path"] = (
        "batch-runner/batch_runner/data/public_claims.v1.json"
    )
    with pytest.raises(ClaimIntegrityError, match="evidence paths"):
        validate_contract(REPO_ROOT, contract)


def test_claim_id_cannot_be_rebound_to_equal_valued_effort():
    contract = _contract()
    claim = _claim(contract, "short-factual-quality-none")
    claim["effort"] = "xhigh"
    claim["source"]["selector"]["where"]["effort"] = "xhigh"
    claim["chart_cross_check"]["selector"]["where"]["effort"] = "xhigh"
    with pytest.raises(ClaimIntegrityError, match="approved slot"):
        validate_contract(REPO_ROOT, contract)

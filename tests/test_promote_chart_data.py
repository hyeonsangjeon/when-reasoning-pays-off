"""Tests for scripts/promote_chart_data.py and the first-tranche chart data.

Covers the first public chart-data tranche:
  * the emitted chart-data payloads are numeric-only and carry no dropped
    pricing-provenance / prose columns;
  * the candidate manifest validates against
    schemas/public_chart_candidates.schema.json and is consistent with the
    on-disk chart files;
  * the generator is deterministic/idempotent (`--check` passes against the
    committed tree);
  * the promotion-set redaction scanner finds the emitted surface clean.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
CHART_ROOT = REPO_ROOT / "results" / "public" / "chart-data"
CANDIDATE_MANIFEST = REPO_ROOT / "release" / "public_chart_candidates.json"
CANDIDATE_SCHEMA = REPO_ROOT / "schemas" / "public_chart_candidates.schema.json"

# Columns that MUST NOT survive into any emitted chart payload.
FORBIDDEN_KEYS = frozenset(
    {
        "pricing_snapshot_path",
        "pricing_source_url",
        "pricing_accessed_date",
        "baseline_label",
    }
)

EXPECTED_FAMILIES = {"cost-curves-effort", "token-composition"}
# Families owned by the first-tranche generator. The chart-data tree and the
# shared candidate manifest are multi-tranche: later tranches append their own
# families, so first-tranche assertions are scoped to the owned families and
# treated as append-aware (subset) checks rather than whole-tree equality.
OWNED_FAMILY_FILE_COUNT = 15


def _owned_chart_files() -> list[Path]:
    return [
        p
        for p in _chart_files()
        if p.relative_to(CHART_ROOT).parts[0] in {"cost-curves-effort", "token-composition"}
    ]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pcd = _load("promote_chart_data")
cps = _load("check_promotion_set")


def _chart_files() -> list[Path]:
    return sorted(CHART_ROOT.rglob("*.json"))


# ---------------------------------------------------------------------------
# Presence / structure
# ---------------------------------------------------------------------------


def test_expected_file_count():
    assert len(_owned_chart_files()) == OWNED_FAMILY_FILE_COUNT


def test_candidate_manifest_validates_against_schema():
    schema = json.loads(CANDIDATE_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    data = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    errors = sorted(Draft7Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    assert errors == [], [f"{list(e.path)}: {e.message}" for e in errors]


def test_candidate_manifest_enumerates_every_chart_file():
    # Multi-tranche aware: every owned-family chart file is enumerated, every
    # listed candidate exists on disk, and the owned families are not dropped
    # when later tranches append their own families.
    data = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    cand_paths = {c["chart_data_path"] for c in data["candidates"]}
    owned_disk = {str(p.relative_to(REPO_ROOT)) for p in _owned_chart_files()}
    assert owned_disk <= cand_paths
    for c in data["candidates"]:
        assert (REPO_ROOT / c["chart_data_path"]).is_file()
    assert EXPECTED_FAMILIES <= {c["family_key"] for c in data["candidates"]}


def test_owned_candidates_match_owned_disk_files():
    data = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    owned_cand_paths = {
        c["chart_data_path"]
        for c in data["candidates"]
        if c["family_key"] in EXPECTED_FAMILIES
    }
    owned_disk = {str(p.relative_to(REPO_ROOT)) for p in _owned_chart_files()}
    assert owned_cand_paths == owned_disk


# ---------------------------------------------------------------------------
# Numeric-only / dropped-field invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _chart_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_chart_payload_is_numeric_only(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tier"] == "SANITIZED_PUBLIC"
    assert data["schema"] == "wrpo.chart_data"
    dims = set(data["dimension_keys"])
    series = set(data["series_keys"])
    for row in data["rows"]:
        # No dropped pricing/prose columns anywhere.
        assert FORBIDDEN_KEYS.isdisjoint(row.keys())
        # Keys are exactly the declared dimensions + series.
        assert set(row.keys()) == dims | series
        # Dimension cells are strings; every series cell is a JSON number.
        for d in dims:
            assert isinstance(row[d], str)
        for s in series:
            assert isinstance(row[s], (int, float)) and not isinstance(row[s], bool)


def test_no_pricing_keys_in_raw_text():
    for path in _chart_files():
        text = path.read_text(encoding="utf-8")
        for key in FORBIDDEN_KEYS:
            assert key not in text, f"{path} leaks {key}"
        assert "azure.microsoft.com" not in text
        assert "pricing/" not in text


# ---------------------------------------------------------------------------
# Generator determinism + redaction scan
# ---------------------------------------------------------------------------


def test_generator_check_is_idempotent():
    assert pcd.generate(check=True) == 0


def test_promotion_set_scan_clean_over_emitted_surface():
    findings, errors = cps.scan_paths(
        [str(CHART_ROOT), str(CANDIDATE_MANIFEST)],
    )
    assert errors == []
    assert findings == [], [f.format() for f in findings]


def test_source_sha_matches_on_disk_source_csv():
    data = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    plan = {out: src for _, _, _, src, out in pcd.build_plan()}
    for cand in data["candidates"]:
        # Only the owned first-tranche candidates derive from a single source
        # CSV in the plan; later-tranche families are validated by their own
        # test module.
        if cand["chart_data_path"] not in plan:
            continue
        src = plan[cand["chart_data_path"]]
        on_disk = pcd.spa._sha256_bytes(src.read_bytes())
        assert cand["source_sanitized_sha256"] == [on_disk]


def test_multitranche_check_preserves_tranche1():
    # The first-tranche generator's --check must remain green even though the
    # shared candidate manifest now also carries later-tranche candidates: it
    # owns only cost-curves-effort + token-composition and upserts those alone.
    assert pcd.generate(check=True) == 0
    data = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    owned = [c for c in data["candidates"] if c["family_key"] in EXPECTED_FAMILIES]
    assert len(owned) == OWNED_FAMILY_FILE_COUNT

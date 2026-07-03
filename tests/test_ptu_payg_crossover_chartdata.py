"""Tests for scripts/promote_ptu_payg_crossover.py and the Tranche-2 data.

Covers the modeled PTU/PAYG crossover companion chart-data family:
  * payloads are numeric / stable-key only with no pricing provenance or
    internal identifiers;
  * the generator is deterministic / idempotent (``--check`` passes);
  * PAYG ``mean_usd_per_request`` is an exact carry-through from the matching
    first-tranche ``cost-curves-effort`` cost-per-request rows (no recompute);
  * the modeled break-even RPM follows the declared lean formula;
  * PTU framing is hypothesis-labelled and paired with the quality series;
  * the shared candidate manifest is appended (Tranche-1 candidates preserved)
    and validates against the public-chart-candidates schema;
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
PTU_ROOT = CHART_ROOT / "ptu-payg-crossover"
CANDIDATE_MANIFEST = REPO_ROOT / "release" / "public_chart_candidates.json"
CANDIDATE_SCHEMA = REPO_ROOT / "schemas" / "public_chart_candidates.schema.json"
PUBLIC_MANIFEST = REPO_ROOT / "release" / "public_sanitized_manifest.json"

FAMILY_KEY = "ptu-payg-crossover"
FRAMING_KEY = "throughput_gain_hypothesis"
BENCHMARKS = ("01", "02", "03")

DIMENSION_KEYS = {"effort", "model"}
SERIES_KEYS = {
    "mean_usd_per_request",
    "throughput_gain_factor",
    "modeled_break_even_rpm",
    "ptu_hourly_rate_usd",
    "min_ptu",
    "n_used",
}

# Provenance / prose field names that must never reach the payload.
FORBIDDEN_KEYS = frozenset(
    {
        "pricing_snapshot_path",
        "pricing_source_url",
        "pricing_accessed_date",
        "title",
        "label",
        "legend",
        "description",
        "baseline_label",
    }
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ppc = _load("promote_ptu_payg_crossover")
cps = _load("check_promotion_set")


def _ptu_files() -> list[Path]:
    return sorted(PTU_ROOT.rglob("*.json"))


def _chart(relpath: str) -> dict:
    return json.loads((REPO_ROOT / relpath).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Presence / structure
# ---------------------------------------------------------------------------


def test_one_file_per_benchmark():
    files = _ptu_files()
    assert len(files) == len(BENCHMARKS)
    names = {p.parent.name for p in files}
    assert names == {f"benchmark-{b}" for b in BENCHMARKS}


@pytest.mark.parametrize("path", _ptu_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_payload_shape(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tier"] == "SANITIZED_PUBLIC"
    assert data["schema"] == "wrpo.chart_data"
    assert data["family_key"] == FAMILY_KEY
    assert data["framing_key"] == FRAMING_KEY
    assert set(data["dimension_keys"]) == DIMENSION_KEYS
    assert set(data["series_keys"]) == SERIES_KEYS
    for row in data["rows"]:
        assert FORBIDDEN_KEYS.isdisjoint(row.keys())
        assert set(row.keys()) == DIMENSION_KEYS | SERIES_KEYS
        for d in DIMENSION_KEYS:
            assert isinstance(row[d], str)
        for s in SERIES_KEYS:
            assert isinstance(row[s], (int, float)) and not isinstance(row[s], bool)


# ---------------------------------------------------------------------------
# Public-safety: no prose / pricing provenance / internal identifiers
# ---------------------------------------------------------------------------


def test_no_pricing_provenance_or_internal_ids_in_text():
    for path in _ptu_files():
        text = path.read_text(encoding="utf-8")
        for key in FORBIDDEN_KEYS:
            assert key not in text, f"{path} leaks {key}"
        # No pricing URLs / local pricing paths / archive URLs.
        assert "azure.microsoft.com" not in text
        assert "learn.microsoft.com" not in text
        assert "web.archive.org" not in text
        assert "pricing/" not in text
        assert "accessed_date" not in text
        assert "source_url" not in text
        # No internal-tree references or task identifiers.
        assert ".internal" not in text
        assert "/tasks/" not in text


def test_pricing_snapshot_is_sha_pin_only():
    for path in _ptu_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        pins = data["pricing_snapshot_sha256"]
        assert set(pins.keys()) == {"ptu_pricing_sha256", "payg_pricing_sha256"}
        for v in pins.values():
            assert isinstance(v, str) and len(v) == 64
            int(v, 16)  # hex
    # The pins match the on-disk snapshots they cite.
    ptu_sha = ppc._sha256_relpath(ppc.PTU_PRICING_RELPATH)
    payg_sha = ppc._sha256_relpath(ppc.PAYG_PRICING_RELPATH)
    data = _chart("results/public/chart-data/ptu-payg-crossover/benchmark-01/crossover.json")
    assert data["pricing_snapshot_sha256"]["ptu_pricing_sha256"] == ptu_sha
    assert data["pricing_snapshot_sha256"]["payg_pricing_sha256"] == payg_sha


# ---------------------------------------------------------------------------
# PAYG carry-through + modeled formula
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bench", BENCHMARKS)
def test_payg_exact_carry_through(bench):
    bkey = f"benchmark-{bench}"
    cost = _chart(f"results/public/chart-data/cost-curves-effort/{bkey}/cost-per-request.json")
    source = {(r["model"], r["effort"]): r["mean_usd_per_request"] for r in cost["rows"]}
    crossover = _chart(f"results/public/chart-data/ptu-payg-crossover/{bkey}/crossover.json")
    for row in crossover["rows"]:
        key = (row["model"], row["effort"])
        # Exact equality (no tolerance) against the canonical source value.
        assert row["mean_usd_per_request"] == source[key]


@pytest.mark.parametrize("bench", BENCHMARKS)
def test_modeled_break_even_formula(bench):
    bkey = f"benchmark-{bench}"
    crossover = _chart(f"results/public/chart-data/ptu-payg-crossover/{bkey}/crossover.json")
    for row in crossover["rows"]:
        expected = round(
            (row["ptu_hourly_rate_usd"] * row["min_ptu"] / row["mean_usd_per_request"])
            / 60.0
            * row["throughput_gain_factor"],
            6,
        )
        assert row["modeled_break_even_rpm"] == expected


@pytest.mark.parametrize("bench", BENCHMARKS)
def test_throughput_gain_carry_through(bench):
    bkey = f"benchmark-{bench}"
    tg = _chart(f"results/public/chart-data/cost-curves-effort/{bkey}/throughput-gain.json")
    source = {(r["model"], r["effort"]): r["throughput_gain_factor"] for r in tg["rows"]}
    crossover = _chart(f"results/public/chart-data/ptu-payg-crossover/{bkey}/crossover.json")
    for row in crossover["rows"]:
        assert row["throughput_gain_factor"] == source[(row["model"], row["effort"])]


@pytest.mark.parametrize("bench", BENCHMARKS)
def test_token_composition_join_is_complete(bench):
    """Token composition is an explanatory presence join only — every modeled
    row must have a matching (model, effort) token-composition row, and no
    token value is copied into the payload."""
    bkey = f"benchmark-{bench}"
    tokens = _chart(f"results/public/chart-data/token-composition/{bkey}/tokens.json")
    token_keys = {(r["model"], r["effort"]) for r in tokens["rows"]}
    crossover = _chart(f"results/public/chart-data/ptu-payg-crossover/{bkey}/crossover.json")
    token_value_keys = {
        "mean_input_tokens_noncached",
        "mean_cached_tokens",
        "mean_output_tokens",
        "mean_reasoning_tokens",
    }
    for row in crossover["rows"]:
        assert (row["model"], row["effort"]) in token_keys
        assert token_value_keys.isdisjoint(row.keys())


# ---------------------------------------------------------------------------
# Quality pairing (cost/throughput never promoted without quality guardrail)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _ptu_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_quality_pairing_present_and_resolvable(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    pairing = data["quality_pairing"]
    assert set(pairing.keys()) == {
        "quality_family_key",
        "quality_metric_key",
        "quality_chart_data_path",
        "quality_benchmark_key",
    }
    assert pairing["quality_family_key"] == "cost-curves-effort"
    assert pairing["quality_metric_key"] == "quality"
    qpath = REPO_ROOT / pairing["quality_chart_data_path"]
    assert qpath.is_file()
    qdata = json.loads(qpath.read_text(encoding="utf-8"))
    assert qdata["metric_key"] == "quality"
    assert qdata["benchmark_key"] == data["benchmark_key"]


# ---------------------------------------------------------------------------
# Determinism + candidate manifest + public manifest + scan
# ---------------------------------------------------------------------------


def test_generator_check_is_idempotent():
    assert ppc.generate(check=True) == 0


def test_candidate_manifest_validates_and_includes_ptu_family():
    schema = json.loads(CANDIDATE_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    data = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    errors = sorted(Draft7Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    assert errors == [], [f"{list(e.path)}: {e.message}" for e in errors]
    ptu = [c for c in data["candidates"] if c["family_key"] == FAMILY_KEY]
    assert len(ptu) == len(BENCHMARKS)
    for cand in ptu:
        assert cand["tier"] == "SANITIZED_PUBLIC"
        assert (REPO_ROOT / cand["chart_data_path"]).is_file()
        # No candidate-manifest framing/quality leakage — stable schema only.
        assert "framing_key" not in cand
        assert "quality_pairing" not in cand


def test_candidate_manifest_preserves_tranche1():
    data = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    families = {c["family_key"] for c in data["candidates"]}
    assert {"cost-curves-effort", "token-composition"} <= families
    assert FAMILY_KEY in families


def test_public_manifest_has_entry_per_ptu_file():
    manifest = json.loads(PUBLIC_MANIFEST.read_text(encoding="utf-8"))
    by_path = {e["artifact_path"]: e for e in manifest["entries"]}
    for path in _ptu_files():
        rel = str(path.relative_to(REPO_ROOT))
        assert rel in by_path
        on_disk = ppc.spa._sha256_bytes(path.read_bytes())
        assert by_path[rel]["sanitized_sha256"] == on_disk
        assert by_path[rel]["tier"] == "SANITIZED_PUBLIC"


def test_promotion_set_scan_clean_over_ptu_surface():
    findings, errors = cps.scan_paths([str(PTU_ROOT), str(CANDIDATE_MANIFEST)])
    assert errors == []
    assert findings == [], [f.format() for f in findings]

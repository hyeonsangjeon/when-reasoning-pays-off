"""Unit tests for ``scripts/cost_calculator.py``.

All tests are pure: no network, no Azure, no wall-clock dependency. The CLI
smoke tests pin ``--snapshot-date`` explicitly (except the dedicated
default-flag regression test, which monkey-patches ``datetime.date.today`` to
raise so any accidental wall-clock call fails loudly).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
import textwrap

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import cost_calculator as cc  # noqa: E402
from scripts import _azure_pricing as azure_pricing  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FIXTURE_PAYG = FIXTURE_DIR / "pricing" / "azure-openai-payg-2026-05.yaml"
FIXTURE_PTU = FIXTURE_DIR / "pricing" / "azure-openai-ptu-2026-05.yaml"
FIXTURE_ANALYSIS = FIXTURE_DIR / "analysis.json"


# ----------------------------------------------------------------------------
# Schema loaders
# ----------------------------------------------------------------------------


def test_load_payg_pricing_accepts_canonical_fixture() -> None:
    payg = cc.load_payg_pricing(FIXTURE_PAYG)
    assert payg.currency == "USD"
    assert payg.source_url.startswith("https://")
    assert payg.accessed_date == "2026-05-19"
    assert "gpt-4o" in payg.models
    assert "gpt-5.2" in payg.models
    g4 = payg.models["gpt-4o"]
    assert isinstance(g4, cc.Gpt4oRates)
    assert g4.input_per_1m_usd > 0
    assert g4.cached_input_per_1m_usd > 0
    assert g4.output_per_1m_usd > 0
    g52 = payg.models["gpt-5.2"]
    assert isinstance(g52, cc.Gpt52Rates)
    assert g52.reasoning_per_1m_usd > 0


def test_canonical_and_packaged_sample_pricing_bytes_are_identical() -> None:
    paths = (
        REPO_ROOT / "pricing" / "azure-openai-payg-sample-2026-05.yaml",
        REPO_ROOT
        / "batch-runner"
        / "batch_runner"
        / "data"
        / "azure_sample_pricing.yaml",
    )
    payloads = [path.read_bytes() for path in paths]
    assert payloads[0] == payloads[1]
    assert hashlib.sha256(payloads[0]).hexdigest() == (
        "858c3c39ca36a7495d2754d8b5e32e7"
        "7e6478d38e2e0da8d7d9cd154ab1f08cd"
    )


def test_historical_pricing_snapshot_remains_immutable_and_unselectable() -> None:
    historical = REPO_ROOT / "pricing" / "azure-openai-payg-2026-05.yaml"
    raw = historical.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "fc19f0ecd4945c883e3e67032ee9877e"
        "155f132c4efcb7f42b58d54b739b64be"
    )
    parsed = cc.load_payg_pricing(historical)
    assert parsed.snapshot_id is None
    assert parsed.records == {}
    with pytest.raises(azure_pricing.PricingSelectionError):
        azure_pricing.resolve_pinned_payg_snapshot(
            snapshot_id="azure-openai-payg-2026-05",
            snapshot_path="pricing/azure-openai-payg-2026-05.yaml",
            snapshot_sha256=hashlib.sha256(raw).hexdigest(),
        )


@pytest.mark.parametrize(
    "missing",
    [
        "model_family",
        "model_version",
        "geography",
        "region",
        "deployment_type",
        "sku",
        "price_key",
        "meters",
        "rates",
    ],
)
def test_load_payg_pricing_rejects_missing_record_identity(
    tmp_path: pathlib.Path, missing: str
) -> None:
    payload = _canonical_payg_dict()
    key = "test:gpt-5.2:2025-12-11:global:global-standard"
    del payload["records"][key][missing]
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(_write_yaml(tmp_path, payload))


def _write_yaml(tmp_path: pathlib.Path, payload: dict, *, name: str = "p.yaml") -> pathlib.Path:
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as fh:
        # ``sort_keys=False`` preserves the dict's insertion order so the
        # canonical-order builders (``_canonical_payg_dict`` /
        # ``_canonical_ptu_dict``) round-trip in canonical order — required
        # by the loader's top-level read-order check.
        yaml.safe_dump(payload, fh, sort_keys=False)
    return p


def _canonical_payg_dict() -> dict:
    return {
        "schema_version": "1.0.0",
        "snapshot_id": "synthetic-payg-2026-05",
        "source_url": "https://example.test/payg",
        "accessed_date": "2026-05-19",
        "archive_url": "https://web.archive.org/x",
        "currency": "USD",
        "selection_policy": {
            "mode": "deterministic-pinned",
            "freshness_policy": "not-applied",
        },
        "records": {
            "test:gpt-4o:2024-11-20:global:global-standard": {
                "model_family": "gpt-4o",
                "model_version": "2024-11-20",
                "geography": "global",
                "region": "global",
                "deployment_type": "Global Standard",
                "sku": "Global Standard",
                "price_key": "test:gpt-4o:2024-11-20:global:global-standard",
                "meters": {
                    "input": "gpt-4o input",
                    "cached_input": "gpt-4o cached",
                    "output": "gpt-4o output",
                },
                "rates": {
                    "input_per_1m_usd": 2.5,
                    "cached_input_per_1m_usd": 1.25,
                    "output_per_1m_usd": 10.0,
                },
            },
            "test:gpt-5.2:2025-12-11:global:global-standard": {
                "model_family": "gpt-5.2",
                "model_version": "2025-12-11",
                "geography": "global",
                "region": "global",
                "deployment_type": "Global Standard",
                "sku": "Global Standard",
                "price_key": "test:gpt-5.2:2025-12-11:global:global-standard",
                "meters": {
                    "input": "gpt-5.2 input",
                    "cached_input": "gpt-5.2 cached",
                    "output": "gpt-5.2 output",
                },
                "rates": {
                    "input_per_1m_usd": 1.75,
                    "cached_input_per_1m_usd": 0.175,
                    "reasoning_per_1m_usd": 14.0,
                    "output_per_1m_usd": 14.0,
                },
            },
        },
    }


def _canonical_ptu_dict() -> dict:
    return {
        "source_url": "https://example.test/ptu",
        "accessed_date": "2026-05-19",
        "currency": "USD",
        "region": "eastus2",
        "models": {
            "gpt-4o": {
                "ptu_hourly_rate_usd": 2.0,
                "min_ptu": 50,
                "max_ptu_per_deployment": 100000,
                "baseline_throughput_tpm_per_ptu": 2500,
            },
            "gpt-5.2": {
                "ptu_hourly_rate_usd": 2.0,
                "min_ptu": 50,
                "max_ptu_per_deployment": 100000,
                "baseline_throughput_tpm_per_ptu": 3400,
            },
        },
    }


def test_load_payg_pricing_rejects_extra_top_level_key(tmp_path: pathlib.Path) -> None:
    d = _canonical_payg_dict()
    d["vendor"] = "Microsoft"
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(path)


def test_load_payg_pricing_rejects_gpt4o_with_reasoning_field(tmp_path: pathlib.Path) -> None:
    d = _canonical_payg_dict()
    record = d["records"]["test:gpt-4o:2024-11-20:global:global-standard"]
    record["rates"]["reasoning_per_1m_usd"] = 10.0
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(path)


def test_load_payg_pricing_rejects_gpt52_missing_reasoning_field(tmp_path: pathlib.Path) -> None:
    d = _canonical_payg_dict()
    record = d["records"]["test:gpt-5.2:2025-12-11:global:global-standard"]
    del record["rates"]["reasoning_per_1m_usd"]
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(path)


def test_load_payg_pricing_rejects_zero_rate(tmp_path: pathlib.Path) -> None:
    d = _canonical_payg_dict()
    record = d["records"]["test:gpt-4o:2024-11-20:global:global-standard"]
    record["rates"]["input_per_1m_usd"] = 0
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(path)


def test_load_payg_pricing_rejects_non_https_source_url(tmp_path: pathlib.Path) -> None:
    d = _canonical_payg_dict()
    d["source_url"] = "http://example.test/payg"
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(path)


def test_load_payg_pricing_rejects_bad_accessed_date(tmp_path: pathlib.Path) -> None:
    d = _canonical_payg_dict()
    d["accessed_date"] = "2026-5-19"
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(path)


def test_load_payg_pricing_accepts_missing_archive_url(tmp_path: pathlib.Path) -> None:
    d = _canonical_payg_dict()
    del d["archive_url"]
    path = _write_yaml(tmp_path, d)
    payg = cc.load_payg_pricing(path)
    assert payg.archive_url is None


def test_load_payg_pricing_rejects_wrong_top_level_key_order(
    tmp_path: pathlib.Path,
) -> None:
    """Regression for §003 Control 'read order checked, not just parse'.

    Same canonical key set but in the wrong order — the loader must reject
    the file. Set-comparison alone (the prior implementation) would have
    silently accepted this.
    """
    canonical = _canonical_payg_dict()
    reordered = {
        "snapshot_id": canonical["snapshot_id"],
        "schema_version": canonical["schema_version"],
        **{k: v for k, v in canonical.items() if k not in {"schema_version", "snapshot_id"}},
    }
    path = _write_yaml(tmp_path, reordered)
    with pytest.raises(cc.PaygSchemaError, match="wrong order"):
        cc.load_payg_pricing(path)


def test_load_payg_pricing_rejects_models_before_currency_order(
    tmp_path: pathlib.Path,
) -> None:
    """Second wrong-order variant: ``records`` moved ahead of ``currency``."""
    canonical = _canonical_payg_dict()
    reordered = {
        **{k: canonical[k] for k in list(canonical)[:5]},
        "records": canonical["records"],
        "currency": canonical["currency"],
        "selection_policy": canonical["selection_policy"],
    }
    path = _write_yaml(tmp_path, reordered)
    with pytest.raises(cc.PaygSchemaError, match="wrong order"):
        cc.load_payg_pricing(path)


def test_load_ptu_pricing_accepts_canonical_fixture() -> None:
    ptu = cc.load_ptu_pricing(FIXTURE_PTU)
    assert ptu.currency == "USD"
    assert ptu.region == "eastus2"
    assert "gpt-4o" in ptu.models
    assert ptu.models["gpt-4o"].baseline_throughput_tpm_per_ptu > 0


def test_load_ptu_pricing_rejects_currency_not_usd(tmp_path: pathlib.Path) -> None:
    d = _canonical_ptu_dict()
    d["currency"] = "EUR"
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PtuSchemaError):
        cc.load_ptu_pricing(path)


def test_load_ptu_pricing_requires_region(tmp_path: pathlib.Path) -> None:
    d = _canonical_ptu_dict()
    del d["region"]
    path = _write_yaml(tmp_path, d)
    with pytest.raises(cc.PtuSchemaError):
        cc.load_ptu_pricing(path)


def test_load_ptu_pricing_rejects_wrong_top_level_key_order(
    tmp_path: pathlib.Path,
) -> None:
    """Regression for §003 Control: PTU YAML read-order check.

    Same canonical key set but in the wrong order — set-comparison alone
    (the prior implementation) would have silently accepted this. Canonical
    PTU order is: source_url, accessed_date, archive_url, currency, region,
    models.
    """
    # Swap currency and region.
    reordered = {
        "source_url": "https://example.test/ptu",
        "accessed_date": "2026-05-19",
        "region": "eastus2",
        "currency": "USD",
        "models": {
            "gpt-4o": {
                "ptu_hourly_rate_usd": 2.0,
                "min_ptu": 50,
                "max_ptu_per_deployment": 100000,
                "baseline_throughput_tpm_per_ptu": 2500,
            },
            "gpt-5.2": {
                "ptu_hourly_rate_usd": 2.0,
                "min_ptu": 50,
                "max_ptu_per_deployment": 100000,
                "baseline_throughput_tpm_per_ptu": 3400,
            },
        },
    }
    path = _write_yaml(tmp_path, reordered)
    with pytest.raises(cc.PtuSchemaError, match="wrong order"):
        cc.load_ptu_pricing(path)


def test_load_ptu_pricing_rejects_archive_url_after_currency_order(
    tmp_path: pathlib.Path,
) -> None:
    """``archive_url`` is optional but must sit between ``accessed_date`` and
    ``currency`` when present. Placing it after ``currency`` is wrong-order."""
    reordered = {
        "source_url": "https://example.test/ptu",
        "accessed_date": "2026-05-19",
        "currency": "USD",
        "archive_url": "https://web.archive.org/x",
        "region": "eastus2",
        "models": {
            "gpt-4o": {
                "ptu_hourly_rate_usd": 2.0,
                "min_ptu": 50,
                "max_ptu_per_deployment": 100000,
                "baseline_throughput_tpm_per_ptu": 2500,
            },
            "gpt-5.2": {
                "ptu_hourly_rate_usd": 2.0,
                "min_ptu": 50,
                "max_ptu_per_deployment": 100000,
                "baseline_throughput_tpm_per_ptu": 3400,
            },
        },
    }
    path = _write_yaml(tmp_path, reordered)
    with pytest.raises(cc.PtuSchemaError, match="wrong order"):
        cc.load_ptu_pricing(path)


# ----------------------------------------------------------------------------
# PAYG cost math
# ----------------------------------------------------------------------------


def test_payg_cost_gpt4o_zero_reasoning_ok() -> None:
    payg = cc.load_payg_pricing(FIXTURE_PAYG)
    usage = cc.TokenUsage(
        input_tokens=1000, cached_tokens=200, output_tokens=300, reasoning_tokens=0
    )
    out = cc.payg_cost_per_call(usage, payg, "gpt-4o")
    # gpt-4o rates in fixture: input=10, cached=1, output=40
    # non_cached = 800; 800*10 + 200*1 + 300*40 + 0 = 8000 + 200 + 12000 = 20200
    # 20200 / 1_000_000 = 0.0202
    assert out.usd_per_request == pytest.approx(0.0202, abs=1e-9)
    assert out.pricing_snapshot_path
    assert out.pricing_source_url.startswith("https://")
    assert out.pricing_accessed_date == "2026-05-19"


def test_payg_cost_gpt4o_nonzero_reasoning_raises() -> None:
    payg = cc.load_payg_pricing(FIXTURE_PAYG)
    usage = cc.TokenUsage(
        input_tokens=1000, cached_tokens=0, output_tokens=300, reasoning_tokens=5
    )
    with pytest.raises(cc.Gpt4oReasoningError):
        cc.payg_cost_per_call(usage, payg, "gpt-4o")


def test_payg_cost_gpt52_canonical_example() -> None:
    payg = cc.load_payg_pricing(FIXTURE_PAYG)
    # Azure GPT-5.x usage contract: reasoning_tokens is a labelled SUBSET of
    # output_tokens (output already includes the 500 reasoning tokens).
    usage = cc.TokenUsage(
        input_tokens=1000, cached_tokens=200, output_tokens=800, reasoning_tokens=500
    )
    out = cc.payg_cost_per_call(usage, payg, "gpt-5.2")
    # fixture rates: input=10 cached=1 output=40 reasoning=40
    # (800*10 + 200*1 + 800*40) / 1_000_000 = 40200 / 1_000_000 = 0.0402
    # NOTE: reasoning_tokens does NOT enter the formula additively — it is
    # billed at the output rate by virtue of being inside output_tokens.
    assert out.usd_per_request == pytest.approx(0.0402, abs=1e-9)


def test_payg_cost_gpt52_all_zero_tokens() -> None:
    payg = cc.load_payg_pricing(FIXTURE_PAYG)
    usage = cc.TokenUsage(0, 0, 0, 0)
    out = cc.payg_cost_per_call(usage, payg, "gpt-5.2")
    assert out.usd_per_request == 0.0


def test_payg_cost_gpt52_all_cached() -> None:
    payg = cc.load_payg_pricing(FIXTURE_PAYG)
    usage = cc.TokenUsage(
        input_tokens=500, cached_tokens=500, output_tokens=0, reasoning_tokens=0
    )
    out = cc.payg_cost_per_call(usage, payg, "gpt-5.2")
    # non_cached = 0; only cached cost: 500 * 1 / 1_000_000 = 0.0005
    assert out.usd_per_request == pytest.approx(0.0005, abs=1e-9)


def test_payg_cost_reasoning_rate_has_no_cost_impact_under_subset_contract() -> None:
    """Azure GPT-5.x contract: ``reasoning_tokens`` is a labelled subset of
    ``output_tokens`` and is billed at the output rate via ``output_tokens``.
    The ``reasoning_per_1m_usd`` schema field is retained as a dedicated
    line (the §6.1 never-collapsed schema invariant) so a future Azure meter
    split surfaces in the YAML diff — but under today's contract it must
    NOT enter the cost formula. This test pins that contract: cost is
    invariant to ``reasoning_per_1m_usd`` because reasoning is already
    inside ``output_tokens``.
    """
    # Build two pricings that differ ONLY in reasoning_per_1m_usd.
    common_kwargs = dict(
        input_per_1m_usd=10,
        cached_input_per_1m_usd=1,
        output_per_1m_usd=40,
    )
    rates_low = cc.Gpt52Rates(reasoning_per_1m_usd=20, **common_kwargs)
    rates_high = cc.Gpt52Rates(reasoning_per_1m_usd=400, **common_kwargs)
    payg_kwargs = dict(
        source_url="https://example.test/x",
        accessed_date="2026-05-19",
        archive_url=None,
        currency="USD",
        snapshot_path="synthetic://payg.yaml",
    )
    payg_low = cc.PaygPricing(models={"gpt-5.2": rates_low}, **payg_kwargs)
    payg_high = cc.PaygPricing(models={"gpt-5.2": rates_high}, **payg_kwargs)
    # output_tokens=200 with reasoning_tokens=100 as a subset.
    usage = cc.TokenUsage(
        input_tokens=0, cached_tokens=0, output_tokens=200, reasoning_tokens=100
    )
    out_low = cc.payg_cost_per_call(usage, payg_low, "gpt-5.2")
    out_high = cc.payg_cost_per_call(usage, payg_high, "gpt-5.2")
    # Both equal: (200 * 40) / 1e6 = 0.008 — independent of reasoning rate.
    assert out_low.usd_per_request == pytest.approx(0.008, abs=1e-9)
    assert out_high.usd_per_request == pytest.approx(0.008, abs=1e-9)
    # If a refactor ever silently re-introduced
    #   + reasoning_tokens * reasoning_per_1m_usd / 1_000_000
    # the two would diverge, catching the double-count regression.
    assert out_low.usd_per_request == out_high.usd_per_request


# ----------------------------------------------------------------------------
# PTU throughput-gain math
# ----------------------------------------------------------------------------


def test_ptu_gain_simple() -> None:
    g = cc.ptu_throughput_gain(target_tokens=350, baseline_tokens=500, baseline_label="gpt-4o")
    assert g.throughput_gain_factor == pytest.approx(500 / 350, abs=1e-9)


def test_ptu_gain_identical() -> None:
    g = cc.ptu_throughput_gain(target_tokens=500, baseline_tokens=500, baseline_label="gpt-4o")
    assert g.throughput_gain_factor == 1.0


def test_ptu_gain_target_zero_raises() -> None:
    with pytest.raises(cc.ThroughputGainError):
        cc.ptu_throughput_gain(target_tokens=0, baseline_tokens=500, baseline_label="gpt-4o")


def test_ptu_gain_baseline_label_propagates() -> None:
    g = cc.ptu_throughput_gain(target_tokens=350, baseline_tokens=500, baseline_label="gpt-4o")
    assert g.baseline_label == "gpt-4o"


def test_ptu_gain_custom_label_format() -> None:
    spec = cc.BaselineSpec(kind="custom", label="custom:500.0", tokens_per_request=500.0)
    assert spec.label == "custom:500.0"
    g = cc.ptu_throughput_gain(
        target_tokens=350, baseline_tokens=500.0, baseline_label=spec.label
    )
    assert g.baseline_label == "custom:500.0"


# ----------------------------------------------------------------------------
# Baseline resolution
# ----------------------------------------------------------------------------


def test_baseline_migration_picks_gpt4o_cell() -> None:
    cells = [
        cc.CellMeasurement(
            model="gpt-4o",
            effort=None,
            sample_count=3,
            input_tokens_mean=400,
            cached_tokens_mean=0,
            output_tokens_mean=100,
            reasoning_tokens_mean=0,
        ),
        cc.CellMeasurement(
            model="gpt-5.2",
            effort="minimal",
            sample_count=3,
            input_tokens_mean=400,
            cached_tokens_mean=0,
            output_tokens_mean=100,
            reasoning_tokens_mean=50,
        ),
    ]
    spec = cc.BaselineSpec(kind="migration", label="gpt-4o")
    tokens = cc._resolve_baseline_tokens(cells, spec)
    assert tokens == 500  # 400 + 100 + 0


def test_baseline_migration_missing_gpt4o_raises() -> None:
    cells = [
        cc.CellMeasurement(
            model="gpt-5.2",
            effort="high",
            sample_count=3,
            input_tokens_mean=400,
            cached_tokens_mean=0,
            output_tokens_mean=100,
            reasoning_tokens_mean=500,
        ),
    ]
    spec = cc.BaselineSpec(kind="migration", label="gpt-4o")
    with pytest.raises(cc.MissingBaselineError):
        cc._resolve_baseline_tokens(cells, spec)


def test_baseline_effort_high_picks_gpt52_high() -> None:
    cells = [
        cc.CellMeasurement(
            model="gpt-5.2", effort="minimal", sample_count=3,
            input_tokens_mean=100, cached_tokens_mean=0,
            output_tokens_mean=60, reasoning_tokens_mean=10,
        ),
        cc.CellMeasurement(
            model="gpt-5.2", effort="high", sample_count=3,
            input_tokens_mean=100, cached_tokens_mean=0,
            output_tokens_mean=550, reasoning_tokens_mean=500,
        ),
    ]
    spec = cc.BaselineSpec(kind="effort-high", label="gpt-5.2 effort=high")
    tokens = cc._resolve_baseline_tokens(cells, spec)
    # Tokens-per-request = input + output (reasoning is a subset of output
    # under Azure GPT-5.x contract; adding it would double-count).
    assert tokens == 650  # 100 + 550


# ----------------------------------------------------------------------------
# Snapshot resolution
# ----------------------------------------------------------------------------


def _make_payg_yaml_file(
    dir_path: pathlib.Path,
    filename: str,
    accessed_date: str | datetime.date,
    *,
    quoted_date: bool = False,
) -> pathlib.Path:
    """Write a minimal valid PAYG YAML with a specified accessed_date.

    When ``quoted_date`` is True, the file uses ``accessed_date: "YYYY-MM-DD"``
    (a string after yaml.safe_load). When False with a string input, it writes
    the string anyway (PyYAML quotes it if needed). When the accessed_date is
    a ``datetime.date``, it goes into the YAML unquoted and parses back as
    ``datetime.date``.
    """
    payload = _canonical_payg_dict()
    if isinstance(accessed_date, datetime.date):
        payload["accessed_date"] = accessed_date
    elif quoted_date:
        payload["accessed_date"] = accessed_date
    else:
        payload["accessed_date"] = datetime.date.fromisoformat(accessed_date)
    p = dir_path / filename
    p.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return p


def test_resolve_active_snapshot_picks_latest_before_target(tmp_path: pathlib.Path) -> None:
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2025-12.yaml", "2025-12-10")
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-05.yaml", "2026-05-19")
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-08.yaml", "2026-08-03")
    chosen = cc.resolve_active_snapshot(
        kind="payg", target_date=datetime.date(2026, 6, 1), pricing_dir=tmp_path
    )
    assert chosen.name == "azure-openai-payg-2026-05.yaml"


def test_resolve_active_snapshot_excludes_future_accessed_dates(tmp_path: pathlib.Path) -> None:
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-05.yaml", "2026-05-19")
    with pytest.raises(cc.SnapshotNotFoundError):
        cc.resolve_active_snapshot(
            kind="payg",
            target_date=datetime.date(2026, 4, 1),
            pricing_dir=tmp_path,
        )


def test_resolve_active_snapshot_filename_month_is_not_authoritative(
    tmp_path: pathlib.Path,
) -> None:
    # Filename suggests 2026-05; in-file accessed_date is 2026-05-19. Target
    # 2026-05-10 (mid-month, before actual access) must NOT match — filename
    # month is not authoritative.
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-05.yaml", "2026-05-19")
    with pytest.raises(cc.SnapshotNotFoundError):
        cc.resolve_active_snapshot(
            kind="payg",
            target_date=datetime.date(2026, 5, 10),
            pricing_dir=tmp_path,
        )


def test_resolve_active_snapshot_alphabetical_tiebreak(tmp_path: pathlib.Path) -> None:
    # Two files with the same in-file accessed_date — deterministic alphabetical pick.
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-05a.yaml", "2026-05-19")
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-05b.yaml", "2026-05-19")
    chosen = cc.resolve_active_snapshot(
        kind="payg", target_date=datetime.date(2026, 6, 1), pricing_dir=tmp_path
    )
    # Pick the lexicographically smaller name on tie.
    assert chosen.name == "azure-openai-payg-2026-05a.yaml"


def test_resolve_active_snapshot_default_picks_newest_no_wallclock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2025-12.yaml", "2025-12-10")
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-05.yaml", "2026-05-19")
    _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-08.yaml", "2026-08-03")

    # Forbid wall-clock for the duration of this test.
    real_date = datetime.date

    class NoTodayDate(real_date):
        @classmethod
        def today(cls) -> "NoTodayDate":  # noqa: D401
            raise RuntimeError("wall-clock forbidden")

    monkeypatch.setattr(cc.datetime, "date", NoTodayDate)
    try:
        chosen = cc.resolve_active_snapshot(
            kind="payg", target_date=None, pricing_dir=tmp_path
        )
    finally:
        monkeypatch.setattr(cc.datetime, "date", real_date)
    assert chosen.name == "azure-openai-payg-2026-08.yaml"


def test_resolve_active_snapshot_accepts_yaml_date_object(tmp_path: pathlib.Path) -> None:
    # accessed_date written unquoted → PyYAML returns datetime.date.
    _make_payg_yaml_file(
        tmp_path, "azure-openai-payg-2026-05.yaml", datetime.date(2026, 5, 19)
    )
    chosen = cc.resolve_active_snapshot(
        kind="payg", target_date=datetime.date(2026, 6, 1), pricing_dir=tmp_path
    )
    assert chosen.name == "azure-openai-payg-2026-05.yaml"


# ----------------------------------------------------------------------------
# CLI smoke
# ----------------------------------------------------------------------------


def _run_cli(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "scripts.cost_calculator", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
    )


def test_cli_payg_emits_valid_json() -> None:
    res = _run_cli(
        "payg",
        "--pricing-dir", str(FIXTURE_DIR / "pricing"),
        "--snapshot-date", "2026-05-19",
        "--output", "json",
        str(FIXTURE_ANALYSIS),
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert set(payload.keys()) == {"lens", "snapshot", "currency", "cells"}
    assert payload["lens"] == "payg"
    assert payload["snapshot"]["accessed_date"] == "2026-05-19"
    assert payload["snapshot"]["source_url"].startswith("https://")
    assert isinstance(payload["cells"], list)


def test_cli_ptu_requires_baseline() -> None:
    res = _run_cli(
        "ptu",
        "--pricing-dir", str(FIXTURE_DIR / "pricing"),
        "--snapshot-date", "2026-05-19",
        str(FIXTURE_ANALYSIS),
    )
    assert res.returncode == 1
    assert "--baseline" in res.stderr


def test_cli_both_two_subsections_markdown() -> None:
    res = _run_cli(
        "both",
        "--baseline", "migration",
        "--pricing-dir", str(FIXTURE_DIR / "pricing"),
        "--snapshot-date", "2026-05-19",
        "--output", "markdown",
        str(FIXTURE_ANALYSIS),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("## PAYG lens") == 1
    assert res.stdout.count("## PTU lens") == 1


def test_cli_schema_error_exit_2(tmp_path: pathlib.Path) -> None:
    # Build a malformed PAYG YAML (extra top-level key).
    bad_dir = tmp_path / "pricing"
    bad_dir.mkdir()
    body = textwrap.dedent(
        """\
        source_url: https://example.test/payg
        accessed_date: 2026-05-19
        currency: USD
        vendor: Microsoft
        models:
          gpt-4o:
            input_per_1m_usd: 2.5
            cached_input_per_1m_usd: 1.25
            output_per_1m_usd: 10.0
          gpt-5.2:
            input_per_1m_usd: 1.75
            cached_input_per_1m_usd: 0.175
            reasoning_per_1m_usd: 14.0
            output_per_1m_usd: 14.0
        """
    )
    (bad_dir / "azure-openai-payg-2026-05.yaml").write_text(body, encoding="utf-8")
    res = _run_cli(
        "payg",
        "--pricing-dir", str(bad_dir),
        "--snapshot-date", "2026-05-19",
        str(FIXTURE_ANALYSIS),
    )
    assert res.returncode == 2, (res.returncode, res.stderr)


def test_cli_gpt4o_reasoning_data_error_exit_3(tmp_path: pathlib.Path) -> None:
    bad_analysis = tmp_path / "analysis.json"
    bad_analysis.write_text(
        json.dumps(
            {
                "benchmark_id": "x",
                "git_commit": "0" * 40,
                "cells": [
                    {
                        "model": "gpt-4o",
                        "effort": None,
                        "sample_count": 3,
                        "input_tokens_mean": 100.0,
                        "cached_tokens_mean": 0.0,
                        "output_tokens_mean": 50.0,
                        "reasoning_tokens_mean": 5.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    res = _run_cli(
        "payg",
        "--pricing-dir", str(FIXTURE_DIR / "pricing"),
        "--snapshot-date", "2026-05-19",
        str(bad_analysis),
    )
    assert res.returncode == 3, (res.returncode, res.stderr)


def test_cli_missing_snapshot_exit_4() -> None:
    res = _run_cli(
        "payg",
        "--pricing-dir", str(FIXTURE_DIR / "pricing"),
        "--snapshot-date", "2020-01-01",
        str(FIXTURE_ANALYSIS),
    )
    assert res.returncode == 4, (res.returncode, res.stderr)


def test_cli_default_snapshot_date_picks_newest(tmp_path: pathlib.Path) -> None:
    # Multiple PAYG snapshots — default flag picks newest by in-file accessed_date.
    pdir = tmp_path / "pricing"
    pdir.mkdir()
    _make_payg_yaml_file(pdir, "azure-openai-payg-2025-12.yaml", "2025-12-10")
    _make_payg_yaml_file(pdir, "azure-openai-payg-2026-05.yaml", "2026-05-19")
    _make_payg_yaml_file(pdir, "azure-openai-payg-2026-08.yaml", "2026-08-03")
    # Need PTU snapshots only if calling ptu/both — payg subcommand alone is fine.
    res = _run_cli(
        "payg",
        "--pricing-dir", str(pdir),
        "--output", "json",
        str(FIXTURE_ANALYSIS),
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    # The newest in-file accessed_date is 2026-08-03.
    assert payload["snapshot"]["accessed_date"] == "2026-08-03"


def test_cli_ptu_only_pricing_dir_succeeds(tmp_path: pathlib.Path) -> None:
    """Regression for §003 Finding 2: the ``ptu`` subcommand is independent
    of PAYG. A pricing dir containing ONLY a PTU snapshot must work — the
    CLI must not resolve or load a PAYG snapshot for ptu-only runs.
    """
    pdir = tmp_path / "pricing"
    pdir.mkdir()
    # Note: no PAYG YAML in this dir on purpose.
    _make_ptu_yaml_file(pdir, "azure-openai-ptu-2026-05.yaml", "2026-05-19")
    res = _run_cli(
        "ptu",
        "--baseline", "migration",
        "--pricing-dir", str(pdir),
        "--snapshot-date", "2026-05-19",
        "--output", "json",
        str(FIXTURE_ANALYSIS),
    )
    assert res.returncode == 0, (res.returncode, res.stderr)
    payload = json.loads(res.stdout)
    assert payload["lens"] == "ptu"
    assert payload["snapshot"]["accessed_date"] == "2026-05-19"
    assert payload["baseline"]["label"] == "gpt-4o"
    assert isinstance(payload["cells"], list)


# ----------------------------------------------------------------------------
# Determinism
# ----------------------------------------------------------------------------


def test_cli_payg_byte_identical_repeated_runs() -> None:
    res1 = _run_cli(
        "payg",
        "--pricing-dir", str(FIXTURE_DIR / "pricing"),
        "--snapshot-date", "2026-05-19",
        "--output", "json",
        str(FIXTURE_ANALYSIS),
    )
    res2 = _run_cli(
        "payg",
        "--pricing-dir", str(FIXTURE_DIR / "pricing"),
        "--snapshot-date", "2026-05-19",
        "--output", "json",
        str(FIXTURE_ANALYSIS),
    )
    assert res1.returncode == 0
    assert res2.returncode == 0
    assert res1.stdout == res2.stdout


# ----------------------------------------------------------------------------
# accessed_date YAML dual-type acceptance
# ----------------------------------------------------------------------------


def test_load_payg_pricing_accepts_unquoted_yaml_date(tmp_path: pathlib.Path) -> None:
    # Literal unquoted YAML date → PyYAML returns datetime.date.
    p = _make_payg_yaml_file(tmp_path, "azure-openai-payg-2026-05.yaml", "2026-05-19")
    payg = cc.load_payg_pricing(p)
    assert payg.accessed_date == "2026-05-19"
    assert isinstance(payg.accessed_date, str)


def test_load_payg_pricing_accepts_quoted_yaml_string_date(tmp_path: pathlib.Path) -> None:
    p = _make_payg_yaml_file(
        tmp_path, "azure-openai-payg-2026-05.yaml", "2026-05-19", quoted_date=True
    )
    payg = cc.load_payg_pricing(p)
    assert payg.accessed_date == "2026-05-19"


def test_load_payg_pricing_rejects_yaml_datetime_object(tmp_path: pathlib.Path) -> None:
    # datetime literal in YAML → datetime.datetime; must reject.
    payload = _canonical_payg_dict()
    payload["accessed_date"] = datetime.datetime(
        2026, 5, 19, 12, tzinfo=datetime.timezone.utc
    )
    p = tmp_path / "azure-openai-payg-bad.yaml"
    p.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(cc.PaygSchemaError):
        cc.load_payg_pricing(p)


def _make_ptu_yaml_file(
    dir_path: pathlib.Path,
    filename: str,
    accessed_date: str | datetime.date,
    *,
    quoted_date: bool = False,
) -> pathlib.Path:
    if isinstance(accessed_date, str) and quoted_date:
        date_yaml = f'"{accessed_date}"'
    elif isinstance(accessed_date, datetime.date):
        date_yaml = accessed_date.isoformat()
    else:
        date_yaml = str(accessed_date)
    body = textwrap.dedent(
        f"""\
        source_url: https://example.test/ptu
        accessed_date: {date_yaml}
        currency: USD
        region: eastus2
        models:
          gpt-4o:
            ptu_hourly_rate_usd: 2.0
            min_ptu: 50
            max_ptu_per_deployment: 100000
            baseline_throughput_tpm_per_ptu: 2500
          gpt-5.2:
            ptu_hourly_rate_usd: 2.0
            min_ptu: 50
            max_ptu_per_deployment: 100000
            baseline_throughput_tpm_per_ptu: 3400
        """
    )
    p = dir_path / filename
    p.write_text(body, encoding="utf-8")
    return p


def test_load_ptu_pricing_accessed_date_unquoted(tmp_path: pathlib.Path) -> None:
    p = _make_ptu_yaml_file(tmp_path, "azure-openai-ptu-2026-05.yaml", "2026-05-19")
    ptu = cc.load_ptu_pricing(p)
    assert ptu.accessed_date == "2026-05-19"


def test_load_ptu_pricing_accessed_date_quoted(tmp_path: pathlib.Path) -> None:
    p = _make_ptu_yaml_file(
        tmp_path, "azure-openai-ptu-2026-05.yaml", "2026-05-19", quoted_date=True
    )
    ptu = cc.load_ptu_pricing(p)
    assert ptu.accessed_date == "2026-05-19"

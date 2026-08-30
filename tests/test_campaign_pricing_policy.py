from __future__ import annotations

import datetime
import pathlib
import json

import pytest
from jsonschema import Draft7Validator

import experiments
from experiments import runner as experiment_runner
from scripts import measure_cache_key_bucketing as cache
from scripts import measure_dual_spillover as dual
from scripts import measure_max_output_tokens_sweep as sweep
from scripts._azure_pricing import (
    CANONICAL_PAYG_SNAPSHOT_ID,
    CANONICAL_PAYG_SNAPSHOT_PATH,
    CANONICAL_PAYG_SNAPSHOT_SHA256,
    HISTORICAL_REPLAY,
    LIVE_MEASUREMENT,
    PRICING_POLICY_VERSION,
    PricingPolicy,
    PricingPolicyError,
    verify_campaign_pricing,
)


def test_historical_replay_is_deterministic_under_2030_clock() -> None:
    verified = verify_campaign_pricing(
        snapshot_path=CANONICAL_PAYG_SNAPSHOT_PATH,
        model_family="gpt-5.2",
        model_version="2025-12-11",
        policy_mode=HISTORICAL_REPLAY,
        today=datetime.date(2030, 1, 1),
    )
    provenance = verified.provenance()
    assert provenance["policy_version"] == PRICING_POLICY_VERSION
    assert provenance["mode"] == HISTORICAL_REPLAY
    assert provenance["freshness"] == {"required": False, "max_age_days": None}
    assert provenance["snapshot"]["snapshot_id"] == CANONICAL_PAYG_SNAPSHOT_ID
    assert provenance["snapshot"]["snapshot_sha256"] == CANONICAL_PAYG_SNAPSHOT_SHA256
    assert provenance["snapshot"]["accessed_date"] == "2026-05-19"
    assert provenance["snapshot"]["price_key"] == (
        "azure-openai:gpt-5.2:2025-12-11:global:global-standard"
    )


def test_live_measurement_rejects_stale_snapshot_under_2030_clock() -> None:
    with pytest.raises(PricingPolicyError, match="add a new immutable snapshot"):
        verify_campaign_pricing(
            snapshot_path=CANONICAL_PAYG_SNAPSHOT_PATH,
            model_family="gpt-5.2",
            model_version="2025-12-11",
            policy_mode=LIVE_MEASUREMENT,
            today=datetime.date(2030, 1, 1),
        )


def test_unknown_policy_and_historical_live_work_fail_closed() -> None:
    with pytest.raises(PricingPolicyError, match="unknown pricing policy"):
        PricingPolicy.parse("best-effort")
    with pytest.raises(PricingPolicyError, match="offline-only"):
        PricingPolicy.parse(HISTORICAL_REPLAY).require_offline_if_historical(
            dry_run=False
        )


def test_snapshot_verification_failures_use_policy_error_contract() -> None:
    with pytest.raises(PricingPolicyError, match="pricing verification failed"):
        verify_campaign_pricing(
            snapshot_path="pricing/not-the-pinned-snapshot.yaml",
            model_family="gpt-5.2",
            model_version="2025-12-11",
            policy_mode=HISTORICAL_REPLAY,
        )


@pytest.mark.parametrize(
    ("module", "experiment"),
    [
        (cache, "experiments/exp006_cache_key_bucketing_inmemory.yaml"),
        (dual, "experiments/exp005_dual_spillover_reactive.yaml"),
        (sweep, "experiments/exp007_max_output_tokens_sweep.yaml"),
    ],
)
def test_live_stale_refusal_precedes_endpoint_resolution(
    module, experiment, monkeypatch
) -> None:
    cfg = module.load_experiment(experiment)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("endpoint or credential resolution was reached")

    if module is sweep:
        monkeypatch.setattr(module, "_resolve_env_value", forbidden)
        kwargs = {
            "cfg": cfg,
            "benchmarks_root": pathlib.Path("benchmarks"),
            "dry_run": False,
            "stage": "smoke",
            "allow_dirty": True,
        }
    elif module is dual:
        monkeypatch.setattr(module, "_require_env", forbidden)
        kwargs = {
            "cfg": cfg,
            "benchmarks_root": pathlib.Path("benchmarks"),
            "pricing_dir": pathlib.Path("pricing"),
            "dry_run": False,
            "smoke": False,
            "allow_dirty": True,
        }
    else:
        monkeypatch.setattr(module, "_require_env", forbidden)
        kwargs = {
            "cfg": cfg,
            "benchmarks_root": pathlib.Path("benchmarks"),
            "dry_run": False,
            "stage": "smoke",
            "allow_dirty": True,
        }
    with pytest.raises(PricingPolicyError, match="add a new immutable snapshot"):
        module.run_measurement(
            **kwargs,
            pricing_policy=LIVE_MEASUREMENT,
            today=datetime.date(2030, 1, 1),
        )


@pytest.mark.parametrize("module", [cache, dual, sweep])
def test_campaign_cli_defaults_to_live_measurement(module) -> None:
    parser = module._build_parser()
    args = parser.parse_args(["--experiment", "unused.yaml"])
    assert args.pricing_policy == LIVE_MEASUREMENT


def test_pricing_policy_provenance_conforms_to_schema() -> None:
    schema = json.loads(
        pathlib.Path("schemas/campaign_pricing_policy.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    provenance = verify_campaign_pricing(
        snapshot_path=CANONICAL_PAYG_SNAPSHOT_PATH,
        model_family="gpt-5.2",
        model_version="2025-12-11",
        policy_mode=HISTORICAL_REPLAY,
        today=datetime.date(2030, 1, 1),
    ).provenance()
    Draft7Validator(schema).validate(provenance)


def test_experiment_api_selects_historical_only_for_offline_campaign(
    monkeypatch,
) -> None:
    captured: list[str] = []

    class FakeModule:
        @staticmethod
        def main(argv):
            captured.extend(argv)
            return 0

    monkeypatch.setattr(
        experiment_runner.importlib, "import_module", lambda _name: FakeModule
    )
    result = experiments.run(
        "exp006_cache_key_bucketing_inmemory.yaml", dry_run=True
    )
    assert result.ok
    policy_index = captured.index("--pricing-policy")
    assert captured[policy_index + 1] == HISTORICAL_REPLAY

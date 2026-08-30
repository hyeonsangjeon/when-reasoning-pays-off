"""tests/test_measure_cache_key_bucketing.py — Task 018 v2.4 measurement-script unit tests.

These tests cover the pure helpers (no Azure traffic), the async_scheduled
dispatcher's cadence + backlog telemetry (network-free via a stubbed
``_call_with_retry``), and Stage 0 dry-run end-to-end via the public CLI.
Live-stage paths (Stage 1 smoke / Stage 2 evidence) are exercised
manually via the Mac Mini runner; the spec does NOT require a
live-replay harness inside pytest.

Test classes (in declaration order):
  - TestSelectBucket                       — pure routing function correctness.
  - TestBuildNamespace                     — namespace format + cross-cell uniqueness.
  - TestRpmTracker                         — 60-second rolling-window math.
  - TestComputeProjectedUsd                — preflight USD projection arithmetic.
  - TestComputeProjectedTpm                — TPM feasibility projection arithmetic.
  - TestLoadExperiment                     — YAML schema gates + v2.4 pinned-control enforcement.
  - TestPreflightUsdGate                   — projected USD > 0.9 × ceiling aborts.
  - TestTpmFeasibilityPreflight            — v2.4 pins pass / v2.2 regime aborts pre-client.
  - TestMidRunHaltAfterCell                — synthetic mid-run halt; partial summary; next cell not started.
  - TestTokenCapEnforcement                — over-cap records are flagged + skipped at HTTP layer.
  - TestAsyncCadenceHappyPath              — scheduled cadence + admitted ≈ scheduled under sem headroom.
  - TestSaturatedSemaphoreBacklogRegression — sem=1 + TTFT >= TPS interval → backlog blows up + backlog_excessive=True.
  - TestHeavyStubHappyPathSem96            — v2.4 sem=96 absorbs heavy TTFT; backlog stays nominal; in-flight <96.
  - TestCounterfactualSem8HeavyStub        — v2.4 counterfactual: sem=8 reproduces v2.3 saturation against the same heavy stub.
  - TestConcurrencyDispatcherEcho          — every record echoes v2.4 controls.
  - TestStartupAbort                       — bad YAML mutations abort before any HTTP client built.
  - TestDryRunEndToEnd                     — Stage 0 produces JSONL + summary with no network.

Test fixtures: the synthetic PAYG pricing at tests/fixtures/pricing/
azure-openai-payg-2026-05.yaml (gpt-5.2 input=10 cached=1 output=40 per 1M)
makes preflight math trivial to hand-verify.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import measure_cache_key_bucketing as m  # noqa: E402
from scripts._pricing_types import PaygPricing  # noqa: E402
from scripts.cost_calculator import load_payg_pricing  # noqa: E402


FIXTURE_PRICING_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "pricing" / "azure-openai-payg-2026-05.yaml"
)
"""Synthetic pricing snapshot — gpt-5.2 input=10/M, cached=1/M, output=40/M USD."""

REAL_PRICING_PATH = REPO_ROOT / "pricing" / "azure-openai-payg-2026-05.yaml"
"""The real May-2026 PAYG snapshot referenced by both Task 018 YAMLs."""

INMEMORY_YAML = REPO_ROOT / "experiments" / "exp006_cache_key_bucketing_inmemory.yaml"
TWENTYFOURH_YAML = REPO_ROOT / "experiments" / "exp006_cache_key_bucketing_24h.yaml"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _load_inmemory_yaml_body() -> dict:
    import yaml
    with INMEMORY_YAML.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _write_yaml(body: dict, cleanup_register) -> pathlib.Path:
    import yaml
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    yaml.safe_dump(body, f)
    f.close()
    p = pathlib.Path(f.name)
    cleanup_register(p.unlink)
    return p


# ----------------------------------------------------------------------------
# TestSelectBucket — pure routing function
# ----------------------------------------------------------------------------


class TestSelectBucket(unittest.TestCase):
    """``select_bucket`` is the routing primitive. It must be pure,
    deterministic, and round-robin over ``[0, cardinality)`` so the
    per-bucket arrival schedule is identical across runs given the same
    ``(cardinality, namespace)`` cell."""

    NS = "benchmark06_inmemory_card04_abcdef12"

    def test_round_robin_within_cardinality(self) -> None:
        for arrival in range(20):
            key = m.select_bucket(arrival, cardinality=4, namespace=self.NS)
            expected_idx = arrival % 4
            self.assertEqual(
                key,
                f"{self.NS}_bucket_{expected_idx:03d}",
                msg=f"arrival_idx={arrival} produced {key!r}",
            )

    def test_cardinality_one_routes_all_to_bucket_zero(self) -> None:
        """v2.3 card=1 is the per-bucket overflow case
        (60 × sustain_tps = 30 RPM common-prefix >> 15 RPM threshold)."""
        keys = {m.select_bucket(i, cardinality=1, namespace=self.NS) for i in range(50)}
        self.assertEqual(keys, {f"{self.NS}_bucket_000"})

    def test_cardinality_eight_yields_eight_buckets(self) -> None:
        """v2.3 card=8 is the default lower-RPM cell
        (30 / 8 = 3.75 RPM per bucket, below 15 RPM threshold)."""
        keys = {m.select_bucket(i, cardinality=8, namespace=self.NS) for i in range(40)}
        self.assertEqual(len(keys), 8)
        for k in keys:
            self.assertRegex(k, rf"^{self.NS}_bucket_\d{{3}}$")

    def test_emitted_keys_match_anonymization_regex(self) -> None:
        """Every emitted bucket key MUST match the audit regex so the
        anonymization grep returns 0 matches against real secrets."""
        for cardinality in (1, 2, 4, 8, 16):
            for retention_tag in ("inmemory", "24h"):
                ns = m.build_namespace(retention_tag, cardinality, "deadbeef")
                for arrival in (0, 1, 7, cardinality - 1):
                    k = m.select_bucket(arrival, cardinality, ns)
                    self.assertTrue(
                        m.BUCKET_KEY_RE.match(k),
                        msg=f"{k!r} did not match BUCKET_KEY_RE",
                    )

    def test_negative_or_zero_cardinality_rejected(self) -> None:
        with self.assertRaises((ValueError, ZeroDivisionError)):
            m.select_bucket(0, cardinality=0, namespace=self.NS)
        with self.assertRaises((ValueError, ZeroDivisionError)):
            m.select_bucket(0, cardinality=-1, namespace=self.NS)

    def test_negative_arrival_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.select_bucket(-1, cardinality=4, namespace=self.NS)


# ----------------------------------------------------------------------------
# TestBuildNamespace — cell-unique namespace construction
# ----------------------------------------------------------------------------


class TestBuildNamespace(unittest.TestCase):

    def test_format_matches_spec(self) -> None:
        ns = m.build_namespace("inmemory", 4, "abcdef01")
        self.assertEqual(ns, "benchmark06_inmemory_card04_abcdef01")

    def test_24h_retention_tag(self) -> None:
        ns = m.build_namespace("24h", 16, "12345678")
        self.assertEqual(ns, "benchmark06_24h_card16_12345678")

    def test_cardinality_zero_padded_to_two_digits(self) -> None:
        ns = m.build_namespace("inmemory", 1, "abcdef01")
        self.assertIn("_card01_", ns)
        ns = m.build_namespace("inmemory", 8, "abcdef01")
        self.assertIn("_card08_", ns)

    def test_different_cardinalities_distinct(self) -> None:
        a = m.build_namespace("inmemory", 1, "abcdef01")
        b = m.build_namespace("inmemory", 4, "abcdef01")
        self.assertNotEqual(a, b)

    def test_different_retentions_distinct(self) -> None:
        a = m.build_namespace("inmemory", 4, "abcdef01")
        b = m.build_namespace("24h", 4, "abcdef01")
        self.assertNotEqual(a, b)

    def test_run_id_short_creates_per_process_isolation(self) -> None:
        a = m.build_namespace("24h", 4, "aaaa1111")
        b = m.build_namespace("24h", 4, "bbbb2222")
        self.assertNotEqual(a, b)

    def test_rejects_invalid_retention_tag(self) -> None:
        with self.assertRaises(ValueError):
            m.build_namespace("forever", 4, "abcdef01")
        with self.assertRaises(ValueError):
            m.build_namespace("", 4, "abcdef01")


# ----------------------------------------------------------------------------
# TestRpmTracker — 60-second rolling-window counter
# ----------------------------------------------------------------------------


class TestRpmTracker(unittest.TestCase):

    def test_records_within_window_counted(self) -> None:
        tr = m.RpmTracker(window_s=60.0)
        for t in [0.0, 1.0, 2.0, 30.0, 59.5]:
            tr.record(t)
        self.assertEqual(tr.count(59.6), 5)

    def test_records_outside_window_dropped(self) -> None:
        tr = m.RpmTracker(window_s=60.0)
        tr.record(0.0)
        tr.record(61.0)
        self.assertEqual(tr.count(61.5), 1)

    def test_empty_tracker_counts_zero(self) -> None:
        tr = m.RpmTracker(window_s=60.0)
        self.assertEqual(tr.count(100.0), 0)

    def test_custom_window(self) -> None:
        tr = m.RpmTracker(window_s=10.0)
        for t in [0.0, 5.0, 9.0, 11.0]:
            tr.record(t)
        self.assertEqual(tr.count(11.5), 3)


# ----------------------------------------------------------------------------
# TestComputeProjectedUsd — preflight USD projection arithmetic
# ----------------------------------------------------------------------------


class TestComputeProjectedUsd(unittest.TestCase):
    """``compute_projected_usd`` drives the preflight USD gate
    (0.9 × ceiling). The fixture pricing makes the math trivial:
       gpt-5.2 input=10/M, cached=1/M, output=40/M USD.
    Per call with input=1000, cached_fraction=0, output=100:
       1000/1M × $10 + 100/1M × $40 = $0.01 + $0.004 = $0.014.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.pricing: PaygPricing = load_payg_pricing(FIXTURE_PRICING_PATH)

    def test_zero_cached_fraction(self) -> None:
        usd = m.compute_projected_usd(
            cardinalities=[1],
            calls_per_cell=1,
            pricing=self.pricing,
            model="gpt-5.2",
            input_tokens=1000.0,
            output_tokens=100.0,
            cached_fraction=0.0,
        )
        self.assertAlmostEqual(usd, 0.014, places=6)

    def test_full_cached_fraction(self) -> None:
        usd = m.compute_projected_usd(
            cardinalities=[1],
            calls_per_cell=1,
            pricing=self.pricing,
            model="gpt-5.2",
            input_tokens=1000.0,
            output_tokens=100.0,
            cached_fraction=1.0,
        )
        self.assertAlmostEqual(usd, 0.005, places=6)

    def test_scales_linearly_with_calls(self) -> None:
        usd_1 = m.compute_projected_usd(
            cardinalities=[1, 1, 1, 1, 1],
            calls_per_cell=100,
            pricing=self.pricing,
            model="gpt-5.2",
            input_tokens=1000.0,
            output_tokens=100.0,
            cached_fraction=0.0,
        )
        self.assertAlmostEqual(usd_1, 7.0, places=6)

    def test_v23_default_sweep_under_real_pricing_below_ceiling(self) -> None:
        """v2.3 default [1, 8] × 480 calls/cell at the REAL May-2026
        pricing must project BELOW the 0.9 × $60 = $54 preflight
        threshold. Extended sweeps [1,2,4,8,16] × 480 likewise stay
        below."""
        real_pricing = load_payg_pricing(REAL_PRICING_PATH)
        usd_default = m.compute_projected_usd(
            cardinalities=[1, 8],
            calls_per_cell=480,
            pricing=real_pricing,
            model="gpt-5.2",
            input_tokens=10000.0,
            output_tokens=float(m.EVIDENCE_PROJECTED_OUTPUT_TOKENS),
            cached_fraction=m.EVIDENCE_CACHED_FRACTION,
        )
        self.assertLess(
            usd_default, 54.0,
            msg=(
                f"v2.3 default sweep projects ${usd_default:.4f} >= $54 "
                f"(0.9 × $60 preflight). Either Azure raised gpt-5.2 PAYG "
                f"rates or the YAML hard ceiling needs raising. Do NOT "
                f"lower pinned controls."
            ),
        )
        usd_extended = m.compute_projected_usd(
            cardinalities=[1, 2, 4, 8, 16],
            calls_per_cell=480,
            pricing=real_pricing,
            model="gpt-5.2",
            input_tokens=10000.0,
            output_tokens=float(m.EVIDENCE_PROJECTED_OUTPUT_TOKENS),
            cached_fraction=m.EVIDENCE_CACHED_FRACTION,
        )
        self.assertLess(usd_extended, 54.0)


# ----------------------------------------------------------------------------
# TestComputeProjectedTpm — TPM feasibility projection arithmetic
# ----------------------------------------------------------------------------


class TestComputeProjectedTpm(unittest.TestCase):
    """``compute_projected_tpm`` drives the v2.3 TPM feasibility preflight.
    Math: ``60 × sustain_tps × estimated_processed_tokens_max``."""

    def test_v23_pins(self) -> None:
        """v2.3: 60 × 0.5 × 11000 = 330000."""
        self.assertEqual(
            m.compute_projected_tpm(
                sustain_tps=0.5,
                estimated_processed_tokens_max=11000,
            ),
            330000.0,
        )

    def test_v22_pins(self) -> None:
        """The v2.2 regime (sustain_tps=1.0, est=30512) projects 1.83M
        TPM, ~5.2× the 350K ceiling. This is exactly the math that
        forced the v2.3 hotfix."""
        projected = m.compute_projected_tpm(
            sustain_tps=1.0,
            estimated_processed_tokens_max=30512,
        )
        self.assertAlmostEqual(projected, 1830720.0, places=1)
        self.assertGreater(projected, 0.70 * 500000)

    def test_zero_tps_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.compute_projected_tpm(
                sustain_tps=0.0,
                estimated_processed_tokens_max=11000,
            )

    def test_negative_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            m.compute_projected_tpm(
                sustain_tps=-0.1,
                estimated_processed_tokens_max=11000,
            )
        with self.assertRaises(ValueError):
            m.compute_projected_tpm(
                sustain_tps=0.5,
                estimated_processed_tokens_max=-1,
            )


# ----------------------------------------------------------------------------
# TestLoadExperiment — YAML schema gates + v2.3 pinned-control enforcement
# ----------------------------------------------------------------------------


class TestLoadExperiment(unittest.TestCase):
    """``load_experiment`` is the single source of truth for v2.3
    pinned-control enforcement. Every pinned control rejects mismatches
    at YAML load time so a bad run cannot reach the network."""

    def test_inmemory_yaml_loads_with_v23_pins(self) -> None:
        cfg = m.load_experiment(INMEMORY_YAML)
        self.assertEqual(cfg.experiment_id, "exp006_cache_key_bucketing_inmemory")
        self.assertEqual(cfg.request_template.prompt_cache_retention, "in_memory")
        self.assertEqual(cfg.request_template.max_output_tokens, 512)
        self.assertEqual(cfg.request_template.reasoning_effort, "low")
        self.assertEqual(
            cfg.request_template.estimated_processed_tokens_max,
            m.ESTIMATED_PROCESSED_TOKENS_MAX,
        )
        self.assertEqual(cfg.client.api_version, m.FOUNDRY_API_VERSION)
        # v2.4 pinned controls: concurrency=96, sustain_tps=0.5, dispatcher=async_scheduled.
        self.assertEqual(cfg.runtime.concurrency, m.CONCURRENCY_PINNED)
        self.assertEqual(cfg.runtime.concurrency, 96)
        self.assertEqual(cfg.runtime.sustain_tps, m.SUSTAIN_TPS_PINNED)
        self.assertEqual(cfg.runtime.dispatcher, m.DISPATCHER_PINNED)
        # v2.4 default sweep is [1, 8]; extension to [1,2,4,8,16] is permitted.
        self.assertEqual(cfg.sweep.bucket_cardinality, [1, 8])
        self.assertEqual(cfg.deployment.family, "gpt-5.2")
        self.assertEqual(cfg.deployment.auth_mode, "entra")
        self.assertEqual(cfg.metadata["consumption_model_context"], "paygo_standard")
        self.assertFalse(cfg.metadata["simulation"])
        self.assertFalse(cfg.metadata["ptu_evidence"])
        # v2.3 pinned metadata field (preserved verbatim in v2.4).
        self.assertEqual(cfg.deployment_tpm_quota, m.DEPLOYMENT_TPM_QUOTA_DEFAULT)

    def test_24h_yaml_loads_and_has_zero_washout(self) -> None:
        cfg = m.load_experiment(TWENTYFOURH_YAML)
        self.assertEqual(cfg.experiment_id, "exp006_cache_key_bucketing_24h")
        self.assertEqual(cfg.request_template.prompt_cache_retention, "24h")
        self.assertEqual(cfg.runtime.washout_seconds, 0)
        # v2.4 pins must hold for the 24h sibling too.
        self.assertEqual(cfg.runtime.concurrency, m.CONCURRENCY_PINNED)
        self.assertEqual(cfg.runtime.concurrency, 96)
        self.assertEqual(cfg.runtime.sustain_tps, m.SUSTAIN_TPS_PINNED)
        self.assertEqual(cfg.runtime.dispatcher, m.DISPATCHER_PINNED)
        self.assertEqual(cfg.deployment_tpm_quota, m.DEPLOYMENT_TPM_QUOTA_DEFAULT)

    def _base_body(self) -> dict:
        return _load_inmemory_yaml_body()

    def test_rejects_throttled_deployment(self) -> None:
        body = self._base_body()
        body["deployment"] = dict(body["deployment"])
        body["deployment"]["deployment"] = "${AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED}"
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "THROTTLED"):
            m.load_experiment(p)

    def test_rejects_wrong_max_output_tokens(self) -> None:
        body = self._base_body()
        body["request_template"] = dict(body["request_template"])
        body["request_template"]["max_output_tokens"] = 256
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            m.load_experiment(p)

    def test_rejects_wrong_reasoning_effort(self) -> None:
        body = self._base_body()
        body["request_template"] = dict(body["request_template"])
        body["request_template"]["reasoning"] = {"effort": "medium"}
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "effort"):
            m.load_experiment(p)

    def test_rejects_wrong_api_version(self) -> None:
        body = self._base_body()
        body["client"] = {"api_version": "2025-03-01-preview"}
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "api_version"):
            m.load_experiment(p)

    def test_rejects_v21_concurrency_1(self) -> None:
        """v2.1 concurrency=1 is now REJECTED. v2.4 pins concurrency=96."""
        body = self._base_body()
        body["runtime"] = dict(body["runtime"])
        body["runtime"]["concurrency"] = 1
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "concurrency"):
            m.load_experiment(p)

    def test_rejects_v23_concurrency_8(self) -> None:
        """v2.4 rejects v2.3 concurrency=8 — sem=8 saturated under live
        gpt-5.2 P95 TTFT ≈ 128 s and tripped backlog_excessive on both
        YAMLs in Stage 1 smoke."""
        body = self._base_body()
        body["runtime"] = dict(body["runtime"])
        body["runtime"]["concurrency"] = 8
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "concurrency"):
            m.load_experiment(p)

    def test_rejects_v21_sustain_tps_1(self) -> None:
        """v2.1 sustain_tps=1.0 is now REJECTED. v2.4 pins sustain_tps=0.5."""
        body = self._base_body()
        body["runtime"] = dict(body["runtime"])
        body["runtime"]["sustain_tps"] = 1.0
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "sustain_tps"):
            m.load_experiment(p)

    def test_rejects_missing_dispatcher(self) -> None:
        """v2.4 requires runtime.dispatcher to be set explicitly."""
        body = self._base_body()
        body["runtime"] = dict(body["runtime"])
        body["runtime"].pop("dispatcher", None)
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "dispatcher"):
            m.load_experiment(p)

    def test_rejects_wrong_dispatcher(self) -> None:
        body = self._base_body()
        body["runtime"] = dict(body["runtime"])
        body["runtime"]["dispatcher"] = "serial"
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "dispatcher"):
            m.load_experiment(p)

    def test_rejects_missing_estimated_processed_tokens_max(self) -> None:
        body = self._base_body()
        body["request_template"] = dict(body["request_template"])
        body["request_template"].pop("estimated_processed_tokens_max", None)
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "estimated_processed_tokens_max"):
            m.load_experiment(p)

    def test_rejects_wrong_estimated_processed_tokens_max(self) -> None:
        body = self._base_body()
        body["request_template"] = dict(body["request_template"])
        body["request_template"]["estimated_processed_tokens_max"] = 30000
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "estimated_processed_tokens_max"):
            m.load_experiment(p)

    def test_rejects_missing_deployment_tpm_quota(self) -> None:
        body = self._base_body()
        body["metadata"] = dict(body["metadata"])
        body["metadata"].pop("deployment_tpm_quota", None)
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "deployment_tpm_quota"):
            m.load_experiment(p)

    def test_rejects_wrong_deployment_tpm_quota(self) -> None:
        body = self._base_body()
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["deployment_tpm_quota"] = 1000000
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "deployment_tpm_quota"):
            m.load_experiment(p)

    def test_rejects_missing_payg_metadata(self) -> None:
        body = self._base_body()
        body["metadata"] = dict(body["metadata"])
        body["metadata"].pop("ptu_evidence", None)
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "ptu_evidence"):
            m.load_experiment(p)

    def test_rejects_ptu_evidence_true(self) -> None:
        body = self._base_body()
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["ptu_evidence"] = True
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "ptu_evidence"):
            m.load_experiment(p)

    def test_rejects_simulation_true(self) -> None:
        body = self._base_body()
        body["metadata"] = dict(body["metadata"])
        body["metadata"]["simulation"] = True
        p = _write_yaml(body, self.addCleanup)
        with self.assertRaisesRegex(ValueError, "simulation"):
            m.load_experiment(p)


# ----------------------------------------------------------------------------
# TestPreflightUsdGate — projected USD > 0.9 × ceiling aborts
# ----------------------------------------------------------------------------


class TestPreflightUsdGate(unittest.TestCase):
    """The preflight USD gate ABORTS the run BEFORE the first network
    call when ``projected_usd > 0.9 × hard_ceiling``."""

    def test_compute_projected_usd_exceeds_threshold(self) -> None:
        pricing = load_payg_pricing(FIXTURE_PRICING_PATH)
        projected = m.compute_projected_usd(
            cardinalities=[1, 8],
            calls_per_cell=1000,
            pricing=pricing,
            model="gpt-5.2",
            input_tokens=10000.0,
            output_tokens=500.0,
            cached_fraction=0.0,
        )
        # 2 × 1000 × (10000/1M × $10 + 500/1M × $40) = 2 × 1000 × 0.12 = 240
        self.assertAlmostEqual(projected, 240.0, places=4)
        hard_ceiling = 60.0
        self.assertGreater(projected, 0.9 * hard_ceiling)

    def test_projected_below_threshold_does_not_abort(self) -> None:
        pricing = load_payg_pricing(FIXTURE_PRICING_PATH)
        projected = m.compute_projected_usd(
            cardinalities=[1, 8],
            calls_per_cell=10,
            pricing=pricing,
            model="gpt-5.2",
            input_tokens=10000.0,
            output_tokens=500.0,
            cached_fraction=0.0,
        )
        # 2 × 10 × 0.12 = 2.4
        hard_ceiling = 60.0
        self.assertLessEqual(projected, 0.9 * hard_ceiling)


# ----------------------------------------------------------------------------
# TestTpmFeasibilityPreflight — v2.3 pins pass, v2.2 regime aborts pre-client
# ----------------------------------------------------------------------------


class _SpyClientFactory:
    """Captures whether ``_build_live_client`` was ever called. Used to
    prove the TPM/startup aborts fire BEFORE any HTTP client is built."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *, endpoint_value: str):
        self.calls += 1
        return object()


class TestTpmFeasibilityPreflight(unittest.TestCase):
    """The v2.3 TPM feasibility preflight gate ABORTS the run BEFORE the
    HTTP client is constructed when
    ``60 × sustain_tps × estimated_processed_tokens_max > 0.70 × deployment_tpm_quota``.
    """

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["AZURE_OPENAI_FOUNDRY_ENDPOINT"] = (
            "https://fake-test-host.invalid.test"
        )
        os.environ["AZURE_OPENAI_DEPLOYMENT_GPT_5_2"] = "test-dep-unthrottled"
        self._tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task018test_"))
        src = REPO_ROOT / "benchmarks" / "06-cache-key-bucketing"
        dst = self._tmpdir / "benchmarks" / "06-cache-key-bucketing"
        shutil.copytree(src, dst)
        (dst / "runs").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_v23_pins_pass(self) -> None:
        """v2.3 pins: 60 × 0.5 × 11000 = 330000 <= 0.70 × 500000 = 350000."""
        cfg = m.load_experiment(INMEMORY_YAML)
        # Dry-run validates the TPM gate fires and lets us see the result
        # in the summary without making any HTTP calls.
        result = m.run_measurement(
            cfg=cfg,
            benchmarks_root=self._tmpdir / "benchmarks",
            dry_run=True,
            stage="evidence",
            allow_dirty=True,
            pricing_policy="historical-replay",
        )
        with open(str(result.jsonl_path) + ".summary.json") as fh:
            summary = json.load(fh)
        self.assertTrue(summary["tpm_feasibility"]["passed"])
        self.assertEqual(summary["tpm_feasibility"]["projected_tpm"], 330000.0)
        self.assertEqual(summary["tpm_feasibility"]["ceiling"], 350000.0)

    def test_v22_pins_abort_before_client_build(self) -> None:
        """v2.2 regime (sustain_tps=1.0, est=30512) is rejected at YAML
        load (because the loader pins sustain_tps=0.5). Even if we
        bypass the loader and stuff the v2.2 values into the cfg, the
        TPM preflight must abort BEFORE ``_build_live_client`` runs.

        We test the loader rejection path here (the stronger guarantee:
        v2.2 YAMLs never reach the runtime).
        """
        body = _load_inmemory_yaml_body()
        body["runtime"] = dict(body["runtime"])
        body["runtime"]["sustain_tps"] = 1.0
        body["request_template"] = dict(body["request_template"])
        body["request_template"]["estimated_processed_tokens_max"] = 30512
        p = _write_yaml(body, self.addCleanup)
        # Loader rejects sustain_tps=1.0 first (it would also reject the
        # estimated_processed_tokens_max=30512 mutation).
        with self.assertRaises(ValueError):
            m.load_experiment(p)

    def test_runtime_tpm_abort_bypasses_client_construction(self) -> None:
        """If a hypothetical YAML somehow passed loader (e.g. via direct
        cfg construction in tests/diagnostics), the runtime TPM preflight
        must still abort BEFORE the live client is built. We patch
        ``_build_live_client`` with a spy and call the async runner with
        an over-quota synthetic cfg."""
        cfg = m.load_experiment(INMEMORY_YAML)
        # Mutate the runtime cfg directly so the runtime preflight fires.
        # We can't reassign frozen dataclass fields, so build a new one.
        from dataclasses import replace
        bad_runtime = replace(cfg.runtime, sustain_tps=2.0)
        # Skip loader validation by replacing cfg.runtime directly via
        # object.__setattr__ on the frozen ExperimentConfig.
        object.__setattr__(cfg, "runtime", bad_runtime)
        spy = _SpyClientFactory()
        orig_builder = m._build_live_client
        m._build_live_client = spy
        try:
            with self.assertRaises(m.TpmFeasibilityAbortError):
                asyncio.run(
                    m._run_measurement_async(
                        cfg=cfg,
                        runs_dir=self._tmpdir / "benchmarks" / cfg.benchmark / "runs",
                        system_prompt="x" * 100,
                        user_prompts=["alpha"],
                        git_commit="testcommit",
                        dirty=True,
                        pricing=load_payg_pricing(FIXTURE_PRICING_PATH),
                        pricing_snapshot_path=str(FIXTURE_PRICING_PATH),
                        endpoint_value="https://fake.test",
                        deployment="dep",
                        dry_run=False,
                        stage="smoke",
                        timestamp_label="20260101T000000Z",
                        run_id_short="deadbeef",
                        today=datetime.date(2026, 5, 19),
                    )
                )
        finally:
            m._build_live_client = orig_builder
        self.assertEqual(
            spy.calls, 0,
            msg="TPM feasibility preflight MUST abort BEFORE the HTTP "
                "client is constructed; spy saw a build call.",
        )


# ----------------------------------------------------------------------------
# TestMidRunHaltAfterCell — synthetic mid-run halt
# ----------------------------------------------------------------------------


class TestMidRunHaltAfterCell(unittest.TestCase):
    """Mid-run gate: when cumulative USD exceeds 0.85 × hard_ceiling
    after a cell finishes, the runner writes a partial summary, does
    NOT start the next cell, and exits with EXIT_OK (partial run is a
    legitimate outcome). Verified deterministically by stubbing the cost
    per call via the fixture pricing + cardinality math."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["AZURE_OPENAI_FOUNDRY_ENDPOINT"] = (
            "https://fake-test-host.invalid.test"
        )
        os.environ["AZURE_OPENAI_DEPLOYMENT_GPT_5_2"] = "test-dep-unthrottled"
        self._tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task018test_"))
        src = REPO_ROOT / "benchmarks" / "06-cache-key-bucketing"
        dst = self._tmpdir / "benchmarks" / "06-cache-key-bucketing"
        shutil.copytree(src, dst)
        (dst / "runs").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_midrun_halt_after_first_cell_writes_partial_summary(self) -> None:
        """Stub the dispatcher so cell 0 spends > 0.85 × hard_ceiling.
        Then assert: only 1 cell ran, partial=True, halt_reason set, the
        partial summary file exists, and the second cell's namespace
        never appears in the JSONL."""
        cfg = m.load_experiment(INMEMORY_YAML)
        # Patch _run_cell to return a synthetic record with cell_usd large
        # enough to trip the mid-run gate.
        orig_run_cell = m._run_cell
        # mid-run threshold = 0.85 × 60.0 = $51. Make cell 0 spend $55.
        synthetic_cell_usd = 55.0

        async def stub_run_cell(**kwargs):
            cell_idx = kwargs["cell_idx"]
            cardinality = kwargs["cardinality"]
            namespace = kwargs["namespace"]
            calls_in_cell = kwargs["calls_in_cell"]
            out_fh = kwargs["out_fh"]
            # Build one synthetic record so the JSONL isn't empty.
            rec = {
                "experiment_id": cfg.experiment_id,
                "cell_idx": cell_idx,
                "arrival_idx_within_cell": 0,
                "bucket_cardinality": cardinality,
                "bucket_index": 0,
                "bucket_namespace": namespace,
                "prompt_cache_key_used": f"{namespace}_bucket_000",
                "scheduled_dispatch_cell_elapsed_ms": 0,
                "admitted_dispatch_cell_elapsed_ms": 1,
                "dispatch_backlog_ms": 1,
                "in_flight_at_dispatch": 0,
                "per_bucket_running_rpm": 0,
                "common_prefix_running_rpm": 0,
                "first_token_latency_ms": 10.0,
                "total_latency_ms": 10.0,
                "rate_limited": False,
                "failed": False,
                "relative_time_s": 0.0,
                "canonical_input_tokens": 0,
                "canonical_cached_tokens": 0,
            }
            out_fh.write(json.dumps(rec) + "\n")
            out_fh.flush()
            return [rec], synthetic_cell_usd, 1

        m._run_cell = stub_run_cell
        try:
            result = m.run_measurement(
                cfg=cfg,
                benchmarks_root=self._tmpdir / "benchmarks",
                dry_run=True,
                stage="evidence",
                allow_dirty=True,
                pricing_policy="historical-replay",
            )
        finally:
            m._run_cell = orig_run_cell

        self.assertEqual(result.cells_completed, 1)
        self.assertEqual(result.cells_planned, 2)
        self.assertTrue(result.partial)
        self.assertEqual(result.halt_reason, "midrun_budget_gate")
        # Partial summary alias must exist next to the canonical summary.
        partial_path = pathlib.Path(
            str(result.jsonl_path).replace(".jsonl", ".partial.summary.json")
        )
        self.assertTrue(partial_path.is_file(), msg=f"missing {partial_path}")
        # Second cell's card08 namespace MUST NOT appear in the JSONL
        # because the gate halted before it started.
        text = pathlib.Path(result.jsonl_path).read_text(encoding="utf-8")
        self.assertNotIn("card08", text)


# ----------------------------------------------------------------------------
# TestTokenCapEnforcement — over-cap records are flagged + skipped
# ----------------------------------------------------------------------------


class TestTokenCapEnforcement(unittest.TestCase):
    """Per-request token cap: the dispatcher MUST refuse to send any
    request whose estimated processed tokens exceed
    ``estimated_processed_tokens_max`` (11000). The record is written
    with ``failed=True, failure_reason='token_cap_exceeded'`` and NO
    HTTP call is issued.

    The cap math is ``int((len(sys)+len(user))/4) + max_output_tokens``.
    With max_output=512 and est_max=11000, the prompt char budget is
    ``(11000 - 512) × 4 = 41952 chars`` (sys + user combined).
    """

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["AZURE_OPENAI_FOUNDRY_ENDPOINT"] = (
            "https://fake.invalid.test"
        )
        os.environ["AZURE_OPENAI_DEPLOYMENT_GPT_5_2"] = "test-dep"
        self._tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task018test_"))
        src = REPO_ROOT / "benchmarks" / "06-cache-key-bucketing"
        dst = self._tmpdir / "benchmarks" / "06-cache-key-bucketing"
        shutil.copytree(src, dst)
        (dst / "runs").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_oversized_prompt_rejects_without_http_send(self) -> None:
        """Force the system prompt to be huge so estimated tokens > cap.
        Spy on ``_call_with_retry`` to confirm it was never invoked."""
        from dataclasses import replace
        cfg = m.load_experiment(INMEMORY_YAML)
        cfg = replace(
            cfg,
            runtime=replace(
                cfg.runtime,
                sustain_tps=10.0,
                cell_duration_seconds=2,
                washout_seconds=0,
            ),
            deployment_tpm_quota=20_000_000,
        )
        # Build a 60000-char system prompt → ~15000 tokens > 11000 cap.
        # Stub _build_system_prompt to return this directly.
        big_prompt = "X" * 60000
        orig_build = m._build_system_prompt
        m._build_system_prompt = lambda *a, **kw: big_prompt
        # Spy on _call_with_retry.
        call_count = {"n": 0}

        async def spy_call(**kwargs):
            call_count["n"] += 1
            return {
                "usage": m._zero_usage_dict(),
                "first_token_latency_ms": 0.0,
                "total_latency_ms": 0.0,
                "rate_limited": False,
                "rate_limited_count": 0,
                "headers": {},
                "raised": None,
            }

        orig_call = m._call_with_retry
        orig_pre = m._preflight_reachability
        m._call_with_retry = spy_call

        async def stub_preflight(**kwargs):
            return {"deployment": kwargs["deployment"], "reachable": True}
        m._preflight_reachability = stub_preflight
        orig_build_client = m._build_live_client
        m._build_live_client = lambda **kw: object()
        try:
            result = m.run_measurement(
                cfg=cfg,
                benchmarks_root=self._tmpdir / "benchmarks",
                dry_run=False,
                stage="smoke",
                allow_dirty=True,
                today=datetime.date(2026, 5, 20),
            )
        finally:
            m._build_system_prompt = orig_build
            m._call_with_retry = orig_call
            m._preflight_reachability = orig_pre
            m._build_live_client = orig_build_client

        # All requests should have been rejected at the token cap.
        self.assertEqual(
            call_count["n"], 0,
            msg="_call_with_retry MUST NOT be invoked for over-cap records",
        )
        # Every record should have failed=True with failure_reason=token_cap_exceeded.
        with open(result.jsonl_path) as fh:
            for line in fh:
                rec = json.loads(line)
                self.assertTrue(rec["failed"])
                self.assertEqual(rec["failure_reason"], "token_cap_exceeded")

    def test_undersized_prompt_dispatches_normally(self) -> None:
        """Inverse: a normal-size prompt does NOT trip the cap."""
        from dataclasses import replace
        cfg = m.load_experiment(INMEMORY_YAML)
        cfg = replace(
            cfg,
            runtime=replace(
                cfg.runtime,
                sustain_tps=10.0,
                cell_duration_seconds=2,
                washout_seconds=0,
            ),
            deployment_tpm_quota=20_000_000,
        )
        # ~5000 char system prompt → ~1300 tokens; well below the 11000 cap.
        orig_build = m._build_system_prompt
        m._build_system_prompt = lambda *a, **kw: "X" * 5000

        async def stub_call(**kwargs):
            return {
                "usage": {
                    "input_tokens": 1300,
                    "input_tokens_details": {"cached_tokens": 1100},
                    "output_tokens": 100,
                },
                "first_token_latency_ms": 100.0,
                "total_latency_ms": 100.0,
                "rate_limited": False,
                "rate_limited_count": 0,
                "headers": {},
                "raised": None,
            }

        orig_call = m._call_with_retry
        m._call_with_retry = stub_call

        async def stub_preflight(**kwargs):
            return {"deployment": kwargs["deployment"], "reachable": True}
        orig_pre = m._preflight_reachability
        m._preflight_reachability = stub_preflight
        orig_build_client = m._build_live_client
        m._build_live_client = lambda **kw: object()
        try:
            result = m.run_measurement(
                cfg=cfg,
                benchmarks_root=self._tmpdir / "benchmarks",
                dry_run=False,
                stage="smoke",
                allow_dirty=True,
                today=datetime.date(2026, 5, 20),
            )
        finally:
            m._build_system_prompt = orig_build
            m._call_with_retry = orig_call
            m._preflight_reachability = orig_pre
            m._build_live_client = orig_build_client

        with open(result.jsonl_path) as fh:
            for line in fh:
                rec = json.loads(line)
                self.assertFalse(rec["failed"])
                self.assertIsNone(rec["failure_reason"])


# ----------------------------------------------------------------------------
# TestAsyncCadenceHappyPath — scheduled cadence under sem headroom
# ----------------------------------------------------------------------------


class TestAsyncCadenceHappyPath(unittest.TestCase):
    """Happy path for the v2.4 async_scheduled dispatcher with abundant
    sem headroom:
       sem = 96, sustain_tps = 10.0 (fast for tests), TTFT stub = 0.18s.
       60 arrivals → admitted timestamps ≈ scheduled timestamps; P95
       backlog < 500ms; cell summary backlog_excessive = False; max
       in_flight observed in [1, 96] (typically ~2-4 with a light stub).
    The test uses sustain_tps=10.0 so the cell completes in ~6s of
    wall-clock time, not the live YAML's 16 minutes.
    """

    def test_admitted_tracks_scheduled_under_headroom(self) -> None:
        # Build a config in-process so we can override sustain_tps and
        # estimated_processed_tokens_max without disturbing the YAML.
        from dataclasses import replace
        cfg = m.load_experiment(INMEMORY_YAML)
        cfg = replace(
            cfg,
            runtime=replace(cfg.runtime, sustain_tps=10.0, cell_duration_seconds=6),
        )

        async def fast_call(**kwargs):
            # ~180ms simulated TTFT; sem=96 is far above the in-flight
            # ceiling, but TPS=10 × TTFT=0.18s ≈ 1.8 in-flight so the
            # max_in_flight observed remains small.
            await asyncio.sleep(0.18)
            return {
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {"cached_tokens": 800},
                    "output_tokens": 100,
                },
                "first_token_latency_ms": 180.0,
                "total_latency_ms": 180.0,
                "rate_limited": False,
                "rate_limited_count": 0,
                "headers": {},
                "raised": None,
            }

        records: list[dict] = []
        max_in_flight = 0

        async def run_one_cell():
            nonlocal max_in_flight
            tmpfh = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False
            )
            self.addCleanup(lambda: pathlib.Path(tmpfh.name).unlink(missing_ok=True))
            orig_call = m._call_with_retry
            m._call_with_retry = fast_call
            try:
                recs, _usd, peak = await m._run_cell(
                    cfg=cfg,
                    cell_idx=0,
                    cardinality=1,
                    calls_in_cell=60,
                    sustain_tps=10.0,
                    concurrency=96,
                    namespace="benchmark06_inmemory_card01_testcd01",
                    client=object(),
                    deployment="dep",
                    system_prompt="x" * 1000,
                    user_prompts=["alpha", "beta", "gamma"],
                    git_commit="testcommit",
                    dirty=True,
                    system_sha="x" * 64,
                    pricing_snapshot_path=str(FIXTURE_PRICING_PATH),
                    pricing=load_payg_pricing(FIXTURE_PRICING_PATH),
                    dry_run=False,
                    out_fh=tmpfh,
                    global_request_offset=0,
                    sim_started_mono=time.monotonic(),
                    run_id_short="testcd01",
                )
                records.extend(recs)
                max_in_flight = peak
            finally:
                m._call_with_retry = orig_call
                tmpfh.close()

        asyncio.run(run_one_cell())

        # 60 arrivals dispatched.
        self.assertEqual(len(records), 60)
        self.assertTrue(all(not r["failed"] for r in records))
        # Admitted-elapsed-ms approximately tracks scheduled-elapsed-ms
        # (sem=96 has abundant headroom for TPS=10 with TTFT=0.18s;
        # ~1.8 in-flight on average). P95 backlog should stay well under
        # 500ms.
        backlogs = [r["dispatch_backlog_ms"] for r in records]
        p95 = m._percentile([float(b) for b in backlogs], 95.0)
        self.assertLess(
            p95, 500.0,
            msg=f"P95 backlog {p95:.0f}ms exceeds 500ms headroom budget",
        )
        # backlog_excessive must be False on the cell summary.
        agg = m._aggregate_cell(records, warmup_exclusion_s=0.0, sustain_tps=10.0)
        self.assertFalse(agg["backlog_excessive"])
        # Max in_flight under saturation hint: with TPS=10 and TTFT=0.18s
        # the average is ~1.8 but bursts up to ~3-4. Cap at sem=96.
        self.assertGreaterEqual(max_in_flight, 1)
        self.assertLessEqual(max_in_flight, 96)
        # Realized admitted common-prefix RPM should be close to the
        # scheduled rate of 60 × 10 = 600 RPM (but RpmTracker has a
        # 60s window and the cell is ~6s, so it just counts everything
        # admitted to date; the per-record max is 60).
        self.assertGreater(agg["realized_admitted_common_prefix_rpm"], 0)


# ----------------------------------------------------------------------------
# TestSaturatedSemaphoreBacklogRegression
# ----------------------------------------------------------------------------


class TestSaturatedSemaphoreBacklogRegression(unittest.TestCase):
    """Saturation path: when sem < sustain_tps × per_call_seconds, the
    backlog grows monotonically. We pin sem=1 and TTFT=0.18s at TPS=10:
       The pacer schedules arrivals every 100ms.
       Sem=1 + 180ms TTFT means each arrival waits at least
       (arrival_idx × 80ms) for the previous one to release.
       The cell summary MUST report backlog_excessive=True.
    """

    def test_saturated_dispatcher_reports_backlog_excessive(self) -> None:
        from dataclasses import replace
        cfg = m.load_experiment(INMEMORY_YAML)
        cfg = replace(
            cfg,
            runtime=replace(cfg.runtime, sustain_tps=10.0),
        )

        async def slow_call(**kwargs):
            await asyncio.sleep(0.18)
            return {
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {"cached_tokens": 800},
                    "output_tokens": 100,
                },
                "first_token_latency_ms": 180.0,
                "total_latency_ms": 180.0,
                "rate_limited": False,
                "rate_limited_count": 0,
                "headers": {},
                "raised": None,
            }

        records: list[dict] = []

        async def run_one_cell():
            tmpfh = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False
            )
            self.addCleanup(lambda: pathlib.Path(tmpfh.name).unlink(missing_ok=True))
            orig_call = m._call_with_retry
            m._call_with_retry = slow_call
            try:
                recs, _usd, _peak = await m._run_cell(
                    cfg=cfg,
                    cell_idx=0,
                    cardinality=1,
                    calls_in_cell=60,
                    sustain_tps=10.0,
                    # SATURATED: sem=1 << TPS × per_call_secs
                    concurrency=1,
                    namespace="benchmark06_inmemory_card01_satur01",
                    client=object(),
                    deployment="dep",
                    system_prompt="x" * 1000,
                    user_prompts=["alpha"],
                    git_commit="testcommit",
                    dirty=True,
                    system_sha="x" * 64,
                    pricing_snapshot_path=str(FIXTURE_PRICING_PATH),
                    pricing=load_payg_pricing(FIXTURE_PRICING_PATH),
                    dry_run=False,
                    out_fh=tmpfh,
                    global_request_offset=0,
                    sim_started_mono=time.monotonic(),
                    run_id_short="satur01",
                )
                records.extend(recs)
            finally:
                m._call_with_retry = orig_call
                tmpfh.close()

        asyncio.run(run_one_cell())

        # The aggregator should report backlog_excessive=True.
        agg = m._aggregate_cell(records, warmup_exclusion_s=0.0, sustain_tps=10.0)
        # P95 backlog must exceed 1500ms OR max must exceed 5000ms.
        self.assertTrue(
            agg["backlog_excessive"],
            msg=(
                f"saturated cell should flag backlog_excessive=True; "
                f"got p95={agg['p95_dispatch_backlog_ms']:.0f}ms "
                f"max={agg['max_dispatch_backlog_ms']:.0f}ms"
            ),
        )
        # And the realized common-prefix RPM should be LOWER than the
        # scheduled rate (the system can't keep up).
        self.assertLess(
            agg["realized_admitted_common_prefix_rpm"],
            60.0 * 10.0,
            msg="saturated cell admitted RPM must lag scheduled RPM",
        )


# ----------------------------------------------------------------------------
# TestHeavyStubHappyPathSem96 — v2.4 sem=96 absorbs heavy TTFT
# ----------------------------------------------------------------------------


class TestHeavyStubHappyPathSem96(unittest.TestCase):
    """v2.4 happy-path deterministic regression. Reproduces the live
    gpt-5.2 PAYG regime that broke v2.3 sem=8 — but with sem=96.

    Spec (from .internal/tasks/018-cache-key-bucketing-benchmark.md
    v2.4 hotfix banner): sem=96, TTFT=128s, TPS=0.5, N=120;
    expected P95 backlog <1500ms, max backlog <5000ms,
    backlog_excessive=False, common-prefix RPM ∈ [28, 32],
    steady in-flight ∈ [50, 96], max_in_flight_observed <96.

    The test scales time 256× to fit in pytest --timeout=120:
       TTFT 128s → 0.5s, TPS 0.5 → 128.0, N 120 unchanged.
       Little's-Law steady-state in-flight = TPS × TTFT = 128 × 0.5 = 64.
       Wall clock: 120 / 128 ≈ 0.94s + last-call TTFT 0.5s ≈ 1.5s.
       Common-prefix scheduled RPM = TPS × 60 = 7680; under the scaled
       cell we keep the spec's [28, 32] target by mapping the scheduled
       window directly to a scaled "minute" of 60s/256 ≈ 0.234s.

    The test asserts the *shape* the spec demands (sem absorbs the
    in-flight steady state without saturating; backlog stays nominal;
    max_in_flight stays strictly below the sem ceiling so the
    semaphore is observably non-binding).
    """

    def test_v24_heavy_stub_absorbed_by_sem_96(self) -> None:
        from dataclasses import replace

        cfg = m.load_experiment(INMEMORY_YAML)
        # 256× time-scale so the test fits in --timeout=120. Preserves
        # Little's-Law steady-state in-flight = TPS × TTFT = 64.
        scaled_tps = 128.0
        scaled_ttft_s = 0.5
        cfg = replace(
            cfg,
            runtime=replace(cfg.runtime, sustain_tps=scaled_tps),
        )

        async def heavy_call(**kwargs):
            await asyncio.sleep(scaled_ttft_s)
            return {
                "usage": {
                    "input_tokens": 10000,
                    "input_tokens_details": {"cached_tokens": 8000},
                    "output_tokens": 512,
                },
                "first_token_latency_ms": scaled_ttft_s * 1000.0,
                "total_latency_ms": scaled_ttft_s * 1000.0,
                "rate_limited": False,
                "rate_limited_count": 0,
                "headers": {},
                "raised": None,
            }

        records: list[dict] = []
        max_in_flight = 0

        async def run_one_cell():
            nonlocal max_in_flight
            tmpfh = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False
            )
            self.addCleanup(lambda: pathlib.Path(tmpfh.name).unlink(missing_ok=True))
            orig_call = m._call_with_retry
            m._call_with_retry = heavy_call
            try:
                recs, _usd, peak = await m._run_cell(
                    cfg=cfg,
                    cell_idx=0,
                    cardinality=1,
                    calls_in_cell=120,
                    sustain_tps=scaled_tps,
                    concurrency=96,
                    namespace="benchmark06_inmemory_card01_v24happ1",
                    client=object(),
                    deployment="dep",
                    system_prompt="x" * 1000,
                    user_prompts=["alpha", "beta", "gamma"],
                    git_commit="testcommit",
                    dirty=True,
                    system_sha="x" * 64,
                    pricing_snapshot_path=str(FIXTURE_PRICING_PATH),
                    pricing=load_payg_pricing(FIXTURE_PRICING_PATH),
                    dry_run=False,
                    out_fh=tmpfh,
                    global_request_offset=0,
                    sim_started_mono=time.monotonic(),
                    run_id_short="v24happ1",
                )
                records.extend(recs)
                max_in_flight = peak
            finally:
                m._call_with_retry = orig_call
                tmpfh.close()

        asyncio.run(run_one_cell())

        # All 120 arrivals dispatched, none failed.
        self.assertEqual(len(records), 120)
        self.assertTrue(all(not r["failed"] for r in records))

        backlogs = [float(r["dispatch_backlog_ms"]) for r in records]
        p95 = m._percentile(backlogs, 95.0)
        max_b = max(backlogs)
        # v2.4 spec: P95 backlog <1500ms, max <5000ms.
        self.assertLess(
            p95, 1500.0,
            msg=(
                f"v2.4 sem=96 happy path: P95 backlog {p95:.0f}ms must be "
                f"<1500ms (spec); got max_in_flight={max_in_flight}"
            ),
        )
        self.assertLess(
            max_b, 5000.0,
            msg=f"v2.4 sem=96 happy path: max backlog {max_b:.0f}ms must be <5000ms",
        )

        agg = m._aggregate_cell(records, warmup_exclusion_s=0.0, sustain_tps=scaled_tps)
        # v2.4 spec: backlog_excessive=false.
        self.assertFalse(
            agg["backlog_excessive"],
            msg=(
                f"v2.4 sem=96 happy path must clear backlog_excessive; "
                f"got p95={agg['p95_dispatch_backlog_ms']:.0f}ms "
                f"max={agg['max_dispatch_backlog_ms']:.0f}ms"
            ),
        )

        # v2.4 spec: max_in_flight_observed <96 (semaphore is observably
        # non-binding — Little's-Law steady state is ~64).
        self.assertLess(
            max_in_flight, 96,
            msg=(
                f"v2.4 sem=96 happy path: max_in_flight_observed must be "
                f"<96 to demonstrate non-saturating sem; got {max_in_flight}"
            ),
        )
        # v2.4 spec: steady in-flight ∈ [50, 96] (strict — no relaxation).
        # Little's-Law derivation: at scaled TPS=128 × TTFT=0.5s the
        # steady-state in-flight count is TPS × TTFT = 64, which is the
        # same ratio as the live regime (TPS=0.5 × TTFT=128s = 64). The
        # spec band [50, 96] is the live-equivalent assertion.
        self.assertGreaterEqual(
            max_in_flight, 50,
            msg=(
                f"v2.4 sem=96 happy path: max_in_flight_observed must "
                f"reach the v2.4 spec steady-state band [50, 96] "
                f"(Little's-Law in-flight = TPS × TTFT = 64); got "
                f"{max_in_flight}"
            ),
        )

        # v2.4 spec: realized common-prefix RPM ∈ [28, 32] (strict —
        # the live-equivalent assertion of the spec band).
        #
        # The cell's stored ``realized_admitted_common_prefix_rpm``
        # field is not a faithful discriminator under the 256× time
        # scale: the cell wall time (~0.94s) is much shorter than the
        # RpmTracker's 60s rolling window, so the rolling window
        # covers the whole cell and the per-record count collapses to
        # ~N/2 regardless of admitted cadence. We therefore translate
        # the spec [28, 32] band into the equivalent assertion in
        # scaled time: live RPM = 60000 / (mean_iat_scaled × time_scale),
        # where time_scale = scaled_tps / LIVE_TPS = 128 / 0.5 = 256.
        # This is the exact arithmetic identity of the spec band; the
        # only translation is from RPM units to time-scaled
        # inter-arrival units.
        admitted_ms = sorted(
            float(r["admitted_dispatch_cell_elapsed_ms"]) for r in records
        )
        inter_arrivals = [
            admitted_ms[i] - admitted_ms[i - 1]
            for i in range(1, len(admitted_ms))
        ]
        mean_iat = sum(inter_arrivals) / len(inter_arrivals)
        expected_iat_ms = 1000.0 / scaled_tps  # ~7.8125 ms at TPS=128
        LIVE_TPS = 0.5  # the v2.4 pinned live sustain_tps
        time_scale = scaled_tps / LIVE_TPS  # 256
        mean_iat_live_ms = mean_iat * time_scale
        live_equivalent_common_prefix_rpm = 60000.0 / mean_iat_live_ms
        self.assertGreaterEqual(
            live_equivalent_common_prefix_rpm, 28.0,
            msg=(
                f"v2.4 sem=96 happy path: live-equivalent realized "
                f"common-prefix RPM must be ≥28 (spec band [28, 32]); "
                f"got {live_equivalent_common_prefix_rpm:.4f} (mean "
                f"scaled IAT {mean_iat:.4f}ms, live IAT "
                f"{mean_iat_live_ms:.2f}ms; scheduled scaled IAT "
                f"{expected_iat_ms:.4f}ms ↔ live 2000ms ↔ 30 RPM)"
            ),
        )
        self.assertLessEqual(
            live_equivalent_common_prefix_rpm, 32.0,
            msg=(
                f"v2.4 sem=96 happy path: live-equivalent realized "
                f"common-prefix RPM must be ≤32 (spec band [28, 32]); "
                f"got {live_equivalent_common_prefix_rpm:.4f} (mean "
                f"scaled IAT {mean_iat:.4f}ms, live IAT "
                f"{mean_iat_live_ms:.2f}ms; scheduled scaled IAT "
                f"{expected_iat_ms:.4f}ms ↔ live 2000ms ↔ 30 RPM)"
            ),
        )


# ----------------------------------------------------------------------------
# TestCounterfactualSem8HeavyStub — v2.3 sem=8 saturates the same workload
# ----------------------------------------------------------------------------


class TestCounterfactualSem8HeavyStub(unittest.TestCase):
    """v2.4 counterfactual deterministic regression. Reproduces the v2.3
    live Stage 1 smoke failure mode in pytest: against the same heavy
    stub as TestHeavyStubHappyPathSem96, sem=8 saturates and trips
    backlog_excessive — exactly as the v2.3 live Stage 1 smoke did
    (inmemory card=1 P95 backlog=2398 ms; 24h card=1 P95 backlog=
    111,238 ms; both max_in_flight_observed=8). This locks in the
    regression test for the bug that v2.4 fixes.
    """

    def test_v23_sem_8_saturates_against_heavy_stub(self) -> None:
        from dataclasses import replace

        cfg = m.load_experiment(INMEMORY_YAML)
        scaled_tps = 128.0
        scaled_ttft_s = 0.5
        cfg = replace(
            cfg,
            runtime=replace(cfg.runtime, sustain_tps=scaled_tps),
        )

        async def heavy_call(**kwargs):
            await asyncio.sleep(scaled_ttft_s)
            return {
                "usage": {
                    "input_tokens": 10000,
                    "input_tokens_details": {"cached_tokens": 8000},
                    "output_tokens": 512,
                },
                "first_token_latency_ms": scaled_ttft_s * 1000.0,
                "total_latency_ms": scaled_ttft_s * 1000.0,
                "rate_limited": False,
                "rate_limited_count": 0,
                "headers": {},
                "raised": None,
            }

        records: list[dict] = []
        max_in_flight = 0

        async def run_one_cell():
            nonlocal max_in_flight
            tmpfh = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False
            )
            self.addCleanup(lambda: pathlib.Path(tmpfh.name).unlink(missing_ok=True))
            orig_call = m._call_with_retry
            m._call_with_retry = heavy_call
            try:
                recs, _usd, peak = await m._run_cell(
                    cfg=cfg,
                    cell_idx=0,
                    cardinality=1,
                    calls_in_cell=120,
                    sustain_tps=scaled_tps,
                    # COUNTERFACTUAL: v2.3 sem=8 against the live-regime stub.
                    concurrency=8,
                    namespace="benchmark06_inmemory_card01_v23cf001",
                    client=object(),
                    deployment="dep",
                    system_prompt="x" * 1000,
                    user_prompts=["alpha", "beta", "gamma"],
                    git_commit="testcommit",
                    dirty=True,
                    system_sha="x" * 64,
                    pricing_snapshot_path=str(FIXTURE_PRICING_PATH),
                    pricing=load_payg_pricing(FIXTURE_PRICING_PATH),
                    dry_run=False,
                    out_fh=tmpfh,
                    global_request_offset=0,
                    sim_started_mono=time.monotonic(),
                    run_id_short="v23cf001",
                )
                records.extend(recs)
                max_in_flight = peak
            finally:
                m._call_with_retry = orig_call
                tmpfh.close()

        asyncio.run(run_one_cell())

        self.assertEqual(len(records), 120)
        self.assertTrue(all(not r["failed"] for r in records))

        # v2.3 saturation signature 1: max_in_flight_observed pinned at sem=8.
        self.assertEqual(
            max_in_flight, 8,
            msg=(
                f"v2.3 counterfactual: sem=8 must saturate at the "
                f"semaphore ceiling against the heavy stub; got "
                f"max_in_flight={max_in_flight} (expected exactly 8)"
            ),
        )

        agg = m._aggregate_cell(records, warmup_exclusion_s=0.0, sustain_tps=scaled_tps)

        # v2.3 saturation signature 2: backlog_excessive=True.
        self.assertTrue(
            agg["backlog_excessive"],
            msg=(
                f"v2.3 counterfactual: sem=8 must trip backlog_excessive "
                f"against the heavy stub; got p95="
                f"{agg['p95_dispatch_backlog_ms']:.0f}ms max="
                f"{agg['max_dispatch_backlog_ms']:.0f}ms"
            ),
        )

        # v2.3 saturation signature 3: P95 backlog >1500ms (the spec's
        # happy-path ceiling, which v2.3 saturated above).
        self.assertGreater(
            agg["p95_dispatch_backlog_ms"], 1500.0,
            msg=(
                f"v2.3 counterfactual: P95 backlog must exceed 1500ms "
                f"(the v2.4 happy-path ceiling); got "
                f"{agg['p95_dispatch_backlog_ms']:.0f}ms"
            ),
        )

        # v2.3 saturation signature 4: admitted inter-arrival cadence
        # is throttled well above scheduled (1/TPS = 7.8ms at TPS=128).
        # Under sem=8 each in-flight slot frees every TTFT/8 ≈ 62.5ms,
        # so steady-state inter-arrival rises to ~8× scheduled. In live
        # time (TPS=0.5, scheduled IAT 2000ms = 30 RPM) this maps to
        # observed ≥ 8× = 16000ms = ~3.75 RPM (v2.3 live saw 22.87 / 13.23
        # RPM per smoke = IATs ~2.6s / ~4.5s, i.e. 1.3–2.3× scheduled —
        # less extreme than this scaled stub because live TTFT had
        # higher variance and warmup ramp). We require >1.5× scheduled
        # IAT, the same threshold the happy path must clear from below.
        admitted_ms = sorted(
            float(r["admitted_dispatch_cell_elapsed_ms"]) for r in records
        )
        inter_arrivals = [
            admitted_ms[i] - admitted_ms[i - 1]
            for i in range(1, len(admitted_ms))
        ]
        mean_iat = sum(inter_arrivals) / len(inter_arrivals)
        expected_iat_ms = 1000.0 / scaled_tps
        self.assertGreater(
            mean_iat, expected_iat_ms * 1.5,
            msg=(
                f"v2.3 counterfactual: sem=8 must throttle admitted "
                f"inter-arrival cadence well above scheduled "
                f"{expected_iat_ms:.2f}ms; got mean IAT {mean_iat:.2f}ms "
                f"(saturated dispatcher should reach ~{expected_iat_ms*8:.0f}ms)"
            ),
        )


# ----------------------------------------------------------------------------
# TestConcurrencyDispatcherEcho — every record echoes v2.4 controls
# ----------------------------------------------------------------------------


class TestConcurrencyDispatcherEcho(unittest.TestCase):
    """Every record in the JSONL MUST carry the v2.4 controls echo so the
    analysis can re-verify they were held constant."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["AZURE_OPENAI_FOUNDRY_ENDPOINT"] = (
            "https://fake.invalid.test"
        )
        os.environ["AZURE_OPENAI_DEPLOYMENT_GPT_5_2"] = "test-dep"
        self._tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task018test_"))
        src = REPO_ROOT / "benchmarks" / "06-cache-key-bucketing"
        dst = self._tmpdir / "benchmarks" / "06-cache-key-bucketing"
        shutil.copytree(src, dst)
        (dst / "runs").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_every_record_echoes_v24_controls(self) -> None:
        cfg = m.load_experiment(INMEMORY_YAML)
        result = m.run_measurement(
            cfg=cfg,
            benchmarks_root=self._tmpdir / "benchmarks",
            dry_run=True,
            stage="smoke",
            allow_dirty=True,
            pricing_policy="historical-replay",
        )
        with open(result.jsonl_path) as fh:
            for line in fh:
                rec = json.loads(line)
                self.assertEqual(rec["request_concurrency"], 96)
                self.assertEqual(rec["request_sustain_tps"], 0.5)
                self.assertEqual(rec["dispatcher_kind"], "async_scheduled")
                self.assertEqual(rec["request_estimated_processed_tokens_max"], 11000)
                # request_estimated_processed_tokens is the actual estimate
                # for this record (sys+user chars / 4 + max_output).
                self.assertLessEqual(
                    rec["request_estimated_processed_tokens"], 11000
                )
                # v2.4 telemetry: per-record admitted/scheduled/backlog/in-flight.
                self.assertIn("scheduled_dispatch_cell_elapsed_ms", rec)
                self.assertIn("admitted_dispatch_cell_elapsed_ms", rec)
                self.assertIn("dispatch_backlog_ms", rec)
                self.assertIn("in_flight_at_dispatch", rec)


# ----------------------------------------------------------------------------
# TestStartupAbort — bad YAML mutations abort before any HTTP client
# ----------------------------------------------------------------------------


class TestStartupAbort(unittest.TestCase):
    """A mutated YAML carrying any rejected control (concurrency=1,
    sustain_tps=1.0, est=30000, missing dispatcher, missing
    deployment_tpm_quota) MUST abort at YAML load — before
    ``_build_live_client`` is ever invoked."""

    def test_each_mutation_aborts_before_client_build(self) -> None:
        mutations: list[tuple[str, callable]] = [
            (
                "v21_concurrency_1",
                lambda b: b["runtime"].__setitem__("concurrency", 1),
            ),
            (
                "v21_sustain_tps_1",
                lambda b: b["runtime"].__setitem__("sustain_tps", 1.0),
            ),
            (
                "v22_est_30000",
                lambda b: b["request_template"].__setitem__(
                    "estimated_processed_tokens_max", 30000
                ),
            ),
            (
                "missing_dispatcher",
                lambda b: b["runtime"].pop("dispatcher", None),
            ),
            (
                "missing_deployment_tpm_quota",
                lambda b: b["metadata"].pop("deployment_tpm_quota", None),
            ),
        ]
        for label, mutate in mutations:
            with self.subTest(mutation=label):
                body = _load_inmemory_yaml_body()
                body["runtime"] = dict(body["runtime"])
                body["request_template"] = dict(body["request_template"])
                body["metadata"] = dict(body["metadata"])
                mutate(body)
                p = _write_yaml(body, self.addCleanup)
                with self.assertRaises(ValueError, msg=label):
                    m.load_experiment(p)


# ----------------------------------------------------------------------------
# TestDryRunEndToEnd — Stage 0 produces JSONL + summary with no network
# ----------------------------------------------------------------------------


class TestDryRunEndToEnd(unittest.TestCase):
    """Stage 0 dry-run end-to-end: 2 cells × 480 calls (v2.4 default
    sweep [1, 8] × evidence calls_per_cell), JSONL + summary written,
    citations + metadata + pinned-confounds echo + v2.4 tpm_feasibility
    block present, ZERO network calls."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["AZURE_OPENAI_FOUNDRY_ENDPOINT"] = (
            "https://fake-test-host.invalid.test"
        )
        os.environ["AZURE_OPENAI_DEPLOYMENT_GPT_5_2"] = "test-dep-unthrottled"
        self._tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="task018test_"))
        src = REPO_ROOT / "benchmarks" / "06-cache-key-bucketing"
        dst = self._tmpdir / "benchmarks" / "06-cache-key-bucketing"
        shutil.copytree(src, dst)
        (dst / "runs").mkdir(exist_ok=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_dry_run_produces_jsonl_and_summary(self) -> None:
        cfg = m.load_experiment(INMEMORY_YAML)
        result = m.run_measurement(
            cfg=cfg,
            benchmarks_root=self._tmpdir / "benchmarks",
            dry_run=True,
            stage="evidence",
            allow_dirty=True,
            pricing_policy="historical-replay",
        )
        # v2.4 default sweep is [1, 8] → 2 cells planned.
        self.assertEqual(result.cells_completed, 2)
        self.assertEqual(result.cells_planned, 2)
        self.assertAlmostEqual(result.total_usd, 0.0)
        self.assertFalse(result.partial)

        jsonl_path = pathlib.Path(result.jsonl_path)
        self.assertTrue(jsonl_path.is_file(), msg=f"missing {jsonl_path}")
        summary_path = pathlib.Path(str(jsonl_path) + ".summary.json")
        self.assertTrue(summary_path.is_file(), msg=f"missing {summary_path}")

        # JSONL: every record's prompt_cache_key matches the audit regex.
        with jsonl_path.open("r", encoding="utf-8") as fh:
            n = 0
            for line in fh:
                rec = json.loads(line)
                n += 1
                self.assertTrue(
                    m.BUCKET_KEY_RE.match(rec["prompt_cache_key_used"]),
                    msg=f"record {n} bucket key {rec['prompt_cache_key_used']!r} "
                        f"failed audit regex",
                )
                self.assertTrue(rec["dry_run"])
                self.assertEqual(rec["api_version"], "preview")
                self.assertEqual(rec["request_max_output_tokens"], 512)
                self.assertEqual(rec["request_reasoning_effort"], "low")
                self.assertEqual(rec["request_concurrency"], 96)
                self.assertEqual(rec["request_sustain_tps"], 0.5)
                self.assertEqual(rec["dispatcher_kind"], "async_scheduled")
                self.assertEqual(rec["prompt_cache_retention"], "in_memory")
            # 2 cells × 480 calls.
            self.assertEqual(n, 2 * 480, "expected 2 cells × 480 calls each")

        with summary_path.open("r", encoding="utf-8") as fh:
            summary = json.load(fh)
        # Citations.
        self.assertIn("citations", summary)
        self.assertEqual(
            summary["citations"]["azure_doc"]["url"],
            m.AZURE_DOC_PROMPT_CACHING_URL,
        )
        self.assertEqual(
            summary["citations"]["azure_doc"]["accessed_date"],
            m.AZURE_DOC_ACCESSED_DATE,
        )
        # v2.4 retains the v2.3 rate-limit doc citation.
        self.assertIn("azure_rate_limit_doc", summary["citations"])
        self.assertEqual(
            summary["citations"]["azure_rate_limit_doc"]["url"],
            m.AZURE_RATE_LIMIT_DOC_URL,
        )
        self.assertIn("pricing", summary["citations"])
        self.assertEqual(
            summary["citations"]["pricing"]["path"],
            "pricing/azure-openai-payg-sample-2026-05.yaml",
        )
        self.assertEqual(
            summary["pricing_policy"]["mode"], "historical-replay"
        )
        self.assertEqual(summary["pricing_policy"]["policy_version"], "1.0.0")
        self.assertEqual(
            summary["pricing_policy"]["snapshot"]["snapshot_sha256"],
            "858c3c39ca36a7495d2754d8b5e32e77e6478d38e2e0da8d7d9cd154ab1f08cd",
        )
        # Metadata pass-through.
        self.assertEqual(summary["metadata"]["consumption_model_context"], "paygo_standard")
        self.assertFalse(summary["metadata"]["simulation"])
        self.assertFalse(summary["metadata"]["ptu_evidence"])
        self.assertEqual(summary["metadata"]["deployment_tpm_quota"], 500000)

        # v2.4 pinned-confounds echo.
        echo = summary["pinned_confounds_echo"]
        self.assertEqual(echo["max_output_tokens"], 512)
        self.assertEqual(echo["reasoning_effort"], "low")
        self.assertEqual(echo["api_version"], "preview")
        self.assertEqual(echo["concurrency"], 96)
        self.assertEqual(echo["sustain_tps"], 0.5)
        self.assertEqual(echo["dispatcher"], "async_scheduled")
        self.assertEqual(echo["estimated_processed_tokens_max"], 11000)
        self.assertEqual(echo["deployment_tpm_quota"], 500000)

        # v2.4 tpm_feasibility block + backlog_excessive_any +
        # max_in_flight_observed_run (NEW in v2.4 — run-level rollup of the
        # per-cell max_in_flight_observed so semaphore-saturation
        # regressions of the v2.3 sem=8 kind are visible without
        # inspecting per-cell rows).
        self.assertIn("tpm_feasibility", summary)
        self.assertTrue(summary["tpm_feasibility"]["passed"])
        self.assertIn("backlog_excessive_any", summary)
        self.assertFalse(summary["backlog_excessive_any"])
        self.assertIn("max_in_flight_observed_run", summary)
        # Dry-run still pushes admissions through the dispatcher's
        # semaphore (the bypass is the network call, not the pacer);
        # accept any value within the v2.4 sem ceiling (0..96). The
        # important regression check is that the field exists and is
        # never above the pinned concurrency.
        self.assertIsInstance(summary["max_in_flight_observed_run"], int)
        self.assertGreaterEqual(summary["max_in_flight_observed_run"], 0)
        self.assertLessEqual(summary["max_in_flight_observed_run"], 96)

        # Per-cell summary: 2 cells, each with a distinct namespace.
        self.assertEqual(len(summary["cell_summaries"]), 2)
        namespaces = [c["namespace"] for c in summary["cell_summaries"]]
        self.assertEqual(len(set(namespaces)), 2)
        for c in summary["cell_summaries"]:
            self.assertIn("backlog_excessive", c)
            self.assertIn("p95_dispatch_backlog_ms", c)
            self.assertIn("max_dispatch_backlog_ms", c)
            self.assertIn("realized_admitted_per_bucket_rpm", c)
            self.assertIn("realized_admitted_common_prefix_rpm", c)
            self.assertIn("max_in_flight_observed", c)

    def test_dry_run_24h_uses_24h_retention_tag(self) -> None:
        cfg = m.load_experiment(TWENTYFOURH_YAML)
        result = m.run_measurement(
            cfg=cfg,
            benchmarks_root=self._tmpdir / "benchmarks",
            dry_run=True,
            stage="evidence",
            allow_dirty=True,
            pricing_policy="historical-replay",
        )
        summary_path = pathlib.Path(str(result.jsonl_path) + ".summary.json")
        with summary_path.open("r", encoding="utf-8") as fh:
            summary = json.load(fh)
        for c in summary["cell_summaries"]:
            self.assertIn("benchmark06_24h_card", c["namespace"])
        self.assertEqual(summary["pinned_confounds_echo"]["prompt_cache_retention"], "24h")

    def test_dry_run_smoke_hoists_card1_first_class_fields(self) -> None:
        """Smoke stage summary hoists card=1 cell stats as first-class
        fields so the Stage 1 gate is a one-liner."""
        cfg = m.load_experiment(INMEMORY_YAML)
        result = m.run_measurement(
            cfg=cfg,
            benchmarks_root=self._tmpdir / "benchmarks",
            dry_run=True,
            stage="smoke",
            allow_dirty=True,
            pricing_policy="historical-replay",
        )
        with open(str(result.jsonl_path) + ".summary.json") as fh:
            summary = json.load(fh)
        self.assertIn("realized_admitted_per_bucket_rpm_card1", summary)
        self.assertIn("max_in_flight_observed_card1", summary)
        self.assertIn("p95_dispatch_backlog_ms_card1", summary)
        self.assertIn("backlog_excessive_card1", summary)


# ----------------------------------------------------------------------------
# TestPreAdmissionVsPostAdmissionFailedAggregates (v2.4 hotfix-2)
# ----------------------------------------------------------------------------


class TestPreAdmissionVsPostAdmissionFailedAggregates(unittest.TestCase):
    """v2.4 admitted-timestamp authoritative rule for `_aggregate_cell`.

    The v2.4 spec (`docs/.../scripts/measure_cache_key_bucketing.py`
    module docstring + Task-018 hotfix-2 review) requires:

    * **Cache-hit and model-latency aggregates** EXCLUDE every
      ``failed=True`` record (their usage / first-token-latency payloads
      are absent or untrusted).
    * **Admitted dispatch backlog, admitted RPM, and in-flight aggregates
      INCLUDE post-admission failures** — ``transport_exception:*`` and
      ``rate_limited_after_retries`` records passed ``sem.acquire`` and
      actually invoked Azure, so they consumed admission capacity and
      MUST be counted in the admission-level aggregates.
    * **Pre-admission failures** (currently only ``token_cap_exceeded``)
      remain EXCLUDED from admitted dispatch backlog, admitted RPM, and
      in-flight aggregates — they never crossed the HTTP admission
      boundary.

    These deterministic tests lock that behavior. They construct
    synthetic records directly (no async driver) so the partition rule
    is asserted in isolation.
    """

    @staticmethod
    def _make_record(
        *,
        arrival_idx: int,
        cardinality: int = 1,
        bucket_index: int = 0,
        admitted_ms: int,
        scheduled_ms: int | None = None,
        backlog_ms: int | None = None,
        in_flight: int,
        input_tokens: int = 0,
        cached_tokens: int = 0,
        first_token_latency_ms: float = 0.0,
        rate_limited: bool = False,
        failed: bool = False,
        failure_reason: str | None = None,
        relative_time_s: float | None = None,
        per_bucket_running_rpm: int = 0,
        common_prefix_running_rpm: int = 0,
    ) -> dict:
        if scheduled_ms is None:
            scheduled_ms = admitted_ms
        if backlog_ms is None:
            backlog_ms = admitted_ms - scheduled_ms
        if relative_time_s is None:
            relative_time_s = admitted_ms / 1000.0
        return {
            "arrival_idx_within_cell": arrival_idx,
            "bucket_cardinality": cardinality,
            "bucket_index": bucket_index,
            "scheduled_dispatch_cell_elapsed_ms": scheduled_ms,
            "admitted_dispatch_cell_elapsed_ms": admitted_ms,
            "dispatch_backlog_ms": backlog_ms,
            "in_flight_at_dispatch": in_flight,
            "canonical_input_tokens": input_tokens,
            "canonical_cached_tokens": cached_tokens,
            "first_token_latency_ms": first_token_latency_ms,
            "rate_limited": rate_limited,
            "failed": failed,
            "failure_reason": failure_reason,
            "relative_time_s": relative_time_s,
            "per_bucket_running_rpm": per_bucket_running_rpm,
            "common_prefix_running_rpm": common_prefix_running_rpm,
        }

    def test_post_admission_failure_included_in_admitted_aggregates(
        self,
    ) -> None:
        """A ``transport_exception:*`` record MUST contribute to
        ``p95_dispatch_backlog_ms``, ``max_dispatch_backlog_ms``,
        ``max_in_flight_observed``, ``n_admitted_records``, and the
        realized admitted RPM aggregates; it MUST NOT contribute to
        ``cache_hit_ratio_steady_state`` or
        ``first_token_latency_ms_p95_steady_state``."""
        # 3 successful records with small backlog + 1 post-admission
        # failure with a deliberately large backlog and large in-flight,
        # which will dominate the admission-aggregate tail if (and only
        # if) the v2.4 rule is honored.
        records = [
            self._make_record(
                arrival_idx=0,
                admitted_ms=100, backlog_ms=10, in_flight=1,
                input_tokens=1000, cached_tokens=800,
                first_token_latency_ms=120.0,
                per_bucket_running_rpm=1, common_prefix_running_rpm=1,
            ),
            self._make_record(
                arrival_idx=1,
                admitted_ms=200, backlog_ms=20, in_flight=2,
                input_tokens=1000, cached_tokens=800,
                first_token_latency_ms=130.0,
                per_bucket_running_rpm=2, common_prefix_running_rpm=2,
            ),
            # Post-admission failure: dispatcher passed sem.acquire and
            # actually invoked Azure but the HTTP layer raised an
            # exception. It MUST count toward admitted aggregates.
            self._make_record(
                arrival_idx=2,
                admitted_ms=300, backlog_ms=2500, in_flight=7,
                input_tokens=0, cached_tokens=0,
                first_token_latency_ms=0.0,
                failed=True,
                failure_reason="transport_exception:APIConnectionError",
                per_bucket_running_rpm=3, common_prefix_running_rpm=3,
            ),
            self._make_record(
                arrival_idx=3,
                admitted_ms=400, backlog_ms=30, in_flight=2,
                input_tokens=1000, cached_tokens=800,
                first_token_latency_ms=140.0,
                per_bucket_running_rpm=4, common_prefix_running_rpm=4,
            ),
        ]

        agg = m._aggregate_cell(
            records, warmup_exclusion_s=0.0, sustain_tps=10.0,
        )

        # Failed counts.
        self.assertEqual(agg["n_records"], 4)
        self.assertEqual(agg["n_failed_records"], 1)
        self.assertEqual(agg["n_pre_admission_failed_records"], 0)
        self.assertEqual(agg["n_post_admission_failed_records"], 1)
        self.assertEqual(agg["n_admitted_records"], 4)

        # Admitted-aggregate inclusion: max backlog and max in-flight
        # both come from the post-admission failure (2500 ms / 7).
        self.assertEqual(agg["max_dispatch_backlog_ms"], 2500.0)
        self.assertEqual(agg["max_in_flight_observed"], 7)
        # P95 over [10, 20, 2500, 30] places the 2500 tail well above
        # 30 (the largest success-only value) — confirms the failed
        # record DID contribute.
        self.assertGreater(
            agg["p95_dispatch_backlog_ms"], 30.0,
            msg=(
                "post-admission failure must contribute to "
                "p95_dispatch_backlog_ms; otherwise the v2.4 admitted-"
                "timestamp authoritative rule is violated"
            ),
        )

        # Admitted RPM aggregates include the failed record's
        # per_bucket_running_rpm / common_prefix_running_rpm values.
        # Means over [1, 2, 3, 4] = 2.5 for both.
        self.assertAlmostEqual(
            agg["realized_admitted_per_bucket_rpm"], 2.5, places=6,
        )
        self.assertAlmostEqual(
            agg["realized_admitted_common_prefix_rpm"], 2.5, places=6,
        )

        # Cache-hit ratio is computed over SUCCESS records only:
        # 3 × (cached 800 / input 1000) = 2400 / 3000 = 0.8. The failed
        # record's zero tokens MUST NOT pull the ratio down.
        self.assertAlmostEqual(
            agg["cache_hit_ratio_steady_state"], 0.8, places=6,
        )

        # Model latency aggregates exclude the failed record: P95 over
        # [120, 130, 140] (the 0.0 from the failed record must not
        # appear; if it did, P95 would drop noticeably).
        self.assertGreaterEqual(
            agg["first_token_latency_ms_p95_steady_state"], 120.0,
        )

    def test_pre_admission_failure_excluded_from_admitted_aggregates(
        self,
    ) -> None:
        """A ``token_cap_exceeded`` record MUST NOT contribute to
        ``p95_dispatch_backlog_ms``, ``max_dispatch_backlog_ms``,
        ``max_in_flight_observed``, or the realized admitted RPM
        aggregates. Its presence is visible ONLY in ``n_failed_records``
        and ``n_pre_admission_failed_records``."""
        records = [
            self._make_record(
                arrival_idx=0,
                admitted_ms=100, backlog_ms=10, in_flight=1,
                input_tokens=1000, cached_tokens=800,
                first_token_latency_ms=120.0,
                per_bucket_running_rpm=1, common_prefix_running_rpm=1,
            ),
            # Pre-admission failure: dispatcher rejected at the per-
            # request token cap before any HTTP call. MUST NOT appear in
            # admitted aggregates even though the synthetic record has
            # plausible-looking backlog_ms / in_flight_at_dispatch
            # values (the live runner does populate those fields for
            # token-cap rejections; the v2.4 rule applies at aggregation
            # time, not at record-assembly time).
            self._make_record(
                arrival_idx=1,
                admitted_ms=200, backlog_ms=9999, in_flight=42,
                input_tokens=0, cached_tokens=0,
                first_token_latency_ms=0.0,
                failed=True,
                failure_reason="token_cap_exceeded",
                per_bucket_running_rpm=99, common_prefix_running_rpm=99,
            ),
            self._make_record(
                arrival_idx=2,
                admitted_ms=300, backlog_ms=20, in_flight=2,
                input_tokens=1000, cached_tokens=800,
                first_token_latency_ms=130.0,
                per_bucket_running_rpm=2, common_prefix_running_rpm=2,
            ),
        ]

        agg = m._aggregate_cell(
            records, warmup_exclusion_s=0.0, sustain_tps=10.0,
        )

        # Failed counts.
        self.assertEqual(agg["n_records"], 3)
        self.assertEqual(agg["n_failed_records"], 1)
        self.assertEqual(agg["n_pre_admission_failed_records"], 1)
        self.assertEqual(agg["n_post_admission_failed_records"], 0)
        self.assertEqual(agg["n_admitted_records"], 2)

        # Admitted aggregates ONLY over the two successful records: max
        # backlog 20 ms (not 9999), max in-flight 2 (not 42).
        self.assertEqual(agg["max_dispatch_backlog_ms"], 20.0)
        self.assertEqual(agg["max_in_flight_observed"], 2)
        # P95 over [10, 20] — strictly < 9999.
        self.assertLess(
            agg["p95_dispatch_backlog_ms"], 100.0,
            msg=(
                "pre-admission failure must NOT contribute to "
                "p95_dispatch_backlog_ms"
            ),
        )
        # Backlog gate must NOT trip.
        self.assertFalse(agg["backlog_excessive"])

        # Admitted RPM mean is over [1, 2] = 1.5 (the failed record's
        # 99-RPM placeholder MUST NOT contaminate the aggregate).
        self.assertAlmostEqual(
            agg["realized_admitted_per_bucket_rpm"], 1.5, places=6,
        )
        self.assertAlmostEqual(
            agg["realized_admitted_common_prefix_rpm"], 1.5, places=6,
        )

        # Cache hit ratio over success records only.
        self.assertAlmostEqual(
            agg["cache_hit_ratio_steady_state"], 0.8, places=6,
        )

    def test_rate_limited_after_retries_treated_as_post_admission(
        self,
    ) -> None:
        """``rate_limited_after_retries`` is generated by
        ``_call_with_retry`` after the dispatcher has already invoked
        Azure (multiple times). The dispatcher consumed admission
        capacity, so the record MUST contribute to the admitted dispatch
        backlog and in-flight aggregates per the v2.4 rule, even though
        its usage payload is absent."""
        records = [
            self._make_record(
                arrival_idx=0,
                admitted_ms=100, backlog_ms=5, in_flight=1,
                input_tokens=1000, cached_tokens=800,
                first_token_latency_ms=120.0,
                per_bucket_running_rpm=1, common_prefix_running_rpm=1,
            ),
            self._make_record(
                arrival_idx=1,
                admitted_ms=200, backlog_ms=3300, in_flight=5,
                input_tokens=0, cached_tokens=0,
                first_token_latency_ms=0.0,
                rate_limited=True,  # set by _call_with_retry
                failed=True,
                failure_reason="rate_limited_after_retries",
                per_bucket_running_rpm=2, common_prefix_running_rpm=2,
            ),
        ]
        agg = m._aggregate_cell(
            records, warmup_exclusion_s=0.0, sustain_tps=10.0,
        )
        self.assertEqual(agg["n_pre_admission_failed_records"], 0)
        self.assertEqual(agg["n_post_admission_failed_records"], 1)
        self.assertEqual(agg["n_admitted_records"], 2)
        # Admitted aggregates include the rate-limited record.
        self.assertEqual(agg["max_dispatch_backlog_ms"], 3300.0)
        self.assertEqual(agg["max_in_flight_observed"], 5)
        self.assertEqual(agg["rate_limited_count"], 1)
        # Cache hit ratio excludes the failed record (only 1 success
        # record contributing 800/1000 = 0.8).
        self.assertAlmostEqual(
            agg["cache_hit_ratio_steady_state"], 0.8, places=6,
        )

    def test_is_pre_admission_failure_helper_classification(self) -> None:
        """Direct test of the classifier helper that drives both
        ``_aggregate_cell`` and the ``_run_cell`` RPM rebuild loop."""
        # Successful record: not a pre-admission failure.
        self.assertFalse(m._is_pre_admission_failure({"failed": False}))
        # token_cap_exceeded: pre-admission.
        self.assertTrue(
            m._is_pre_admission_failure(
                {"failed": True, "failure_reason": "token_cap_exceeded"}
            )
        )
        # transport_exception:*: post-admission.
        self.assertFalse(
            m._is_pre_admission_failure(
                {
                    "failed": True,
                    "failure_reason": "transport_exception:APIConnectionError",
                }
            )
        )
        # rate_limited_after_retries: post-admission.
        self.assertFalse(
            m._is_pre_admission_failure(
                {
                    "failed": True,
                    "failure_reason": "rate_limited_after_retries",
                }
            )
        )
        # Failed with None / missing failure_reason: NOT pre-admission
        # (conservative — only the explicit pre-admission whitelist
        # short-circuits the admission aggregates).
        self.assertFalse(
            m._is_pre_admission_failure(
                {"failed": True, "failure_reason": None}
            )
        )
        self.assertFalse(m._is_pre_admission_failure({"failed": True}))


# ----------------------------------------------------------------------------
# TestRunCellPostAdmissionFailureRpmBookkeeping (v2.4 hotfix-2)
# ----------------------------------------------------------------------------


class TestRunCellPostAdmissionFailureRpmBookkeeping(unittest.TestCase):
    """Integration test for the v2.4 RPM rebuild-loop fix in
    ``_run_cell``: post-admission failures (here, ``transport_exception:*``
    records produced by a stubbed ``_call_with_retry`` that returns
    ``raised=<exc>`` for select arrivals) MUST receive non-zero
    ``per_bucket_running_rpm`` / ``common_prefix_running_rpm`` values
    from the post-cell admitted-order RpmTracker rebuild, and the cell
    summary's admitted-RPM aggregates MUST include them."""

    def test_post_admission_failures_count_in_admitted_rpm(self) -> None:
        from dataclasses import replace
        cfg = m.load_experiment(INMEMORY_YAML)
        cfg = replace(
            cfg,
            runtime=replace(
                cfg.runtime, sustain_tps=10.0, cell_duration_seconds=2,
            ),
        )

        # Stub that fails the 3rd, 7th, and 11th arrivals with a non-429
        # exception (so they are recorded as
        # ``failure_reason="transport_exception:RuntimeError"`` —
        # post-admission per the v2.4 rule). All other arrivals succeed.
        fail_arrivals = {2, 6, 10}
        call_seq = {"n": 0}

        async def patchy_call(**kwargs):
            i = call_seq["n"]
            call_seq["n"] += 1
            await asyncio.sleep(0.05)
            if i in fail_arrivals:
                exc = RuntimeError("stubbed transport failure")
                return {
                    "usage": None,
                    "first_token_latency_ms": 50.0,
                    "total_latency_ms": 50.0,
                    "rate_limited": False,
                    "rate_limited_count": 0,
                    "headers": m._parse_response_headers(None),
                    "raised": exc,
                }
            return {
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {"cached_tokens": 800},
                    "output_tokens": 100,
                },
                "first_token_latency_ms": 50.0,
                "total_latency_ms": 50.0,
                "rate_limited": False,
                "rate_limited_count": 0,
                "headers": {},
                "raised": None,
            }

        records: list[dict] = []
        n_arrivals = 20

        async def run_one_cell():
            tmpfh = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False,
            )
            self.addCleanup(
                lambda: pathlib.Path(tmpfh.name).unlink(missing_ok=True)
            )
            orig_call = m._call_with_retry
            m._call_with_retry = patchy_call
            try:
                recs, _usd, _peak = await m._run_cell(
                    cfg=cfg,
                    cell_idx=0,
                    cardinality=1,
                    calls_in_cell=n_arrivals,
                    sustain_tps=10.0,
                    concurrency=96,
                    namespace="benchmark06_inmemory_card01_failints",
                    client=object(),
                    deployment="dep",
                    system_prompt="x" * 1000,
                    user_prompts=["alpha"],
                    git_commit="testcommit",
                    dirty=True,
                    system_sha="x" * 64,
                    pricing_snapshot_path=str(FIXTURE_PRICING_PATH),
                    pricing=load_payg_pricing(FIXTURE_PRICING_PATH),
                    dry_run=False,
                    out_fh=tmpfh,
                    global_request_offset=0,
                    sim_started_mono=time.monotonic(),
                    run_id_short="failints",
                )
                records.extend(recs)
            finally:
                m._call_with_retry = orig_call
                tmpfh.close()

        asyncio.run(run_one_cell())

        self.assertEqual(len(records), n_arrivals)
        failed = [r for r in records if r["failed"]]
        succeeded = [r for r in records if not r["failed"]]
        self.assertEqual(len(failed), len(fail_arrivals))
        self.assertEqual(len(succeeded), n_arrivals - len(fail_arrivals))

        # Every failed record is a POST-admission failure (transport
        # exception), not a pre-admission token_cap_exceeded.
        for rec in failed:
            self.assertTrue(rec["failure_reason"].startswith(
                "transport_exception:"
            ))
            self.assertFalse(m._is_pre_admission_failure(rec))

        # v2.4 fix: failed (post-admission) records receive non-zero
        # per_bucket_running_rpm / common_prefix_running_rpm from the
        # post-cell rebuild loop (cardinality=1 means per-bucket equals
        # common-prefix).
        for rec in failed:
            self.assertGreater(
                rec["per_bucket_running_rpm"], 0,
                msg=(
                    "post-admission failed record must receive a "
                    "non-zero per_bucket_running_rpm from the v2.4 "
                    "RpmTracker rebuild"
                ),
            )
            self.assertGreater(
                rec["common_prefix_running_rpm"], 0,
                msg=(
                    "post-admission failed record must receive a "
                    "non-zero common_prefix_running_rpm from the v2.4 "
                    "RpmTracker rebuild"
                ),
            )

        # Cell summary admitted-RPM aggregate spans all 20 admitted
        # records (17 successes + 3 post-admission failures), not just
        # the 17 successes.
        agg = m._aggregate_cell(
            records, warmup_exclusion_s=0.0, sustain_tps=10.0,
        )
        self.assertEqual(agg["n_records"], n_arrivals)
        self.assertEqual(agg["n_failed_records"], len(fail_arrivals))
        self.assertEqual(agg["n_pre_admission_failed_records"], 0)
        self.assertEqual(
            agg["n_post_admission_failed_records"], len(fail_arrivals),
        )
        self.assertEqual(agg["n_admitted_records"], n_arrivals)

        # Cross-check: the cell summary's realized common-prefix RPM is
        # the MEAN of the per-record common_prefix_running_rpm values
        # over the rpm_target slice (success + post-admission failures
        # filtered by warmup). With warmup=0, that is every admitted
        # record. Compute the same mean explicitly from each record's
        # rebuilt per-record value and verify they agree.
        expected_mean = sum(
            float(r["common_prefix_running_rpm"]) for r in records
        ) / len(records)
        self.assertAlmostEqual(
            agg["realized_admitted_common_prefix_rpm"],
            expected_mean,
            places=6,
        )

        # And the all-admitted mean MUST DIFFER from the success-only
        # mean (the regression we lock against). If the v2.4 fix is
        # missing, post-admission failures would carry
        # per_bucket_running_rpm=0 (assembly-time placeholder) and be
        # skipped by `_aggregate_cell`, making the reported aggregate
        # equal to the success-only mean.
        success_only_mean = sum(
            float(r["common_prefix_running_rpm"]) for r in succeeded
        ) / len(succeeded)
        self.assertGreater(
            agg["realized_admitted_common_prefix_rpm"],
            0.0,
            msg="admitted RPM aggregate must be non-zero",
        )
        self.assertNotAlmostEqual(
            agg["realized_admitted_common_prefix_rpm"],
            success_only_mean,
            places=4,
            msg=(
                "v2.4 admitted-aggregate must include post-admission "
                "failures; equality with success-only mean is the "
                "exact regression signature this test locks against"
            ),
        )


if __name__ == "__main__":
    unittest.main()

"""Acceptance tests for the ``reasoning-payoff experiment run`` dispatcher.

Everything here is offline. A socket guard proves the dry-run path opens no
connection; live is exercised only through preflight rejections and a fake
``experiments.run`` so no billed call is ever made. No credentials are read.
"""

from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator, FormatChecker

from batch_runner.experiment.adapters import (
    ADAPTERS,
    AdapterError,
    ExperimentAdapter,
    LiveUnsupportedError,
    UnknownAdapterError,
    _build_registry,
    get_adapter,
)
from batch_runner.experiment.catalog import load_packaged_catalog
from batch_runner.experiment import dispatch as dispatch_mod
from batch_runner.experiment.dispatch import (
    ConfigValidationError,
    DryRunOutcome,
    ExperimentResolutionError,
    LiveNotConfirmedError,
    LiveNotSupportedError,
    SourceCheckoutMissingError,
    dispatch_dry_run,
    dispatch_live,
    find_source_root,
    preflight_live,
    resolve_entry,
)
from batch_runner.experiment.plan import (
    PlanConflictError,
    build_plan,
    plan_to_json,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_a: Any, **_k: Any) -> None:
        raise AssertionError("network access attempted in a dry-run test")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def _schema_validator() -> Draft7Validator:
    schema = json.loads(
        (_repo_root() / "schemas" / "experiment_execution_plan.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema, format_checker=FormatChecker())


def _all_ids() -> list[str]:
    return [e["experiment_id"] for e in load_packaged_catalog()["experiments"]]


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------
def test_registry_covers_every_runner_module_one_to_one():
    catalog = load_packaged_catalog()
    used = {e["runner_module"] for e in catalog["experiments"]}
    assert used == set(ADAPTERS), (used, set(ADAPTERS))
    # Every catalogued adapter block agrees with the registry.
    for e in catalog["experiments"]:
        adapter = get_adapter(e["runner_module"])
        assert e["adapter"]["id"] == adapter.adapter_id
        assert e["adapter"]["source_module"] == adapter.source_module
        assert e["adapter"]["supports_live"] == adapter.supports_live


def test_get_adapter_rejects_unknown():
    with pytest.raises(UnknownAdapterError):
        get_adapter("does_not_exist")


def test_registry_rejects_duplicate_ids(monkeypatch):
    dup = ADAPTERS["run_benchmark"]
    monkeypatch.setattr(
        "batch_runner.experiment.adapters._ADAPTER_LIST", (dup, dup)
    )
    with pytest.raises(AdapterError):
        _build_registry()


def test_dry_run_argv_forwards_replay_only_for_pricing_aware():
    rb = get_adapter("run_benchmark")
    assert "--pricing-policy" not in rb.dry_run_argv("experiments/x.yaml")
    dual = get_adapter("measure_dual_spillover")
    argv = dual.dry_run_argv("experiments/x.yaml")
    assert argv[argv.index("--pricing-policy") + 1] == "historical-replay"


def test_live_argv_refuses_offline_simulation_adapter():
    offline = ExperimentAdapter(
        adapter_id="fake_sim",
        version="1.0.0",
        source_module="scripts.fake_sim",
        supports_dry_run=True,
        supports_live=False,
        live_kind="offline-simulation",
        pricing_policy_aware=False,
    )
    with pytest.raises(LiveUnsupportedError):
        offline.live_argv("experiments/x.yaml")


def test_live_argv_never_contains_historical_replay():
    for adapter in ADAPTERS.values():
        argv = adapter.live_argv("experiments/x.yaml")
        assert "historical-replay" not in argv


# ---------------------------------------------------------------------------
# Resolution: deterministic, reject unknown/ambiguous
# ---------------------------------------------------------------------------
def test_resolve_exact_id():
    entry = resolve_entry("exp001_short-factual_baseline")
    assert entry["experiment_id"] == "exp001_short-factual_baseline"


def test_resolve_unique_prefix():
    entry = resolve_entry("exp007")
    assert entry["experiment_id"] == "exp007_max_output_tokens_sweep"


def test_resolve_unknown_is_rejected():
    with pytest.raises(ExperimentResolutionError):
        resolve_entry("nope-not-a-real-id")


def test_resolve_ambiguous_is_rejected():
    with pytest.raises(ExperimentResolutionError) as exc:
        resolve_entry("exp006")
    assert "several" in str(exc.value)


# ---------------------------------------------------------------------------
# Dry-run: all 20, isolated temp outputs, socket guard, schema-valid
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("experiment_id", _all_ids())
def test_dry_run_every_experiment(experiment_id, tmp_path, monkeypatch):
    _no_network(monkeypatch)
    validator = _schema_validator()
    out = tmp_path / experiment_id
    outcome = dispatch_dry_run(experiment_id, out_dir=out)
    assert isinstance(outcome, DryRunOutcome)
    doc = json.loads(outcome.plan_path.read_text(encoding="utf-8"))

    assert not list(validator.iter_errors(doc))
    assert doc["stage"] == "dry-run"
    assert doc["network_calls"] == 0 and doc["billed_calls"] == 0
    assert doc["input"]["credentials_resolved"] is False
    assert doc["input"]["endpoint_resolved"] is False
    assert doc["command_scope"]["executed"] is False
    # Adapter identity matches the catalog.
    entry = resolve_entry(experiment_id)
    assert doc["adapter"]["id"] == entry["runner_module"]
    # Inputs are hashed and present (source checkout has the corpora).
    assert doc["data"]["inputs"], experiment_id
    for f in doc["data"]["inputs"]:
        assert f["present"] is True
        assert f["sha256"] and len(f["sha256"]) == 64
    # No absolute host path leaks into the plan JSON.
    assert str(_repo_root()) not in json.dumps(doc)


def test_dry_run_count_is_twenty():
    assert len(_all_ids()) == 20


def test_dry_run_is_deterministic(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    a = dispatch_dry_run("exp001_short-factual_baseline", out_dir=tmp_path / "a")
    b = dispatch_dry_run("exp001_short-factual_baseline", out_dir=tmp_path / "b")
    assert a.plan_id == b.plan_id
    assert a.plan_path.read_bytes() == b.plan_path.read_bytes()


def test_dry_run_plan_is_immutable_no_overwrite(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    out = tmp_path / "plans-out"
    dispatch_dry_run("exp001_short-factual_baseline", out_dir=out)
    with pytest.raises(PlanConflictError):
        dispatch_dry_run("exp001_short-factual_baseline", out_dir=out)


def test_dry_run_refuses_protected_tree(monkeypatch):
    _no_network(monkeypatch)
    protected = _repo_root() / "benchmarks" / "sneaky-plans"
    with pytest.raises(PlanConflictError):
        dispatch_dry_run("exp001_short-factual_baseline", out_dir=protected)
    assert not protected.exists()


def test_dry_run_leaves_protected_trees_clean(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    before = _snapshot_tree(_repo_root() / "benchmarks")
    for eid in _all_ids():
        dispatch_dry_run(eid, out_dir=tmp_path / eid)
    after = _snapshot_tree(_repo_root() / "benchmarks")
    assert before == after


def _snapshot_tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# Source checkout required (wheel honesty)
# ---------------------------------------------------------------------------
def test_dry_run_requires_source_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch_mod, "find_source_root", lambda start=None: None)
    with pytest.raises(SourceCheckoutMissingError) as exc:
        dispatch_dry_run("exp001_short-factual_baseline", out_dir=tmp_path / "o")
    # Actionable and free of absolute host paths.
    assert "source checkout" in str(exc.value)
    assert str(_repo_root()) not in str(exc.value)


def test_find_source_root_finds_repo():
    root = find_source_root(_repo_root())
    assert root is not None
    assert (root / "experiments").is_dir() and (root / "scripts").is_dir()


# ---------------------------------------------------------------------------
# Live preflight: guards fire before any side effect, no billed call
# ---------------------------------------------------------------------------
def test_live_requires_confirm_cost():
    with pytest.raises(LiveNotConfirmedError):
        preflight_live("exp001_short-factual_baseline", confirm_cost=False)


def test_live_confirm_passes_preflight_without_calling_runner():
    entry, adapter, root = preflight_live(
        "exp001_short-factual_baseline", confirm_cost=True
    )
    assert entry["experiment_id"] == "exp001_short-factual_baseline"
    assert adapter.adapter_id == "run_benchmark"
    assert (root / "experiments").is_dir()


def test_live_rejects_adapter_without_billed_path(monkeypatch):
    # Force the resolved adapter to an offline-simulation capability.
    offline = ExperimentAdapter(
        adapter_id="run_benchmark",
        version="1.0.0",
        source_module="scripts.run_benchmark",
        supports_dry_run=True,
        supports_live=False,
        live_kind="offline-simulation",
        pricing_policy_aware=False,
    )
    monkeypatch.setattr(dispatch_mod, "_adapter_for", lambda entry: offline)
    with pytest.raises(LiveNotSupportedError):
        preflight_live("exp001_short-factual_baseline", confirm_cost=True)


def test_dispatch_live_delegates_to_typed_adapter_with_fake(monkeypatch):
    calls: dict[str, Any] = {}

    def _fake_main(argv):
        calls["argv"] = list(argv)
        return 0

    entry = resolve_entry("exp001_short-factual_baseline")
    adapter = get_adapter(entry["adapter"]["id"])
    root = _repo_root()
    fake_module = type("_FakeRunner", (), {"main": staticmethod(_fake_main)})
    monkeypatch.setattr(
        dispatch_mod,
        "preflight_live",
        lambda *args, **kwargs: (entry, adapter, root),
    )
    real_import = dispatch_mod.importlib.import_module
    monkeypatch.setattr(
        dispatch_mod.importlib,
        "import_module",
        lambda name: fake_module
        if name == adapter.source_module
        else real_import(name),
    )

    outcome = dispatch_live("exp001_short-factual_baseline", confirm_cost=True)
    assert outcome.exit_code == 0
    assert calls["argv"] == [
        "--experiment",
        "experiments/exp001_short-factual_baseline.yaml",
    ]
    assert "historical-replay" not in calls["argv"]


def test_dispatch_live_without_confirm_never_touches_runner(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("runner must not be invoked without confirm-cost")

    fake_runner = types.ModuleType("scripts.run_benchmark")
    fake_runner.main = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scripts.run_benchmark", fake_runner)
    with pytest.raises(LiveNotConfirmedError):
        dispatch_live("exp001_short-factual_baseline", confirm_cost=False)


# ---------------------------------------------------------------------------
# Invalid config is rejected by the strict loader
# ---------------------------------------------------------------------------
def test_dry_run_rejects_invalid_config(tmp_path, monkeypatch):
    _no_network(monkeypatch)

    entry = resolve_entry("exp001_short-factual_baseline")
    bad_dir = tmp_path / "fakerepo"
    (bad_dir / "experiments").mkdir(parents=True)
    (bad_dir / "scripts").mkdir()
    # A syntactically-valid YAML that violates the runner's config contract.
    (bad_dir / entry["config_path"]).write_text(
        "experiment_id: exp001_short-factual_baseline\n", encoding="utf-8"
    )
    # Point resolution at the fake repo but keep the real scripts importable.
    monkeypatch.setattr(dispatch_mod, "find_source_root", lambda start=None: bad_dir)
    with pytest.raises(ConfigValidationError):
        dispatch_dry_run(
            "exp001_short-factual_baseline",
            out_dir=tmp_path / "o",
            source_root=bad_dir,
        )


# ---------------------------------------------------------------------------
# Plan builder unit properties
# ---------------------------------------------------------------------------
def test_build_plan_bounded_knobs_are_capped():
    import yaml as _yaml  # noqa: PLC0415

    repo = _repo_root()
    entry = resolve_entry("exp001_short-factual_baseline")
    cfg_path = repo / entry["config_path"]
    cfg_bytes = cfg_path.read_bytes()
    cfg = _yaml.safe_load(cfg_bytes)
    adapter = get_adapter(entry["runner_module"])
    plan = build_plan(
        entry=entry,
        config=cfg,
        config_bytes=cfg_bytes,
        adapter=adapter,
        repo_root=repo,
    )
    assert plan.knobs.bounded["dataset_size"] == 20
    assert plan.knobs.bounded["sweep_effort"] == ["none", "low", "medium", "high", "xhigh"]
    text = plan_to_json(plan)
    assert text.endswith("\n")


def test_pricing_aware_plan_uses_current_august_snapshot(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    outcome = dispatch_dry_run(
        "exp005_dual_spillover_reactive", out_dir=tmp_path / "plans"
    )
    pricing = outcome.plan.pricing
    assert pricing.policy == "historical-replay"
    assert pricing.snapshot_path == "pricing/azure-openai-payg-sample-2026-08.yaml"
    assert pricing.snapshot_sha256 is not None


# ---------------------------------------------------------------------------
# CLI surface: exit codes + JSON contract
# ---------------------------------------------------------------------------
def test_dry_run_never_invokes_the_runner(tmp_path, monkeypatch):
    # A dry-run builds a static plan; it must never call into the runner layer,
    # so no provider/client is ever constructed. Poison experiments.run to prove
    # the dry-run path does not touch it.
    _no_network(monkeypatch)

    def _boom(*_a, **_k):
        raise AssertionError("dry-run must not invoke the runner")

    fake_experiments = types.ModuleType("experiments")
    fake_experiments.run = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "experiments", fake_experiments)

    outcome = dispatch_dry_run(
        "exp005_dual_spillover_reactive", out_dir=tmp_path / "o"
    )
    doc = json.loads(outcome.plan_path.read_text(encoding="utf-8"))
    assert doc["billed_calls"] == 0 and doc["network_calls"] == 0


def test_cli_experiment_run_dry_run_json(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    from batch_runner.cli import main  # noqa: PLC0415

    code = main(
        [
            "experiment",
            "run",
            "exp001_short-factual_baseline",
            "--stage",
            "dry-run",
            "--out",
            str(tmp_path / "plans"),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "dry-run"
    assert payload["adapter"] == "run_benchmark"
    assert payload["network_calls"] == 0 and payload["billed_calls"] == 0
    assert Path(payload["plan_path"]).is_file()


def test_cli_experiment_run_unknown_exit_3(tmp_path, capsys):
    from batch_runner.cli import main  # noqa: PLC0415

    code = main(
        ["experiment", "run", "nope-nope", "--out", str(tmp_path / "p")]
    )
    assert code == 3
    assert "input error" in capsys.readouterr().err


def test_cli_experiment_run_ambiguous_exit_3(tmp_path, capsys):
    from batch_runner.cli import main  # noqa: PLC0415

    code = main(["experiment", "run", "exp006", "--out", str(tmp_path / "p")])
    assert code == 3
    assert "several" in capsys.readouterr().err


def test_cli_experiment_run_live_without_confirm_exit_7(capsys):
    from batch_runner.cli import main  # noqa: PLC0415

    code = main(
        ["experiment", "run", "exp001_short-factual_baseline", "--stage", "live"]
    )
    assert code == 7
    assert "cost error" in capsys.readouterr().err


def test_cli_experiment_run_conflict_exit_5(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    from batch_runner.cli import main  # noqa: PLC0415

    out = str(tmp_path / "plans")
    assert main(["experiment", "run", "exp001_short-factual_baseline", "--out", out]) == 0
    capsys.readouterr()
    code = main(["experiment", "run", "exp001_short-factual_baseline", "--out", out])
    assert code == 5
    assert "report error" in capsys.readouterr().err

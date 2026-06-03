"""Unit tests for ``scripts/run_benchmark.py``.

All tests are pure: zero outbound HTTPS, zero Azure credential resolution. Each
CLI / function-level exercise builds a synthetic benchmark tree under
``tmp_path`` and runs ``--dry-run``. The seven test cases enumerated in
``.internal/tasks/004-run-benchmark-runner.md`` map to ``test_*`` functions
below; the source-tree invariants (six grep contracts) live in
``test_source_invariants``.

Test 7's "no endpoint value in captured logs" wording in the task list is
implemented as the precise variant from the Success Criteria block: the
``AZURE_OPENAI_FOUNDRY_ENDPOINT`` *value* must not appear in any log record.
The broad "no ``https://`` substring" reading is intentionally not used — the
task spec itself (Test/Verification Plan, paragraph on source-tree invariants)
disavows that weak check, and the runner legitimately logs the PAYG pricing
``source_url`` as part of the pre-run estimate citation.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import sys

import jsonschema
import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import run_benchmark as rb  # noqa: E402

FIXTURE_PRICING_DIR = REPO_ROOT / "tests" / "fixtures" / "pricing"
RUNNER_SRC_PATH = REPO_ROOT / "scripts" / "run_benchmark.py"

# Synthetic endpoint URL used across all tests. The exact substring is asserted
# absent from every log record (no-endpoint-value-in-logs invariant).
TEST_ENDPOINT_VALUE = (
    "https://wrpo-test-endpoint.services.ai.azure.com/api/projects/test-proj"
)
TEST_DEPLOYMENT_GPT_5_2 = "test-gpt-5-2-deployment"
TEST_DEPLOYMENT_GPT_4O = "test-gpt-4o-deployment"

SYSTEM_PROMPT_TEXT = "You are a helpful assistant. Answer briefly.\n"
USER_INPUTS = [
    "What is the capital of France?",
    "What year did the Apollo 11 land on the Moon?",
]


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject deterministic Azure env vars; strip any caller-leaked overrides."""
    monkeypatch.setenv("AZURE_OPENAI_FOUNDRY_ENDPOINT", TEST_ENDPOINT_VALUE)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_GPT_5_2", TEST_DEPLOYMENT_GPT_5_2)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_GPT_4O", TEST_DEPLOYMENT_GPT_4O)
    monkeypatch.setenv("AZURE_AUTH_MODE", "entra")
    monkeypatch.delenv("MAX_COST_PER_BENCHMARK_USD", raising=False)


def _write_synthetic_tree(
    tmp_path: pathlib.Path,
    *,
    family: str,
    sweep_efforts: list[str] | None = None,
    dataset_size: int = 2,
    repeats: int = 2,
    estimated_cost_usd: float = 0.01,
    hard_ceiling_usd: float = 5.0,
    budget_confirmed: bool = True,
    experiment_id: str | None = None,
    benchmark_name: str = "01-short-factual",
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Create benchmarks/{name}/dataset.json + prompts/ and one experiment YAML.

    Returns ``(experiment_yaml_path, benchmarks_root, benchmark_dir)``.
    """
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / benchmark_name
    (bench_dir / "prompts").mkdir(parents=True)
    (bench_dir / "prompts" / "system.md").write_text(
        SYSTEM_PROMPT_TEXT, encoding="utf-8"
    )
    dataset = [
        {"id": f"s{idx}", "user_input": text}
        for idx, text in enumerate(USER_INPUTS[:dataset_size])
    ]
    (bench_dir / "dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False), encoding="utf-8"
    )

    if family == "gpt-5.2":
        sweep_block = {"effort": list(sweep_efforts or ["none", "low"])}
        deployment_template = "${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}"
        model_version = "test-5.2"
        # Reasoning families reject ``temperature`` on Foundry v1.
        call_params: dict = {"max_output_tokens": 64}
    elif family == "gpt-4o":
        sweep_block = {"effort": []}
        deployment_template = "${AZURE_OPENAI_DEPLOYMENT_GPT_4O}"
        model_version = "test-4o"
        call_params = {"max_output_tokens": 64, "temperature": 0.0}
    else:  # pragma: no cover - defensive guard
        raise ValueError(f"unsupported family in test fixture: {family!r}")

    exp_id = experiment_id or f"exptest_{family.replace('.', '_').replace('-', '_')}"
    cfg = {
        "experiment_id": exp_id,
        "description": "synthetic fixture (Task 004 unit tests; no live calls)",
        "parent_experiment": None,
        "benchmark": benchmark_name,
        "dataset_size": dataset_size,
        "repeats": repeats,
        "model": {
            "deployment": deployment_template,
            "family": family,
            "version": model_version,
            "endpoint_env": "AZURE_OPENAI_FOUNDRY_ENDPOINT",
            "auth_mode": "entra",
        },
        "call_params": call_params,
        "sweep": sweep_block,
        "capture": {
            "response_text": True,
            "token_categories": True,
            "latency_ms": True,
        },
        "budget": {
            "estimated_cost_usd": estimated_cost_usd,
            "hard_ceiling_usd": hard_ceiling_usd,
            "confirmed": budget_confirmed,
        },
        "metadata": {
            "created_at": "2026-05-20",
            "git_commit": None,
            "tenant": "test",
            "consumption_model_context": "paygo",
        },
        "concurrency": 2,
    }
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir(exist_ok=True)
    exp_path = exp_dir / f"{exp_id}.yaml"
    exp_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return exp_path, benchmarks_root, bench_dir


def _run_dry(
    tmp_path: pathlib.Path,
    exp_path: pathlib.Path,
    benchmarks_root: pathlib.Path,
) -> rb.RunResult:
    """Invoke ``run_experiment`` in dry-run mode with the synthetic tree."""
    cfg = rb.load_experiment(exp_path)
    return rb.run_experiment(
        cfg=cfg,
        benchmarks_root=benchmarks_root,
        pricing_dir=FIXTURE_PRICING_DIR,
        dry_run=True,
        max_samples=None,
        allow_dirty=True,
    )


# ----------------------------------------------------------------------------
# Spec test 1 — dry-run produces the expected JSON file shape
# ----------------------------------------------------------------------------


def test_dry_run_gpt5_2_writes_NxExR_files(tmp_path: pathlib.Path) -> None:
    """gpt-5.2 sweep emits exactly ``N × len(effort) × R`` records."""
    efforts = ["none", "low", "medium", "high"]
    exp_path, benchmarks_root, bench_dir = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=efforts,
        dataset_size=2,
        repeats=3,
    )

    result = _run_dry(tmp_path, exp_path, benchmarks_root)

    expected_cells = 2 * len(efforts) * 3
    runs = sorted((bench_dir / "runs").glob("*.json"))
    assert len(runs) == expected_cells == result.cells_written

    # Each record has dry_run=true and a zero-valued usage object.
    for run in runs:
        with run.open("r", encoding="utf-8") as fh:
            rec = json.load(fh)
        assert rec["dry_run"] is True
        assert rec["model"] == "gpt-5.2"
        assert rec["effort"] in efforts
        assert rec["api_version"] == "preview"
        assert rec["auth_mode"] == "entra"
        usage = rec["usage"]
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["input_tokens_details"]["cached_tokens"] == 0
        assert usage["output_tokens_details"]["reasoning_tokens"] == 0
        assert "call_metadata" in rec
        assert rec["call_metadata"]["system_prompt_sha256"]
        assert rec["call_metadata"]["user_input_sha256"]


def test_dry_run_gpt4o_writes_Nx1xR_files_with_no_reasoning_key(
    tmp_path: pathlib.Path,
) -> None:
    """gpt-4o run emits ``N × 1 × R`` records with ``effort=None`` and no
    ``reasoning`` parameter in any captured call kwargs."""
    exp_path, benchmarks_root, bench_dir = _write_synthetic_tree(
        tmp_path, family="gpt-4o", dataset_size=2, repeats=3
    )

    result = _run_dry(tmp_path, exp_path, benchmarks_root)

    expected_cells = 2 * 1 * 3
    runs = sorted((bench_dir / "runs").glob("*.json"))
    assert len(runs) == expected_cells == result.cells_written

    for rec_path in runs:
        with rec_path.open("r", encoding="utf-8") as fh:
            rec = json.load(fh)
        assert rec["model"] == "gpt-4o"
        assert rec["effort"] is None

    # Every captured call_kwargs dict on the gpt-4o branch must lack `reasoning`.
    assert result.captured_call_kwargs, "no call_kwargs captured"
    for kwargs in result.captured_call_kwargs:
        assert "reasoning" not in kwargs, (
            f"gpt-4o call kwargs must not include 'reasoning'; got {kwargs!r}"
        )


# ----------------------------------------------------------------------------
# Spec tests 2 + 3 — pure ``build_call_kwargs`` contract
# ----------------------------------------------------------------------------


def test_build_call_kwargs_gpt4o_has_no_reasoning_key() -> None:
    kwargs = rb.build_call_kwargs(
        family="gpt-4o",
        deployment=TEST_DEPLOYMENT_GPT_4O,
        prompt="ignored",
        effort=None,
        call_params={"max_output_tokens": 64, "temperature": 0.0},
    )
    assert kwargs["model"] == TEST_DEPLOYMENT_GPT_4O
    assert "reasoning" not in kwargs


def test_build_call_kwargs_gpt5_2_has_reasoning_effort() -> None:
    for level in ("none", "low", "medium", "high"):
        kwargs = rb.build_call_kwargs(
            family="gpt-5.2",
            deployment=TEST_DEPLOYMENT_GPT_5_2,
            prompt="ignored",
            effort=level,
            call_params={"max_output_tokens": 64},
        )
        assert kwargs["model"] == TEST_DEPLOYMENT_GPT_5_2
        assert kwargs["reasoning"] == {"effort": level}


def test_build_call_kwargs_gpt5_2_rejects_temperature() -> None:
    """Foundry v1 reasoning models return HTTP 400 for ``temperature``.

    The runner surfaces this at config-validation time rather than silently
    dropping the param (silent drop would hide measurement-design mistakes:
    an operator who set ``temperature=0.0`` expected determinism; the
    runner must not pretend it honored that intent).
    """
    with pytest.raises(ValueError, match="temperature"):
        rb.build_call_kwargs(
            family="gpt-5.2",
            deployment=TEST_DEPLOYMENT_GPT_5_2,
            prompt="ignored",
            effort="low",
            call_params={"max_output_tokens": 64, "temperature": 0.0},
        )


def test_build_call_kwargs_gpt5_2_rejects_top_p() -> None:
    with pytest.raises(ValueError, match="top_p"):
        rb.build_call_kwargs(
            family="gpt-5.2",
            deployment=TEST_DEPLOYMENT_GPT_5_2,
            prompt="ignored",
            effort="low",
            call_params={"max_output_tokens": 64, "top_p": 0.5},
        )


def test_build_call_kwargs_gpt5_2_requires_effort() -> None:
    with pytest.raises(ValueError, match="requires a non-None effort"):
        rb.build_call_kwargs(
            family="gpt-5.2",
            deployment=TEST_DEPLOYMENT_GPT_5_2,
            prompt="ignored",
            effort=None,
            call_params={},
        )


def test_build_call_kwargs_gpt4o_rejects_effort() -> None:
    with pytest.raises(ValueError, match="must not carry a reasoning effort"):
        rb.build_call_kwargs(
            family="gpt-4o",
            deployment=TEST_DEPLOYMENT_GPT_4O,
            prompt="ignored",
            effort="medium",
            call_params={},
        )


# ----------------------------------------------------------------------------
# ``_render_user_template`` contract — non-string fields render as pretty JSON
# ----------------------------------------------------------------------------


def test_render_user_template_string_passes_through_unchanged() -> None:
    """A string ``input`` field must round-trip without quoting or escaping.

    Regression guard for the pre-fix render path that wrapped *all* values
    through ``str()`` — strings were unaffected only because ``str(str)``
    is identity; the test pins that contract explicitly so a future change
    to JSON-dump strings (which would add surrounding quotes) cannot land
    silently.
    """
    template = "Input:\n{input}\n\nOutput shape: {expected_output_shape}\n"
    entry = {
        "id": "sf_05",
        "input": "Meeting started at 9 AM. Next sync is on Friday.",
        "expected_output_shape": "A one-sentence summary.",
    }
    rendered = rb._render_user_template(template, entry)
    assert "Meeting started at 9 AM. Next sync is on Friday." in rendered
    assert '"Meeting started at 9 AM' not in rendered  # no spurious quoting
    assert "Output shape: A one-sentence summary." in rendered


def test_render_user_template_dict_renders_as_pretty_json_not_str() -> None:
    """The benchmark 02 / 03 hot path: nested dict fields MUST render as
    pretty JSON, not Python ``repr``.

    Pre-fix behavior was ``user_template.format_map(_DefaultMissing(entry))``
    which fell back to ``str(dict)`` — single-quoted keys, no indentation,
    ``True`` instead of ``true``. This test pins the new contract:
    ``json.dumps(value, ensure_ascii=False, indent=2)`` so the model sees
    the same canonical JSON shape the dataset author wrote.
    """
    template = "Input:\n{input}\n\nOutput shape: {expected_output_shape}\n"
    entry = {
        "id": "sf_01",
        "input": {
            "order_id": "ORD-1042",
            "customer_name": "Jane Doe",
            "items": ["Widget A", "Widget B"],
        },
        "expected_output_shape": "A single sentence.",
    }
    rendered = rb._render_user_template(template, entry)

    # Python repr markers must be ABSENT — these are the smoking guns of
    # ``str(dict)`` rendering.
    assert "'order_id'" not in rendered
    assert "'Jane Doe'" not in rendered
    assert "{'order_id'" not in rendered

    # Pretty JSON markers must be PRESENT.
    assert '"order_id": "ORD-1042"' in rendered
    assert '"customer_name": "Jane Doe"' in rendered
    assert '"items"' in rendered
    assert '"Widget A"' in rendered
    assert '"Widget B"' in rendered
    # ``indent=2`` produces a multi-line block with newlines and 2-space
    # indentation; the rendered prompt MUST be multi-line for nested values.
    json_block = rendered.split("Input:\n", 1)[1].split("\n\nOutput shape:", 1)[0]
    assert "\n" in json_block
    assert "  " in json_block


def test_render_user_template_list_renders_as_pretty_json() -> None:
    template = "Input:\n{input}\n\nOutput shape: {expected_output_shape}\n"
    entry = {
        "id": "sf_08",
        "input": [1, 4, 7, 2, 9, 6],
        "expected_output_shape": "A bullet list.",
    }
    rendered = rb._render_user_template(template, entry)
    assert "[1, 4, 7, 2, 9, 6]" not in rendered  # not the repr one-liner
    json_block = rendered.split("Input:\n", 1)[1].split("\n\nOutput shape:", 1)[0]
    assert json_block.startswith("[")
    assert json_block.endswith("]")
    for n in (1, 4, 7, 2, 9, 6):
        assert str(n) in json_block
    assert "\n" in json_block  # ``indent=2`` ⇒ one element per line


def test_render_user_template_non_string_scalars_use_json_literals() -> None:
    """``True`` / ``None`` / numbers render as JSON literals (``true`` /
    ``null`` / ``42``), not Python ``repr`` (``True`` / ``None`` / ``42``).

    Python's ``True`` and JSON's ``true`` differ in casing; ``None`` vs
    ``null`` differs entirely. The model receives the JSON form.
    """
    template = "v={value}\n"
    assert rb._render_user_template(template, {"value": True}).strip() == "v=true"
    assert rb._render_user_template(template, {"value": False}).strip() == "v=false"
    assert rb._render_user_template(template, {"value": None}).strip() == "v=null"
    assert rb._render_user_template(template, {"value": 42}).strip() == "v=42"
    assert rb._render_user_template(template, {"value": 3.5}).strip() == "v=3.5"


def test_render_user_template_missing_key_preserved_as_literal() -> None:
    """A placeholder for a field absent from ``entry`` must survive as the
    literal ``{key}`` (handled by :class:`_DefaultMissing`). The fix to
    JSON-dump non-string values must not regress this contract.
    """
    template = "a={present} b={absent}\n"
    rendered = rb._render_user_template(template, {"present": "ok"})
    assert rendered.strip() == "a=ok b={absent}"


def test_render_user_template_unicode_not_escaped() -> None:
    """``ensure_ascii=False`` preserves non-ASCII characters; the runner
    must not silently mojibake Korean / accented / emoji input.
    """
    template = "Input:\n{input}\n"
    entry = {"input": {"name": "조병현", "tag": "café"}}
    rendered = rb._render_user_template(template, entry)
    assert "조병현" in rendered
    assert "café" in rendered
    assert "\\u" not in rendered


# ----------------------------------------------------------------------------
# Spec test 4 — budget halt → exit code 1
# ----------------------------------------------------------------------------


def test_budget_tracker_ladder_triggers_halt() -> None:
    """Synthetic cost ladder over ``BudgetTracker`` flips ``is_halted`` once
    the running total crosses ``hard_ceiling_usd``."""
    tracker = rb.BudgetTracker(hard_ceiling_usd=1.0)
    assert not tracker.is_halted

    ladder = [0.25, 0.25, 0.25, 0.30]
    halted_at_step: int | None = None
    for step, increment in enumerate(ladder):
        tracker.record(increment)
        if tracker.is_halted and halted_at_step is None:
            halted_at_step = step

    # The fourth increment (0.30 → cumulative 1.05) crosses the ceiling.
    assert halted_at_step == 3, (
        f"ladder should halt on step 3; got {halted_at_step!r}, "
        f"total={tracker.total_usd:.4f}"
    )
    assert tracker.total_usd > tracker.hard_ceiling_usd

    with pytest.raises(ValueError, match="must be >= 0"):
        tracker.record(-0.01)


def test_pre_run_budget_halt_returns_exit_code_1(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MAX_COST_PER_BENCHMARK_USD`` below the YAML estimate triggers
    ``BudgetExceededError`` → CLI exit code 1.

    This exercises the pre-run "synthetic cost ladder" — the estimator sums
    ``payg_cost_per_call`` across the full ``N × effort × R`` cartesian
    product — without requiring any live API call.
    """
    exp_path, benchmarks_root, _ = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=["none", "low", "medium", "high"],
        dataset_size=2,
        repeats=2,
        estimated_cost_usd=1.0,
        hard_ceiling_usd=2.0,
        budget_confirmed=False,
    )
    # Cap at well below the YAML's estimated_cost_usd → pre-run halt fires.
    monkeypatch.setenv("MAX_COST_PER_BENCHMARK_USD", "0.0001")

    exit_code = rb.main(
        [
            "--experiment",
            str(exp_path),
            "--dry-run",
            "--benchmarks-root",
            str(benchmarks_root),
            "--pricing-dir",
            str(FIXTURE_PRICING_DIR),
            "--allow-dirty",
        ]
    )
    assert exit_code == rb.EXIT_BUDGET == 1


# ----------------------------------------------------------------------------
# Spec test 5 — filename collision is a hard abort
# ----------------------------------------------------------------------------


def test_filename_collision_aborts(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing target JSON file aborts the run via
    ``FilenameCollisionError`` (no overwrite, no ``_N`` suffix rename).

    Monkey-patches ``run_benchmark._utc_now`` to return a frozen timestamp so
    we can pre-place a file at the exact path the runner will compute, then
    asserts the runner surfaces the typed error.
    """
    import datetime as _dt

    exp_path, benchmarks_root, bench_dir = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=["none"],
        dataset_size=2,
        repeats=1,
        experiment_id="exptest_collision",
    )

    frozen_ts = _dt.datetime(2099, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(rb, "_utc_now", lambda: frozen_ts)

    # Pre-place a file at the exact path the runner will try to write to for
    # the first cell (sample_idx=0, effort="none", repeat=0).
    runs_dir = bench_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    blocker = rb._target_path(
        runs_dir=runs_dir,
        timestamp_utc=frozen_ts,
        exp_id="exptest_collision",
        sample_idx=0,
        family="gpt-5.2",
        effort="none",
        repeat=0,
    )
    blocker.write_text("{}", encoding="utf-8")
    assert blocker.exists()

    # The runner must abort with FilenameCollisionError on the first cell.
    with pytest.raises(rb.FilenameCollisionError, match="COLLISION_ABORT"):
        _run_dry(tmp_path, exp_path, benchmarks_root)

    # Subclass relationship lets downstream call sites catch either name.
    assert issubclass(rb.FilenameCollisionError, FileExistsError)

    # And: the CLI maps this to exit code 3 (dataset class) — assert by
    # re-invoking through ``main()`` with the same monkey-patched timestamp.
    exit_code = rb.main(
        [
            "--experiment",
            str(exp_path),
            "--dry-run",
            "--benchmarks-root",
            str(benchmarks_root),
            "--pricing-dir",
            str(FIXTURE_PRICING_DIR),
            "--allow-dirty",
        ]
    )
    assert exit_code == rb.EXIT_DATASET == 3


# ----------------------------------------------------------------------------
# Spec test 6 — system_prompt_sha256 deterministic across two dry-runs
# ----------------------------------------------------------------------------


def test_system_prompt_sha256_is_deterministic_across_dry_runs(
    tmp_path: pathlib.Path,
) -> None:
    """Two dry-runs of the same YAML against identical prompts produce
    identical ``call_metadata.system_prompt_sha256`` (and ``user_input_sha256``)
    digests."""
    exp_path_a, root_a, bench_a = _write_synthetic_tree(
        tmp_path / "run_a",
        family="gpt-5.2",
        sweep_efforts=["none"],
        dataset_size=2,
        repeats=1,
    )
    exp_path_b, root_b, bench_b = _write_synthetic_tree(
        tmp_path / "run_b",
        family="gpt-5.2",
        sweep_efforts=["none"],
        dataset_size=2,
        repeats=1,
    )

    _run_dry(tmp_path / "run_a", exp_path_a, root_a)
    _run_dry(tmp_path / "run_b", exp_path_b, root_b)

    def _shas(bench_dir: pathlib.Path) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        for p in sorted((bench_dir / "runs").glob("*.json")):
            with p.open("r", encoding="utf-8") as fh:
                rec = json.load(fh)
            out.append(
                (
                    rec["sample_idx"],
                    rec["call_metadata"]["system_prompt_sha256"],
                    rec["call_metadata"]["user_input_sha256"],
                )
            )
        return out

    a = _shas(bench_a)
    b = _shas(bench_b)
    assert a, "run_a produced no records"
    assert a == b, (
        "system_prompt_sha256 / user_input_sha256 must be deterministic across "
        f"identical dry-runs; got a={a!r} b={b!r}"
    )

    # And: the digest must equal sha256_text(system_prompt) computed directly.
    expected_system_sha = rb.sha256_text(SYSTEM_PROMPT_TEXT)
    for _idx, system_sha, _user_sha in a:
        assert system_sha == expected_system_sha


# ----------------------------------------------------------------------------
# Spec test 7 — endpoint value never appears in captured log records
# ----------------------------------------------------------------------------


def test_endpoint_value_never_in_logs(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Per Success Criteria: ``AZURE_OPENAI_FOUNDRY_ENDPOINT`` *value* must not
    appear in any captured log record. The env var *name* may legitimately
    be logged.
    """
    exp_path, benchmarks_root, _ = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=["none"],
        dataset_size=2,
        repeats=1,
    )
    with caplog.at_level(logging.DEBUG, logger="scripts.run_benchmark"):
        _run_dry(tmp_path, exp_path, benchmarks_root)

    assert caplog.records, "no log records captured; logging path may be broken"
    for record in caplog.records:
        msg = record.getMessage()
        assert TEST_ENDPOINT_VALUE not in msg, (
            f"endpoint value leaked into log record: {msg!r}"
        )
    # And: the full caplog.text should contain the env var NAME at least
    # once (e.g. in error paths) — but not its value.
    assert TEST_ENDPOINT_VALUE not in caplog.text


# ----------------------------------------------------------------------------
# Source-tree invariants (six grep contracts from the spec)
# ----------------------------------------------------------------------------


def test_source_invariants() -> None:
    """Enforce the six grep invariants the spec mandates against
    ``scripts/run_benchmark.py``::

      1. ``api_version="preview"`` literal appears at least once (the
         methodology label recorded in every raw JSON's ``api_version``
         field).
      2. ``2025-03-01-preview`` (legacy api_version) appears nowhere.
      3. No plaintext API key code path: ``AZURE_OPENAI_API_KEY``
         (env-var read), ``openai.api_key`` (module attr assignment), or
         a string-literal key (``api_key="sk-..."`` / ``api_key='sk-..."``)
         all forbidden. The SDK parameter name ``api_key`` is permitted
         *only* when its right-hand side is a bearer-token resolution
         (a ``token_provider()`` call) — never a string literal.
      4. ``reasoning={...}`` literal sits inside a gpt-5.2 branch (verified
         by counting matches and checking surrounding lines for the family
         guard).
      5. ``print(`` appears at most once, and that one match is inside the
         function that emits the CLI final summary.
      6. ``AZURE_OPENAI_FOUNDRY_ENDPOINT = ...`` (literal assignment) appears
         nowhere outside of a docstring/comment — the env var is only *read*.

    Task 006 follow-up: invariant (1) anchors against the Foundry v1 client
    construction site (``AsyncOpenAI(`` with a ``base_url=.../openai/v1/``
    pattern), NOT against the legacy ``AsyncAzureOpenAI(`` site. The classic
    client class targets ``*.openai.azure.com/openai/responses`` and fails
    against the Foundry v1 endpoint with 401 / 400; the runner therefore no
    longer references it at any runtime call site.
    """
    src = RUNNER_SRC_PATH.read_text(encoding="utf-8")
    src_lines = src.splitlines()

    # (1) api_version="preview" literal — at least one match, AND at least
    # one of those matches must sit within ±10 source lines of an
    # ``AsyncOpenAI(`` call so the methodology label anchors near the
    # runtime client-construction site (not a docstring proxy). The
    # FOUNDRY_API_VERSION constant is asserted equal to the string
    # ``"preview"`` at the call site so the literal and the constant
    # cannot drift apart.
    pat_preview = re.compile(r'api_version\s*=\s*"preview"')
    preview_hits = [i for i, ln in enumerate(src_lines, 1) if pat_preview.search(ln)]
    assert preview_hits, "api_version=\"preview\" literal must appear at least once"

    pat_async_call = re.compile(r"AsyncOpenAI\s*\(")
    async_call_hits = [
        i for i, ln in enumerate(src_lines, 1) if pat_async_call.search(ln)
    ]
    runtime_call_sites = [i for i in async_call_hits if "import" not in src_lines[i - 1]]
    assert runtime_call_sites, (
        "expected at least one runtime AsyncOpenAI(...) call site"
    )
    anchored = False
    for call_line in runtime_call_sites:
        for preview_line in preview_hits:
            if abs(preview_line - call_line) <= 10:
                anchored = True
                break
        if anchored:
            break
    assert anchored, (
        "api_version=\"preview\" literal must appear within ±10 source lines "
        f"of an AsyncOpenAI(...) call site; call sites={runtime_call_sites!r}, "
        f"preview literal hits={preview_hits!r}"
    )

    # (2) Legacy 2025-03-01-preview — zero matches.
    assert "2025-03-01-preview" not in src, (
        "legacy classic-endpoint api_version is forbidden in this codebase"
    )

    # (3) No plaintext API key path. The SDK parameter name ``api_key`` is
    # permitted, but ONLY when its right-hand side is a bearer-token
    # resolution (i.e. a callable invocation like ``token_provider()``).
    # Plaintext literal keys, env-var reads of AZURE_OPENAI_API_KEY, and
    # ``openai.api_key = ...`` module-attr assignments are all forbidden.
    assert "AZURE_OPENAI_API_KEY" not in src, (
        "Entra-only auth: no AZURE_OPENAI_API_KEY env read is allowed"
    )
    assert "openai.api_key" not in src, (
        "Entra-only auth: no `openai.api_key = ...` module-attr assignment"
    )
    pat_literal_key = re.compile(r"""api_key\s*=\s*["'](sk-|[A-Za-z0-9]{8,})""")
    literal_key_hits = [
        i for i, ln in enumerate(src_lines, 1) if pat_literal_key.search(ln)
    ]
    assert not literal_key_hits, (
        f"plaintext literal key value passed to api_key=: matched lines: "
        f"{literal_key_hits}"
    )

    # (4) ``reasoning={...}`` literal — must be guarded by a gpt-5.2 family
    # branch within the preceding 10 source lines.
    pat_reasoning = re.compile(r"reasoning\s*=\s*\{")
    pat_family_guard = re.compile(r'family\s*==\s*"gpt-5\.2"')
    reasoning_hits = [
        i for i, ln in enumerate(src_lines, 1) if pat_reasoning.search(ln)
    ]
    assert reasoning_hits, "expected at least one `reasoning={...}` literal"
    for hit in reasoning_hits:
        lookback = "\n".join(src_lines[max(0, hit - 11) : hit])
        assert pat_family_guard.search(lookback), (
            f"reasoning={{...}} on line {hit} is not guarded by a "
            f"`family == \"gpt-5.2\"` check within the preceding 10 lines"
        )

    # (5) ``print(`` at most one match, and it must sit inside the CLI summary
    # function (``main``) per the methodology rule.
    pat_print = re.compile(r"^\s*print\(")
    print_hits = [i for i, ln in enumerate(src_lines, 1) if pat_print.search(ln)]
    assert len(print_hits) <= 1, (
        f"`print(` may only appear in the CLI final summary; matched lines: {print_hits}"
    )
    if print_hits:
        # Walk backwards to find the enclosing `def main(`.
        line_no = print_hits[0]
        enclosing_def = None
        for back in range(line_no - 1, -1, -1):
            stripped = src_lines[back].lstrip()
            if stripped.startswith("def "):
                enclosing_def = stripped
                break
        assert enclosing_def is not None and enclosing_def.startswith("def main("), (
            f"the sole `print(` must sit inside `def main(...)`; "
            f"found enclosing def: {enclosing_def!r}"
        )

    # (6) ``AZURE_OPENAI_FOUNDRY_ENDPOINT = <literal>`` (the env var is only
    # *read*; the only assignment that mentions this name should be the
    # ``ENV_FOUNDRY_ENDPOINT_NAME = "AZURE_OPENAI_FOUNDRY_ENDPOINT"`` constant,
    # which assigns the env var *name* to a Python identifier — not the env
    # var value to anything).
    pat_endpoint_assign = re.compile(
        r"^\s*AZURE_OPENAI_FOUNDRY_ENDPOINT\s*="
    )
    bad_assigns = [
        i for i, ln in enumerate(src_lines, 1) if pat_endpoint_assign.search(ln)
    ]
    assert not bad_assigns, (
        f"AZURE_OPENAI_FOUNDRY_ENDPOINT must not be assigned a literal value; "
        f"matched lines: {bad_assigns}"
    )


# ----------------------------------------------------------------------------
# Auxiliary coverage — typed errors + env resolution
# ----------------------------------------------------------------------------


def test_missing_dataset_returns_exit_3(tmp_path: pathlib.Path) -> None:
    """A YAML pointing at a non-existent benchmark directory exits code 3."""
    exp_path, benchmarks_root, bench_dir = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=["none"],
        dataset_size=2,
        repeats=1,
    )
    # Remove the dataset.json so load_dataset raises DatasetMissingError.
    (bench_dir / "dataset.json").unlink()

    exit_code = rb.main(
        [
            "--experiment",
            str(exp_path),
            "--dry-run",
            "--benchmarks-root",
            str(benchmarks_root),
            "--pricing-dir",
            str(FIXTURE_PRICING_DIR),
            "--allow-dirty",
        ]
    )
    assert exit_code == rb.EXIT_DATASET == 3


def test_missing_endpoint_env_returns_exit_2(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``AZURE_OPENAI_FOUNDRY_ENDPOINT`` env var exits code 2."""
    exp_path, benchmarks_root, _ = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=["none"],
        dataset_size=2,
        repeats=1,
    )
    monkeypatch.delenv("AZURE_OPENAI_FOUNDRY_ENDPOINT", raising=False)

    exit_code = rb.main(
        [
            "--experiment",
            str(exp_path),
            "--dry-run",
            "--benchmarks-root",
            str(benchmarks_root),
            "--pricing-dir",
            str(FIXTURE_PRICING_DIR),
            "--allow-dirty",
        ]
    )
    assert exit_code == rb.EXIT_AUTH == 2


# ----------------------------------------------------------------------------
# Reviewer fix (1): live-call BUDGET_HALT — fake client drives the running
# USD total over the hard ceiling mid-run; the runner must emit a
# ``BUDGET_HALT`` log line and exit code 1.
# ----------------------------------------------------------------------------


class _FakeUsage:
    """Minimal stand-in for ``response.usage`` exposing ``model_dump``."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self) -> dict:
        return dict(self._payload)


class _FakeResponse:
    def __init__(self, usage: dict, output_text: str = "ok") -> None:
        self.usage = _FakeUsage(usage)
        self.output_text = output_text


class _FakeResponses:
    """``client.responses`` namespace returning a deterministic high-cost
    usage object for every call."""

    def __init__(self, usage: dict) -> None:
        self._usage = dict(usage)
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> _FakeResponse:
        self.calls.append(dict(kwargs))
        return _FakeResponse(self._usage)


class _FakeLiveClient:
    """Stand-in for ``openai.AsyncAzureOpenAI`` — no SDK install needed."""

    def __init__(self, usage_per_call: dict) -> None:
        self.responses = _FakeResponses(usage_per_call)


def test_live_budget_halt_with_fake_client_returns_exit_1(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Reviewer fix (1): when a live response carries enough tokens that
    ``payg_cost_per_call(...).usd_per_request`` × cumulative-calls crosses
    ``budget.hard_ceiling_usd``, the runner must

      * commit each cell's cost to ``BudgetTracker`` (was silently 0.0
        before the fix — the hard ceiling could never fire),
      * emit a ``BUDGET_HALT`` log line with ``total_usd`` and
        ``ceiling_usd``,
      * exit with code ``1`` (``EXIT_BUDGET``).

    The fake client is injected by monkey-patching
    ``run_benchmark._build_live_client``; zero outbound HTTPS happens.
    """
    import scripts.run_benchmark as rb_mod

    # Sized so the FIRST live call already crosses hard_ceiling_usd=0.01
    # against the in-tree pricing fixture. The fixture's gpt-5.2 rates are
    # listed in tests/fixtures/pricing/azure-openai-payg-2026-05.yaml; we
    # send a huge reasoning-token bill so the math is unambiguous.
    high_cost_usage = {
        "input_tokens": 500,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 200,
        "output_tokens_details": {"reasoning_tokens": 1_000_000},
        "total_tokens": 1_000_700,
    }
    fake_client = _FakeLiveClient(high_cost_usage)

    def _fake_factory(*, endpoint_value: str) -> object:
        # The fake client never touches HTTPS; ``endpoint_value`` is
        # captured-but-ignored so the live-path codepath through
        # _build_live_client is exercised end-to-end.
        return fake_client

    exp_path, benchmarks_root, _ = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=["none", "low", "medium"],
        dataset_size=2,
        repeats=2,
        estimated_cost_usd=0.001,
        hard_ceiling_usd=0.01,
        budget_confirmed=True,
        experiment_id="exptest_live_budget_halt",
    )

    # NOTE: we deliberately do NOT pass --dry-run. The runner builds the
    # (fake) live client via _build_live_client; the only outbound contact
    # is into our in-process fake.
    with caplog.at_level(logging.INFO, logger="scripts.run_benchmark"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(rb_mod, "_build_live_client", _fake_factory)
            exit_code = rb_mod.main(
                [
                    "--experiment",
                    str(exp_path),
                    "--benchmarks-root",
                    str(benchmarks_root),
                    "--pricing-dir",
                    str(FIXTURE_PRICING_DIR),
                    "--allow-dirty",
                ]
            )

    # (a) The runner exits via the budget code path.
    assert exit_code == rb_mod.EXIT_BUDGET == 1, (
        f"expected exit code 1; got {exit_code!r}. "
        f"caplog text: {caplog.text!r}"
    )

    # (b) At least one BUDGET_HALT log line was emitted with the running
    # total and the configured ceiling.
    halt_records = [
        rec for rec in caplog.records if "BUDGET_HALT" in rec.getMessage()
    ]
    assert halt_records, (
        f"expected at least one BUDGET_HALT log line; got log text:\n{caplog.text}"
    )
    halt_text = "\n".join(rec.getMessage() for rec in halt_records)
    assert "total_usd=" in halt_text
    assert "ceiling_usd=" in halt_text

    # (c) The fake client was actually called (budget recording happened
    # against real usage rather than a phantom zero) AND at least one cell
    # was skipped post-halt (proving the halt is enforced before the next
    # call, not after the entire batch completes).
    assert fake_client.responses.calls, "fake client was never invoked"
    n_total_cells = 2 * 3 * 2  # samples × efforts × repeats
    assert len(fake_client.responses.calls) < n_total_cells, (
        f"halt did not skip any cells: {len(fake_client.responses.calls)} "
        f"of {n_total_cells} cells executed"
    )


# ----------------------------------------------------------------------------
# Reviewer fix (2): concurrency must be real, and outputs must be invariant
# to scheduling order.
# ----------------------------------------------------------------------------


def _record_signature(
    bench_dir: pathlib.Path,
) -> set[tuple[int, str, str | None, int, str, str]]:
    """Reduce a runs/ tree to the deterministic signature of each record.

    The tuple intentionally excludes scheduling-dependent fields
    (``timestamp_utc``, ``latency_ms``, filename timestamp prefix). What
    remains — ``(sample_idx, model, effort, repeat, system_prompt_sha256,
    user_input_sha256)`` — MUST be identical regardless of concurrency.
    """
    out: set[tuple[int, str, str | None, int, str, str]] = set()
    for p in (bench_dir / "runs").glob("*.json"):
        with p.open("r", encoding="utf-8") as fh:
            rec = json.load(fh)
        out.add(
            (
                rec["sample_idx"],
                rec["model"],
                rec["effort"],
                rec["repeat"],
                rec["call_metadata"]["system_prompt_sha256"],
                rec["call_metadata"]["user_input_sha256"],
            )
        )
    return out


def test_concurrency_determinism_invariant_to_scheduling(
    tmp_path: pathlib.Path,
) -> None:
    """Reviewer fix (2): the runner now schedules cells via
    ``asyncio.gather`` under an actual ``asyncio.Semaphore``.

    Two dry-runs of the SAME synthetic experiment, one with
    ``concurrency=1`` (effectively serial) and one with ``concurrency=5``
    (parallel), must produce the SAME set of records identified by
    ``(sample_idx, model, effort, repeat, system_sha, user_sha)``. The
    file-content fields that depend on wall-clock (``timestamp_utc``,
    ``latency_ms``) are intentionally excluded — they may differ; the
    measurement payload may not.
    """
    # Build two isolated trees with different concurrency.
    def _build(tree_root: pathlib.Path, concurrency: int) -> pathlib.Path:
        exp_path, _, bench_dir = _write_synthetic_tree(
            tree_root,
            family="gpt-5.2",
            sweep_efforts=["none", "low", "medium"],
            dataset_size=2,
            repeats=2,
        )
        with exp_path.open("r", encoding="utf-8") as fh:
            cfg_yaml = yaml.safe_load(fh)
        cfg_yaml["concurrency"] = concurrency
        exp_path.write_text(yaml.safe_dump(cfg_yaml, sort_keys=False), encoding="utf-8")
        cfg = rb.load_experiment(exp_path)
        result = rb.run_experiment(
            cfg=cfg,
            benchmarks_root=tree_root / "benchmarks",
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            max_samples=None,
            allow_dirty=True,
        )
        assert result.cells_written == 2 * 3 * 2
        return bench_dir

    bench_serial = _build(tmp_path / "serial", concurrency=1)
    bench_parallel = _build(tmp_path / "parallel", concurrency=5)

    sig_serial = _record_signature(bench_serial)
    sig_parallel = _record_signature(bench_parallel)
    assert sig_serial == sig_parallel, (
        "concurrency=1 vs concurrency=5 produced different record sets — "
        "the runner is not invariant to scheduling order.\n"
        f"only_in_serial   = {sig_serial - sig_parallel!r}\n"
        f"only_in_parallel = {sig_parallel - sig_serial!r}"
    )
    # And: the record count matches the cartesian product exactly. No
    # "lost" cells, no duplicates from concurrent collisions.
    assert len(sig_serial) == 2 * 3 * 2


def test_concurrency_semaphore_actually_limits_inflight(
    tmp_path: pathlib.Path,
) -> None:
    """Reviewer fix (2): the semaphore must really gate parallel execution
    — not just exist in the source.

    We monkey-patch ``_execute_cell`` with an awaitable that sleeps long
    enough for several concurrent siblings to enter the semaphore. With
    ``concurrency=3`` the observed max-in-flight should be exactly 3
    (not 1 — proving the runner is no longer serial — and not ``N*E*R``
    — proving the semaphore caps the in-flight count).
    """
    import asyncio as _asyncio

    import scripts.run_benchmark as rb_mod

    exp_path, benchmarks_root, _ = _write_synthetic_tree(
        tmp_path,
        family="gpt-5.2",
        sweep_efforts=["none", "low", "medium"],
        dataset_size=2,
        repeats=2,
    )
    with exp_path.open("r", encoding="utf-8") as fh:
        cfg_yaml = yaml.safe_load(fh)
    cfg_yaml["concurrency"] = 3
    exp_path.write_text(yaml.safe_dump(cfg_yaml, sort_keys=False), encoding="utf-8")

    inflight = 0
    max_inflight = 0
    lock = _asyncio.Lock()

    async def _fake_execute_cell(**_kwargs: object) -> dict:
        nonlocal inflight, max_inflight
        async with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        await _asyncio.sleep(0.02)
        async with lock:
            inflight -= 1
        return {"cells": "fake"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rb_mod, "_execute_cell", _fake_execute_cell)
        cfg = rb_mod.load_experiment(exp_path)
        rb_mod.run_experiment(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            max_samples=None,
            allow_dirty=True,
        )

    n_cells = 2 * 3 * 2
    assert max_inflight == 3, (
        f"expected exactly concurrency=3 in-flight cells; observed {max_inflight}. "
        f"max_inflight==1 means the runner is still serial; "
        f"max_inflight=={n_cells} means the semaphore is not gating."
    )


# ----------------------------------------------------------------------------
# Task 010: agent.tool_loop YAML schema + tool_config_sha256 invariants
# ----------------------------------------------------------------------------


def _write_tool_loop_tree(
    tmp_path: pathlib.Path,
    *,
    family: str = "gpt-5.2",
) -> tuple[pathlib.Path, pathlib.Path]:
    """Build a benchmark tree with prompts/tool_schemas/*.json + agent block."""
    benchmarks_root = tmp_path / "benchmarks"
    bench_dir = benchmarks_root / "03-tool-using-agent"
    (bench_dir / "prompts" / "tool_schemas").mkdir(parents=True)
    (bench_dir / "prompts" / "system.md").write_text(
        "You are a tool-using agent.\n", encoding="utf-8"
    )
    (bench_dir / "dataset.json").write_text(
        json.dumps([{"id": "tu_01", "user_input": "test"}]), encoding="utf-8"
    )
    (bench_dir / "prompts" / "tool_schemas" / "calculator.json").write_text(
        json.dumps(
            {
                "name": "calculator",
                "description": "calc",
                "parameters": {
                    "type": "object",
                    "properties": {"expr": {"type": "string"}},
                    "required": ["expr"],
                },
            }
        ),
        encoding="utf-8",
    )
    (bench_dir / "prompts" / "tool_schemas" / "web_search.json").write_text(
        json.dumps(
            {
                "name": "web_search",
                "description": "search",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ),
        encoding="utf-8",
    )
    (bench_dir / "search_kb.json").write_text("{}", encoding="utf-8")

    cfg = {
        "experiment_id": "exptest_tool_loop",
        "description": "tool-loop unit test",
        "parent_experiment": None,
        "benchmark": "03-tool-using-agent",
        "dataset_size": 1,
        "repeats": 1,
        "model": {
            "deployment": "${AZURE_OPENAI_DEPLOYMENT_GPT_5_2}",
            "family": family,
            "version": "test-5.2",
            "endpoint_env": "AZURE_OPENAI_FOUNDRY_ENDPOINT",
            "auth_mode": "entra",
        },
        "call_params": {"max_output_tokens": 64},
        "sweep": {"effort": ["low"]} if family == "gpt-5.2" else {"effort": []},
        "capture": {"response_text": True, "token_categories": True, "latency_ms": True},
        "budget": {"estimated_cost_usd": 0.01, "hard_ceiling_usd": 1.0, "confirmed": True},
        "metadata": {"created_at": "2026-05-24", "tenant": "test"},
        "concurrency": 2,
        "agent": {
            "tool_loop": True,
            "max_tool_iterations": 4,
            "tools": [
                {
                    "name": "calculator",
                    "schema_path": "benchmarks/03-tool-using-agent/prompts/tool_schemas/calculator.json",
                },
                {
                    "name": "web_search",
                    "schema_path": "benchmarks/03-tool-using-agent/prompts/tool_schemas/web_search.json",
                },
            ],
            "search_kb_path": "benchmarks/03-tool-using-agent/search_kb.json",
        },
    }
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir(exist_ok=True)
    exp_path = exp_dir / "exptest_tool_loop.yaml"
    exp_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return exp_path, benchmarks_root


def test_tool_loop_yaml_parses(tmp_path: pathlib.Path) -> None:
    exp_path, _ = _write_tool_loop_tree(tmp_path)
    cfg = rb.load_experiment(exp_path)
    assert cfg.agent is not None
    assert cfg.agent.tool_loop is True
    assert cfg.agent.max_tool_iterations == 4
    assert cfg.agent.tools == ("calculator", "web_search")


def test_tool_loop_requires_search_kb_when_web_search(tmp_path: pathlib.Path) -> None:
    """Removing search_kb_path while web_search remains must raise."""
    exp_path, _ = _write_tool_loop_tree(tmp_path)
    raw = yaml.safe_load(exp_path.read_text(encoding="utf-8"))
    del raw["agent"]["search_kb_path"]
    exp_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="search_kb_path"):
        rb.load_experiment(exp_path)


def test_tool_loop_requires_non_empty_tools(tmp_path: pathlib.Path) -> None:
    exp_path, _ = _write_tool_loop_tree(tmp_path)
    raw = yaml.safe_load(exp_path.read_text(encoding="utf-8"))
    raw["agent"]["tools"] = []
    exp_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        rb.load_experiment(exp_path)


def test_tool_loop_max_iter_lower_bound(tmp_path: pathlib.Path) -> None:
    exp_path, _ = _write_tool_loop_tree(tmp_path)
    raw = yaml.safe_load(exp_path.read_text(encoding="utf-8"))
    raw["agent"]["max_tool_iterations"] = 0
    exp_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="max_tool_iterations"):
        rb.load_experiment(exp_path)


def test_build_tool_list_for_request_and_sha(tmp_path: pathlib.Path) -> None:
    exp_path, _ = _write_tool_loop_tree(tmp_path)
    cfg = rb.load_experiment(exp_path)
    # Resolve paths relative to the tmp_path so the schema files are found.
    tools = rb.build_tool_list_for_request(cfg.agent, base_dir=tmp_path)
    assert [t["name"] for t in tools] == ["calculator", "web_search"]
    sha = rb.tool_config_sha256(tools)
    # SHA must be deterministic.
    assert sha == rb.tool_config_sha256(tools)
    assert len(sha) == 64


def test_tool_loop_dry_run_emits_tool_config_sha(tmp_path: pathlib.Path) -> None:
    """Dry-run with agent.tool_loop=true writes tool_config_sha256 into call_metadata."""
    exp_path, benchmarks_root = _write_tool_loop_tree(tmp_path)
    bench_dir = benchmarks_root / "03-tool-using-agent"
    cfg = rb.load_experiment(exp_path)

    # Mock the base_dir for tool list loading (we monkey-patch at module level).
    # We point base_dir at tmp_path so the relative paths resolve.
    real_build = rb.build_tool_list_for_request

    def _build_with_base(agent: rb.AgentConfig, *, base_dir: pathlib.Path | None = None) -> list[dict]:
        return real_build(agent, base_dir=tmp_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rb, "build_tool_list_for_request", _build_with_base)
        result = rb.run_experiment(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            max_samples=None,
            allow_dirty=True,
        )

    runs = sorted((bench_dir / "runs").glob("*.json"))
    assert len(runs) == result.cells_written
    shas: set[str] = set()
    for run in runs:
        rec = json.loads(run.read_text(encoding="utf-8"))
        sha = rec["call_metadata"]["tool_config_sha256"]
        assert isinstance(sha, str) and len(sha) == 64, f"expected hex sha; got {sha!r}"
        shas.add(sha)
        assert "tool_calls" in rec, "tool-loop mode must emit tool_calls list"
        assert isinstance(rec["tool_calls"], list)
    # Tool-config sha must be a single value across all cells.
    assert len(shas) == 1


# ----------------------------------------------------------------------------
# Task 010: live tool-loop path now actually runs the ReAct loop
# ----------------------------------------------------------------------------
# The runner extension for ``agent.tool_loop: true`` is fully in-scope —
# there is no LOC-threshold escape hatch and no deferred sub-task. The
# runner dispatches function calls through ``scripts.tools.TOOL_REGISTRY``,
# feeds tool results back, honors ``agent.max_tool_iterations``, sums
# per-iteration usage, and records ``tool_loop_terminated``.


class _FakeFunctionCallItem:
    """Stand-in for ``ResponseFunctionToolCall`` used by the fake client."""

    def __init__(self, name: str, arguments: str, call_id: str) -> None:
        self._payload = {
            "type": "function_call",
            "name": name,
            "arguments": arguments,
            "call_id": call_id,
        }

    def model_dump(self) -> dict:
        return dict(self._payload)


class _FakeToolLoopResponse:
    """Two-step fake response sequence: first call returns a function_call,
    second call returns a final answer. Drives the loop end-to-end."""

    def __init__(self, usage: dict, output_items: list, output_text: str = "") -> None:
        self.usage = _FakeUsage(usage)
        self.output_text = output_text
        self.output = output_items


class _FakeToolLoopResponses:
    """``client.responses`` namespace that returns a function_call on the
    first invocation and a final-answer message on the second.
    """

    def __init__(self, usage: dict) -> None:
        self._usage = dict(usage)
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> _FakeToolLoopResponse:
        self.calls.append(dict(kwargs))
        if len(self.calls) == 1:
            # First call: request a calculator invocation.
            return _FakeToolLoopResponse(
                self._usage,
                [_FakeFunctionCallItem("calculator", '{"expr": "1+1"}', "call-1")],
                "",
            )
        # Subsequent calls: emit a final answer (no function_call).
        return _FakeToolLoopResponse(self._usage, [], "2")


class _FakeToolLoopLiveClient:
    def __init__(self, usage_per_call: dict) -> None:
        self.responses = _FakeToolLoopResponses(usage_per_call)


def test_live_tool_loop_dispatches_tool_calls(tmp_path: pathlib.Path) -> None:
    """Live tool-loop path runs end-to-end through the fake client:

    * First ``responses.create`` returns a ``function_call`` for the
      calculator with ``{"expr": "1+1"}``;
    * The runner dispatches via ``TOOL_REGISTRY``, getting back ``"2"``;
    * Second ``responses.create`` returns a plain final-answer message;
    * The cell record carries ``tool_calls`` (non-empty), summed usage,
      ``tool_loop_terminated="ok"``, and the byte-identical
      ``tool_config_sha256`` shared across cells.
    """
    import scripts.run_benchmark as rb_mod

    benign_usage = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 50,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 150,
    }
    fake_client = _FakeToolLoopLiveClient(benign_usage)

    def _fake_factory(*, endpoint_value: str) -> object:
        return fake_client

    exp_path, benchmarks_root = _write_tool_loop_tree(tmp_path)
    bench_dir = benchmarks_root / "03-tool-using-agent"

    real_build = rb.build_tool_list_for_request

    def _build_with_base(
        agent: rb.AgentConfig, *, base_dir: pathlib.Path | None = None
    ) -> list[dict]:
        return real_build(agent, base_dir=tmp_path)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("AZURE_OPENAI_FOUNDRY_ENDPOINT", TEST_ENDPOINT_VALUE)
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_GPT_5_2", TEST_DEPLOYMENT_GPT_5_2)
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_GPT_4O", TEST_DEPLOYMENT_GPT_4O)
        monkeypatch.setattr(rb_mod, "_build_live_client", _fake_factory)
        monkeypatch.setattr(rb_mod, "build_tool_list_for_request", _build_with_base)
        cfg = rb_mod.load_experiment(exp_path)
        result = rb_mod.run_experiment(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=False,
            max_samples=None,
            allow_dirty=True,
        )
    finally:
        monkeypatch.undo()

    assert result.cells_written >= 1
    # Two responses.create() invocations per cell (the function_call leg
    # and the final-answer leg).
    assert len(fake_client.responses.calls) >= 2

    runs = sorted((bench_dir / "runs").glob("*.json"))
    assert runs, "live tool-loop must emit cell records"
    for run in runs:
        rec = json.loads(run.read_text(encoding="utf-8"))
        assert rec["tool_loop_terminated"] == "ok"
        assert isinstance(rec["tool_calls"], list)
        assert len(rec["tool_calls"]) >= 1, (
            f"expected at least one dispatched tool call; got {rec['tool_calls']!r}"
        )
        # Tool config SHA must be a stable 64-char hex string.
        sha = rec["call_metadata"]["tool_config_sha256"]
        assert isinstance(sha, str) and len(sha) == 64
        # Summed usage should reflect more than one iteration's tokens.
        assert rec["usage"]["input_tokens"] >= 200
        assert rec["response_text"] == "2"


def test_dry_run_tool_loop_still_works(tmp_path: pathlib.Path) -> None:
    """Sanity check: --dry-run with tool_loop=true is still legal and
    emits the skeleton record (tool_loop_terminated='dry_run_skeleton').
    The raise is gated on dry_run=False; we must not break the dry-run
    path or the existing fixture / smoke-prep workflow.
    """
    exp_path, benchmarks_root = _write_tool_loop_tree(tmp_path)
    bench_dir = benchmarks_root / "03-tool-using-agent"
    cfg = rb.load_experiment(exp_path)

    real_build = rb.build_tool_list_for_request

    def _build_with_base(agent: rb.AgentConfig, *, base_dir: pathlib.Path | None = None) -> list[dict]:
        return real_build(agent, base_dir=tmp_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rb, "build_tool_list_for_request", _build_with_base)
        rb.run_experiment(
            cfg=cfg,
            benchmarks_root=benchmarks_root,
            pricing_dir=FIXTURE_PRICING_DIR,
            dry_run=True,
            max_samples=None,
            allow_dirty=True,
        )

    runs = sorted((bench_dir / "runs").glob("*.json"))
    assert runs, "dry-run must still emit a skeleton record"
    for run in runs:
        rec = json.loads(run.read_text(encoding="utf-8"))
        assert rec["tool_loop_terminated"] == "dry_run_skeleton", (
            f"dry-run skeleton must carry the explicit sentinel value; "
            f"got tool_loop_terminated={rec.get('tool_loop_terminated')!r}"
        )
        # And the dead 'not_applicable' literal MUST NOT reappear.
        assert rec["tool_loop_terminated"] != "not_applicable"


# ----------------------------------------------------------------------------
# Task 017: live tool-loop body coverage.
# ----------------------------------------------------------------------------
#
# The five test cases below exercise ``_live_tool_loop_call`` directly via a
# fake ``responses`` namespace so we get deterministic coverage without the
# YAML/CLI machinery. Each fake drives one specific termination shape:
#
#   * normal (model emits a final answer after one tool call) — already
#     covered end-to-end above via ``test_live_tool_loop_dispatches_tool_calls``;
#     we re-cover it at the helper level here so the per-iteration trajectory
#     schema and the usage-summation invariant get explicit assertions.
#   * iteration cap (model emits an unbounded function-call sequence) — the
#     loop must fire ``max_tool_iterations`` model calls plus one
#     tools-less recovery call and record ``tool_loop_terminated="iteration_cap"``.
#   * tool exception recovery (the registered callable raises) — the model
#     must receive the exception message as the tool result and the cell
#     must still complete.
#
# All three share one usage payload so the summation arithmetic is trivial
# to assert by hand.


_TOOL_LOOP_USAGE_PAYLOAD: dict = {
    "input_tokens": 100,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 50,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 150,
}


class _TLFakeOutputItem:
    """Stand-in for the SDK's ``ResponseFunctionToolCall`` / message items.

    Carries a ``model_dump`` accessor so :func:`_extract_output_items`
    converts it to the runner-facing dict shape used by the loop.
    """

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self) -> dict:
        return dict(self._payload)


class _TLFakeResponse:
    def __init__(self, usage: dict, output_items: list, output_text: str = "") -> None:
        self.usage = _FakeUsage(usage)
        self.output_text = output_text
        self.output = output_items


class _ScriptedResponses:
    """Replays a fixed list of pre-built fake responses, one per call.

    The script length must match (or exceed) the number of calls the loop
    is expected to issue. Each script entry is a callable
    ``(call_kwargs) -> _TLFakeResponse`` so a test can decide what the
    fake should emit based on what was sent (e.g. whether ``tools=`` was
    omitted on the cap-recovery call).
    """

    def __init__(
        self,
        script: list,
    ) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> _TLFakeResponse:
        idx = len(self.calls)
        self.calls.append(dict(kwargs))
        if idx >= len(self._script):
            raise AssertionError(
                f"fake responses exhausted at call #{idx}; "
                f"loop issued more calls than scripted"
            )
        entry = self._script[idx]
        return entry(kwargs)


class _TLFakeClient:
    def __init__(self, script: list) -> None:
        self.responses = _ScriptedResponses(script)


def _make_function_call_item(name: str, args_json: str, call_id: str) -> _TLFakeOutputItem:
    return _TLFakeOutputItem(
        {
            "type": "function_call",
            "name": name,
            "arguments": args_json,
            "call_id": call_id,
        }
    )


def _run_loop_helper(
    client: object,
    *,
    max_iterations: int,
    base_call_kwargs: dict | None = None,
) -> tuple:
    """Drive :func:`_live_tool_loop_call` directly with a fake client.

    Returns the full 5-tuple ``(summed_usage, final_text, retry_count,
    trajectory, terminated)`` so each test can assert against its slice.
    """
    import asyncio

    base = dict(base_call_kwargs or {"model": "test-deployment", "input": "do math"})
    coro = rb._live_tool_loop_call(
        client=client,
        base_call_kwargs=base,
        tool_list_for_request=[
            {
                "type": "function",
                "name": "calculator",
                "description": "calc",
                "parameters": {
                    "type": "object",
                    "properties": {"expr": {"type": "string"}},
                    "required": ["expr"],
                },
            }
        ],
        max_iterations=max_iterations,
        search_kb_path=None,
        cell_id="tool-loop-test",
    )
    return asyncio.run(coro)


def test_tool_loop_normal_termination_helper_level() -> None:
    """One function_call leg → one final-answer leg → terminated='ok'.

    Pinned at the helper level (no YAML / CLI shim) so the trajectory
    schema and usage-summation are unambiguous.
    """
    script = [
        lambda _kw: _TLFakeResponse(
            _TOOL_LOOP_USAGE_PAYLOAD,
            [_make_function_call_item("calculator", '{"expr": "1+1"}', "c-1")],
            "",
        ),
        lambda _kw: _TLFakeResponse(_TOOL_LOOP_USAGE_PAYLOAD, [], "2"),
    ]
    client = _TLFakeClient(script)
    summed_usage, final_text, _retries, trajectory, terminated = _run_loop_helper(
        client, max_iterations=4
    )

    assert terminated == "ok"
    assert final_text == "2"
    # Exactly two model calls (function_call leg + final-answer leg).
    assert len(client.responses.calls) == 2
    # One dispatched tool → one trajectory row. The final-answer leg is
    # NOT appended on normal termination (the spec reserves the
    # tool_name=None marker for the cap-recovery leg).
    assert len(trajectory) == 1
    row = trajectory[0]
    assert row["tool_name"] == "calculator"
    assert row["tool_args"] == {"expr": "1+1"}
    assert row["tool_result_summary"] == "2"
    # Usage summed across BOTH legs (function_call + final-answer).
    assert summed_usage["input_tokens"] == 2 * _TOOL_LOOP_USAGE_PAYLOAD["input_tokens"]
    assert summed_usage["output_tokens"] == 2 * _TOOL_LOOP_USAGE_PAYLOAD["output_tokens"]


def test_tool_loop_iteration_cap_forces_final_answer() -> None:
    """Unbounded tool-call sequence → loop hits cap, fires recovery call.

    Asserts:
      * ``tool_loop_terminated == "iteration_cap"``.
      * Total model invocations == ``max_iterations + 1`` (the +1 is the
        forced final-answer call).
      * The cap-recovery call is invoked WITHOUT ``tools=`` so the model
        physically cannot emit another function_call.
      * The cap-recovery leg is appended as a trajectory row with
        ``tool_name is None`` and ``tool_args is None``.
    """
    final_answer_text = "best guess: 42"

    def _unbounded_call(_kw: dict) -> _TLFakeResponse:
        # Every call returns a function_call so the loop never naturally
        # terminates.
        return _TLFakeResponse(
            _TOOL_LOOP_USAGE_PAYLOAD,
            [_make_function_call_item("calculator", '{"expr": "1+1"}', "c-?")],
            "",
        )

    def _recovery_call(kw: dict) -> _TLFakeResponse:
        # Cap-recovery leg: ``tools=`` MUST be absent on this call so the
        # model is forced to produce a final answer.
        assert "tools" not in kw, (
            "cap-recovery call must omit ``tools=``; got kwargs with tools="
            f"{kw.get('tools')!r}"
        )
        return _TLFakeResponse(_TOOL_LOOP_USAGE_PAYLOAD, [], final_answer_text)

    max_iter = 3
    script = [_unbounded_call] * max_iter + [_recovery_call]
    client = _TLFakeClient(script)
    summed_usage, final_text, _retries, trajectory, terminated = _run_loop_helper(
        client, max_iterations=max_iter
    )

    assert terminated == "iteration_cap"
    assert final_text == final_answer_text
    # max_iter normal calls + 1 cap-recovery call.
    assert len(client.responses.calls) == max_iter + 1
    # Per Task 017 spec: cap-recovery leg appended as a trajectory row.
    cap_row = trajectory[-1]
    assert cap_row["tool_name"] is None
    assert cap_row["tool_args"] is None
    assert cap_row["iteration"] == max_iter + 1
    assert cap_row["tool_result_summary"] == final_answer_text
    # The non-cap rows still record the dispatched tool calls.
    for row in trajectory[:-1]:
        assert row["tool_name"] == "calculator"
        assert row["iteration"] <= max_iter
    # Usage summed across all (max_iter + 1) calls.
    assert summed_usage["input_tokens"] == (max_iter + 1) * _TOOL_LOOP_USAGE_PAYLOAD["input_tokens"]


def test_tool_loop_tool_exception_recovery() -> None:
    """A registered callable that raises must be captured into the trajectory.

    Uses an intentionally malformed ``calculator`` expression so the
    real ``scripts.tools.calculator`` raises ``CalculatorError``. The
    runner must:
      * Capture the exception message as the tool_result_summary string
        on the trajectory row (no propagation up).
      * Feed the same message back to the model as the
        ``function_call_output``.
      * Let the model emit a final answer on the next leg, with
        ``tool_loop_terminated == "ok"``.
    """
    captured_outputs: list[str] = []

    def _bad_calculator_call(_kw: dict) -> _TLFakeResponse:
        return _TLFakeResponse(
            _TOOL_LOOP_USAGE_PAYLOAD,
            [_make_function_call_item("calculator", '{"expr": "1++"}', "c-bad")],
            "",
        )

    def _recovery_after_error(kw: dict) -> _TLFakeResponse:
        # The function_call_output the runner builds for this call must
        # carry the error string so the model has a chance to recover.
        items = kw.get("input") or []
        for it in items:
            if isinstance(it, dict) and it.get("type") == "function_call_output":
                captured_outputs.append(str(it.get("output", "")))
        return _TLFakeResponse(_TOOL_LOOP_USAGE_PAYLOAD, [], "sorry, I cannot")

    client = _TLFakeClient([_bad_calculator_call, _recovery_after_error])
    summed_usage, final_text, _retries, trajectory, terminated = _run_loop_helper(
        client, max_iterations=4
    )

    assert terminated == "ok"
    assert final_text == "sorry, I cannot"
    assert len(trajectory) == 1
    err_row = trajectory[0]
    assert err_row["tool_name"] == "calculator"
    # The summary must reflect the captured CalculatorError text, NOT a
    # blank string and NOT a raised exception bubbling up.
    summary = err_row["tool_result_summary"]
    assert isinstance(summary, str) and summary, "exception summary must be non-empty"
    assert "error" in summary.lower(), (
        f"exception summary should mention 'error'; got {summary!r}"
    )
    # And the same string was fed back as the function_call_output.
    assert captured_outputs, "runner must feed the error back to the model"
    assert "error" in captured_outputs[0].lower()
    # Usage still sums across both legs.
    assert summed_usage["input_tokens"] == 2 * _TOOL_LOOP_USAGE_PAYLOAD["input_tokens"]


def test_tool_loop_trajectory_schema_required_keys() -> None:
    """Every trajectory row matches the Task 010 schema exactly.

    The schema (per Task 017 Success Criteria, line 89-92) is the *exact*
    key set ``{"iteration", "tool_name", "tool_args",
    "tool_result_summary", "latency_ms", "usage"}``. No extra keys are
    permitted: the Task 017 Codex review explicitly rejected the runner's
    earlier ``tool_call_id`` audit field as an extra key that broke the
    cell-JSON contract analyzers downstream rely on. Two row variants are
    valid:

      * Normal tool-dispatch rows — ``tool_name`` is a non-empty string,
        ``tool_args`` is an object (the parsed JSON arguments).
      * Cap-recovery row (iteration_cap path only) — ``tool_name`` and
        ``tool_args`` are both ``null`` (no tool was dispatched on the
        forced final-answer leg).

    Both variants are encoded as ``oneOf`` in the JSON Schema below with
    ``additionalProperties: false`` so a future regression that appends
    e.g. a ``tool_call_id`` audit field would fail validation, not slip
    through with a permissive set-difference check.
    """
    normal_row_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "iteration",
            "tool_name",
            "tool_args",
            "tool_result_summary",
            "latency_ms",
            "usage",
        ],
        "properties": {
            "iteration": {"type": "integer", "minimum": 1},
            "tool_name": {"type": "string", "minLength": 1},
            "tool_args": {"type": "object"},
            "tool_result_summary": {"type": "string"},
            "latency_ms": {"type": "number"},
            "usage": {
                "type": "object",
                "required": [
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "input_tokens_details",
                    "output_tokens_details",
                ],
            },
        },
    }
    cap_recovery_row_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "iteration",
            "tool_name",
            "tool_args",
            "tool_result_summary",
            "latency_ms",
            "usage",
        ],
        "properties": {
            "iteration": {"type": "integer", "minimum": 1},
            "tool_name": {"type": "null"},
            "tool_args": {"type": "null"},
            "tool_result_summary": {"type": "string"},
            "latency_ms": {"type": "number"},
            "usage": {
                "type": "object",
                "required": [
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "input_tokens_details",
                    "output_tokens_details",
                ],
            },
        },
    }
    trajectory_row_schema = {
        "oneOf": [normal_row_schema, cap_recovery_row_schema],
    }
    validator = jsonschema.Draft202012Validator(trajectory_row_schema)

    # --- Case 1: normal termination produces only normal-variant rows. ----
    # Drive a 3-iteration trajectory: calc → calc → final answer. Two
    # dispatched-tool rows + zero cap-recovery rows = 2 rows total.
    def _calc_call(_kw: dict) -> _TLFakeResponse:
        return _TLFakeResponse(
            _TOOL_LOOP_USAGE_PAYLOAD,
            [_make_function_call_item("calculator", '{"expr": "2+2"}', "c-a")],
            "",
        )

    def _final_call(_kw: dict) -> _TLFakeResponse:
        return _TLFakeResponse(_TOOL_LOOP_USAGE_PAYLOAD, [], "4")

    client = _TLFakeClient([_calc_call, _calc_call, _final_call])
    _summed, _final, _retries, trajectory, terminated = _run_loop_helper(
        client, max_iterations=4
    )

    assert terminated == "ok"
    assert len(trajectory) == 2
    for i, row in enumerate(trajectory, start=1):
        validator.validate(row)
        # The oneOf must resolve to the normal variant for these rows
        # (string tool_name + object tool_args).
        jsonschema.Draft202012Validator(normal_row_schema).validate(row)
        assert row["iteration"] == i

    # --- Case 2: iteration_cap termination appends a null-variant row. ----
    def _unbounded_call(_kw: dict) -> _TLFakeResponse:
        return _TLFakeResponse(
            _TOOL_LOOP_USAGE_PAYLOAD,
            [_make_function_call_item("calculator", '{"expr": "1+1"}', "c-?")],
            "",
        )

    def _recovery_call(_kw: dict) -> _TLFakeResponse:
        return _TLFakeResponse(_TOOL_LOOP_USAGE_PAYLOAD, [], "best guess: 42")

    cap_client = _TLFakeClient([_unbounded_call, _unbounded_call, _recovery_call])
    _u2, _f2, _r2, cap_trajectory, cap_terminated = _run_loop_helper(
        cap_client, max_iterations=2
    )

    assert cap_terminated == "iteration_cap"
    # 2 normal tool rows + 1 cap-recovery row.
    assert len(cap_trajectory) == 3
    for row in cap_trajectory:
        validator.validate(row)
    # The first two rows must satisfy the normal variant; the last MUST
    # satisfy the cap-recovery variant (tool_name=null, tool_args=null).
    jsonschema.Draft202012Validator(normal_row_schema).validate(cap_trajectory[0])
    jsonschema.Draft202012Validator(normal_row_schema).validate(cap_trajectory[1])
    jsonschema.Draft202012Validator(cap_recovery_row_schema).validate(
        cap_trajectory[-1]
    )
    assert cap_trajectory[-1]["tool_name"] is None
    assert cap_trajectory[-1]["tool_args"] is None

    # --- Case 3: extra keys MUST fail validation. -------------------------
    # The whole point of switching to explicit jsonschema with
    # ``additionalProperties: false`` (versus the previous
    # ``required - set(row.keys())`` check) is that an unexpected field
    # like ``tool_call_id`` now triggers a hard failure instead of slipping
    # through. Exercise that contract directly on a known-good row.
    polluted_normal = dict(trajectory[0])
    polluted_normal["tool_call_id"] = "extra-field-not-in-schema"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(polluted_normal)

    polluted_cap = dict(cap_trajectory[-1])
    polluted_cap["tool_call_id"] = None
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(polluted_cap)


def test_tool_loop_usage_summation_invariant_three_iterations() -> None:
    """The cell-level summed_usage equals the per-iteration sum.

    Drives a 3-iteration trajectory with **distinct** per-call usage
    values so the assertion catches a "single iteration's usage was
    forwarded as-is" regression (which would otherwise be invisible
    when every call carries the same payload).
    """
    payloads = [
        {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 120,
        },
        {
            "input_tokens": 200,
            "input_tokens_details": {"cached_tokens": 50},
            "output_tokens": 40,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 240,
        },
        {
            "input_tokens": 300,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 80,
            "output_tokens_details": {"reasoning_tokens": 5},
            "total_tokens": 380,
        },
    ]

    def _make_step(payload: dict, *, emit_final: bool):
        def _emit(_kw: dict) -> _TLFakeResponse:
            if emit_final:
                return _TLFakeResponse(payload, [], "done")
            return _TLFakeResponse(
                payload,
                [_make_function_call_item("calculator", '{"expr": "1+1"}', "c-s")],
                "",
            )

        return _emit

    script = [
        _make_step(payloads[0], emit_final=False),
        _make_step(payloads[1], emit_final=False),
        _make_step(payloads[2], emit_final=True),
    ]
    client = _TLFakeClient(script)
    summed_usage, final_text, _retries, trajectory, terminated = _run_loop_helper(
        client, max_iterations=5
    )

    assert terminated == "ok"
    assert final_text == "done"
    # Two dispatched-tool rows.
    assert len(trajectory) == 2

    # Per-iteration usage on the trajectory rows is the per-call payload
    # the model returned on that iteration. The cell-level summed_usage
    # must equal the sum of per-call payloads across ALL three legs
    # (including the final-answer leg whose usage is summed but whose
    # row is not appended on normal termination).
    expected_input = sum(p["input_tokens"] for p in payloads)
    expected_output = sum(p["output_tokens"] for p in payloads)
    expected_cached = sum(p["input_tokens_details"]["cached_tokens"] for p in payloads)
    expected_reasoning = sum(
        p["output_tokens_details"]["reasoning_tokens"] for p in payloads
    )

    assert summed_usage["input_tokens"] == expected_input
    assert summed_usage["output_tokens"] == expected_output
    assert summed_usage["total_tokens"] == expected_input + expected_output
    assert (
        summed_usage["input_tokens_details"]["cached_tokens"] == expected_cached
    )
    assert (
        summed_usage["output_tokens_details"]["reasoning_tokens"]
        == expected_reasoning
    )

    # Per-iteration row usage matches the per-call payload one-for-one.
    assert trajectory[0]["usage"]["input_tokens"] == payloads[0]["input_tokens"]
    assert trajectory[1]["usage"]["input_tokens"] == payloads[1]["input_tokens"]

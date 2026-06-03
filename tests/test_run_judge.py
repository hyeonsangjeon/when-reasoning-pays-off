"""Tests for the Task 010 additive run_judge.py extensions."""

from __future__ import annotations

import json
import pathlib


from scripts.run_judge import (
    JUDGE_PROMPT_TEMPLATE,
    JUDGE_PROMPT_TEMPLATE_WITH_TOOLS,
    JudgeTask,
    build_judge_tasks,
    judge_prompt_sha256,
    judge_prompt_sha256_with_tools,
    parse_judge_response_with_tools,
    render_judge_prompt_with_tools,
    write_judge_record,
)


def test_original_sha_is_unchanged() -> None:
    """The pre-Task-010 SHA value must remain frozen — benchmarks 01/02
    judge JSONs reference it and must not require regeneration."""
    # This value matches the SHA committed to bench 01/02 judge JSONs.
    assert (
        judge_prompt_sha256()
        == "fb997c90fe0778681194809793cdce218bcb9d6537304a525f2916cf29ba6942"
    )


def test_tool_aware_sha_is_distinct() -> None:
    assert judge_prompt_sha256() != judge_prompt_sha256_with_tools()


def test_tool_aware_template_preserves_correctness_rubric() -> None:
    """The 0|1|2 correctness rubric block must be byte-identical in the
    tool-aware template (the only addition is the tool_efficiency_score
    block + tool_calls/expected_tool_calls fields)."""
    rubric_block = "Score on this 0|1|2 scale:\n- 0 = fail (the rubric requirement is not met)\n- 1 = partial (some rubric requirements met, some not)\n- 2 = pass (every rubric requirement met)"
    assert rubric_block in JUDGE_PROMPT_TEMPLATE
    assert rubric_block in JUDGE_PROMPT_TEMPLATE_WITH_TOOLS


def test_parse_response_rejects_missing_tef() -> None:
    """parse_judge_response_with_tools must REQUIRE tool_efficiency_score."""
    txt = '{"score": 2, "rationale": "ok"}'
    assert parse_judge_response_with_tools(txt) is None


def test_parse_response_accepts_well_formed() -> None:
    txt = '{"score": 2, "tool_efficiency_score": 0.85, "rationale": "ok"}'
    parsed = parse_judge_response_with_tools(txt)
    assert parsed is not None
    score, tef, rationale = parsed
    assert score == 2
    assert tef == 0.85
    assert rationale == "ok"


def test_parse_response_rejects_out_of_range_tef() -> None:
    for bad in (-0.01, 1.01, 5.0, -1.0):
        txt = f'{{"score": 2, "tool_efficiency_score": {bad}, "rationale": "ok"}}'
        assert parse_judge_response_with_tools(txt) is None, f"failed to reject {bad}"


def test_parse_response_rounds_two_decimals() -> None:
    txt = '{"score": 2, "tool_efficiency_score": 0.123456, "rationale": "ok"}'
    parsed = parse_judge_response_with_tools(txt)
    assert parsed is not None
    assert parsed[1] == 0.12  # rounded to 2 decimals


def test_render_template_with_tools_includes_trajectory() -> None:
    out = render_judge_prompt_with_tools(
        "rubric text",
        '"input text"',
        "response text",
        [{"tool_name": "calculator", "tool_args": {"expr": "1+1"}, "tool_result_summary": "2"}],
        ["calculator"],
    )
    assert "calculator" in out
    assert "tool_efficiency_score" in out
    assert "rubric text" in out


def test_write_judge_record_with_tef(tmp_path: pathlib.Path) -> None:
    """write_judge_record persists tool_efficiency_score when supplied."""
    target = tmp_path / "judge_test.json"
    task = JudgeTask(
        source_run_path=str(tmp_path / "src.json"),
        sample_id="tu_07",
        model="gpt-5.2",
        effort="low",
        repeat=0,
        rubric="rubric",
        sample_input='"input"',
        response_text="answer",
        target_path=target,
        tool_calls=[{"tool_name": "calculator", "tool_args": {}, "tool_result_summary": "ok"}],
        expected_tool_calls=["calculator"],
    )
    write_judge_record(
        task,
        score=2,
        rationale="ok",
        judge_model="gpt-4o",
        raw_response=None,
        timestamp_utc="2026-05-24T01:00:00Z",
        git_commit="HEAD",
        tool_efficiency_score=0.85,
    )
    d = json.loads(target.read_text(encoding="utf-8"))
    assert d["score"] == 2
    assert d["tool_efficiency_score"] == 0.85
    assert d["judge_prompt_sha256"] == judge_prompt_sha256_with_tools()


def test_write_judge_record_without_tef(tmp_path: pathlib.Path) -> None:
    """write_judge_record omits tool_efficiency_score when None — preserves
    bench 01/02 byte-identical judge JSON contract."""
    target = tmp_path / "judge_test.json"
    task = JudgeTask(
        source_run_path=str(tmp_path / "src.json"),
        sample_id="sf_01",
        model="gpt-4o",
        effort=None,
        repeat=0,
        rubric="rubric",
        sample_input='"input"',
        response_text="answer",
        target_path=target,
    )
    write_judge_record(
        task,
        score=2,
        rationale="ok",
        judge_model="gpt-4o",
        raw_response=None,
        timestamp_utc="2026-05-24T01:00:00Z",
        git_commit="HEAD",
        tool_efficiency_score=None,
    )
    d = json.loads(target.read_text(encoding="utf-8"))
    assert d["score"] == 2
    assert "tool_efficiency_score" not in d
    assert d["judge_prompt_sha256"] == judge_prompt_sha256()


def test_build_judge_tasks_propagates_tool_calls(tmp_path: pathlib.Path) -> None:
    """When a measurement JSON carries a tool_calls list, build_judge_tasks
    must propagate it onto the JudgeTask so the tool-aware prompt fires."""
    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    runs_dir.mkdir()
    judge_dir.mkdir()
    # Measurement JSON with tool_calls
    (runs_dir / "001_gpt-5.2_low_r0.json").write_text(
        json.dumps(
            {
                "experiment_id": "exp003_benchmark03_gpt5_2",
                "sample_id": "tu_15",
                "model": "gpt-5.2",
                "effort": "low",
                "repeat": 0,
                "response_text": "101550",
                "tool_calls": [
                    {"tool_name": "web_search", "tool_args": {"query": "x"}, "tool_result_summary": "812400"}
                ],
            }
        ),
        encoding="utf-8",
    )
    dataset = [
        {
            "id": "tu_15",
            "input": "test",
            "quality_rubric_notes": "rubric",
            "expected_tool_calls": ["web_search", "calculator"],
        }
    ]
    tasks = build_judge_tasks(
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset=dataset,
        experiment_prefix="exp003_benchmark03",
    )
    assert len(tasks) == 1
    t = tasks[0]
    assert t.tool_calls is not None
    assert len(t.tool_calls) == 1
    assert t.expected_tool_calls == ["web_search", "calculator"]


def test_build_judge_tasks_no_tool_calls_preserved(tmp_path: pathlib.Path) -> None:
    """When a measurement JSON has NO tool_calls (benchmarks 01/02 shape),
    the JudgeTask's tool_calls stays None — the judge uses the original
    template byte-identically."""
    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    runs_dir.mkdir()
    judge_dir.mkdir()
    (runs_dir / "001_gpt-4o_null_r0.json").write_text(
        json.dumps(
            {
                "experiment_id": "exp001_short-factual_baseline",
                "sample_id": "sf_01",
                "model": "gpt-4o",
                "effort": None,
                "repeat": 0,
                "response_text": "answer",
            }
        ),
        encoding="utf-8",
    )
    dataset = [{"id": "sf_01", "input": "test", "quality_rubric_notes": "rubric"}]
    tasks = build_judge_tasks(
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset=dataset,
        experiment_prefix="exp001",
    )
    assert len(tasks) == 1
    assert tasks[0].tool_calls is None
    assert tasks[0].expected_tool_calls is None


def test_build_judge_tasks_empty_tool_calls_propagated(tmp_path: pathlib.Path) -> None:
    """Regression: a benchmark-03 no-tool cell writes ``tool_calls: []`` on
    the measurement JSON when the model legitimately declined to invoke any
    tool. ``build_judge_tasks`` must still propagate the empty list (NOT
    coerce it to ``None``) so the tool-aware judge prompt fires and the
    resulting judge JSON carries ``tool_efficiency_score``. The gating
    signal is KEY PRESENCE on the measurement JSON, not list length.

    Together with ``test_build_judge_tasks_no_tool_calls_preserved`` this
    pins the bug fix from the Task 010 correction-round 2 review: the
    pre-fix code coerced empty lists to ``None``, dropping every no-tool
    benchmark-03 cell out of the tool-aware judge path."""
    runs_dir = tmp_path / "runs"
    judge_dir = tmp_path / "judge_runs"
    runs_dir.mkdir()
    judge_dir.mkdir()
    # Measurement JSON with tool_calls=[] (no-tool benchmark-03 cell)
    (runs_dir / "001_gpt-5.2_low_r0.json").write_text(
        json.dumps(
            {
                "experiment_id": "exp003_benchmark03_gpt5_2",
                "sample_id": "tu_03",
                "model": "gpt-5.2",
                "effort": "low",
                "repeat": 0,
                "response_text": "answer without tools",
                "tool_calls": [],
            }
        ),
        encoding="utf-8",
    )
    dataset = [
        {
            "id": "tu_03",
            "input": "test (no tool needed)",
            "quality_rubric_notes": "rubric",
            "expected_tool_calls": None,
        }
    ]
    tasks = build_judge_tasks(
        runs_dir=runs_dir,
        judge_dir=judge_dir,
        dataset=dataset,
        experiment_prefix="exp003_benchmark03",
    )
    assert len(tasks) == 1
    t = tasks[0]
    # The empty list must survive — NOT be coerced to None.
    assert t.tool_calls is not None, (
        "empty tool_calls=[] must be propagated, not coerced to None — "
        "otherwise the no-tool benchmark-03 cells get judged on the "
        "non-tool template and emit no tool_efficiency_score"
    )
    assert t.tool_calls == []
    # No-tool dataset row → expected_tool_calls stays None.
    assert t.expected_tool_calls is None

"""tests/test_analyze_max_output_tokens_sweep.py — Task 019 v2.1
protocol-correction tests for the analyzer.

Covers:
- ``evaluate_smoke_gate`` returns the same verdict whether the summary
  already carries the runner-written gate block or has to recompute from
  ``cell_summaries`` + ``sweep_planned`` (backwards compatibility for
  smoke summaries written before the protocol-correction hotfix).
- ``render_markdown`` emits a verdict header ("SMOKE GATE: **FAIL**" /
  "PASS") and the ``n_429`` column never shows None for a count.
- ``--require-gate-pass`` CLI exits non-zero on smoke FAIL.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import analyze_max_output_tokens_sweep as A  # noqa: E402


SWEEP = [256, 512, 1024, 2048, 4096, 8192, 16384]


def _cell(mo: int, n_429: int = 0, **extras) -> dict:
    base = {
        "max_output_tokens": mo,
        "n_429_records": n_429,
        "n_records": 30,
        "warm_criterion_passed": True,
        "backlog_excessive": False,
        "visible_output_tokens_p50_steady_state": 500.0,
        "visible_output_tokens_p95_steady_state": 600.0,
        "reasoning_tokens_p50_steady_state": 0.0,
        "cache_hit_ratio_steady_state": 0.6,
        "cell_usd": 0.25,
    }
    base.update(extras)
    return base


def _summary(stage: str, cells: list[dict], gate_block: dict | None = None) -> dict:
    out = {
        "stage": stage,
        "cell_summaries": cells,
        "sweep_planned": SWEEP,
        "cells_completed": len(cells),
        "cells_planned": len(SWEEP),
        "total_usd": 1.5,
        "hard_ceiling_usd": 4.0,
        "midrun_threshold_usd": 3.4,
        "partial": False,
        "halt_reason": None,
        "backlog_excessive_any": False,
        "cache_not_warm_any": False,
        "max_in_flight_observed_run": 4,
        "first_429_arrival_rpm_per_cell": {
            str(c["max_output_tokens"]):
                None if c["n_429_records"] == 0 else 10.0
            for c in cells
        },
        "n_429_records_per_cell": {
            str(c["max_output_tokens"]): int(c["n_429_records"])
            for c in cells
        },
    }
    if gate_block is not None:
        key = "smoke_gate" if stage == "smoke" else "evidence_429_contrast_gate"
        out[key] = gate_block
    return out


class TestEvaluateSmokeGate:

    def test_uses_runner_written_block_when_present(self):
        cells = [_cell(mo, n_429=0) for mo in SWEEP]
        gate = {
            "passed": False,
            "reason": "no_429_in_largest_cell",
            "largest_cell_max_output_tokens": 16384,
            "largest_cell_n_429": 0,
            "smallest_cell_max_output_tokens": 256,
            "smallest_cell_n_429": 0,
            "cells_completed": 7,
            "cells_planned": 7,
            "stage2_promotable": False,
        }
        s = _summary("smoke", cells, gate_block=gate)
        assert A.evaluate_smoke_gate(s) == gate

    def test_recomputes_when_block_missing_smoke(self):
        cells = [_cell(mo, n_429=0) for mo in SWEEP]
        s = _summary("smoke", cells, gate_block=None)
        result = A.evaluate_smoke_gate(s)
        assert result["passed"] is False
        assert result["reason"] == "no_429_in_largest_cell"
        assert result["stage2_promotable"] is False

    def test_recomputes_when_block_missing_evidence(self):
        cells = [_cell(mo, n_429=0) for mo in SWEEP]
        s = _summary("evidence", cells, gate_block=None)
        result = A.evaluate_smoke_gate(s)
        assert result["passed"] is False

    def test_pass_path_recomputed(self):
        cells = []
        for mo in SWEEP:
            cells.append(_cell(mo, n_429=(2 if mo == 16384 else 0)))
        s = _summary("smoke", cells, gate_block=None)
        result = A.evaluate_smoke_gate(s)
        assert result["passed"] is True
        assert result["stage2_promotable"] is True


class TestRenderMarkdown:

    def test_fail_summary_emits_fail_verdict(self):
        cells = [_cell(mo, n_429=0) for mo in SWEEP]
        s = _summary("smoke", cells, gate_block=None)
        md = A.render_markdown(s)
        assert "SMOKE GATE" in md
        assert "FAIL" in md
        assert "no_429_in_largest_cell" in md
        assert "do NOT promote to Stage 2" in md

    def test_pass_summary_emits_pass_verdict(self):
        cells = []
        for mo in SWEEP:
            cells.append(_cell(mo, n_429=(2 if mo == 16384 else 0)))
        s = _summary("smoke", cells, gate_block=None)
        md = A.render_markdown(s)
        assert "SMOKE GATE" in md
        assert "PASS" in md
        assert "do NOT promote to Stage 2" not in md

    def test_n_429_column_present_and_never_none(self):
        cells = [_cell(mo, n_429=0) for mo in SWEEP]
        s = _summary("smoke", cells, gate_block=None)
        md = A.render_markdown(s)
        assert "| cap | n_429 | first-429 RPM | warm | backlog excess |" in md
        # The literal string "None" must never appear in the table body —
        # 0 (count) renders as "0"; missing RPM renders as "—".
        # We check by scanning the lines that begin with "| 16384 |" etc.
        for cap in SWEEP:
            row_prefix = f"| {cap} | "
            row_lines = [ln for ln in md.splitlines() if ln.startswith(row_prefix)]
            assert row_lines, f"no markdown row for cap={cap}"
            for ln in row_lines:
                assert "None" not in ln, f"None in row: {ln}"

    def test_evidence_stage_label(self):
        cells = [_cell(mo, n_429=0) for mo in SWEEP]
        s = _summary("evidence", cells, gate_block=None)
        md = A.render_markdown(s)
        assert "EVIDENCE 429-CONTRAST GATE" in md


class TestRequireGatePassCLI:

    def test_real_smoke_summary_exits_3(self, tmp_path):
        """The real Stage-1 smoke summary triggers exit 3 under
        ``--require-gate-pass`` because the largest cell observed 0 429s.
        """
        smoke = (
            REPO_ROOT
            / "benchmarks/07-max-output-tokens-reservation/runs"
            / "20260529T160517Z_exp007_max_output_tokens_sweep_smoke.jsonl.summary.json"
        )
        if not smoke.is_file():
            pytest.skip(f"real smoke summary not present at {smoke}")
        out_dir = tmp_path / "figures"
        result = subprocess.run(
            [
                sys.executable,
                "-m", "scripts.analyze_max_output_tokens_sweep",
                "--summary", str(smoke),
                "--out-dir", str(out_dir),
                "--require-gate-pass",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 3, (
            f"expected exit 3, got {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        assert "no_429_in_largest_cell" in result.stderr
        assert "FAIL" in result.stdout

"""D-011 Invariant 2 — supplementary-stats reproducibility check (T-064).

This is the byte-for-byte regenerate-into-tmp-and-diff verifier that the
``repro-check.yml`` workflow runs for D-011 Invariant 2. It regenerates the
three supplementary statistics artifacts for each scoped benchmark into a
throwaway temp directory and asserts they match the committed copies under
``results/supplementary/<benchmark>/`` byte for byte:

* ``bootstrap_ci.json``  — :mod:`scripts.stats.bootstrap_ci` (T-021)
* ``cohens_d.json``      — :mod:`scripts.stats.cohens_d` (T-022)
* ``inter_rater.json``   — :mod:`scripts.stats.inter_rater` (T-023)

Scope and safety
----------------
* The benchmark set is **fixed** to the three authored cohorts
  (``01-short-factual``, ``02-multi-step-reasoning``, ``03-tool-using-agent``);
  the wrapper never defaults to "every benchmark with a runs/ dir".
* Only ``--output-dir`` is redirected to the temp directory. The canonical
  ``--benchmarks-dir`` / ``--pricing-dir`` defaults are kept so the inputs are
  the committed audit evidence.
* The wrapper **never writes into ``results/``**. As a guard, it captures the
  ``git status --porcelain`` of the results tree before and after the run and
  fails if the run introduced any new dirt there.
* :mod:`scripts.stats.inter_rater` also discovers manual spot-check scores from
  ``--output-dir``. If a committed
  ``results/supplementary/<benchmark>/manual_spot_checks.json`` exists it is
  pre-seeded into the temp tree first, so the regenerated ``inter_rater.json``
  sees the same manual-score input the committed artifact was built with. When
  no manual scores are committed (today's state) the regenerated report is the
  honest deterministic missing-data report, and that report is compared too.

Determinism note
----------------
The bootstrap CI uses NumPy's ``Generator``/PCG64 seeded from a stable digest,
so the output is byte-stable for a fixed NumPy. The CI workflow pins NumPy to
the version the committed artifacts were generated with to avoid false drift.

Exit status
-----------
``0`` — every scoped artifact matched byte-for-byte and the run left
``results/`` untouched. ``1`` — any mismatch, missing file, or new
``results/`` dirt.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable

REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.stats import bootstrap_ci, cohens_d, inter_rater  # noqa: E402

# Fixed cohort scope. Intentionally explicit — do NOT fall back to discovering
# every benchmark with a runs/ directory (see module docstring).
DEFAULT_BENCHMARKS: tuple[str, ...] = (
    "01-short-factual",
    "02-multi-step-reasoning",
    "03-tool-using-agent",
)

# The three supplementary artifacts compared per benchmark.
ARTIFACTS: tuple[str, ...] = (
    "bootstrap_ci.json",
    "cohens_d.json",
    "inter_rater.json",
)

# Committed manual spot-check input pre-seeded into the temp tree so inter_rater
# regenerates against the same scores the committed artifact used.
MANUAL_SPOT_CHECKS: str = "manual_spot_checks.json"

Regenerator = Callable[..., None]


# ----------------------------------------------------------------------------
# Regeneration
# ----------------------------------------------------------------------------


def _run_module(module: object, argv: list[str]) -> None:
    rc = module.main(argv)  # type: ignore[attr-defined]
    if rc != 0:
        name = getattr(module, "__name__", str(module))
        raise RuntimeError(f"{name}.main exited {rc} (argv={argv})")


def default_regenerate(
    benchmarks: tuple[str, ...] | list[str],
    *,
    benchmarks_dir: pathlib.Path,
    pricing_dir: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    """Regenerate all three artifacts for ``benchmarks`` into ``output_dir``.

    Only ``--output-dir`` is redirected; ``--benchmarks-dir`` and
    ``--pricing-dir`` stay canonical so the inputs are the committed evidence.
    """
    bench_args: list[str] = []
    for benchmark in benchmarks:
        bench_args += ["--benchmark", benchmark]
    common = bench_args + [
        "--benchmarks-dir",
        str(benchmarks_dir),
        "--pricing-dir",
        str(pricing_dir),
        "--output-dir",
        str(output_dir),
    ]
    _run_module(bootstrap_ci, common)
    _run_module(cohens_d, common)
    _run_module(inter_rater, common)


# ----------------------------------------------------------------------------
# Manual spot-check pre-seed
# ----------------------------------------------------------------------------


def preseed_manual_spot_checks(
    benchmarks: tuple[str, ...] | list[str],
    *,
    results_dir: pathlib.Path,
    output_dir: pathlib.Path,
) -> list[str]:
    """Copy committed ``manual_spot_checks.json`` files into the temp tree.

    Returns the list of benchmarks whose manual scores were pre-seeded (empty
    when none are committed, which is today's state).
    """
    seeded: list[str] = []
    for benchmark in benchmarks:
        src = results_dir / benchmark / MANUAL_SPOT_CHECKS
        if src.is_file():
            dst_dir = output_dir / benchmark
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst_dir / MANUAL_SPOT_CHECKS)
            seeded.append(benchmark)
    return seeded


# ----------------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactResult:
    benchmark: str
    artifact: str
    status: str  # "match" | "differ" | "missing_committed" | "missing_regenerated"

    @property
    def ok(self) -> bool:
        return self.status == "match"


def compare_artifacts(
    benchmarks: tuple[str, ...] | list[str],
    artifacts: tuple[str, ...] | list[str],
    *,
    committed_dir: pathlib.Path,
    regenerated_dir: pathlib.Path,
) -> list[ArtifactResult]:
    """Byte-compare each (benchmark, artifact) pair across the two trees."""
    results: list[ArtifactResult] = []
    for benchmark in benchmarks:
        for name in artifacts:
            committed = committed_dir / benchmark / name
            regenerated = regenerated_dir / benchmark / name
            if not committed.is_file():
                status = "missing_committed"
            elif not regenerated.is_file():
                status = "missing_regenerated"
            elif committed.read_bytes() == regenerated.read_bytes():
                status = "match"
            else:
                status = "differ"
            results.append(ArtifactResult(benchmark, name, status))
    return results


# ----------------------------------------------------------------------------
# results/ dirt guard
# ----------------------------------------------------------------------------


def results_porcelain(
    repo_root: pathlib.Path,
    results_subpath: str = "results",
) -> set[str]:
    """Return the set of ``git status --porcelain`` lines for the results tree.

    On any git failure (e.g. not a checkout) returns an empty set rather than
    raising; the dirt guard then degrades to a no-op, which is safe because the
    wrapper only ever writes into the temp directory.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", results_subpath],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {line for line in proc.stdout.splitlines() if line.strip()}


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------


@dataclass
class ReproOutcome:
    results: list[ArtifactResult]
    seeded_benchmarks: list[str]
    tmp_dir: pathlib.Path | None

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)


def check_repro(
    benchmarks: tuple[str, ...] | list[str] = DEFAULT_BENCHMARKS,
    *,
    benchmarks_dir: pathlib.Path = REPO_ROOT / "benchmarks",
    pricing_dir: pathlib.Path = REPO_ROOT / "pricing",
    results_dir: pathlib.Path = REPO_ROOT / "results" / "supplementary",
    regenerate: Regenerator = default_regenerate,
    tmp_root: pathlib.Path | None = None,
    cleanup: bool = True,
) -> ReproOutcome:
    """Pre-seed, regenerate into a temp dir, and byte-compare with committed."""
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="repro-stats-", dir=tmp_root))
    try:
        seeded = preseed_manual_spot_checks(
            benchmarks, results_dir=results_dir, output_dir=tmp_dir
        )
        regenerate(
            benchmarks,
            benchmarks_dir=benchmarks_dir,
            pricing_dir=pricing_dir,
            output_dir=tmp_dir,
        )
        results = compare_artifacts(
            benchmarks,
            ARTIFACTS,
            committed_dir=results_dir,
            regenerated_dir=tmp_dir,
        )
    finally:
        if cleanup:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir = None  # type: ignore[assignment]
    return ReproOutcome(
        results=results, seeded_benchmarks=seeded, tmp_dir=tmp_dir
    )


_STATUS_LABEL: dict[str, str] = {
    "match": "MATCH",
    "differ": "DIFFER",
    "missing_committed": "MISSING(committed)",
    "missing_regenerated": "MISSING(regenerated)",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_repro",
        description=(
            "D-011 Invariant 2: regenerate the supplementary stats artifacts "
            "into a temp dir and verify they byte-match the committed "
            "results/supplementary/<benchmark>/*.json. Never writes into "
            "results/."
        ),
    )
    parser.add_argument(
        "--benchmark",
        action="append",
        dest="benchmarks",
        metavar="NAME",
        help=(
            "Benchmark to check. Repeatable. Default: the three authored "
            "cohorts (01-short-factual, 02-multi-step-reasoning, "
            "03-tool-using-agent)."
        ),
    )
    parser.add_argument(
        "--benchmarks-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "benchmarks",
        help="Canonical benchmark input dir (default: ./benchmarks).",
    )
    parser.add_argument(
        "--pricing-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "pricing",
        help="Canonical pricing snapshot dir (default: ./pricing).",
    )
    parser.add_argument(
        "--results-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "results" / "supplementary",
        help=(
            "Committed supplementary stats root to compare against "
            "(default: ./results/supplementary)."
        ),
    )
    parser.add_argument(
        "--keep-tmp",
        action="store_true",
        help="Keep the regenerated temp dir instead of deleting it (debug).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    benchmarks = tuple(args.benchmarks) if args.benchmarks else DEFAULT_BENCHMARKS

    if not args.benchmarks_dir.is_dir():
        parser.error(f"benchmarks dir not found: {args.benchmarks_dir}")
    if not args.results_dir.is_dir():
        parser.error(f"results dir not found: {args.results_dir}")

    # Capture the results-tree dirt baseline so the guard reacts only to dirt
    # this run introduces (CI checkout is always clean, so this is empty there).
    before = results_porcelain(REPO_ROOT)

    outcome = check_repro(
        benchmarks,
        benchmarks_dir=args.benchmarks_dir,
        pricing_dir=args.pricing_dir,
        results_dir=args.results_dir,
        cleanup=not args.keep_tmp,
    )

    after = results_porcelain(REPO_ROOT)
    new_dirt = sorted(after - before)

    sys.stdout.write(
        f"[check_repro] benchmarks: {', '.join(benchmarks)}\n"
        f"[check_repro] manual spot-checks pre-seeded: "
        f"{', '.join(outcome.seeded_benchmarks) or 'none (deterministic missing-data report)'}\n"
    )
    for r in outcome.results:
        sys.stdout.write(
            f"  {_STATUS_LABEL[r.status]:<22} {r.benchmark}/{r.artifact}\n"
        )

    failed = [r for r in outcome.results if not r.ok]
    status = 0
    if failed:
        status = 1
        sys.stdout.write(
            f"::error title=Invariant 2::{len(failed)} supplementary stats "
            "artifact(s) did not reproduce byte-for-byte.\n"
        )
    if new_dirt:
        status = 1
        sys.stdout.write(
            "::error title=Invariant 2::the repro run modified results/ "
            "(it must only write into a temp dir):\n"
        )
        for line in new_dirt:
            sys.stdout.write(f"  {line}\n")

    if status == 0:
        sys.stdout.write(
            f"[check_repro] OK: {len(outcome.results)} artifacts reproduced "
            "byte-for-byte; results/ untouched.\n"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())

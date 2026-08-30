"""Official offline-first ``reasoning-payoff`` command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from importlib import resources
from pathlib import Path

from batch_runner import __version__
from batch_runner.contracts import InputValidationError
from batch_runner.experiment.dataset import DatasetError
from batch_runner.experiment.ledger import LedgerError
from batch_runner.experiment.record import BudgetNotConfirmedError, ProviderError
from batch_runner.experiment.runner import ExperimentOutputConflict
from batch_runner.privacy import PrivacyViolation
from batch_runner.reporting import (
    OutputConflictError,
    ReportValidationError,
    analyze_files,
    load_report,
    write_report_bundle,
)

_EXPERIMENT_INPUT_ERRORS = (LedgerError, DatasetError)
_BUDGET_ERRORS = (BudgetNotConfirmedError,)
_PROVIDER_ERRORS = (ProviderError,)
_EXPERIMENT_OUTPUT_ERRORS = ExperimentOutputConflict

_INIT_RESOURCES = {
    "sample_usage.jsonl": "usage.jsonl",
    "sample_workload.yaml": "workload.yaml",
    "sample_pricing.yaml": "pricing.yaml",
    "sample_ptu_pricing.yaml": "ptu-pricing.yaml",
    "sample_density.yaml": "density.yaml",
}

# Per-provider ledger shipped by `sample init`; the chosen one becomes
# ledger.yaml in the workspace. The dataset and config are provider-neutral.
_SAMPLE_LEDGERS = {
    "ollama": "ledger.ollama.yaml",
    "mock": "ledger.mock.yaml",
    "azure": "ledger.azure.yaml",
}
_SAMPLE_DATA_RESOURCES = {
    "sample.jsonl": "sample.jsonl",
    "sample.json": "sample.json",
    "config.env.example": ".env.example",
}


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _RedactingArgumentParser(
        prog="reasoning-payoff",
        description=(
            "Analyze recorded usage offline, or run a small, explicitly "
            "selected model sample. The analyze/report path makes no live "
            "service calls; `sample run` makes one real call only when you "
            "select a live provider (Ollama or Azure)."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_RedactingArgumentParser,
    )

    init = subparsers.add_parser("init", help="Write a credential-free sample")
    init.add_argument(
        "--out",
        default=".reasoning-payoff",
        metavar="DIR",
        help="Empty output directory (default: .reasoning-payoff)",
    )

    analyze = subparsers.add_parser(
        "analyze",
        help="Analyze UsageEnvelope JSONL and emit four offline artifacts",
    )
    analyze.add_argument("usage_jsonl", metavar="USAGE_JSONL")
    analyze.add_argument("--workload", required=True, metavar="WORKLOAD_YAML")
    analyze.add_argument("--out", required=True, metavar="DIR")

    report = subparsers.add_parser(
        "report",
        help="Deterministically re-render a pinned report.json",
    )
    report.add_argument("report_json", metavar="REPORT_JSON")
    report.add_argument(
        "--out",
        metavar="DIR",
        help="Output directory (default: the report.json directory)",
    )

    _add_experiment_parser(subparsers)
    _add_sample_parser(subparsers)
    return parser


def _add_experiment_parser(subparsers: argparse._SubParsersAction) -> None:
    experiment = subparsers.add_parser(
        "experiment",
        help="Browse the catalog of committed benchmark experiments",
        description=(
            "Read-only catalog of the repository's committed experiments "
            "(exp001-exp007 and their smoke probes). Shows the DATA -> IN -> "
            "EXECUTE -> OUT view for each. These are full Azure-billed sweeps; "
            "to make one small real call yourself, use `sample run` instead."
        ),
    )
    exp_sub = experiment.add_subparsers(
        dest="experiment_command",
        required=True,
        parser_class=_RedactingArgumentParser,
    )
    exp_list = exp_sub.add_parser("list", help="List every catalogued experiment")
    exp_list.add_argument(
        "--json", action="store_true", help="Emit the full catalog as JSON"
    )
    exp_desc = exp_sub.add_parser(
        "describe", help="Show DATA/IN/EXECUTE/OUT for one experiment"
    )
    exp_desc.add_argument("target", metavar="ID_OR_FILE")
    exp_desc.add_argument(
        "--json", action="store_true", help="Emit the entry as JSON"
    )


def _add_sample_parser(subparsers: argparse._SubParsersAction) -> None:
    sample = subparsers.add_parser(
        "sample",
        help="Do a small real model run (Ollama/Azure) or an offline preview",
        description=(
            "Run a handful of committed public prompts against a real model so "
            "you can confirm the DATA -> IN -> EXECUTE -> OUT flow end to end. "
            "This is an illustrative live sample, NOT the published benchmark: "
            "there is no quality judge and no reasoning-effort sweep."
        ),
    )
    sample_sub = sample.add_subparsers(
        dest="sample_command",
        required=True,
        parser_class=_RedactingArgumentParser,
    )
    init = sample_sub.add_parser(
        "init", help="Copy a ledger, dataset, and .env.example into a workspace"
    )
    init.add_argument(
        "--provider",
        choices=sorted(_SAMPLE_LEDGERS),
        default="ollama",
        help="Which ledger template to install as ledger.yaml (default: ollama)",
    )
    init.add_argument(
        "--out",
        default="sample-workspace",
        metavar="DIR",
        help="Empty output directory (default: sample-workspace)",
    )
    run = sample_sub.add_parser(
        "run", help="Execute a ledger.yaml into a new immutable run"
    )
    retry = sample_sub.add_parser(
        "retry-failed",
        help="Create a child run that calls only failed parent attempts",
    )
    retry.add_argument(
        "--parent-run-id",
        required=True,
        metavar="RUN_ID",
        help="Immutable parent run ID under out/runs/",
    )
    doctor = sample_sub.add_parser(
        "doctor",
        help="Diagnose package, workspace, lock, and Ollama runtime health",
    )
    doctor.add_argument(
        "--ledger",
        default="ledger.yaml",
        metavar="LEDGER_YAML",
        help="Ledger in the sample workspace (default: ledger.yaml)",
    )
    doctor.add_argument(
        "--repair-stale-lock",
        action="store_true",
        help="Repair only a proven same-host stale lock and owned staging dirs",
    )
    doctor.add_argument(
        "--allow-remote-ollama",
        action="store_true",
        help="Permit diagnosis to contact a non-local Ollama endpoint explicitly",
    )
    doctor.add_argument(
        "--json", action="store_true", help="Emit the doctor result as JSON"
    )
    for command in (run, retry):
        command.add_argument(
            "--ledger",
            default="ledger.yaml",
            metavar="LEDGER_YAML",
            help="Ledger describing the run (default: ledger.yaml)",
        )
        command.add_argument(
            "--confirm-cost",
            action="store_true",
            help="Acknowledge Azure billing. Required for a billed provider.",
        )
        command.add_argument(
            "--allow-remote-ollama",
            action="store_true",
            help="Permit a non-localhost Ollama base URL (off by default).",
        )
        command.add_argument(
            "--json", action="store_true", help="Emit the run summary as JSON"
        )
    return


def _init_sample(out_dir: Path) -> None:
    out_dir = out_dir.resolve()
    if out_dir.exists() and not out_dir.is_dir():
        raise OutputConflictError("init output exists and is not a directory")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise OutputConflictError("init output directory must be empty")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage: Path | None = Path(
        tempfile.mkdtemp(
            prefix=f".{out_dir.name}.",
            suffix=".tmp",
            dir=out_dir.parent,
        )
    )
    try:
        data_root = resources.files("batch_runner.data")
        for resource_name, output_name in _INIT_RESOURCES.items():
            assert stage is not None
            target = stage / output_name
            with target.open("wb") as handle:
                handle.write(data_root.joinpath(resource_name).read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
        if out_dir.exists():
            out_dir.rmdir()
        assert stage is not None
        os.replace(stage, out_dir)
        stage = None
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


def _write_resource(target: Path, resource: object) -> None:
    with target.open("wb") as handle:
        handle.write(resource.read_bytes())  # type: ignore[attr-defined]
        handle.flush()
        os.fsync(handle.fileno())


def _write_text_resource(target: Path, text: str) -> None:
    with target.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _init_sample_workspace(out_dir: Path, provider: str) -> None:
    out_dir = out_dir.resolve()
    if out_dir.exists() and not out_dir.is_dir():
        raise OutputConflictError("init output exists and is not a directory")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise OutputConflictError("init output directory must be empty")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    ledger_resource = _SAMPLE_LEDGERS[provider]
    stage: Path | None = Path(
        tempfile.mkdtemp(
            prefix=f".{out_dir.name}.", suffix=".tmp", dir=out_dir.parent
        )
    )
    try:
        data_root = resources.files("batch_runner.experiment.resources")
        assert stage is not None
        _write_resource(stage / "ledger.yaml", data_root.joinpath(ledger_resource))
        for resource_name, output_name in _SAMPLE_DATA_RESOURCES.items():
            _write_resource(stage / output_name, data_root.joinpath(resource_name))
        # A workspace-local .gitignore so a cloner's run outputs and any local
        # .env never get committed, regardless of the chosen workspace name.
        _write_text_resource(
            stage / ".gitignore",
            "# Created by `reasoning-payoff sample init`.\n"
            "# Immutable sample runs live under out/runs/.\n"
            "out/\n"
            ".env\n"
            ".reasoning-payoff-sample.lock\n"
            ".reasoning-payoff-sample-repairs.jsonl\n"
            "*.tmp\n",
        )
        if out_dir.exists():
            out_dir.rmdir()
        os.replace(stage, out_dir)
        stage = None
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


def _cmd_experiment_list(as_json: bool) -> int:
    from batch_runner.experiment.catalog import (  # noqa: PLC0415
        load_packaged_catalog,
        render_list,
    )

    catalog = load_packaged_catalog()
    if as_json:
        print(json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_list(catalog))
    return 0


def _cmd_experiment_describe(target: str, as_json: bool) -> int:
    from batch_runner.experiment.catalog import (  # noqa: PLC0415
        find_entry,
        load_packaged_catalog,
        render_entry,
    )

    catalog = load_packaged_catalog()
    entry = find_entry(catalog, target)
    if entry is None:
        candidates = [
            e["experiment_id"]
            for e in catalog["experiments"]
            if e["experiment_id"].startswith(target)
        ]
        if candidates:
            joined = "\n  ".join(candidates)
            print(
                f"input error: {target!r} matches several experiments; "
                f"pick one:\n  {joined}",
                file=sys.stderr,
            )
        else:
            print(
                f"input error: no experiment matches {target!r}", file=sys.stderr
            )
        return 3
    if as_json:
        print(json.dumps(entry, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_entry(entry))
    return 0


def _cmd_sample_run(args: argparse.Namespace) -> int:
    from batch_runner.experiment.ledger import load_ledger  # noqa: PLC0415
    from batch_runner.experiment.runner import (  # noqa: PLC0415
        retry_failed_run,
        run_ledger,
    )

    ledger_path = Path(args.ledger).resolve()
    # Bounded, value-free loader: a malformed or oversized YAML fails with a
    # documented LedgerError (mapped to exit 3), never a traceback or path leak.
    ledger = load_ledger(ledger_path)
    base_dir = ledger_path.parent

    # Show the exact request count before running, so "a small run" is concrete.
    planned_requests = ledger.execution.max_samples * ledger.execution.repeats
    print(
        f"plan: up to {planned_requests} request(s) "
        f"({ledger.execution.max_samples} row(s) x {ledger.execution.repeats} "
        f"repeat(s)) via provider {ledger.provider!r}",
        file=sys.stderr,
    )

    def _show_preflight(plan: object) -> None:
        print(plan.plan_line(), file=sys.stderr)  # type: ignore[attr-defined]

    run_kwargs = {
        "base_dir": base_dir,
        "allow_remote_ollama": args.allow_remote_ollama,
        "confirm_cost": args.confirm_cost,
        "preflight_sink": _show_preflight,
    }
    if args.sample_command == "retry-failed":
        result = retry_failed_run(
            ledger,
            parent_run_id=args.parent_run_id,
            **run_kwargs,
        )
    else:
        result = run_ledger(ledger, **run_kwargs)
    relative_run_dir = f"{ledger.output.dir}/runs/{result.run_id}"
    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "run_id": result.run_id,
                    "out_dir": relative_run_dir,
                    "ok_count": result.ok_count,
                    "error_count": result.error_count,
                    "run_json": f"{relative_run_dir}/run.json",
                    "records": f"{relative_run_dir}/records.jsonl",
                    "summary": f"{relative_run_dir}/summary.md",
                    "manifest": f"{relative_run_dir}/manifest.json",
                    "artifacts_sha256": (
                        f"{relative_run_dir}/artifacts.sha256"
                    ),
                    "latest": f"{ledger.output.dir}/latest.json",
                    "answer_preview": result.answer_preview,
                    "failures": [
                        {
                            "row_id": f.row_id,
                            "repeat_index": f.repeat_index,
                            "error_type": f.error_type,
                        }
                        for f in result.failures
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(
            f"{result.status}: {result.ok_count} ok, {result.error_count} failed"
        )
        print(f"run: {result.run_id}")
        print(f"output: {relative_run_dir}")
        if result.answer_preview:
            print(f"preview: {result.answer_preview}")
        if result.failures:
            print("failures:", file=sys.stderr)
            for failure in result.failures:
                print(
                    f"  {failure.row_id}#{failure.repeat_index}: "
                    f"{failure.error_type}",
                    file=sys.stderr,
                )
    return result.exit_code


def _cmd_sample_doctor(args: argparse.Namespace) -> int:
    from batch_runner.experiment.doctor import diagnose_workspace  # noqa: PLC0415

    result = diagnose_workspace(
        Path(args.ledger),
        repair_stale_lock=args.repair_stale_lock,
        allow_remote_ollama=args.allow_remote_ollama,
    )
    if args.json:
        print(json.dumps(result.payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(result.render())
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "experiment":
            if args.experiment_command == "list":
                return _cmd_experiment_list(args.json)
            return _cmd_experiment_describe(args.target, args.json)
        if args.command == "sample":
            if args.sample_command == "init":
                _init_sample_workspace(Path(args.out), args.provider)
                print(
                    f"Initialized a {args.provider} sample workspace in "
                    f"{args.out}. Edit ledger.yaml if needed, then run: "
                    f"reasoning-payoff sample run --ledger {args.out}/ledger.yaml"
                )
                return 0
            if args.sample_command == "doctor":
                return _cmd_sample_doctor(args)
            return _cmd_sample_run(args)
        if args.command == "init":
            _init_sample(Path(args.out))
            print(
                "Initialized usage, workload, PAYG, PTU, and density snapshots. "
                "Run: reasoning-payoff analyze usage.jsonl "
                "--workload workload.yaml --out report"
            )
            return 0
        if args.command == "analyze":
            report = analyze_files(
                Path(args.usage_jsonl),
                Path(args.workload),
            )
            write_report_bundle(
                report,
                Path(args.out),
                allow_existing_generated=False,
            )
            print("Created report.json, report.md, report.html, and policy.json")
            return 0
        pinned = load_report(Path(args.report_json))
        out_dir = (
            Path(args.out)
            if args.out is not None
            else Path(args.report_json).resolve().parent
        )
        write_report_bundle(
            pinned,
            out_dir,
            allow_existing_generated=True,
        )
        print("Re-rendered report.json, report.md, report.html, and policy.json")
        return 0
    except _EXPERIMENT_INPUT_ERRORS as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 3
    except InputValidationError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 3
    except _BUDGET_ERRORS as exc:
        print(f"cost error: {exc}", file=sys.stderr)
        return 7
    except _PROVIDER_ERRORS as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 7
    except PrivacyViolation as exc:
        print(f"privacy error: {exc}", file=sys.stderr)
        return 4
    except (OutputConflictError, ReportValidationError, _EXPERIMENT_OUTPUT_ERRORS) as exc:
        print(f"report error: {exc}", file=sys.stderr)
        return 5
    except OSError:
        print("I/O error: operation could not be completed", file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())

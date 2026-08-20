"""Official offline-first ``reasoning-payoff`` command."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from importlib import resources
from pathlib import Path

from batch_runner import __version__
from batch_runner.contracts import InputValidationError
from batch_runner.privacy import PrivacyViolation
from batch_runner.reporting import (
    OutputConflictError,
    ReportValidationError,
    analyze_files,
    load_report,
    write_report_bundle,
)

_INIT_RESOURCES = {
    "sample_usage.jsonl": "usage.jsonl",
    "sample_workload.yaml": "workload.yaml",
    "sample_pricing.yaml": "pricing.yaml",
    "sample_ptu_pricing.yaml": "ptu-pricing.yaml",
    "sample_density.yaml": "density.yaml",
}


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _RedactingArgumentParser(
        prog="reasoning-payoff",
        description=(
            "Generate deterministic, privacy-safe provenance reports from "
            "local usage metadata. No live service calls."
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
    return parser


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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
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
    except InputValidationError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 3
    except PrivacyViolation as exc:
        print(f"privacy error: {exc}", file=sys.stderr)
        return 4
    except (OutputConflictError, ReportValidationError) as exc:
        print(f"report error: {exc}", file=sys.stderr)
        return 5
    except OSError:
        print("I/O error: operation could not be completed", file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())

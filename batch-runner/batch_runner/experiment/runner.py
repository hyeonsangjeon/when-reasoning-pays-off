"""Orchestration: load a ledger + dataset, execute rows, write owned artifacts.

This is the engine behind ``reasoning-payoff sample run``. It ties the four
stages together::

    DATA     load_dataset(...)          -> validated rows + sha256
    IN       load_ledger(...)           -> strict RunLedger + sha256
    EXECUTE  provider.run_row(...)      -> normalized OutputRecord per row/repeat
    OUT      write run.json/records.jsonl/summary.md into an *owned* directory

It is deliberately small and honest:

* A billed Azure run needs the ledger's confirmed budget *and* an explicit
  ``--confirm-cost`` acknowledgement before any client is built.
* Partial failures are preserved: completed rows are still written, the failing
  rows are listed, and the process exits non-zero.
* Output goes only into a directory this tool owns (marked with a sentinel
  file); it never overwrites an unrelated directory.
* The run is an *illustrative live sample*, not the published benchmark — the
  summary says so in plain words.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import stat
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

from batch_runner.experiment.cost import CostPreflight, estimate_azure_cost
from batch_runner.experiment.dataset import LoadedDataset, load_dataset, row_input_text
from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.providers.azure import FOUNDRY_AUDIENCE
from batch_runner.experiment.providers.base import (
    Provider,
    ResolvedEndpoint,
    build_provider,
    resolve_endpoint,
)
from batch_runner.experiment.record import (
    BudgetNotConfirmedError,
    OutputRecord,
    ProviderError,
)

#: Marker file that identifies a directory as owned by the experiment runner.
OWNED_MARKER_NAME = ".reasoning-payoff-experiment-owned"
_OWNED_MARKER_BYTES = b"reasoning-payoff experiment output\n"

#: Plain-words honesty tail shared by every sample banner.
SAMPLE_BANNER_TAIL = (
    "not the published benchmark; no quality judge or comparable "
    "reasoning-effort sweep"
)

#: Default banner (kept for backwards compatibility / live providers).
SAMPLE_BANNER = f"illustrative live sample — {SAMPLE_BANNER_TAIL}"


def sample_banner(provider: str) -> str:
    """Return the honesty banner appropriate to ``provider``.

    The mock provider makes no network call, so calling its output a "live
    sample" would be false. It is an offline preview instead. Both variants keep
    the same "not the published benchmark" tail so no artifact can be mistaken
    for published evidence.
    """
    lead = (
        "illustrative offline preview"
        if provider == "mock"
        else "illustrative live sample"
    )
    return f"{lead} — {SAMPLE_BANNER_TAIL}"

EXIT_OK = 0
EXIT_PARTIAL = 20
EXIT_ALL_FAILED = 21

#: Directory names that hold published benchmark evidence. Sample/preview runs
#: must never write inside these, so a real run cannot masquerade as evidence.
_EVIDENCE_DIR_NAMES = frozenset({"benchmarks", "results"})

Clock = Callable[[], float]
ProviderBuilder = Callable[[RunLedger, ResolvedEndpoint, bool], Provider]


def _reject_evidence_tree(out_dir: Path) -> None:
    """Refuse an output directory inside the published evidence tree."""
    parts = {part.lower() for part in out_dir.parts}
    if parts & _EVIDENCE_DIR_NAMES:
        raise ExperimentOutputConflict(
            "refusing to write sample output inside the published evidence "
            "tree (a path segment named 'benchmarks' or 'results'); move the "
            "sample workspace"
        )


class ExperimentOutputConflict(RuntimeError):
    """The chosen output directory exists and is not owned by this tool."""


@dataclasses.dataclass(frozen=True)
class RowFailure:
    row_id: str
    repeat_index: int
    error_type: str


@dataclasses.dataclass(frozen=True)
class RunResult:
    """The outcome of :func:`run_ledger` — where things landed and how it went."""

    status: str  # "ok" | "partial" | "failed"
    exit_code: int
    out_dir: Path
    ok_count: int
    error_count: int
    run_json_path: Path
    records_path: Path
    summary_path: Path
    failures: list[RowFailure]

    @property
    def answer_preview(self) -> str | None:
        return self._preview

    _preview: str | None = None


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _resolve_input_path(base_dir: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base_dir`` and confirm it does not escape it."""
    base = base_dir.resolve()
    candidate = (base / rel).resolve()
    if base != candidate and base not in candidate.parents:
        raise ExperimentOutputConflict("input path escapes the workspace directory")
    return candidate


def _select_output_dir(
    ledger: RunLedger, *, base_dir: Path, out_dir: Path | None
) -> Path:
    base = Path(os.path.abspath(base_dir))
    if base.is_symlink() or not base.is_dir():
        raise ExperimentOutputConflict(
            "sample workspace must be a real directory, not a symlink"
        )
    declared = base / ledger.output.dir
    if out_dir is None:
        return declared
    supplied = out_dir if out_dir.is_absolute() else base / out_dir
    if Path(os.path.abspath(supplied)) != declared:
        raise ExperimentOutputConflict(
            "sample output is fixed to the workspace 'out' directory"
        )
    return declared


def _claim_output_dir(
    out_dir: Path, artifacts: tuple[str, str, str]
) -> dict[Path, tuple[int, int]]:
    """Create/verify an owned output directory, refusing foreign non-empty dirs."""
    marker_valid = False
    if out_dir.is_symlink():
        raise ExperimentOutputConflict("output path is not a real directory")
    if out_dir.exists():
        if not out_dir.is_dir():
            raise ExperimentOutputConflict("output path is not a real directory")
        marker = out_dir / OWNED_MARKER_NAME
        if marker.is_file() and not marker.is_symlink():
            try:
                if marker.read_bytes() == _OWNED_MARKER_BYTES:
                    marker_valid = True
                else:
                    raise ExperimentOutputConflict("output directory marker is invalid")
            except OSError:
                raise ExperimentOutputConflict(
                    "output directory marker could not be read"
                ) from None
        if not marker_valid and any(out_dir.iterdir()):
            raise ExperimentOutputConflict(
                "output directory exists and is not owned by this tool; "
                "remove it or initialize a fresh sample workspace"
            )
    else:
        out_dir.mkdir(parents=True, exist_ok=False)
    marker = out_dir / OWNED_MARKER_NAME
    if not marker_valid:
        with marker.open("xb") as handle:
            handle.write(_OWNED_MARKER_BYTES)
            handle.flush()
            os.fsync(handle.fileno())
    allowed = {OWNED_MARKER_NAME, *artifacts}
    for child in out_dir.iterdir():
        if child.name not in allowed:
            raise ExperimentOutputConflict(
                "owned output directory contains an unexpected path"
            )
        if child.name != OWNED_MARKER_NAME and (
            child.is_symlink() or not child.is_file()
        ):
            raise ExperimentOutputConflict(
                "an output artifact path is not a regular file"
            )
    created: dict[Path, tuple[int, int]] = {}
    for artifact_name in artifacts:
        artifact = out_dir / artifact_name
        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        was_missing = not artifact.exists()
        if was_missing:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(artifact, flags, 0o600)
            artifact_stat = os.fstat(fd)
            if not stat.S_ISREG(artifact_stat.st_mode):
                raise ExperimentOutputConflict(
                    "an output artifact path is not a regular file"
                )
            if was_missing:
                created[artifact] = (artifact_stat.st_dev, artifact_stat.st_ino)
        except OSError as exc:
            raise ExperimentOutputConflict(
                "an output artifact path is not writable"
            ) from exc
        finally:
            if "fd" in locals():
                os.close(fd)
                del fd
    probe = out_dir / f".reasoning-payoff-write-probe-{secrets.token_hex(8)}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(probe, flags, 0o600)
        os.write(fd, b"probe")
        os.fsync(fd)
    except OSError as exc:
        raise ExperimentOutputConflict("output directory is not writable") from exc
    finally:
        if "fd" in locals():
            os.close(fd)
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
    return created


def _remove_unchanged_reservations(
    reservations: dict[Path, tuple[int, int]],
) -> None:
    for path, identity in reservations.items():
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISREG(current.st_mode)
            and current.st_size == 0
            and (current.st_dev, current.st_ino) == identity
        ):
            path.unlink()


@contextmanager
def _exclusive_run_lock(base_dir: Path) -> Iterator[None]:
    lock_path = base_dir / ".reasoning-payoff-sample.lock"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except FileExistsError:
        raise ExperimentOutputConflict(
            "another sample run holds the workspace output lock"
        ) from None
    locked = os.fstat(fd)
    try:
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        try:
            current = lock_path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == (locked.st_dev, locked.st_ino):
            lock_path.unlink()


def _hold_workspace_output_lock(
    func: Callable[..., RunResult],
) -> Callable[..., RunResult]:
    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> RunResult:
        ledger = args[0] if args else kwargs["ledger"]
        base_dir = kwargs["base_dir"]
        selected = _select_output_dir(
            ledger, base_dir=base_dir, out_dir=kwargs.get("out_dir")
        )
        kwargs["out_dir"] = selected
        with _exclusive_run_lock(selected.parent):
            reservations = _claim_output_dir(selected, ledger.output.artifacts)
            try:
                return func(*args, **kwargs)
            except BaseException:
                _remove_unchanged_reservations(reservations)
                raise

    return wrapped


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via an unpredictable, no-follow temp file.

    A predictable ``*.tmp`` name opened with a normal ``open`` follows a symlink
    an attacker could plant, letting a write escape the owned directory. Instead
    we create a fresh same-directory temp file with an unguessable name using
    ``O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`` (so an existing symlink at that name
    is refused, not followed), then atomically ``os.replace`` it onto ``path``.
    """
    directory = path.parent
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(8):
        tmp = directory / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:  # pragma: no cover - name collision is rare
            continue
        break
    else:  # pragma: no cover - exhausting 8 random names is effectively impossible
        raise ExperimentOutputConflict("could not create a temp file for output")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _endpoint_identity(
    ledger: RunLedger, endpoint: ResolvedEndpoint
) -> dict[str, Any]:
    """Return a discriminated, secret-free endpoint description for artifacts.

    Azure's resolved host is deliberately omitted: only the env-var *name*, the
    deployment (model), the auth mode, and the audience are recorded. Ollama's
    localhost base URL is safe to record; the mock has none.
    """
    if ledger.provider == "azure":
        return {
            "provider": "azure",
            "endpoint_env_var": ledger.endpoint.env_var,
            "endpoint_source": endpoint.source,
            "deployment": ledger.model,
            "auth_mode": ledger.auth.mode,
            "auth_env_vars": list(ledger.auth.env_vars),
            "audience": FOUNDRY_AUDIENCE,
        }
    if ledger.provider == "ollama":
        identity: dict[str, Any] = {
            "provider": "ollama",
            "endpoint_env_var": ledger.endpoint.env_var,
            "endpoint_source": endpoint.source,
            "model": ledger.model,
            "locality": "local" if endpoint.is_local else "remote",
            "remote_opt_in": not endpoint.is_local,
        }
        # A localhost base URL is safe to record; a remote (opt-in) URL is not
        # persisted at all — only its env-var source and locality class are.
        if endpoint.is_local:
            identity["base_url"] = endpoint.base_url
        return identity
    return {"provider": "mock", "model": ledger.model}


def run_ledger(
    ledger: RunLedger,
    *,
    base_dir: Path,
    out_dir: Path | None = None,
    environ: dict[str, str] | None = None,
    allow_remote_ollama: bool = False,
    confirm_cost: bool = False,
    clock: Clock | None = None,
    provider_builder: ProviderBuilder | None = None,
    preflight_sink: Callable[[CostPreflight], None] | None = None,
) -> RunResult:
    """Execute ``ledger`` end to end and write owned artifacts.

    Args:
        base_dir: Sample workspace containing the ledger and input data.
        out_dir: Optional explicit workspace ``out`` path. Other paths are refused.
        environ: Environment mapping for endpoint resolution (defaults to os).
        allow_remote_ollama: Permit a non-localhost Ollama endpoint (opt-in).
        confirm_cost: The explicit CLI acknowledgement for a billed run.
        clock: Injectable wall clock (epoch seconds) for deterministic tests.
        provider_builder: Injectable provider factory for tests.
        preflight_sink: Optional callback invoked with the conservative Azure
            cost plan *before* the run is authorized, so a caller can display
            the planned request count/estimate/ceiling.

    Raises:
        DatasetError, LedgerError, ProviderError, ExperimentOutputConflict,
        BudgetNotConfirmedError: On a global (pre-write) failure.
    """
    env = os.environ if environ is None else environ
    now = time.time if clock is None else clock
    build = provider_builder or (
        lambda lg, ep, cap: build_provider(lg, ep, capture_io=cap)
    )

    # --- DATA ---------------------------------------------------------------
    input_path = _resolve_input_path(base_dir, ledger.input.path)
    dataset = load_dataset(input_path, ledger.input)
    rows = dataset.selected(
        selector=ledger.input.sample_selector, limit=ledger.execution.max_samples
    )
    prompts = [row_input_text(row) for row in rows]

    # --- OUT (claimed first): refuse the evidence tree and lock an owned dir
    # BEFORE resolving an endpoint, building a provider, or making any billable
    # call. A foreign/unwritable output directory fails here with zero calls.
    resolved_out = _select_output_dir(
        ledger, base_dir=base_dir, out_dir=out_dir
    )
    _reject_evidence_tree(resolved_out)
    _claim_output_dir(resolved_out, ledger.output.artifacts)

    # --- IN: cost gate + conservative pre-flight before anything is reached --
    preflight: CostPreflight | None = None
    if ledger.provider == "azure":
        if not (ledger.execution.cost.confirmed and confirm_cost):
            raise BudgetNotConfirmedError(
                "billed Azure run requires both execution.cost.confirmed in the "
                "ledger and the explicit --confirm-cost flag"
            )
        preflight = estimate_azure_cost(ledger, prompts)
        if preflight_sink is not None:
            preflight_sink(preflight)
        if not preflight.within_ceiling:
            raise BudgetNotConfirmedError(
                "refusing billed Azure run: the conservative pre-flight "
                f"estimate (${preflight.estimated_usd:.4f} over "
                f"{preflight.planned_requests} request(s)) exceeds "
                f"execution.cost.hard_ceiling_usd "
                f"(${preflight.hard_ceiling_usd:.2f}); lower max_samples/repeats/"
                "max_output_tokens or raise the ceiling"
            )

    # --- EXECUTE: resolve endpoint, prepare provider (may abort globally) ----
    endpoint = resolve_endpoint(
        ledger, environ=env, allow_remote=allow_remote_ollama
    )
    provider = build(ledger, endpoint, ledger.execution.capture_io)
    provider.prepare()

    started = now()
    records: list[OutputRecord] = []
    failures: list[RowFailure] = []
    for row in rows:
        row_id = str(row.get("id"))
        prompt = row_input_text(row)
        for repeat_index in range(ledger.execution.repeats):
            record = _execute_one(
                provider, row_id, repeat_index, prompt, model=ledger.model
            )
            records.append(record)
            if not record.ok:
                failures.append(
                    RowFailure(
                        row_id=row_id,
                        repeat_index=repeat_index,
                        error_type=record.error_type or "provider_error",
                    )
                )
    ended = now()

    ok_count = sum(1 for r in records if r.ok)
    error_count = len(records) - ok_count
    if error_count == 0:
        status, exit_code = "ok", EXIT_OK
    elif ok_count == 0:
        status, exit_code = "failed", EXIT_ALL_FAILED
    else:
        status, exit_code = "partial", EXIT_PARTIAL

    preview = _answer_preview(records)
    run_json = _build_run_json(
        ledger=ledger,
        endpoint=endpoint,
        dataset=dataset,
        provider=provider,
        records=records,
        failures=failures,
        status=status,
        started=started,
        ended=ended,
        selected=len(rows),
        preflight=preflight,
    )
    records_path = resolved_out / "records.jsonl"
    run_json_path = resolved_out / "run.json"
    summary_path = resolved_out / "summary.md"

    _atomic_write_text(
        records_path,
        "".join(json.dumps(r.to_json(), ensure_ascii=False) + "\n" for r in records),
    )
    _atomic_write_text(
        run_json_path, json.dumps(run_json, indent=2, ensure_ascii=False) + "\n"
    )
    _atomic_write_text(
        summary_path,
        _build_summary_md(run_json, preview=preview, records_name="records.jsonl"),
    )

    return RunResult(
        status=status,
        exit_code=exit_code,
        out_dir=resolved_out,
        ok_count=ok_count,
        error_count=error_count,
        run_json_path=run_json_path,
        records_path=records_path,
        summary_path=summary_path,
        failures=failures,
        _preview=preview,
    )


run_ledger = _hold_workspace_output_lock(run_ledger)


def _execute_one(
    provider: Provider, row_id: str, repeat_index: int, prompt: str, *, model: str
) -> OutputRecord:
    wall_start = time.monotonic()
    try:
        record = provider.run_row(row_id, repeat_index, prompt)
    except ProviderError as exc:
        wall_ms = int((time.monotonic() - wall_start) * 1000)
        return OutputRecord(
            row_id=row_id,
            repeat_index=repeat_index,
            provider=provider.name,
            model=model,
            status="error",
            latency_ms=wall_ms,
            error_type=exc.error_type,
            error_detail=str(exc),
        )
    # Fill latency for providers that do not self-time (azure); keep server- or
    # synthetically-timed values (ollama/mock) as they are more accurate.
    if record.provider == "azure" and record.latency_ms == 0:
        wall_ms = int((time.monotonic() - wall_start) * 1000)
        record = dataclasses.replace(record, latency_ms=wall_ms)
    return record


def _answer_preview(records: list[OutputRecord]) -> str | None:
    for record in records:
        if record.ok and isinstance(record.response_text, str):
            text = " ".join(record.response_text.split())
            if len(text) > 200:
                text = text[:200] + "…"
            return text
    return None


def _agg_int(values: list[int | None]) -> dict[str, int] | None:
    present = [v for v in values if isinstance(v, int)]
    if not present:
        return None
    return {"sum": sum(present), "count": len(present)}


def _build_run_json(
    *,
    ledger: RunLedger,
    endpoint: ResolvedEndpoint,
    dataset: LoadedDataset,
    provider: Provider,
    records: list[OutputRecord],
    failures: list[RowFailure],
    status: str,
    started: float,
    ended: float,
    selected: int,
    preflight: CostPreflight | None = None,
) -> dict[str, Any]:
    ok_records = [r for r in records if r.ok]
    cost_block: dict[str, Any] = {
        "billed": ledger.execution.cost.billed,
        "confirmed": ledger.execution.cost.confirmed,
        "estimated_usd": ledger.execution.cost.estimated_usd,
        "hard_ceiling_usd": ledger.execution.cost.hard_ceiling_usd,
    }
    if preflight is not None:
        cost_block["preflight"] = preflight.to_json()
    return {
        "schema_version": ledger.schema_version,
        "banner": sample_banner(ledger.provider),
        "method": {
            "method_id": ledger.provenance.method_id,
            "method_version": ledger.provenance.method_version,
        },
        "experiment": {
            "id": ledger.experiment.id,
            "purpose": ledger.experiment.purpose,
        },
        "provider": ledger.provider,
        "model": ledger.model,
        "capabilities": provider.capabilities().to_json(),
        "endpoint": _endpoint_identity(ledger, endpoint),
        "ledger_sha256": ledger.sha256(),
        "input": {
            "path": ledger.input.path,
            "format": ledger.input.format,
            "sha256": dataset.sha256,
            "total_records": dataset.total_records,
            "selected_records": selected,
            "repeats": ledger.execution.repeats,
        },
        "execution": {
            "max_samples": ledger.execution.max_samples,
            "concurrency": ledger.execution.concurrency,
            "timeout_seconds": ledger.execution.timeout_seconds,
            "max_output_tokens": ledger.execution.max_output_tokens,
            "reasoning_effort": ledger.execution.reasoning_effort,
            "capture_io": ledger.execution.capture_io,
        },
        "cost": cost_block,
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "status": status,
        "counts": {
            "total": len(records),
            "ok": len(ok_records),
            "error": len(records) - len(ok_records),
        },
        "usage": {
            "input_tokens": _agg_int([r.input_tokens for r in ok_records]),
            "output_tokens": _agg_int([r.output_tokens for r in ok_records]),
            "reasoning_tokens": _agg_int([r.reasoning_tokens for r in ok_records]),
            "cached_tokens": _agg_int([r.cached_tokens for r in ok_records]),
        },
        "latency_ms": _agg_int([r.latency_ms for r in ok_records]),
        "failures": [
            {
                "row_id": f.row_id,
                "repeat_index": f.repeat_index,
                "error_type": f.error_type,
            }
            for f in failures
        ],
        "artifacts": {
            "run_json": "run.json",
            "records_jsonl": "records.jsonl",
            "summary_md": "summary.md",
        },
    }


def _build_summary_md(
    run_json: dict[str, Any], *, preview: str | None, records_name: str
) -> str:
    counts = run_json["counts"]
    cost = run_json["cost"]
    lines = [
        f"# Sample run — {run_json['experiment']['id']}",
        "",
        f"> {run_json['banner']}",
        "",
        f"- provider: **{run_json['provider']}**  model: **{run_json['model']}**",
        f"- status: **{run_json['status']}**  "
        f"(ok {counts['ok']} / error {counts['error']} of {counts['total']})",
        f"- input: `{run_json['input']['path']}` "
        f"({run_json['input']['format']}, {run_json['input']['total_records']} rows, "
        f"sha256 `{run_json['input']['sha256'][:12]}…`)",
        f"- ledger sha256: `{run_json['ledger_sha256'][:12]}…`",
        f"- started: {run_json['started_at']}  ended: {run_json['ended_at']}",
    ]
    billed = "billed" if cost["billed"] else "no cloud cost"
    lines.append(
        f"- cost boundary: **{billed}** "
        f"(estimated ${cost['estimated_usd']:.2f}, ceiling ${cost['hard_ceiling_usd']:.2f})"
    )
    caps = run_json["capabilities"]
    lines.append(
        f"- provider capabilities: token_usage={caps['token_usage']}, "
        f"reasoning_tokens={caps['reasoning_tokens']}, "
        f"cached_tokens={caps['cached_tokens']}"
    )
    lines.append("")
    if preview is not None:
        lines += ["## Answer preview (first row)", "", f"> {preview}", ""]
    else:
        lines += [
            "## Answer preview",
            "",
            "_Response text capture is off for this run; enable "
            "`execution.capture_io` to save answers._",
            "",
        ]
    if run_json["failures"]:
        lines += ["## Failures", ""]
        for failure in run_json["failures"]:
            lines.append(
                f"- row `{failure['row_id']}` (repeat {failure['repeat_index']}): "
                f"{failure['error_type']}"
            )
        lines.append("")
    lines += [
        "## Where the data is",
        "",
        f"- per-row records: `{records_name}`",
        "- full run metadata: `run.json`",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "run_ledger",
    "RunResult",
    "RowFailure",
    "ExperimentOutputConflict",
    "OWNED_MARKER_NAME",
    "SAMPLE_BANNER",
    "sample_banner",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_ALL_FAILED",
]

"""Orchestration: load a ledger + dataset, execute rows, write owned artifacts.

This is the engine behind ``reasoning-payoff sample run``. It ties the four
stages together::

    DATA     load_dataset(...)          -> validated rows + sha256
    IN       load_ledger(...)           -> strict RunLedger + sha256
    EXECUTE  provider.run_row(...)      -> normalized OutputRecord per row/repeat
    OUT      publish five artifacts into a new *immutable owned* run directory

It is deliberately small and honest:

* A billed Azure run needs the ledger's confirmed budget *and* an explicit
  ``--confirm-cost`` acknowledgement before any client is built.
* Partial failures are preserved: completed rows are still written, the failing
  rows are listed, and the process exits non-zero.
* Output goes only into directories this tool owns (marked with sentinel files);
  a completed run is never overwritten.
* The run is an *illustrative live sample*, not the published benchmark — the
  summary says so in plain words.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import secrets
import shutil
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from batch_runner.experiment.cost import CostPreflight, estimate_azure_cost
from batch_runner.experiment.dataset import LoadedDataset, load_dataset, row_input_text
from batch_runner.experiment.ledger import RunLedger
from batch_runner.experiment.locking import (
    OWNED_MARKER_BYTES,
    OWNED_MARKER_NAME,
    LockSafetyError,
    exclusive_run_lock,
    valid_owned_marker,
)
from batch_runner.experiment.manifest import (
    RunManifest,
    build_manifest,
    sha256_bytes,
)
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
RandomHex = Callable[[int], str]
ProviderBuilder = Callable[[RunLedger, ResolvedEndpoint, bool], Provider]
_RUN_ID_RE = re.compile(
    r"^\d{8}T\d{6}Z_[0-9a-f]{8}_[0-9a-f]{8}_[0-9a-f]{8}$"
)


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
    run_id: str
    ok_count: int
    error_count: int
    run_json_path: Path
    records_path: Path
    summary_path: Path
    manifest_path: Path
    artifacts_sha256_path: Path
    latest_path: Path
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


def _valid_owned_marker(directory: Path) -> bool:
    return valid_owned_marker(directory)


def _ensure_owned_directory(directory: Path, *, label: str) -> None:
    """Create or verify a real runner-owned directory."""
    marker_valid = False
    if directory.is_symlink():
        raise ExperimentOutputConflict(f"{label} path is not a real directory")
    if directory.exists():
        if not directory.is_dir():
            raise ExperimentOutputConflict(f"{label} path is not a real directory")
        marker_valid = _valid_owned_marker(directory)
        if not marker_valid and any(directory.iterdir()):
            raise ExperimentOutputConflict(
                f"{label} directory exists and is not owned by this tool; "
                "remove it or initialize a fresh sample workspace"
            )
    else:
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    marker = directory / OWNED_MARKER_NAME
    if not marker_valid:
        with marker.open("xb") as handle:
            handle.write(OWNED_MARKER_BYTES)
            handle.flush()
            os.fsync(handle.fileno())


def _claim_output_root(out_dir: Path) -> Path:
    """Claim the immutable output root and its owned ``runs`` directory."""
    _ensure_owned_directory(out_dir, label="output")
    allowed = {OWNED_MARKER_NAME, "runs", "latest.json"}
    for child in list(out_dir.iterdir()):
        runner_temp = (
            re.fullmatch(r"\.latest\.json\.[0-9a-f]{16}\.tmp", child.name)
            or re.fullmatch(
                r"\.reasoning-payoff-write-probe-[0-9a-f]{16}", child.name
            )
        )
        if runner_temp and (child.is_symlink() or child.is_file()):
            child.unlink()
            continue
        if child.name not in allowed:
            raise ExperimentOutputConflict(
                "owned output directory contains an unexpected path; legacy "
                "flat outputs must be moved before using immutable runs"
            )
        if child.name == "latest.json" and (child.is_symlink() or not child.is_file()):
            raise ExperimentOutputConflict(
                "latest pointer path is not a regular file"
            )
        if child.name == "latest.json":
            try:
                if child.stat().st_size > 16_384:
                    raise ValueError
                pointer = json.loads(child.read_text(encoding="utf-8"))
                run_id = pointer["run_id"]
                if (
                    set(pointer)
                    != {
                        "schema_version",
                        "run_id",
                        "run_path",
                        "status",
                        "manifest_sha256",
                    }
                    or pointer["schema_version"] != "1.0.0"
                    or not isinstance(run_id, str)
                    or not _RUN_ID_RE.fullmatch(run_id)
                    or pointer["run_path"] != f"runs/{run_id}"
                    or pointer["status"] not in {"ok", "partial", "failed"}
                    or not re.fullmatch(
                        r"[0-9a-f]{64}", pointer["manifest_sha256"]
                    )
                ):
                    raise ValueError
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError):
                raise ExperimentOutputConflict(
                    "latest pointer is invalid or unsafe"
                ) from None
    runs_dir = out_dir / "runs"
    _ensure_owned_directory(runs_dir, label="runs")
    for child in runs_dir.iterdir():
        if child.name == OWNED_MARKER_NAME:
            continue
        if child.is_symlink() or not child.is_dir():
            raise ExperimentOutputConflict("runs directory contains an unsafe path")
        if child.name.startswith("."):
            if not _valid_owned_marker(child):
                raise ExperimentOutputConflict(
                    "runs directory contains an unowned staging path"
                )
            continue
        if not _RUN_ID_RE.fullmatch(child.name) or not _valid_owned_marker(child):
            raise ExperimentOutputConflict(
                "runs directory contains an invalid or unowned run directory"
            )
        expected = {
            OWNED_MARKER_NAME,
            "run.json",
            "records.jsonl",
            "summary.md",
            "manifest.json",
            "artifacts.sha256",
        }
        children = {entry.name: entry for entry in child.iterdir()}
        if set(children) != expected:
            raise ExperimentOutputConflict(
                "an immutable run has an incomplete artifact set"
            )
        for name, artifact in children.items():
            if name != OWNED_MARKER_NAME and (
                artifact.is_symlink() or not artifact.is_file()
            ):
                raise ExperimentOutputConflict(
                    "an immutable run artifact path is not a regular file"
                )
    latest_path = out_dir / "latest.json"
    if latest_path.exists():
        try:
            pointer = json.loads(latest_path.read_text(encoding="utf-8"))
            manifest = runs_dir / pointer["run_id"] / "manifest.json"
            if (
                manifest.is_symlink()
                or not manifest.is_file()
                or sha256_bytes(manifest.read_bytes())
                != pointer["manifest_sha256"]
            ):
                raise ValueError
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            raise ExperimentOutputConflict(
                "latest pointer does not match a complete immutable run"
            ) from None
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
    return runs_dir


def _new_run_id(
    *, epoch: float, ledger_sha256: str, input_sha256: str, random_hex: RandomHex
) -> str:
    suffix = random_hex(4)
    if not re.fullmatch(r"[0-9a-f]{8}", suffix):
        raise ExperimentOutputConflict("run ID randomness source returned invalid data")
    utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))
    return f"{utc}_{ledger_sha256[:8]}_{input_sha256[:8]}_{suffix}"


def _create_run_stage(runs_dir: Path, run_id: str) -> tuple[Path, Path]:
    final = runs_dir / run_id
    if final.exists() or final.is_symlink():
        raise ExperimentOutputConflict("run ID collision; refusing to reuse a run")
    for _ in range(8):
        stage = runs_dir / f".{run_id}.staging-{secrets.token_hex(8)}"
        try:
            stage.mkdir(mode=0o700)
        except FileExistsError:
            continue
        _ensure_owned_directory(stage, label="run staging")
        return stage, final
    raise ExperimentOutputConflict("could not reserve a staging directory for the run")


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
        operation = "sample-retry" if kwargs.get("parent_run_id") else "sample-run"
        try:
            with exclusive_run_lock(
                selected.parent,
                operation=operation,
                ledger_sha256=ledger.sha256(),
            ):
                _reject_evidence_tree(selected)
                _claim_output_root(selected)
                return func(*args, **kwargs)
        except LockSafetyError as exc:
            raise ExperimentOutputConflict(str(exc)) from None

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
        _fsync_directory(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    """Make a completed rename durable before publishing a parent pointer."""
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _endpoint_identity(
    ledger: RunLedger, endpoint: ResolvedEndpoint
) -> dict[str, Any]:
    """Return a discriminated, secret-free endpoint description for artifacts.

    A resolved host is always omitted. Only the endpoint env-var *name*, source
    class, deployment/model, auth mode, and safe locality metadata are recorded.
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
    random_hex: RandomHex | None = None,
    provider_builder: ProviderBuilder | None = None,
    preflight_sink: Callable[[CostPreflight], None] | None = None,
    parent_run_id: str | None = None,
    retry_keys: set[tuple[str, int]] | None = None,
) -> RunResult:
    """Execute ``ledger`` end to end and write owned artifacts.

    Args:
        base_dir: Sample workspace containing the ledger and input data.
        out_dir: Optional explicit workspace ``out`` path. Other paths are refused.
        environ: Environment mapping for endpoint resolution (defaults to os).
        allow_remote_ollama: Permit a non-localhost Ollama endpoint (opt-in).
        confirm_cost: The explicit CLI acknowledgement for a billed run.
        clock: Injectable wall clock (epoch seconds) for deterministic tests.
        random_hex: Injectable lowercase hex source for deterministic run IDs.
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
    token_hex = secrets.token_hex if random_hex is None else random_hex
    build = provider_builder or (
        lambda lg, ep, cap: build_provider(lg, ep, capture_io=cap)
    )

    # --- DATA ---------------------------------------------------------------
    input_path = _resolve_input_path(base_dir, ledger.input.path)
    dataset = load_dataset(input_path, ledger.input)
    selected_rows = dataset.selected(
        selector=ledger.input.sample_selector, limit=ledger.execution.max_samples
    )
    planned = [
        (row, repeat_index)
        for row in selected_rows
        for repeat_index in range(ledger.execution.repeats)
    ]
    if parent_run_id is None and retry_keys is not None:
        raise ExperimentOutputConflict("retry keys require a parent run")
    if parent_run_id is not None:
        if not _RUN_ID_RE.fullmatch(parent_run_id):
            raise ExperimentOutputConflict("parent run ID is invalid")
        if not retry_keys:
            raise ExperimentOutputConflict("parent run has no failed rows to retry")
        planned_keys = {
            (str(row.get("id")), repeat_index) for row, repeat_index in planned
        }
        if not retry_keys <= planned_keys:
            raise ExperimentOutputConflict(
                "parent run does not match the current input selection"
            )
        execution_items = [
            (row, repeat_index)
            for row, repeat_index in planned
            if (str(row.get("id")), repeat_index) in retry_keys
        ]
    else:
        execution_items = planned
    prompts = [row_input_text(row) for row, _ in execution_items]

    # --- OUT (claimed first): stage a fresh immutable run before resolving an
    # endpoint, building a provider, or making any billable call.
    resolved_out = _select_output_dir(
        ledger, base_dir=base_dir, out_dir=out_dir
    )
    _reject_evidence_tree(resolved_out)
    runs_dir = _claim_output_root(resolved_out)
    run_id = _new_run_id(
        epoch=now(),
        ledger_sha256=ledger.sha256(),
        input_sha256=dataset.sha256,
        random_hex=token_hex,
    )
    stage_dir, final_dir = _create_run_stage(runs_dir, run_id)
    try:
        # --- IN: cost gate + conservative pre-flight -------------------------
        preflight: CostPreflight | None = None
        if ledger.provider == "azure":
            if not (ledger.execution.cost.confirmed and confirm_cost):
                raise BudgetNotConfirmedError(
                    "billed Azure run requires both execution.cost.confirmed in the "
                    "ledger and the explicit --confirm-cost flag"
                )
            preflight = estimate_azure_cost(ledger, prompts, repeats=1)
            if preflight_sink is not None:
                preflight_sink(preflight)
            if not preflight.within_ceiling:
                raise BudgetNotConfirmedError(
                    "refusing billed Azure run: the conservative pre-flight "
                    f"estimate (${preflight.estimated_usd:.4f} over "
                    f"{preflight.planned_requests} request(s)) exceeds "
                    f"execution.cost.hard_ceiling_usd "
                    f"(${preflight.hard_ceiling_usd:.2f}); lower "
                    "max_samples/repeats/max_output_tokens or raise the ceiling"
                )

        # --- EXECUTE: resolve endpoint, prepare provider ---------------------
        endpoint = resolve_endpoint(
            ledger, environ=env, allow_remote=allow_remote_ollama
        )
        provider = build(ledger, endpoint, ledger.execution.capture_io)
        provider.prepare()
        fingerprint_getter = getattr(provider, "fingerprint", None)
        ollama_fingerprint = (
            fingerprint_getter() if callable(fingerprint_getter) else None
        )
        if (
            ledger.provider == "ollama"
            and provider_builder is None
            and ollama_fingerprint is None
        ):
            raise ProviderError(
                "ollama provider did not produce a runtime/model fingerprint"
            )
        if ledger.provider == "ollama" and ollama_fingerprint is None:
            ollama_fingerprint = {
                "runtime_version": None,
                "tag": ledger.model,
                "digest": None,
                "format": None,
                "family": None,
                "parameter_size": None,
                "quantization": None,
                "template_sha256": None,
                "model_info_sha256": None,
            }

        started = now()
        records: list[OutputRecord] = []
        failures: list[RowFailure] = []
        for row, repeat_index in execution_items:
            row_id = str(row.get("id"))
            prompt = row_input_text(row)
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

        ok_count = sum(1 for record in records if record.ok)
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
            selected=len({str(row.get("id")) for row, _ in execution_items}),
            preflight=preflight,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
        records_bytes = "".join(
            json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ).encode("utf-8")
        run_json_bytes = (
            json.dumps(run_json, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        summary_bytes = _build_summary_md(
            run_json, preview=preview, records_name="records.jsonl"
        ).encode("utf-8")
        payload_artifacts = {
            "records.jsonl": records_bytes,
            "run.json": run_json_bytes,
            "summary.md": summary_bytes,
        }
        manifest = build_manifest(
            run_id=run_id,
            ledger=ledger,
            dataset=dataset,
            selected_ids=sorted(
                {str(row.get("id")) for row, _ in execution_items}
            ),
            endpoint=endpoint,
            capabilities=provider.capabilities(),
            status=status,
            parent_run_id=parent_run_id,
            retried_failed_count=len(execution_items) if parent_run_id else 0,
            artifact_bytes=payload_artifacts,
            cost_confirmed_by_cli=confirm_cost,
            remote_ollama_opt_in=allow_remote_ollama,
            ollama_fingerprint=ollama_fingerprint,
            pricing_preflight=preflight,
        )
        manifest_bytes = (
            json.dumps(
                manifest.model_dump(mode="json"),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        checksummed = {**payload_artifacts, "manifest.json": manifest_bytes}
        checksum_bytes = "".join(
            f"{sha256_bytes(content)}  {name}\n"
            for name, content in sorted(checksummed.items())
        ).encode("ascii")

        for name, content in {
            **checksummed,
            "artifacts.sha256": checksum_bytes,
        }.items():
            _atomic_write_text(stage_dir / name, content.decode("utf-8"))

        if final_dir.exists() or final_dir.is_symlink():
            raise ExperimentOutputConflict("run ID collision; refusing to reuse a run")
        os.rename(stage_dir, final_dir)
        _fsync_directory(runs_dir)
        latest = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "run_path": f"runs/{run_id}",
            "status": status,
            "manifest_sha256": sha256_bytes(manifest_bytes),
        }
        latest_path = resolved_out / "latest.json"
        _atomic_write_text(
            latest_path,
            json.dumps(latest, indent=2, sort_keys=True) + "\n",
        )

        return RunResult(
            status=status,
            exit_code=exit_code,
            out_dir=final_dir,
            run_id=run_id,
            ok_count=ok_count,
            error_count=error_count,
            run_json_path=final_dir / "run.json",
            records_path=final_dir / "records.jsonl",
            summary_path=final_dir / "summary.md",
            manifest_path=final_dir / "manifest.json",
            artifacts_sha256_path=final_dir / "artifacts.sha256",
            latest_path=latest_path,
            failures=failures,
            _preview=preview,
        )
    except BaseException:
        if stage_dir.exists() and not stage_dir.is_symlink():
            shutil.rmtree(stage_dir)
        raise


run_ledger = _hold_workspace_output_lock(run_ledger)


def _read_run_artifact(run_dir: Path, name: str, *, max_bytes: int) -> bytes:
    path = run_dir / name
    if path.is_symlink() or not path.is_file():
        raise ExperimentOutputConflict("parent run contains an unsafe artifact path")
    try:
        if path.stat().st_size > max_bytes:
            raise ExperimentOutputConflict("parent run artifact is too large")
        return path.read_bytes()
    except OSError:
        raise ExperimentOutputConflict("parent run artifact could not be read") from None


def _retry_keys_for_parent(
    ledger: RunLedger, *, base_dir: Path, parent_run_id: str
) -> set[tuple[str, int]]:
    """Validate an immutable parent and return only its failed row attempts."""
    if not _RUN_ID_RE.fullmatch(parent_run_id):
        raise ExperimentOutputConflict("parent run ID is invalid")
    out_dir = _select_output_dir(ledger, base_dir=base_dir, out_dir=None)
    run_dir = out_dir / "runs" / parent_run_id
    if run_dir.is_symlink() or not run_dir.is_dir() or not _valid_owned_marker(run_dir):
        raise ExperimentOutputConflict("parent run is missing or not safely owned")
    expected_children = {
        OWNED_MARKER_NAME,
        "run.json",
        "records.jsonl",
        "summary.md",
        "manifest.json",
        "artifacts.sha256",
    }
    if {child.name for child in run_dir.iterdir()} != expected_children:
        raise ExperimentOutputConflict("parent run artifact set is incomplete")

    try:
        checksum_text = _read_run_artifact(
            run_dir, "artifacts.sha256", max_bytes=16_384
        ).decode("ascii")
    except UnicodeDecodeError:
        raise ExperimentOutputConflict("parent checksum file is invalid") from None
    expected_hashes: dict[str, str] = {}
    for line in checksum_text.splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ExperimentOutputConflict("parent checksum file is invalid")
        expected_hashes[parts[1]] = parts[0]
    if set(expected_hashes) != {
        "manifest.json",
        "records.jsonl",
        "run.json",
        "summary.md",
    }:
        raise ExperimentOutputConflict("parent checksum coverage is incomplete")

    artifacts = {
        name: _read_run_artifact(run_dir, name, max_bytes=32 * 1024 * 1024)
        for name in expected_hashes
    }
    if any(
        sha256_bytes(artifacts[name]) != expected_hashes[name]
        for name in expected_hashes
    ):
        raise ExperimentOutputConflict("parent run artifact checksum mismatch")
    try:
        manifest = RunManifest.model_validate_json(artifacts["manifest.json"])
        records = [
            json.loads(line)
            for line in artifacts["records.jsonl"].decode("utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ExperimentOutputConflict("parent run metadata is invalid") from None
    input_path = _resolve_input_path(base_dir, ledger.input.path)
    dataset = load_dataset(input_path, ledger.input)
    if (
        manifest.run_id != parent_run_id
        or manifest.ledger_sha256 != ledger.sha256()
        or manifest.input.sha256 != dataset.sha256
    ):
        raise ExperimentOutputConflict(
            "parent run does not match the current ledger and input"
        )

    retry_keys: set[tuple[str, int]] = set()
    seen: set[tuple[str, int]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ExperimentOutputConflict("parent run record is invalid")
        row_id = record.get("row_id")
        repeat_index = record.get("repeat_index")
        status = record.get("status")
        if (
            not isinstance(row_id, str)
            or not isinstance(repeat_index, int)
            or isinstance(repeat_index, bool)
            or status not in {"ok", "error"}
        ):
            raise ExperimentOutputConflict("parent run record is invalid")
        key = (row_id, repeat_index)
        if key in seen:
            raise ExperimentOutputConflict("parent run contains duplicate attempts")
        seen.add(key)
        if status == "error":
            retry_keys.add(key)
    if not retry_keys:
        raise ExperimentOutputConflict("parent run has no failed rows to retry")
    return retry_keys


def retry_failed_run(
    ledger: RunLedger,
    *,
    base_dir: Path,
    parent_run_id: str,
    environ: dict[str, str] | None = None,
    allow_remote_ollama: bool = False,
    confirm_cost: bool = False,
    clock: Clock | None = None,
    random_hex: RandomHex | None = None,
    provider_builder: ProviderBuilder | None = None,
    preflight_sink: Callable[[CostPreflight], None] | None = None,
) -> RunResult:
    """Create a child run containing calls for failed parent attempts only."""
    retry_keys = _retry_keys_for_parent(
        ledger, base_dir=base_dir, parent_run_id=parent_run_id
    )
    return run_ledger(
        ledger,
        base_dir=base_dir,
        environ=environ,
        allow_remote_ollama=allow_remote_ollama,
        confirm_cost=confirm_cost,
        clock=clock,
        random_hex=random_hex,
        provider_builder=provider_builder,
        preflight_sink=preflight_sink,
        parent_run_id=parent_run_id,
        retry_keys=retry_keys,
    )


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
    run_id: str,
    parent_run_id: str | None,
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
        "schema_version": "2.0.0",
        "ledger_schema_version": ledger.schema_version,
        "run_id": run_id,
        "lineage": {
            "kind": "retry_failed" if parent_run_id else "initial",
            "parent_run_id": parent_run_id,
        },
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
            "manifest_json": "manifest.json",
            "artifacts_sha256": "artifacts.sha256",
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
        f"- immutable run ID: `{run_json['run_id']}`",
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
        "- code/environment manifest: `manifest.json`",
        "- artifact checksums: `artifacts.sha256`",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "run_ledger",
    "retry_failed_run",
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

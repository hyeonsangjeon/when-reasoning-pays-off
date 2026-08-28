"""The ``DATA`` stage: load and validate a small input dataset.

A dataset is either:

* **JSONL** — one JSON object per line (the recommended shape); or
* **JSON**  — a single top-level array of objects.

Each row is validated against the ledger's declared :class:`RowShape`: every
required field must be present with the declared type, and no undeclared field
may appear. This keeps the ``DATA`` promise in the ledger honest — the file on
disk must match the shape the README documents.

Loading is offline and bounded: it reads at most ``max_records`` rows, rejects
oversized files, and never evaluates or executes any field value.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from batch_runner.experiment.ledger import InputSpec, RowShape

MAX_DATASET_FILE_BYTES = 16 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1_000_000

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


class DatasetError(ValueError):
    """A dataset validation failure that names the row/field, never the value."""


@dataclass(frozen=True)
class LoadedDataset:
    """A validated dataset ready to feed the ``EXECUTE`` stage."""

    path: str
    format: str
    rows: list[dict[str, Any]]
    total_records: int
    sha256: str

    def selected(self, *, selector: str, limit: int) -> list[dict[str, Any]]:
        """Return the rows the run will actually execute, in file order.

        Both ``"first"`` and ``"all"`` selectors take rows in stable file order
        up to ``limit``; ``"first"`` documents intent when ``limit`` is small.
        """
        return list(self.rows[:limit])


def _validate_row(row: Any, shape: RowShape, *, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise DatasetError(f"row {index} is not a JSON object")
    declared = set(shape.required_fields) | set(shape.optional_fields)
    for field_name, type_name in shape.required_fields.items():
        if field_name not in row:
            raise DatasetError(f"row {index} is missing required field {field_name!r}")
        if not _TYPE_CHECKS[type_name](row[field_name]):
            raise DatasetError(
                f"row {index} field {field_name!r} must be {type_name}"
            )
    for field_name, type_name in shape.optional_fields.items():
        if field_name in row and row[field_name] is not None:
            if not _TYPE_CHECKS[type_name](row[field_name]):
                raise DatasetError(
                    f"row {index} field {field_name!r} must be {type_name}"
                )
    extra = set(row) - declared
    if extra:
        # Name the offending field(s) but never their values.
        names = ", ".join(sorted(str(name) for name in extra)[:8])
        raise DatasetError(f"row {index} has undeclared field(s): {names}")
    return row


def _read_bytes(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError:
        raise DatasetError("dataset file could not be read") from None
    if size > MAX_DATASET_FILE_BYTES:
        raise DatasetError("dataset file is too large")
    try:
        return path.read_bytes()
    except OSError:
        raise DatasetError("dataset file could not be read") from None


def load_dataset(path: Path, spec: InputSpec) -> LoadedDataset:
    """Load ``path`` as declared by ``spec`` and validate every row.

    Raises:
        DatasetError: On any structural or shape violation (value-free message).
    """
    raw = _read_bytes(path)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DatasetError("dataset file is not valid UTF-8") from None

    rows: list[dict[str, Any]] = []
    if spec.format == "jsonl":
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
                raise DatasetError(f"row {line_no} exceeds the line size limit")
            if len(rows) >= spec.max_records:
                raise DatasetError(
                    f"dataset has more than max_records={spec.max_records} rows"
                )
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                raise DatasetError(f"row {line_no} is not valid JSON") from None
            rows.append(_validate_row(obj, spec.row_shape, index=line_no))
    else:  # json
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            raise DatasetError("dataset file is not valid JSON") from None
        if not isinstance(obj, list):
            raise DatasetError("json dataset must be a top-level array of objects")
        if len(obj) > spec.max_records:
            raise DatasetError(
                f"dataset has more than max_records={spec.max_records} rows"
            )
        for index, item in enumerate(obj, start=1):
            rows.append(_validate_row(item, spec.row_shape, index=index))

    if not rows:
        raise DatasetError("dataset contains no rows")

    _enforce_identity(rows, spec.row_shape)

    return LoadedDataset(
        path=str(spec.path),
        format=spec.format,
        rows=rows,
        total_records=len(rows),
        sha256=digest,
    )


def _enforce_identity(rows: list[dict[str, Any]], shape: RowShape) -> None:
    """Enforce nonempty ``id``/``input`` and unique row ids before any run.

    A blank id/input or a duplicate id is rejected here, before a provider is
    built or the network is touched, so a malformed dataset can never reach a
    billable call. Row values are never echoed — only the 1-based index.
    """
    checks_id = "id" in shape.required_fields
    checks_input = "input" in shape.required_fields
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if checks_input:
            value = row.get("input")
            if not isinstance(value, str) or not value.strip():
                raise DatasetError(f"row {index} field 'input' must be non-empty")
        if checks_id:
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id.strip():
                raise DatasetError(f"row {index} field 'id' must be non-empty")
            if row_id in seen:
                raise DatasetError(f"row {index} repeats an earlier 'id'")
            seen.add(row_id)


def row_input_text(row: dict[str, Any]) -> str:
    """Return the prompt text for a row, from its required ``input`` field."""
    value = row.get("input")
    if not isinstance(value, str):
        raise DatasetError("row 'input' field must be a string")
    return value


__all__ = [
    "DatasetError",
    "LoadedDataset",
    "load_dataset",
    "row_input_text",
    "MAX_DATASET_FILE_BYTES",
]

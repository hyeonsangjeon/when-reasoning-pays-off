"""Task 020 tests — retry_after_ms characterization.

Covers:
- parse_retry_after (ms, seconds, http-date, missing)
- source-aware 429 selectors
- per-source distribution preserved
- sparse / imbalanced flags
- overshoot not_computable in absence of utilization proxy + denominator
- allowlist guard (CLI fails closed on disallowed benchmark id)
- no-network static check (forbidden imports absent from source text)
- no-network runtime check (socket.create_connection monkeypatched)
- anonymization grep over generated outputs
"""

from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import retry_after_ms_characterization as mod  # noqa: E402


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_parse_retry_after_ms_numeric():
    val, skip = mod.parse_retry_after({"retry_after_ms": 1234})
    assert val == 1234.0
    assert skip is None


def test_parse_retry_after_seconds_converted_to_ms():
    val, skip = mod.parse_retry_after({"retry_after": 2})
    assert val == 2000.0
    assert skip is None


def test_parse_retry_after_seconds_legacy_field():
    val, skip = mod.parse_retry_after({"retry_after_seconds": 3.5})
    assert val == 3500.0
    assert skip is None


def test_parse_retry_after_http_date_skipped_and_reason_set():
    val, skip = mod.parse_retry_after({"retry_after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert val is None
    assert skip == "http_date"


def test_parse_retry_after_unparseable_ms_field():
    val, skip = mod.parse_retry_after({"retry_after_ms": "not-a-number"})
    assert val is None
    assert skip == "unparseable_retry_after_ms"


def test_parse_retry_after_missing():
    val, skip = mod.parse_retry_after({})
    assert val is None
    assert skip == "missing"


def test_task013_selector_uses_real_429_observed():
    assert mod.is_429_event({"real_429_observed": True}, "task013") is True
    assert mod.is_429_event({"real_429_observed": False}, "task013") is False
    # task013 must NOT honor the task019 field
    assert mod.is_429_event({"429_observed": True}, "task013") is False


def test_task019_selector_uses_429_observed_or_first_429_metadata():
    assert mod.is_429_event({"429_observed": True}, "task019") is True
    assert (
        mod.is_429_event(
            {"first_429_metadata": {"retry_after_ms": 500}}, "task019"
        )
        is True
    )
    # task019 must NOT silently honor the task013 field
    assert mod.is_429_event({"real_429_observed": True}, "task019") is False
    assert mod.is_429_event({}, "task019") is False


def test_empirical_percentiles_match_known_distribution():
    # 100 values 1..100 ms; p50 ~= 50.5, p90 ~= 90.1
    vals = [float(i) for i in range(1, 101)]
    s = mod.summarize(vals)
    assert s["count"] == 100
    assert s["min"] == 1.0
    assert s["max"] == 100.0
    assert abs(s["p50"] - 50.5) < 1e-6
    assert abs(s["p10"] - 10.9) < 1e-6
    assert abs(s["p90"] - 90.1) < 1e-6
    assert abs(s["p99"] - 99.01) < 1e-6


# ---------------------------------------------------------------------------
# Synthetic-fixture aggregator tests
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _make_fake_repo(tmp_path: Path, t013: list[dict], t019: list[dict]) -> Path:
    repo = tmp_path / "fake_repo"
    (repo / "benchmarks" / "05-dual-spillover" / "runs").mkdir(parents=True)
    (repo / "benchmarks" / "07-max-output-tokens-reservation" / "runs").mkdir(parents=True)
    _write_jsonl(
        repo / "benchmarks" / "05-dual-spillover" / "runs" / "synthetic.jsonl",
        t013,
    )
    _write_jsonl(
        repo
        / "benchmarks"
        / "07-max-output-tokens-reservation"
        / "runs"
        / "synthetic.jsonl",
        t019,
    )
    return repo


def test_aggregator_preserves_source_labels_and_per_source_distribution(tmp_path):
    t013 = [
        {"real_429_observed": True, "retry_after_ms": v} for v in [100, 200, 300, 400]
    ]
    t019 = [
        {"429_observed": True, "retry_after_ms": v} for v in [1000, 2000]
    ]
    # extra non-429 records should be ignored
    t013.append({"real_429_observed": False, "retry_after_ms": 9999})
    t019.append({"429_observed": False, "retry_after_ms": 8888})

    repo = _make_fake_repo(tmp_path, t013, t019)
    events, counts = mod.extract_events(
        ["05-dual-spillover", "07-max-output-tokens-reservation"], repo
    )
    assert counts.total_429 == 6
    assert counts.task013_429 == 4
    assert counts.task019_429 == 2
    sources = sorted({e.source for e in events})
    assert sources == ["task013", "task019"]

    analysis = mod.build_analysis(events, counts)
    assert analysis["per_source_distribution"]["task013"]["count"] == 4
    assert analysis["per_source_distribution"]["task019"]["count"] == 2
    assert analysis["overall_distribution"]["count"] == 6


def test_task019_first_429_metadata_only_is_not_dropped(tmp_path):
    t019 = [
        {
            "first_429_metadata": {"retry_after_ms": 750},
            "retry_after_ms": 750,
        }
    ]
    repo = _make_fake_repo(tmp_path, [], t019)
    events, counts = mod.extract_events(
        ["05-dual-spillover", "07-max-output-tokens-reservation"], repo
    )
    assert counts.task019_429 == 1
    assert len(events) == 1
    assert events[0].source == "task019"
    assert events[0].retry_after_ms == 750.0


def test_http_date_retry_after_skipped_and_counted(tmp_path):
    t013 = [
        {"real_429_observed": True, "retry_after": "Wed, 21 Oct 2026 07:28:00 GMT"},
        {"real_429_observed": True, "retry_after_ms": 500},
    ]
    repo = _make_fake_repo(tmp_path, t013, [])
    events, counts = mod.extract_events(["05-dual-spillover"], repo)
    assert counts.total_429 == 2
    assert counts.http_date_retry_after_skipped == 1
    assert len(events) == 1
    assert events[0].retry_after_ms == 500.0


def test_sparse_and_imbalanced_flags(tmp_path):
    # 10 events, all task013 => sparse (<50) and imbalanced (>=80%)
    t013 = [{"real_429_observed": True, "retry_after_ms": 100} for _ in range(10)]
    repo = _make_fake_repo(tmp_path, t013, [])
    events, counts = mod.extract_events(
        ["05-dual-spillover", "07-max-output-tokens-reservation"], repo
    )
    analysis = mod.build_analysis(events, counts)
    assert analysis["flags"]["sparse"] is True
    assert analysis["flags"]["imbalanced"] is True


def test_balanced_non_sparse(tmp_path):
    t013 = [{"real_429_observed": True, "retry_after_ms": 100} for _ in range(40)]
    t019 = [{"429_observed": True, "retry_after_ms": 200} for _ in range(40)]
    repo = _make_fake_repo(tmp_path, t013, t019)
    events, counts = mod.extract_events(
        ["05-dual-spillover", "07-max-output-tokens-reservation"], repo
    )
    analysis = mod.build_analysis(events, counts)
    assert analysis["flags"]["sparse"] is False
    assert analysis["flags"]["imbalanced"] is False


def test_overshoot_correlation_is_not_computable(tmp_path):
    t013 = [{"real_429_observed": True, "retry_after_ms": 100}]
    t019 = [{"429_observed": True, "retry_after_ms": 200, "arrival_rpm_at_request_time": 5}]
    repo = _make_fake_repo(tmp_path, t013, t019)
    events, counts = mod.extract_events(
        ["05-dual-spillover", "07-max-output-tokens-reservation"], repo
    )
    analysis = mod.build_analysis(events, counts)
    assert analysis["correlation_with_overshoot"]["status"] == "not_computable"
    assert "reason" in analysis["correlation_with_overshoot"]


def test_analysis_md_answers_quantized_or_continuous_question(tmp_path):
    t013 = [{"real_429_observed": True, "retry_after_ms": 43} for _ in range(8)]
    t013.extend({"real_429_observed": True, "retry_after_ms": v} for v in [44, 45])
    repo = _make_fake_repo(tmp_path, t013, [])
    events, counts = mod.extract_events(["05-dual-spillover"], repo)
    analysis = mod.build_analysis(events, counts)
    md = mod.render_analysis_md(analysis)
    assert analysis["distribution_shape"]["overall"]["appearance"] == (
        "clustered / integer-ms quantized"
    )
    assert "Quantization / continuity answer" in md
    compact_md = " ".join(md.split())
    assert "not like a smooth continuous distribution" in compact_md


# ---------------------------------------------------------------------------
# Allowlist / CLI guard
# ---------------------------------------------------------------------------


def test_cli_rejects_disallowed_benchmark_id(tmp_path, capsys):
    out = tmp_path / "analysis.json"
    rc = mod.main(
        [
            "--benchmarks",
            "99-not-a-benchmark",
            "--out",
            str(out),
            "--results-dir",
            str(tmp_path / "results"),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "not in allowlist" in captured.err


def test_resolve_jsonl_paths_rejects_unknown_id(tmp_path):
    with pytest.raises(SystemExit):
        mod.resolve_jsonl_paths("99-unknown", tmp_path)


def test_resolve_jsonl_paths_only_returns_jsonl_under_runs(tmp_path):
    runs = tmp_path / "benchmarks" / "05-dual-spillover" / "runs"
    runs.mkdir(parents=True)
    (runs / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (runs / "a.jsonl.summary.json").write_text("{}", encoding="utf-8")
    (runs / "b.txt").write_text("x", encoding="utf-8")
    paths = mod.resolve_jsonl_paths("05-dual-spillover", tmp_path)
    names = [p.name for p in paths]
    assert names == ["a.jsonl"]


# ---------------------------------------------------------------------------
# No-network static check
# ---------------------------------------------------------------------------


FORBIDDEN_IMPORTS = [
    r"\bimport\s+openai\b",
    r"\bfrom\s+openai\b",
    r"\bAzureOpenAI\b",
    r"\bAsyncAzureOpenAI\b",
    r"\bimport\s+requests\b",
    r"\bfrom\s+requests\b",
    r"\bimport\s+httpx\b",
    r"\bfrom\s+httpx\b",
    r"\bimport\s+aiohttp\b",
    r"\bfrom\s+aiohttp\b",
    r"\bsocket\.create_connection\b",
    r"\burllib\.request\.urlopen\b",
    r"\bhttp\.client\.HTTPConnection\b",
    r"\bhttp\.client\.HTTPSConnection\b",
]


def test_script_source_has_no_forbidden_imports():
    src = (REPO_ROOT / "scripts" / "retry_after_ms_characterization.py").read_text(
        encoding="utf-8"
    )
    for pat in FORBIDDEN_IMPORTS:
        assert re.search(pat, src) is None, f"forbidden token matched: {pat}"


# ---------------------------------------------------------------------------
# No-network runtime check
# ---------------------------------------------------------------------------


def test_no_network_runtime_aggregation(tmp_path, monkeypatch):
    """Aggregator runs to completion on a tiny fixture with sockets/http blocked."""

    def boom(*a, **kw):
        raise RuntimeError("network access is forbidden in Task 020")

    monkeypatch.setattr(socket, "create_connection", boom, raising=False)
    monkeypatch.setattr(socket, "socket", boom, raising=False)
    import http.client as _hc

    monkeypatch.setattr(_hc, "HTTPConnection", boom, raising=False)
    monkeypatch.setattr(_hc, "HTTPSConnection", boom, raising=False)
    import urllib.request as _ur

    monkeypatch.setattr(_ur, "urlopen", boom, raising=False)

    t013 = [{"real_429_observed": True, "retry_after_ms": 250}]
    t019 = [{"429_observed": True, "retry_after_ms": 750}]
    repo = _make_fake_repo(tmp_path, t013, t019)
    out = tmp_path / "analysis.json"
    rc = mod.main(
        [
            "--benchmarks",
            "05-dual-spillover,07-max-output-tokens-reservation",
            "--out",
            str(out),
            "--results-dir",
            str(tmp_path / "results"),
            "--repo-root",
            str(repo),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"]["total_429"] == 2


# ---------------------------------------------------------------------------
# Anonymization grep over generated outputs
# ---------------------------------------------------------------------------


ANON_PATTERNS = [
    re.compile(r"[a-z0-9-]+\.openai\.azure\.com", re.IGNORECASE),
    re.compile(r"[a-z0-9-]+\.cognitiveservices\.azure\.com", re.IGNORECASE),
    re.compile(r"[a-z0-9-]+\.services\.ai\.azure\.com", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"api-key\s*[:=]", re.IGNORECASE),
    re.compile(r"Authorization\s*[:=]", re.IGNORECASE),
    # IPv4 (loose)
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # GUIDs (tenant/subscription/resource IDs)
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
]

# explicit token-substring blocklist (env var keys may legitimately appear
# in narrative documentation; their *values* must never appear)
TOKEN_BLOCKLIST = [
    "AZURE_OPENAI_API_KEY=",
    "OPENAI_API_KEY=",
    "AZURE_CLIENT_SECRET=",
]


def _scan_for_anonymization(text: str, src_label: str) -> list[str]:
    hits = []
    for pat in ANON_PATTERNS:
        for m in pat.finditer(text):
            hits.append(f"{src_label}: pattern {pat.pattern!r} matched {m.group()!r}")
    for tok in TOKEN_BLOCKLIST:
        if tok in text:
            hits.append(f"{src_label}: blocklist token {tok!r}")
    return hits


def test_generated_outputs_pass_anonymization_grep(tmp_path):
    # Run aggregator against fake repo with synthetic, safe records
    t013 = [{"real_429_observed": True, "retry_after_ms": 250}]
    t019 = [{"429_observed": True, "retry_after_ms": 750}]
    repo = _make_fake_repo(tmp_path, t013, t019)
    out = tmp_path / "benchmarks" / "08-retry-after-characterization" / "analysis.json"
    rc = mod.main(
        [
            "--benchmarks",
            "05-dual-spillover,07-max-output-tokens-reservation",
            "--out",
            str(out),
            "--results-dir",
            str(tmp_path / "results"),
            "--repo-root",
            str(repo),
        ]
    )
    assert rc == 0

    targets = [out]
    res = tmp_path / "results"
    for p in res.rglob("*"):
        if p.is_file() and p.suffix in {".csv", ".md", ".json"}:
            targets.append(p)

    hits: list[str] = []
    for p in targets:
        text = p.read_text(encoding="utf-8")
        hits.extend(_scan_for_anonymization(text, str(p)))
    assert hits == [], "anonymization grep failed:\n" + "\n".join(hits)


def test_committed_outputs_pass_anonymization_grep():
    """Grep the actual committed Task 020 deliverables, if present."""
    targets: list[Path] = []
    bench_dir = REPO_ROOT / "benchmarks" / "08-retry-after-characterization"
    if bench_dir.is_dir():
        for name in ("README.md", "analysis.md", "analysis.json"):
            p = bench_dir / name
            if p.is_file():
                targets.append(p)
    res_dir = REPO_ROOT / "results" / "retry-after-characterization"
    if res_dir.is_dir():
        for p in res_dir.rglob("*"):
            if p.is_file() and p.suffix in {".csv", ".md", ".json"}:
                targets.append(p)
    hits: list[str] = []
    for p in targets:
        text = p.read_text(encoding="utf-8")
        hits.extend(_scan_for_anonymization(text, str(p)))
    assert hits == [], "anonymization grep failed:\n" + "\n".join(hits)

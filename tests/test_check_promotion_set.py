"""Tests for scripts/check_promotion_set.py (Task 032 Phase C, WP-C1).

All "dirty" inputs are constructed at runtime from string fragments so the
test-source bytes never contain a literal secret / endpoint / private-tree
pattern that scripts/check_public_surface.sh greps for. Committed fixtures are
clean (numeric) only.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "task032_promotion"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cps = _load("check_promotion_set")


def _categories(findings):
    return {f.category for f in findings}


# ---------------------------------------------------------------------------
# Clean inputs pass
# ---------------------------------------------------------------------------


def test_clean_chart_data_json_is_clean():
    findings = cps.scan_file(FIXTURES / "clean_chart_data.json")
    assert findings == [], [f.format() for f in findings]


def test_clean_series_csv_is_clean():
    findings = cps.scan_file(FIXTURES / "clean_series.csv")
    assert findings == [], [f.format() for f in findings]


def test_clean_aggregate_json_is_clean():
    findings = cps.scan_file(FIXTURES / "clean_aggregate.json")
    # Auto-detected as aggregate via its tier field; n>=5, rounded timestamps.
    assert findings == [], [f.format() for f in findings]
    assert all(f.tier == cps.AGGREGATE_TIER for f in findings)


def test_main_clean_exit_zero(capsys):
    code = cps.main([str(FIXTURES / "clean_chart_data.json"), str(FIXTURES / "clean_series.csv")])
    assert code == 0


# ---------------------------------------------------------------------------
# Negative corpus — one (or more) case per docs/16 §3 category
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_secret_openai_key(tmp_path):
    token = "sk-" + "A" * 32
    p = _write(tmp_path, "data.csv", f"effort,note\nlow,{token}\n")
    cats = _categories(cps.scan_file(p))
    assert "secret-openai-sk" in cats


def test_bearer_token(tmp_path):
    token = "Bea" + "rer " + "B" * 32
    p = _write(tmp_path, "notes.md", f"auth header: {token}\n")
    assert "bearer-token" in _categories(cps.scan_file(p))


def test_azure_openai_endpoint_in_cell(tmp_path):
    host = "ex" + "ample.openai.azure.com"
    p = _write(tmp_path, "series.csv", f"series,endpoint_host\na,{host}\n")
    cats = _categories(cps.scan_file(p))
    assert "azure-openai-endpoint" in cats


def test_ai_services_endpoint(tmp_path):
    host = "res" + "ource.services.ai.azure.com"
    p = _write(tmp_path, "d.txt", f"{host}\n")
    assert "ai-services-endpoint" in _categories(cps.scan_file(p))


def test_request_id_header(tmp_path):
    line = "x-request-id: " + "a1b2c3d4e5f6\n"
    p = _write(tmp_path, "log.md", line)
    assert "request-id" in _categories(cps.scan_file(p))


def test_email(tmp_path):
    addr = "u" + "ser@ex" + "ample.org"
    p = _write(tmp_path, "c.md", f"contact {addr}\n")
    assert "email" in _categories(cps.scan_file(p))


def test_internal_hostname(tmp_path):
    host = "build01" + "." + "internal"
    p = _write(tmp_path, "h.txt", f"host={host}\n")
    assert "internal-hostname" in _categories(cps.scan_file(p))


def test_internal_tree_reference(tmp_path):
    ref = "." + "internal" + "/release/secret.json"
    p = _write(tmp_path, "r.md", f"see {ref}\n")
    assert "internal-tree-ref" in _categories(cps.scan_file(p))


def test_absolute_home_path(tmp_path):
    ap = "/Users/" + "alice/work/data.csv"
    p = _write(tmp_path, "p.md", f"path: {ap}\n")
    assert "absolute-home-path" in _categories(cps.scan_file(p))


def test_rfc1918_ip(tmp_path):
    p = _write(tmp_path, "ip.txt", "node at 10.0.12.7 responded\n")
    assert "rfc1918-ip" in _categories(cps.scan_file(p))


def test_region_token(tmp_path):
    p = _write(tmp_path, "reg.csv", "series,location\na,eastus2\n")
    assert "region-token" in _categories(cps.scan_file(p))


def test_deployment_label_value(tmp_path):
    p = _write(tmp_path, "dep.csv", "series,value\nmodel,gpt4o-prod\n")
    assert "deployment-label" in _categories(cps.scan_file(p))


def test_sas_signature(tmp_path):
    url = "https://" + "blob.example/x?sig=" + "Z" * 40
    p = _write(tmp_path, "u.txt", url + "\n")
    assert "sas-signature" in _categories(cps.scan_file(p))


def test_storage_account_key(tmp_path):
    s = "AccountKey=" + "Q" * 40
    p = _write(tmp_path, "k.txt", s + "\n")
    assert "storage-account-key" in _categories(cps.scan_file(p))


# ---------------------------------------------------------------------------
# Structural field-name rejection (free-text / identity / per-request)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["prompt", "response", "message", "content", "tool_calls", "arguments",
     "request_id", "deployment_name", "region", "endpoint", "run_id", "headers"],
)
def test_forbidden_field_names(tmp_path, field):
    import json
    p = _write(tmp_path, "obj.json", json.dumps({field: "anything", "n": 9}))
    cats = _categories(cps.scan_file(p))
    assert "forbidden-field-name" in cats


def test_camelcase_field_normalized(tmp_path):
    import json
    p = _write(tmp_path, "c.json", json.dumps({"deploymentName": "x"}))
    assert "forbidden-field-name" in _categories(cps.scan_file(p))


def test_namespace_forbidden_but_namespace_id_allowed(tmp_path):
    import json
    bad = _write(tmp_path, "ns.json", json.dumps({"namespace": "topo-a"}))
    assert "forbidden-field-name" in _categories(cps.scan_file(bad))
    ok = _write(tmp_path, "nsid.json", json.dumps({"namespace_id": "role-1"}))
    assert "forbidden-field-name" not in _categories(cps.scan_file(ok))


def test_csv_forbidden_header(tmp_path):
    p = _write(tmp_path, "rows.csv", "effort,prompt\nlow,hello\n")
    assert "forbidden-field-name" in _categories(cps.scan_file(p))


# ---------------------------------------------------------------------------
# Aggregate-tier-only rules
# ---------------------------------------------------------------------------


def test_aggregate_n_below_min_flagged(tmp_path):
    import json
    obj = {"tier": "AGGREGATE_AZURE_SAMPLE", "cells": [{"cell_id": "c", "n": 3, "mean": 1.0}]}
    p = _write(tmp_path, "agg.json", json.dumps(obj))
    cats = _categories(cps.scan_file(p))
    assert "aggregate-n-below-min" in cats


def test_count_not_checked_for_sanitized_tier(tmp_path):
    import json
    # No aggregate tier and no 'aggregate' in path: n<5 must NOT be flagged.
    obj = {"tier": "SANITIZED_PUBLIC", "rows": [{"effort": "low", "n": 2}]}
    p = _write(tmp_path, "chart.json", json.dumps(obj))
    cats = _categories(cps.scan_file(p))
    assert "aggregate-n-below-min" not in cats


def test_aggregate_free_text_field_flagged(tmp_path):
    import json
    obj = {"tier": "AGGREGATE_AZURE_SAMPLE", "cells": [{"cell_id": "c", "n": 9, "response": "raw text"}]}
    p = _write(tmp_path, "agg2.json", json.dumps(obj))
    assert "forbidden-field-name" in _categories(cps.scan_file(p))


def test_aggregate_unrounded_wallclock_flagged(tmp_path):
    import json
    obj = {
        "tier": "AGGREGATE_AZURE_SAMPLE",
        "cells": [{"cell_id": "c", "n": 9, "window_start_iso": "2026-06-03T04:18:27Z"}],
    }
    p = _write(tmp_path, "agg3.json", json.dumps(obj))
    assert "unrounded-wallclock" in _categories(cps.scan_file(p))


def test_aggregate_rounded_wallclock_clean(tmp_path):
    import json
    obj = {
        "tier": "AGGREGATE_AZURE_SAMPLE",
        "cells": [{"cell_id": "c", "n": 9, "window_start_iso": "2026-06-03T04:00:00Z"}],
    }
    p = _write(tmp_path, "agg4.json", json.dumps(obj))
    assert "unrounded-wallclock" not in _categories(cps.scan_file(p))


def test_sanitized_unrounded_wallclock_json_flagged(tmp_path):
    import json
    obj = {"tier": "SANITIZED_PUBLIC", "captured_at": "2026-06-04T12:34:56Z"}
    p = _write(tmp_path, "chart.json", json.dumps(obj))
    assert "unrounded-wallclock" in _categories(cps.scan_file(p))


def test_sanitized_unrounded_wallclock_timestamp_iso_json_flagged(tmp_path):
    import json
    obj = {"tier": "SANITIZED_PUBLIC", "wallclock_timestamp_iso": "2026-06-04T12:34:56Z"}
    p = _write(tmp_path, "chart.json", json.dumps(obj))
    assert "unrounded-wallclock" in _categories(cps.scan_file(p))


def test_sanitized_unrounded_timestamp_utc_json_flagged(tmp_path):
    import json
    obj = {"tier": "SANITIZED_PUBLIC", "timestamp_utc": "2026-06-04T12:34:56Z"}
    p = _write(tmp_path, "chart.json", json.dumps(obj))
    assert "unrounded-wallclock" in _categories(cps.scan_file(p))


def test_sanitized_unrounded_wallclock_csv_flagged(tmp_path):
    p = _write(tmp_path, "series.csv", "series,started_at\na,2026-06-04T12:34:56Z\n")
    assert "unrounded-wallclock" in _categories(cps.scan_file(p))


def test_sanitized_unrounded_wallclock_timestamp_iso_csv_flagged(tmp_path):
    p = _write(tmp_path, "series.csv", "series,wallclock_timestamp_iso\na,2026-06-04T12:34:56Z\n")
    assert "unrounded-wallclock" in _categories(cps.scan_file(p))


def test_sanitized_rounded_wallclock_json_clean(tmp_path):
    import json
    obj = {"tier": "SANITIZED_PUBLIC", "captured_at": "2026-06-04T12:00:00Z"}
    p = _write(tmp_path, "chart.json", json.dumps(obj))
    assert "unrounded-wallclock" not in _categories(cps.scan_file(p))


def test_sanitized_rounded_canonical_timestamps_clean(tmp_path):
    import json
    obj = {
        "tier": "SANITIZED_PUBLIC",
        "wallclock_timestamp_iso": "2026-06-04T12:00:00Z",
        "timestamp_utc": "2026-06-04T13:00:00Z",
        "started_at_iso": "2026-06-04T14:00:00Z",
        "captured_at_iso": "2026-06-04T15:00:00Z",
        "probe_started_at_iso": "2026-06-04T16:00:00Z",
        "probe_window_end_iso": "2026-06-04T17:00:00Z",
    }
    p = _write(tmp_path, "chart.json", json.dumps(obj))
    assert cps.scan_file(p) == []


def test_force_aggregate_flag(tmp_path):
    import json
    obj = {"cells": [{"cell_id": "c", "n": 2}]}
    p = _write(tmp_path, "plain.json", json.dumps(obj))
    # Without aggregate detection: not flagged.
    assert "aggregate-n-below-min" not in _categories(cps.scan_file(p))
    # Forced aggregate: flagged.
    assert "aggregate-n-below-min" in _categories(cps.scan_file(p, force_aggregate=True))


def test_path_with_aggregate_segment_autodetected(tmp_path):
    import json
    d = tmp_path / "aggregate"
    d.mkdir()
    obj = {"cells": [{"cell_id": "c", "n": 1}]}
    p = d / "x.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    assert "aggregate-n-below-min" in _categories(cps.scan_file(p))


# ---------------------------------------------------------------------------
# JSONL / TSV handling
# ---------------------------------------------------------------------------


def test_jsonl_forbidden_field(tmp_path):
    import json
    lines = "\n".join(json.dumps(r) for r in [{"n": 9}, {"prompt": "hi"}])
    p = _write(tmp_path, "rows.jsonl", lines + "\n")
    assert "forbidden-field-name" in _categories(cps.scan_file(p))


def test_tsv_cell_secret(tmp_path):
    token = "sk-" + "C" * 32
    p = _write(tmp_path, "t.tsv", f"series\tnote\na\t{token}\n")
    assert "secret-openai-sk" in _categories(cps.scan_file(p))


# ---------------------------------------------------------------------------
# Path collection / promotion-root + CLI
# ---------------------------------------------------------------------------


def test_promotion_root_collects_known_surface(tmp_path):
    import json
    chart = tmp_path / "results" / "public" / "chart-data" / "fam"
    chart.mkdir(parents=True)
    (chart / "c.json").write_text(json.dumps({"family_key": "fam", "v": 1}), encoding="utf-8")
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "public_chart_candidates.json").write_text(
        json.dumps({"prompt": "leak"}), encoding="utf-8"
    )
    findings, errors = cps.scan_paths([], promotion_root=str(tmp_path))
    assert errors == []
    paths = {f.path for f in findings}
    assert any("public_chart_candidates.json" in p for p in paths)


def test_nonexistent_path_is_usage_error(capsys):
    code = cps.main(["/no/such/path/xyz.json"])
    assert code == 2


def test_main_dirty_exit_one(tmp_path):
    import json
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"prompt": "x"}), encoding="utf-8")
    assert cps.main([str(p)]) == 1

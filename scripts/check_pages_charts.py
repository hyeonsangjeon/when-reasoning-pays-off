#!/usr/bin/env python3
"""Validate the static public chart dashboard surface."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "blog" / "data" / "chart-data"
PAGE = ROOT / "docs" / "blog" / "charts" / "index.html"
RENDERER = ROOT / "docs" / "assets" / "charts.js"
LOCALES = ["en", "ko", "ja", "zh-CN", "hi"]
REQUIRED_FAMILIES = {"cost-curves-effort", "token-composition", "ptu-payg-crossover"}
REQUIRED_SERIES = {
    "mean_usd_per_request", "std_usd_per_request", "mean_latency_ms", "std_latency_ms",
    "mean_judge_score", "std_judge_score", "judge_n", "throughput_gain_factor",
    "tokens_per_request", "baseline_tokens_per_request", "mean_input_tokens_noncached",
    "mean_cached_tokens", "mean_output_tokens", "mean_reasoning_tokens",
    "modeled_break_even_rpm", "ptu_hourly_rate_usd", "min_ptu", "n_used"
}
DISPLAY_KEYS = {"title", "label", "labels", "legend", "description", "descriptions"}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def nested_get(obj, parts):
    cur = obj
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def require(condition, message):
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def scan_chart_json_for_labels():
    for path in DATA.glob("**/*.json"):
        if "locales" in path.parts or path.name in {"public_chart_candidates.json", "snapshot_manifest.json"}:
            continue
        obj = load(path)
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for key, value in cur.items():
                    require(key not in DISPLAY_KEYS, f"display/prose key {key!r} in {path}")
                    stack.append(value)
            elif isinstance(cur, list):
                stack.extend(cur)


def check_locales(manifest):
    en = load(DATA / "locales" / "en.json")
    ko = load(DATA / "locales" / "ko.json")
    ja = load(DATA / "locales" / "ja.json")
    for loc in LOCALES:
        path = DATA / "locales" / f"{loc}.json"
        require(path.is_file(), f"missing locale {loc}")
        data = load(path)
        meta = data.get("meta", {})
        require(meta.get("locale") == loc, f"locale meta mismatch for {loc}")
        if loc in {"en", "ko", "ja"}:
            require(meta.get("fallback") is False, f"{loc} must be real labels")
        else:
            require(meta.get("fallback") is True and meta.get("fallback_to") == "en", f"{loc} must explicitly fall back to en")
    for loc, labels in {"en": en, "ko": ko, "ja": ja}.items():
        for family in REQUIRED_FAMILIES:
            require(
                nested_get(labels, ["families", family, "title"]),
                f"{loc} missing family {family}",
            )
        for key in REQUIRED_SERIES:
            require(
                nested_get(labels, ["series", key]),
                f"{loc} missing series {key}",
            )
    note_en = nested_get(en, ["notes", "ptu_payg_modeled_hypothesis"])
    note_ko = nested_get(ko, ["notes", "ptu_payg_modeled_hypothesis"])
    note_ja = nested_get(ja, ["notes", "ptu_payg_modeled_hypothesis"])
    require("not measured PTU throughput" in note_en, "en PTU hypothesis note must say not measured PTU throughput")
    require("측정된 PTU 처리량이 아닙니다" in note_ko, "ko PTU hypothesis note must say not measured PTU throughput")
    require(note_ja, "ja missing PTU modeled-hypothesis note")
    require(nested_get(en, ["notes", "quality_guardrail"]), "en missing quality guardrail note")
    require(nested_get(ko, ["notes", "quality_guardrail"]), "ko missing quality guardrail note")
    require(nested_get(ja, ["notes", "quality_guardrail"]), "ja missing quality guardrail note")


def check_renderer_and_page():
    require(PAGE.is_file(), "missing charts page")
    require(RENDERER.is_file(), "missing renderer")
    page = PAGE.read_text(encoding="utf-8")
    js = RENDERER.read_text(encoding="utf-8")
    require("../../assets/charts.js" in page, "page must load renderer relatively")
    require("../data/chart-data/" in js, "renderer must use same-origin relative chart-data root")
    require("fetch(" in js, "renderer must fetch static JSON")
    require(not re.search(r"https?://|//[A-Za-z0-9]", js), "renderer must not contain external URL fetches")
    require("ptu_payg_modeled_hypothesis" in js, "renderer must display PTU modeled-hypothesis note")
    require("quality-guardrail" in js and "quality-pairing" in js, "renderer must display quality guardrail/pairing")
    require("<table" in js and "<svg" in js, "renderer must provide table and visual output")


def check_pairing(manifest):
    candidates = manifest["candidates"]
    paths = {c["chart_data_path"] for c in candidates}
    for candidate in candidates:
        path = candidate["chart_data_path"]
        payload = load(DATA / path.replace("results/public/chart-data/", ""))
        metric = payload.get("metric_key")
        if metric in {"cost_per_request", "throughput_gain"}:
            quality_path = path.rsplit("/", 1)[0] + "/quality.json"
            require(quality_path in paths, f"missing quality candidate for {path}")
        if metric == "ptu_payg_crossover":
            qp = payload.get("quality_pairing", {})
            require(qp.get("quality_chart_data_path") in paths, f"missing crossover quality pairing for {path}")
            require(payload.get("framing_key") == "throughput_gain_hypothesis", f"missing modeled framing for {path}")


def main():
    manifest = load(DATA / "public_chart_candidates.json")
    require(len(manifest.get("candidates", [])) == 18, "manifest must contain 18 candidates")
    require({c["family_key"] for c in manifest["candidates"]} == REQUIRED_FAMILIES, "family set mismatch")
    scan_chart_json_for_labels()
    check_locales(manifest)
    check_renderer_and_page()
    check_pairing(manifest)
    print("check passed: static chart page, locales, framing, quality pairing")


if __name__ == "__main__":
    main()

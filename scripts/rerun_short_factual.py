"""Re-run benchmark 01 (short-factual) on newer reasoning models, 2026-11.

Same dataset, same prompt construction (``scripts.run_benchmark.load_dataset``:
system prompt + blank line + rendered user template), same Responses API call
shape (``max_output_tokens`` 4096, ``reasoning={"effort": ...}``, no
temperature/top_p) and the same per-cell JSON fields that ``scripts/run_judge.py``
reads. What differs from exp001: the models, the effort list each model accepts,
and the endpoint (Foundry resource-level ``/openai/v1/``).

Each cell is one JSON file in ``--out``. Rejected effort values are not swept;
they are recorded separately by ``--probe-rejected`` as one call each.

usage:
  python -m scripts.rerun_short_factual --endpoint https://<resource>.services.ai.azure.com \
      --deployment gpt-5.6-sol --family gpt-5.6-sol --efforts none,low,medium,high,xhigh,max \
      --experiment-id exp020_short-factual_rerun_gpt-5.6-sol --out benchmarks/01-short-factual/rerun-2026-11/runs
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import pathlib
import subprocess
import time

from scripts.run_benchmark import load_dataset, sha256_text

BENCH = pathlib.Path("benchmarks/01-short-factual")
API_VERSION = "preview"


def _client(endpoint: str):
    from azure.identity import AzureCliCredential, get_bearer_token_provider
    from openai import AsyncOpenAI

    sync = get_bearer_token_provider(AzureCliCredential(), "https://ai.azure.com/.default")

    async def token() -> str:  # openai>=2.x AsyncOpenAI wants an async key provider
        return sync()

    return AsyncOpenAI(base_url=endpoint.rstrip("/") + "/openai/v1/", api_key=token)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def _usage(u) -> dict:
    if u is None:
        return {"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 0}
    d = u.model_dump()
    d.setdefault("input_tokens_details", {}) or {}
    d["input_tokens_details"] = {"cached_tokens": (d.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0}
    d["output_tokens_details"] = {"reasoning_tokens": (d.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0}
    return {k: d[k] for k in ("input_tokens", "input_tokens_details", "output_tokens", "output_tokens_details", "total_tokens")}


async def _cell(client, sem, ns, system_prompt, sample, effort, repeat, commit):
    prompt = system_prompt + "\n\n" + sample.user_input
    kwargs = {"model": ns.deployment, "input": prompt, "max_output_tokens": ns.max_output_tokens}
    if effort is not None:
        kwargs["reasoning"] = {"effort": effort}
    async with sem:
        retries, err, resp, t0 = 0, None, None, time.time()
        for attempt in range(6):
            t0 = time.time()
            try:
                resp = await client.responses.create(**kwargs)
                err = None
                break
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {str(exc)[:300]}"
                if "429" in err or "Timeout" in err or "502" in err or "503" in err:
                    retries += 1
                    await asyncio.sleep(2 ** attempt)
                    continue
                break
        latency_ms = round((time.time() - t0) * 1000)
    ts = datetime.datetime.now(datetime.timezone.utc)
    rec = {
        "experiment_id": ns.experiment_id,
        "benchmark": "01-short-factual",
        "model": ns.family,
        "deployment_name": ns.deployment,
        "model_reported": getattr(resp, "model", None),
        "effort": effort,
        "sample_id": sample.sample_id,
        "sample_idx": sample.sample_idx,
        "repeat": repeat,
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latency_ms": latency_ms,
        "retry_count": retries,
        "status": "ok" if resp is not None else "error",
        "error": err,
        "incomplete_reason": getattr(getattr(resp, "incomplete_details", None), "reason", None) if resp is not None else None,
        "usage": _usage(getattr(resp, "usage", None)),
        "response_text": (getattr(resp, "output_text", None) or "") if resp is not None else "",
        "call_params": {k: v for k, v in kwargs.items() if k not in ("input", "model")},
        "call_metadata": {"system_prompt_sha256": sha256_text(system_prompt), "user_input_sha256": sha256_text(sample.user_input)},
        "endpoint": "https://<resource>.services.ai.azure.com",
        "api_version": API_VERSION,
        "auth_mode": "entra",
        "git_commit": commit,
        "dry_run": False,
        "pricing_snapshot_path": ns.pricing_snapshot,
    }
    name = f"{ts.strftime('%Y%m%dT%H%M%SZ')}_{ns.experiment_id}_{sample.sample_idx:03d}_{ns.family}_{effort or 'null'}_r{repeat}.json"
    out = pathlib.Path(ns.out) / name
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rec


async def _probe(client, ns, system_prompt, sample, efforts):
    rows = []
    for e in efforts:
        try:
            r = await client.responses.create(model=ns.deployment, input=system_prompt + "\n\n" + sample.user_input,
                                              max_output_tokens=ns.max_output_tokens, reasoning={"effort": e})
            rows.append({"effort": e, "accepted": True, "model_reported": r.model})
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            rows.append({"effort": e, "accepted": False, "error": msg[:400]})
    return rows


async def main_async(ns) -> int:
    system_prompt, _tmpl, samples = load_dataset(BENCH, max_samples=20)
    assert len(samples) == 20
    client = _client(ns.endpoint)
    commit = _git_commit()
    if ns.probe_rejected:
        rows = await _probe(client, ns, system_prompt, samples[0], ns.probe_rejected.split(","))
        out = pathlib.Path(ns.out).parent / f"rejected_efforts_{ns.family}.json"
        out.write_text(json.dumps({"model": ns.family, "deployment_name": ns.deployment,
                                   "checked_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(rows, ensure_ascii=False)[:600])
        return 0
    efforts = [None] if ns.efforts in ("", "null") else ns.efforts.split(",")
    pathlib.Path(ns.out).mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(ns.concurrency)
    jobs = [_cell(client, sem, ns, system_prompt, s, e, r, commit)
            for e in efforts for s in samples for r in range(ns.repeats)]
    recs = await asyncio.gather(*jobs)
    bad = [r for r in recs if r["status"] != "ok"]
    print(f"{ns.experiment_id}: {len(recs)} cells, {len(bad)} errors")
    for r in bad[:5]:
        print("  ", r["effort"], r["sample_id"], r["error"])
    return 1 if bad else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True)
    p.add_argument("--deployment", required=True)
    p.add_argument("--family", required=True)
    p.add_argument("--efforts", default="")
    p.add_argument("--experiment-id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--max-output-tokens", type=int, default=4096)
    p.add_argument("--pricing-snapshot", default="pricing/azure-openai-payg-2026-11.yaml")
    p.add_argument("--probe-rejected", default="")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

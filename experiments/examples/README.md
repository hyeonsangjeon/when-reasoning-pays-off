# Examples — driving the experiments from Python

Every experiment in this repo is a YAML file (its **input**) consumed by one
runner under [`scripts/`](../../scripts). The [`experiments`](../__init__.py)
package gives you **one uniform call** so you never have to remember which
runner or flags an experiment needs:

```python
import experiments

spec   = experiments.describe("exp001_short-factual_baseline.yaml")  # read-only, no network
result = experiments.run("exp001_short-factual_baseline.yaml", dry_run=True)  # zero-spend run
```

Run everything below **from the repository root**.

> Just want a green check first? Run `bash scripts/verify_setup.sh` — it
> exercises the catalog, the pure functions, and a runner dry-run with no
> credentials in about 10 seconds.

| # | Example | What it shows | Needs credentials? |
|---|---------|---------------|--------------------|
| 1 | [`quickstart.py`](quickstart.py) | Describe one experiment, then dry-run it | dry-run needs env vars¹ |
| 2 | [`run_any_experiment.py`](run_any_experiment.py) | Run **any** YAML by name (dry-run or evidence) | dry-run needs env vars¹ |
| 3 | [`pure_functions.py`](pure_functions.py) | Call the deterministic primitives directly — literal input → output | **no** — nothing at all |
| 4 | [`describe_all.py`](describe_all.py) | Print the whole catalog (input → variable → output) | **no** — YAML only |

```bash
python experiments/examples/quickstart.py
python experiments/examples/run_any_experiment.py exp006_cache_key_bucketing_inmemory.yaml
python experiments/examples/pure_functions.py          # no setup needed
python experiments/examples/describe_all.py            # no setup needed
# module form works too:
python -m experiments.examples.describe_all
```

## The interface

```python
experiments.describe(config) -> ExperimentSpec
```
Parses the YAML only (no network, no heavy imports). `ExperimentSpec` fields:
`experiment_id`, `config_path`, `runner_module`, `command`, `benchmark`,
`inputs`, `variable`, `output_dir`, `description`. Call `spec.summary()` for a
printable block.

```python
experiments.run(config, *, dry_run=False, allow_dirty=None, extra_args=None) -> ExperimentResult
```
Dispatches to the correct runner's `main([...])` and returns the exit code and
the location of the records (`result.ok`, `result.summary()`).
- `dry_run=True` → forwards `--dry-run`: **no HTTPS call**; synthetic
  zero-usage records are still written under the benchmark's `runs/`.
- `allow_dirty` defaults to the value of `dry_run`: evidence runs
  (`dry_run=False`) require a **clean git tree** so the `git_commit` embedded
  in every raw record is meaningful; dry-runs tolerate a dirty tree.
- `extra_args` are forwarded verbatim (e.g. `["--max-samples", "2"]`,
  `["--smoke"]`).

```python
experiments.list_experiments() -> list[ExperimentSpec]
```
Describe every `exp*.yaml` (the annotated `_template.yaml` is excluded).

## ¹ Credentials for `run(...)`

The runners read Azure endpoint/deployment names from environment variables and
embed them in the audit trail **even in dry-run** (they are recorded, not
called). Copy [`.env.example`](../../.env.example) and set at least:

```
AZURE_OPENAI_FOUNDRY_ENDPOINT   AZURE_OPENAI_DEPLOYMENT_GPT_5_2
AZURE_OPENAI_DEPLOYMENT_GPT_4O  AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED
AZURE_AUTH_MODE                 MAX_COST_PER_BENCHMARK_USD  MAX_TOTAL_COST_USD
```

For a dry-run the **values** are never contacted, so placeholders are fine.
`describe(...)`, `list_experiments(...)`, and `pure_functions.py` need none of
this.

## Every experiment in one line

`experiments.run("<file>", dry_run=True)` for any row below. `describe_all.py`
prints this table live.

| experiment (`.yaml`) | runner | variable swept |
|----------------------|--------|----------------|
| `exp001_short-factual_baseline` (+`_gpt4o`) | `run_benchmark` | `reasoning.effort` sweep (gpt-4o = baseline) |
| `exp002_benchmark02_gpt5_2` (+`_gpt4o`) | `run_benchmark` | `reasoning.effort` sweep (gpt-4o = baseline) |
| `exp003_benchmark03_gpt5_2` (+`_gpt4o`) | `run_benchmark` | `reasoning.effort` sweep (gpt-4o = baseline) |
| `exp004_spillover_baseline_reactive` / `_proactive` | `simulate_spillover` | `policy.type` reactive vs proactive |
| `exp005_dual_spillover_reactive` / `_proactive` | `measure_dual_spillover` | `policy.type` reactive vs proactive (dual endpoint) |
| `exp006_cache_key_bucketing_inmemory` / `_24h` | `measure_cache_key_bucketing` | `bucket_cardinality {1,8}` × `retention {in_memory,24h}` |
| `exp007_max_output_tokens_sweep` | `measure_max_output_tokens_sweep` | `max_output_tokens {256…16384}` |
| `exp_smoke_01` / `_02` / `_03` (+`_gpt4o`, `_warmprobe`) | `run_benchmark` | Phase-1 smoke of the effort sweep |

See [`../README.md`](../README.md) for the full input/variable/output catalog
and the blog-article ↔ experiment map.

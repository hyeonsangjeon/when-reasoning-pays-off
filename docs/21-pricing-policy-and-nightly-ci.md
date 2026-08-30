# Campaign pricing policy and nightly CI

The pricing-aware campaign runners use policy version `1.0.0`:

| Mode | Intended use | Freshness | Network and billing boundary |
| --- | --- | --- | --- |
| `historical-replay` | Dry-run or reanalysis of committed evidence | Wall-clock age is ignored after immutable identity verification | Offline-only; a non-dry invocation is refused |
| `live-measurement` | Any run that can issue a billed request | Snapshot must be no more than 90 days old and not future-dated | Default; refusal happens before endpoint, credential, provider, or network work |

Both modes verify the commit-pinned snapshot ID, repository path, SHA-256,
source URL, accessed date, packaged-copy parity, exact price key, model
family/version, geography, region, deployment type, and currency. A stale live
snapshot is replaced by adding a new immutable snapshot; historical files are
never edited. Every campaign summary embeds the selected mode and verified
record under `pricing_policy`, conforming to
`schemas/campaign_pricing_policy.v1.schema.json`.

Use historical replay only with a dry-run:

```bash
python -m scripts.measure_cache_key_bucketing \
  --experiment experiments/exp006_cache_key_bucketing_inmemory.yaml \
  --dry-run --pricing-policy historical-replay --allow-dirty
```

Omitting `--pricing-policy` selects `live-measurement`. The
`experiments.run(..., dry_run=True)` API adds historical replay automatically
for the three pricing-aware campaign runners. It never adds historical mode to
a live invocation.

## CI badge scope

The **PR fast CI** workflow is the required deterministic pull-request gate. It
checks lint, the batch suite, the root fast subset, schemas, claims, dependency
contracts, public/redaction/docs gates, and wheel/platform compatibility. It
deliberately does not claim collection or execution of the three large campaign
test modules.

The separate **Nightly offline full campaign** workflow runs on a schedule and
by manual dispatch against locked and current dependency graphs. It
machine-verifies collection of each campaign module, then runs batch tests,
non-campaign root tests, and each campaign module in separate pytest processes.
The locked graph layers a separate hash-pinned test-tool lock on the release
lock. The release lock contains runtime and provider packages only; the nightly
test-tool lock contains pytest, pytest plugins, JSON Schema validation, and
their transitive dependencies. Both nightly matrix jobs and the PR-fast locked
regression use `scripts/install_nightly_test_environment.sh`, preventing their
setup paths from drifting apart.

Regenerate the test-only lock without changing the runtime release lock:

```bash
uv pip compile \
  batch-runner/batch_runner/data/dependencies/nightly-test-tools.in \
  --constraint batch-runner/batch_runner/data/dependencies/release-py311-linux-x86_64.txt \
  --generate-hashes --python-platform x86_64-manylinux_2_17 \
  --python-version 3.11 \
  --output-file batch-runner/batch_runner/data/dependencies/nightly-test-tools-py311-linux-x86_64.txt
```

During test execution, credentials are blank, an OS network namespace has no
external interface, and Python socket/DNS entry points are blocked as defense
in depth. Package installation may use the network before these guards are
enabled. Uploaded diagnostics contain only sanitized test outcomes plus commit,
Python, OS, lock hashes, dependency graph, and installed-package metadata.

## Protected live-provider health is a separate signal

The `Protected Azure provider smoke` workflow is neither PR-fast CI nor the
nightly offline campaign. It can run only for `main` by schedule or manual
dispatch, waits on a protected GitHub environment, and targets an Azure
self-hosted runner whose model call uses managed identity. Public PR CI runs
only the no-network fake of the same orchestration. The protected workflow
uploads one short-lived schema-validated health file and no raw run artifacts.
Its badge reports only workflow status; it does not expose protected
configuration. Setup, threat model, health schema, and failure classes are in
[`docs/22-protected-azure-smoke.md`](22-protected-azure-smoke.md).

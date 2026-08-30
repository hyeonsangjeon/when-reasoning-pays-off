# Contributing

Thank you for considering a contribution to this research repository.

This is a research artifact, not a hosted product. The most valuable
contributions are reproducibility reports, new testable hypotheses for the
cache-hit / PTU-throughput / cost-model framework, and improvements to the
decision tools under `batch-runner/`. Pull requests are welcome under the
constraints below.

Please also read:

- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- [`GOVERNANCE.md`](GOVERNANCE.md) — decision-making model and what
  requires owner approval.
- [`SECURITY.md`](SECURITY.md) — how to report security or data-leakage
  issues (not by opening a public issue).
- [`docs/05-methodology.md`](docs/05-methodology.md) — the reproducibility
  contract (frozen; changes go through the process in
  [`GOVERNANCE.md`](GOVERNANCE.md)).
- [`docs/15-spec-vs-inference-taxonomy.md`](docs/15-spec-vs-inference-taxonomy.md) —
  how to cite Azure / OpenAI behaviour (`OFFICIAL_SPEC` Tier 1 vs
  `OPERATIONAL_INFERENCE` Tier 2). Every new claim added to the docs
  must carry a citation tier label.
- [`docs/16-release-tiers-and-redaction-policy.md`](docs/16-release-tiers-and-redaction-policy.md) —
  the public-release tiering (`RAW_PRIVATE`, `SANITIZED_PUBLIC`,
  `AGGREGATE_AZURE_SAMPLE`). Every artifact you propose to add to the
  public tree must carry a tier label.
- [`docs/17-foundry-packaging-relationship.md`](docs/17-foundry-packaging-relationship.md) —
  the conditional channel policy that governs the relationship between
  this repo, a separate downstream Azure AI Foundry sample (if created),
  and the external publication channels (GitHub Pages, Medium, arXiv).

## What kind of contribution

| Kind | Where to start |
| --- | --- |
| Bug report (script or benchmark code does not work as documented) | Open an issue → "Bug report" template. |
| Reproducibility report (you cannot reproduce a published number) | Open an issue → "Reproducibility report" template. Cite the commit SHA, the artifact path, and your environment. |
| New testable hypothesis (extends `docs/07`, `docs/08`, `docs/14`) | Open an issue → "Hypothesis proposal" template. Link the proposal to the relevant `docs/` file and to the spec-vs-inference taxonomy. |
| Documentation fix (typo, broken link, clarification) | Open a PR with a short description. CI must pass. |
| New decision tool, calculator, or library code under `batch-runner/` | Open an issue first to discuss scope. Then a PR with tests under `batch-runner/tests/`. |
| Change to `docs/05-methodology.md` | Frozen. See [`GOVERNANCE.md`](GOVERNANCE.md) escalation. |
| Change to `benchmarks/*/analysis.md` after the analysis has been approved | Frozen post-APPROVE per `docs/16` §6. |

## Pull request checklist

The PR template in [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)
formalizes this. Briefly:

1. **Tier label.** If your PR adds a published artifact, declare its
   release tier (`RAW_PRIVATE` is **forbidden** in the public tree;
   `SANITIZED_PUBLIC` or `AGGREGATE_AZURE_SAMPLE` only).
2. **Redaction clean.** No endpoint hostnames, deployment names, request
   IDs, region identifiers, customer-shape fingerprints, or secret
   patterns. CI runs a defensive grep over the diff; locally you can run
   `bash scripts/check_public_surface.sh` before pushing. **If you edit a
   manifest-tracked public artifact** (e.g. `results/**`, chart data, CSVs),
   re-pin its recorded hash and commit the refreshed
   `release/public_sanitized_manifest.json` in the **same** PR — otherwise
   the release gate fails on a stale `sanitized_sha256` (drift). For a
   token-clean edit to an already-tracked artifact, re-pin from the public
   tree with `python scripts/sanitize_public_artifacts.py --refresh-hashes`
   (no private archive needed); use `--apply` (which needs the private
   redaction archive) only when adding a new artifact or if a workload
   token was reintroduced. Verify locally with
   `python scripts/sanitize_public_artifacts.py --verify --require-public-manifest`.
3. **Citation tier.** If your PR adds a claim about Azure / OpenAI /
   Foundry behaviour, mark it `OFFICIAL_SPEC` (Tier 1) with a vendor URL
   + ISO access date, or `OPERATIONAL_INFERENCE` (Tier 2) with rationale
   and in-repo evidence.
4. **Tests pass.** `pytest -m "not adaptive_calibration" batch-runner/tests/`
   must pass locally. PR fast CI runs the deterministic root subset and the
   non-editable minimal wheel on Ubuntu, macOS, and Windows with CPython 3.11
   and 3.13. The separate nightly workflow runs the complete offline surface,
   including each large campaign suite in its own pytest process.
5. **Ruff clean.** `ruff check .` on any Python you change.
6. **Frozen method changes are explicit.** Approved `benchmarks/*/analysis.md`
   stays untouched. A change to `docs/05-methodology.md` must identify the
   methodology-version change and follow the governance escalation.

## Local development

```bash
git clone https://github.com/hyeonsangjeon/when-reasoning-pays-off.git
cd when-reasoning-pays-off
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[all,dev]"
ruff check .
pytest -q -m "not adaptive_calibration" batch-runner/tests/
bash scripts/run_nightly_offline_tests.sh
python scripts/check_schema_conformance.py schema-meta
python scripts/check_schema_conformance.py artifact-conformance
python scripts/measure_cold_mock.py \
  --threshold-seconds 300 --output cold-mock-timing.json
python scripts/verify_core_wheel.py
```

`run_nightly_offline_tests.sh` blanks provider credentials and blocks Python
socket/DNS access for test execution. It machine-verifies collection of all
three campaign modules without `--ignore`, then runs batch, root, and campaign
suites in isolated processes. For a pricing-aware campaign dry-run, pass
`--pricing-policy historical-replay`; any potentially billed command defaults
to `live-measurement` and must pass the 90-day freshness gate before endpoint
or credential resolution. See
[`docs/21-pricing-policy-and-nightly-ci.md`](docs/21-pricing-policy-and-nightly-ci.md).

The installed package supports CPython 3.11–3.13. The minimal core/sample CLI
is supported on Linux, macOS, and Windows; the full research campaign is not a
native-Windows contract because its release and measurement paths require
POSIX/Bash, `fcntl`, and shell SHA-256 utilities. Use WSL/Linux for a campaign.

### Dependency compatibility and release lock

`pyproject.toml` is the dependency source of truth. `requirements.txt` selects
`.[all]`, while `requirements-dev.txt` selects editable `.[all,dev]`; neither
repeats version floors. CI verifies the duplicated runtime guard floors and
`minimum-direct.txt` pins against project metadata.

The release lock is
`batch-runner/batch_runner/data/dependencies/release-py311-linux-x86_64.txt`.
It is a hash-pinned resolution specifically for CPython 3.11 on Linux x86_64
using manylinux_2_17 wheels. It is not a promise of bit-for-bit resolution on
other platforms. Regenerate and verify it with:

```bash
uv pip compile pyproject.toml --extra all --generate-hashes \
  --python-platform x86_64-manylinux_2_17 --python-version 3.11 \
  --no-emit-package when-reasoning-pays-off \
  --output-file batch-runner/batch_runner/data/dependencies/release-py311-linux-x86_64.txt
python scripts/dependency_inventory.py generate
python scripts/dependency_inventory.py verify
```

The adjacent deterministic inventory records the lock SHA-256, resolver command,
index provenance, scope, and exact package/version list. Release CI installs the
lock with `--require-hashes` in a clean environment and verifies the installed
inventory. Immutable sample manifests record both the lock and inventory hashes,
plus whether the active runtime matches the locked graph.

Enable the shared git hooks once per clone so the same read-only release
and docs gates that run in CI (schema meta-validation, committed artifact
instance conformance, public-manifest integrity, public-surface grep, and static
Pages validation) also run locally before every `git push`:

```bash
git config core.hooksPath .githooks
```

You do not need Azure credentials to run the `batch-runner/tests/`
unit-test suite. Reproducing the original measurements (`scripts/run_benchmark.py`)
does require Azure OpenAI access with a configured deployment; see the
README "Reproducing These Measurements" section.

## Scope notes

- Issues that ask for paid support or commercial integration are out of
  scope. See [`SUPPORT.md`](SUPPORT.md).
- A separate downstream Azure AI Foundry sample repository, if created,
  is governed separately (see `docs/17`). Contributions targeted at
  Foundry sample conformance belong in that downstream repo, not here.

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
   must pass locally. CI also runs the relevant subset of `tests/`.
5. **Ruff clean.** `ruff check .` on any Python you change.
6. **Frozen files untouched.** `docs/05-methodology.md` and approved
   `benchmarks/*/analysis.md` must not be modified by your PR. CI does
   not enforce this yet; reviewers do.

## Local development

```bash
git clone https://github.com/hyeonsangjeon/when-reasoning-pays-off.git
cd when-reasoning-pays-off
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest -q -m "not adaptive_calibration" batch-runner/tests/
```

Enable the shared git hooks once per clone so the same read-only release
and docs gates CI runs (public-manifest integrity, public-surface grep,
static Pages validation) also run locally before every `git push`:

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

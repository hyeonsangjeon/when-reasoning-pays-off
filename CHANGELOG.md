# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
tracks the benchmark task series specified under the private task-spec tree (lab-only).

## [Unreleased]

### Added — Task 031: GitHub Pages public surface + README hero visual (2026-06-03)

Stands up a static, dependency-light GitHub Pages site for the public mirror
and adds a single evergreen hero visual to the README. Rich narratives and any
data visuals live on the site, not in the README; the README carries one hero
and one link only.

- Adds `docs/` static site served verbatim via a `.nojekyll` marker (no
  server runtime, no build framework, no runtime data fetches). Root
  `docs/index.html` detects the browser language and redirects to a locale,
  degrading to English with `<noscript>` and a visible fallback link.
- Explicit per-locale i18n tree at `docs/<locale>/index.html` for `en`, `ko`,
  `ja`, `zh-CN`, and `hi`. English and Korean are authored; Japanese,
  Simplified Chinese, and Hindi are structurally complete placeholders with a
  language switcher, hero, "translation in progress" banner, and a link to the
  authored English page. The switcher is plain `<a>` links (works without
  JavaScript) and marks the active language with `aria-current`; `assets/i18n.js`
  is progressive enhancement only. The pattern and publish flow are documented
  in `docs/i18n.md`.
- Adds exactly one evergreen static hero, `docs/assets/hero.svg` (conceptual,
  no drift-prone numbers), embedded in `README.md` alongside a single project
  site link. No data charts are added to the README.
- Introduces `docs/validate.sh` as the minimal static check (locale coverage,
  language-switcher integrity, hero presence/reference, and a public-surface
  leakage grep); it also defers to the repository's existing public-surface
  checker when present. Wired into CI.
- Public-mirror separation preserved: only sanitized/aggregate-tier content
  appears; no private-tree references, internal identifiers, endpoint
  hostnames, or secrets are introduced. Repository visibility is unchanged.

### Added — Task 034 follow-up: public-safe provenance manifest for SANITIZED_PUBLIC artifacts (2026-06-03)

Closes the final review gate REQUEST-CHANGES on public release readiness
(policy ↔ implementation drift on SANITIZED_PUBLIC provenance). `docs/16-release-tiers-and-redaction-policy.md`
previously said each SANITIZED_PUBLIC artifact carries provenance as a
sidecar manifest or first JSONL row, but the only manifest the sanitizer
actually wrote was the private release manifest (lab-only)
(not publishable). This change introduces a single tracked, public,
deterministic provenance record covering every SANITIZED_PUBLIC artifact.

- Adds `release/public_sanitized_manifest.json` (tracked, public-safe).
  One entry per SANITIZED_PUBLIC artifact, keyed by `artifact_path`.
  Per-entry fields: `artifact_path`, `tier` (`"SANITIZED_PUBLIC"`),
  `sanitized_sha256`, `source_raw_sha256`, opaque `source_archive_id`
  (`raw-<32 hex>` derived from `sha256(source_raw_sha256 + ":" + artifact_path)`),
  `redaction_rules_sha256`, `redacted_at_iso`, `redactor_commit_sha`,
  `redactor_script_sha256`, `sweep_id`. Top-level fields: `schema`
  (`wrpo-public-sanitized-manifest`), `schema_version` (`1.0.0`),
  `tier`, `sweep_id`, `redaction_rules_sha256`, `redactor_commit_sha`,
  `redactor_script_sha256`, `redacted_at_iso`, `entries` (sorted by
  `artifact_path`). No path beneath the lab-only workspace is recorded; no
  `archive_relative_path`; no concrete endpoint URL, deployment name,
  region tag, request id, or secret pattern; only opaque ids and
  SHA-256 digests. Initial entries: 1534.
- `redaction_rules_sha256` is computed over a **public-safe canonical
  description** of the redaction rules — class labels, placeholder
  outputs, and the ordering rule — not over the private workload
  tokens being replaced. A deterministic hash of low-entropy private
  values would otherwise act as an offline confirmation oracle; this
  design keeps the rules hash stable, recomputable by a public-only
  verifier, and free of leakage. `redactor_script_sha256` provides
  provable provenance even when the worktree is dirty and the
  recorded HEAD commit does not yet contain the redactor change.
- `scripts/sanitize_public_artifacts.py` rebuilds the public manifest
  from the private manifest on every `--apply`. For each unique source
  path in the private manifest that still exists in the working tree
  AND is in public sweep scope, one public entry is emitted with
  `sanitized_sha256` set to the **current on-disk file's sha256** (so
  legitimate downstream edits that do not reintroduce forbidden tokens
  are tracked as the new sanitized snapshot) and `source_raw_sha256` /
  `source_archive_id` pinned to the original RAW source from the
  latest matching private entry (deterministic tie-break:
  `archived_at_iso → original_sha → sanitized_sha → path`). The
  top-level `redacted_at_iso` is derived from the maximum per-entry
  timestamp (not wall-clock `now()`), so two consecutive `--apply`
  invocations on unchanged inputs produce byte-identical manifest
  bytes (confirmed live: two back-to-back applies on this checkout
  produced the same SHA-256). The public manifest path is explicitly
  excluded from the candidate sweep and from `is_in_scope` (defensive
  — even a future schema string that matched a replacement key would
  not corrupt the provenance file).
- `--verify` now additionally validates the public manifest: schema,
  schema_version, tier, top-level vs per-entry `redaction_rules_sha256`
  / `sweep_id` / `tier` consistency, no forbidden workload tokens,
  no `.internal/` substring, no Bearer/sk-/AccountKey/`AZURE_OPENAI_API_KEY`
  patterns, entries sorted by `artifact_path` with no duplicates,
  every entry's `source_archive_id` matches the deterministic
  derivation, every entry's `artifact_path` is in scope and the
  on-disk sha256 matches the entry's `sanitized_sha256`. Completeness
  check: if the private manifest exists, every private entry whose
  on-disk file matches its `sanitized_sha256` MUST appear in the
  public manifest (otherwise verify fails with a list of missing
  paths). New `--require-public-manifest` flag enforces presence of
  the manifest at release-readiness CI; without the flag, a missing
  manifest is treated as a soft pass so a fresh-clone verify succeeds.
- `docs/16-release-tiers-and-redaction-policy.md` updated to align
  policy with implementation: §2.2 Provenance rewritten to mandate
  the central tracked public manifest as the canonical provenance
  record (per-file sidecars and first-JSONL-row provenance are not
  used in this repo; any future surface that adds them must also
  represent the same entries in the central manifest). §8.1 Tier
  gate updated to require the public manifest entry per artifact and
  to reference the release-CI verify command. §8.5 stop-the-release
  conditions updated to name `release/public_sanitized_manifest.json`
  explicitly for the SANITIZED_PUBLIC tier.
- 27 new tests added in `tests/test_sanitize_public_artifacts.py`
  (67 total): determinism of `redaction_rules_sha256` and its
  public-safety (no private tokens hashed); `source_archive_id`
  format and (path, sha) sensitivity; `_is_clean_artifact_path`
  rejects absolute / traversal / backslash / `.internal/` paths;
  `public_manifest_entry` rejects unsafe paths and omits forbidden
  tokens; end-to-end synthetic-repo apply + verify clean; tampered
  `sanitized_sha256` detected; injected `.internal/` substring
  detected; duplicate `artifact_path` detected; unsorted entries
  detected; drifted top-level `redaction_rules_sha256` detected;
  completeness check against the private manifest fails when a
  public entry is missing; rebuild of the public manifest after a
  no-op sweep produces byte-identical bytes; a previously-sanitized
  file edited post-sanitization has its `sanitized_sha256` *refreshed*
  to the new on-disk sha while `source_raw_sha256` / `source_archive_id`
  remain pinned to the original RAW source; verify catches a stale
  manifest when a file is edited without re-running `--apply`; the
  public manifest path is excluded from the candidate sweep and from
  `is_in_scope`; the substring-corruption regression (project name
  before resource short name) is re-asserted with the public-
  manifest layer active. Strict `--require-public-manifest` fails
  when the file is absent; default verify treats absence as a soft
  pass.
- `scripts/check_public_surface.sh` requires no change — the new
  `release/public_sanitized_manifest.json` contains only opaque ids,
  hashes, sanitized public paths, and ISO timestamps; it carries no
  `.internal/` substring, no agent role names, no `.github/agents/`
  references, and no secret patterns, so the existing scan passes.
- `.github/workflows/ci.yml` now wires the docs/16 §8.1 release-
  readiness gate into CI as a new step
  (`Sanitizer release gate (forbidden tokens + public manifest)`)
  that runs `python scripts/sanitize_public_artifacts.py --verify
  --require-public-manifest` after dependencies are installed and
  immediately before the existing defensive public-surface grep.
  The gate emits the two confirmations
  (`verify: clean — no forbidden tokens in tracked non-docs files.`
  and
  `verify (public manifest): clean — release/public_sanitized_manifest.json is well-formed and integrity-checked.`)
  and fails the workflow if either is violated, closing the
  first review gate advisory that flagged the prior CI as relying on
  `scripts/check_public_surface.sh` alone. Security posture
  preserved: `pull_request` / `push` triggers only (no
  `pull_request_target`), `permissions: contents: read`,
  `actions/checkout` and `actions/setup-python` pinned to commit
  SHAs, `persist-credentials: false`, `concurrency` cancel-in-
  progress, `timeout-minutes: 15`, defensive empty
  `AZURE_OPENAI_API_KEY` / `OPENAI_API_KEY` / `HF_TOKEN` env on the
  new step (matching the existing pytest steps), no secrets
  consumed, no Azure SDK import path exercised.

### Added — Task 029: spec vs inference taxonomy (zero-spend, 2026-06-03)

Publishes the two-tier citation taxonomy that anchors Tasks 023–028 and
future task templates. Adds `docs/15-spec-vs-inference-taxonomy.md`
(methodology, 220 lines) and `docs/15-spec-vs-inference-taxonomy.examples.md`
(worked-examples appendix, 15 rows, 57 lines). Adds
`batch_runner.methodology` with `Tier` (OFFICIAL_SPEC,
OPERATIONAL_INFERENCE), `Citation`, `assert_well_formed`,
`render_for_docstring`, `render_for_doc_footer`. The library is
deterministic, pure, and stdlib-only (no Azure / OpenAI SDK, no
network, no measurement). Tier 1 citations require a Microsoft Learn /
OpenAI / Azure URL or a pinned SDK source identifier plus an ISO
`YYYY-MM-DD` access date; accepted-host matching uses parsed URL
hostnames (exact match against the allow-list) so lookalike hosts
such as `notopenai.com` and accepted host names appearing only in the
URL path are rejected; SDK sources must be cited as a pinned
identifier string showing source family + file path + `L<line>` +
a non-empty version/tag (bare `Azure SDK` and `OpenAI SDK` strings
are rejected, and mutable `github.com` `blob/main` URLs are rejected
unless re-expressed as a pinned identifier string). Tier 2 citations
require a non-empty rationale (>= 20 chars) and accept in-repo paths.
Verified by 39 focused unit tests including adversarial Tier 1
source-parsing cases and doc audits that every Tier 1 footer bullet
in the new docs carries a URL and ISO date, every Tier 2 footer
bullet carries a rationale or in-repo path, voice-grep is clean, and
the frozen Task 001 methodology doc is untouched.
`docs/05-methodology.md`, the private scaffold spec (lab-only), and all
benchmark `analysis.md` files are unmodified. No `pyproject.toml`
edit, no dependency added.

### Added — Task 028: PTU observability schema — canonical record contract (zero-spend, 2026-06-03)

Adds `batch_runner.observability` with `PTURequestRecord`,
`PTUCellSummary`, `hash_cache_key`, `AZURE_MONITOR_PTU_METRICS`, and
`azure_monitor_correlation_window`. Header field names match Azure
OpenAI PTU Operations Guide Appendix A and B verbatim
(`retry-after-ms`, `retry-after`, `x-ms-region`, `x-request-id`,
`x-ms-deployment-name`, `x-ms-spillover-from-deployment`,
`x-ms-spillover-error`, `x-ratelimit-remaining-requests`); the
`x-ratelimit-*` field is flagged optional on the PTU path.
`retry-after-ms` and `retry-after` coexist as two fields
(`retry_after_ms`, `retry_after_seconds`) for raw fidelity.
`prompt_cache_key_used` is a stable 16-hex-char SHA-256 prefix, never
the raw key. Every field in the emitted JSON Schema files
(`schemas/ptu_request_record.schema.json`,
`schemas/ptu_cell_summary.schema.json`) carries an `official_spec` vs
`operational_inference` category tag per Task 029.
`AZURE_MONITOR_PTU_METRICS` is the frozen Appendix C six-metric tuple
and `azure_monitor_correlation_window` is a pure helper (no Azure SDK,
no network call, no credential read). Documented in
`docs/14-observability-schema.md` (228 lines) with a migration
mapping from Task 013 / 019 / 020 / 021 / 023 / 024 keys. Verified by
29 focused unit tests. No changes to existing measurement scripts, no
new dependencies, no `pyproject.toml` edits.

### Added — Task 027: PTU vs PAYG decision calculator + sizing runbook (zero-spend, 2026-06-03)

Adds `batch_runner.sizing` with a deterministic PTU-vs-PAYG
calculator, model-density loader, and `scripts/ptu_sizing.py` JSON CLI.
The calculator reads explicit local PAYG/PTU pricing snapshots and the
frozen `pricing/ptu-density-2026-05.yaml` Guide §3 density table, then
returns a recommended PTU count, crossover RPM, decision label, and
four-driver diagnostic. The task's worked example (`gpt-5.2`, 1000
prompt, 30% cached, 200 visible, 0 reasoning, 8000 `max_output_tokens`,
60 RPM) returns `recommended_ptu_count = 1632` and
`dominant_driver = "max_tokens_oversize"`. The 8:1 working assumption
for GPT-5.2 / 5.1 family output weighting is labeled as operational
inference in the YAML, calculator rationale, and runbook; `gpt-4o`
remains labeled unspecified. Documented in
`docs/13-ptu-vs-payg-decision-runbook.md`; verified by focused unit
tests and stable CLI JSON output. No live API calls, no environment
variable reads, no pricing auto-fetch, and no dependency changes.

### Added — Task 026: `prompt_cache_key` policy library + sizing runbook (zero-spend, 2026-06-03)

Adds `batch_runner.cache` — a deployable Python library for
deterministic `prompt_cache_key` composition, bucket sizing, and
`prompt_cache_retention` enforcement. `cache_key(...)` composes the
Guide §1 workload-oriented key (tenant + flow + locale + schema +
optional category); `assert_deterministic` / `anti_pattern_reasons`
detect UUIDs, request-id tokens, timestamp tokens, and long
digit/hex runs without ever echoing the offending key value back
into error messages or logs. `recommended_bucket_count(...)` returns
both the official-spec minimum (against the 15 req/min threshold)
and the recommended count (against `target_rpm_per_bucket`, default
10 — labeled as operational inference per Task 029); reproduces the
Guide §1 worked example (1.4 TPS → 84 RPM → 6 minimum, 9
recommended). `ensure_explicit(model_id, retention)` enforces the
Guide §2 "must be explicit" rule for the eleven listed models,
raising `ImplicitInMemoryError` when callers omit
`prompt_cache_retention` on a model whose documented default is
`in_memory`. The library imports no `time` / `random` / `datetime` /
`uuid` modules — composition and sizing are pure functions of their
inputs. Documented in `docs/12-prompt-cache-key-policy.md` (238
lines). Verified by 41 unit tests across
`batch-runner/tests/test_key_composition.py`,
`batch-runner/tests/test_bucket_sizing.py`, and
`batch-runner/tests/test_retention_policy.py`. No changes to
`pyproject.toml`; no new dependencies.

### Added — Task 025: Multi-worker PTU cooldown coordination (zero-spend, 2026-06-02)

Adds `batch_runner.ptu.cooldown_coordinator` and
`batch_runner.ptu.cooldown_backends` — a coordination layer that wraps
the Task 023 `AdmissionController` so N workers receiving 429s with
the same `retry-after-ms` do not resume at the same wall-clock
instant. The slot-claim mechanism is labeled as **operational
inference** per Task 029 (the PTU Operations Guide is silent on
cross-worker retry-timing coordination). The coordinator does NOT own
a retry budget; the wrapped controller remains the single retry owner.
Default backend is process-local (`InMemoryCooldownBackend`); a
`KeyValueCooldownBackend` interface example is included but no
external client library is added to `pyproject.toml`. Documented in
`docs/11-multi-worker-cooldown.md` (192 lines). Verified by 19 unit
tests and a 20-worker thundering-herd concurrency simulation
(`batch-runner/tests/test_cooldown_coordinator.py`,
`batch-runner/tests/test_cooldown_thundering_herd.py`); N=1 invariant
test confirms zero added latency vs the bare Task 023 controller.

### Added — Task 024: PTU utilization estimator and offline replay simulator (zero-spend offline, 2026-06-02)

Adds `batch_runner.ptu.utilization_model` and `batch_runner.ptu.replay_simulator`
plus the CLI `scripts/replay_ptu_utilization.py` — a pure, offline
token-bucket replay of legacy Task 013 / Task 019 source JSONLs against
the Azure OpenAI PTU Operations Guide §0 admission-reservation formula
and the Guide §3 Input TPM / PTU capacity table.

Process and result:

- Implementation worker (analyzer role, local implementation worker on Mac Mini local) wrote the pure model, source-aware legacy adapters
  for `task013_dual_spillover` and `task019_max_output_tokens_proxy`,
  the deterministic 1-D leak-constant calibration (grid + golden-section
  refine, recorded in `calibration.json`), the source-run holdout
  protocol, and the validation report writer. No Azure / OpenAI / HTTP
  client is imported anywhere in the new modules or CLI — a static
  no-network guard enforces this in `test_replay_simulator.py`.
- Methodology v2 audit (local CLI reviewer worker) approved the spec ahead of
  implementation; this entry preserves its handoff notes: one-unit
  token bucket only; capacity is the Guide §3 table value, never fit;
  source labels preserved in every aggregate; Task 019 always carried
  as PAYG-throttled-quota proxy evidence; default validation mode does
  not mutate state on observed-429 records; holdout residuals are
  reported separately from in-sample residuals; reports use only
  descriptive percentile / mean-and-std language with caveated wording
  and avoid generalization beyond these source runs and any causal
  wording stronger than "consistent with".

Deliverables:

- `batch-runner/batch_runner/ptu/utilization_model.py` — pure typed
  `admission_cost_tokens`, `capacity_tokens`, `leak_tokens`, with a
  read-only `INPUT_TPM_PER_PTU` Guide §3 table.
- `batch-runner/batch_runner/ptu/replay_simulator.py` — token-denominated
  replay loop, source adapters with timestamp / prompt-cache / max-output
  priority, retry-after-ms normalization, zero-usage-429 demand recovery
  from nearest `(source_run_id, cell_key)` accepted neighbor, completion
  reserve / release approximation, deterministic 1-D calibration, LOSO
  fold builder, confusion-matrix and percentile helpers.
- `batch-runner/batch_runner/ptu/__init__.py` — extends the Task 023
  exports without removing any prior symbol.
- `scripts/replay_ptu_utilization.py` — CLI accepting explicit JSONL
  inputs and either `--ptu-count` or `--deployment-tpm-quota`, writing
  `benchmarks/10-replay-validation/{calibration.json, validation.md}`
  and `results/replay-validation/predicted_vs_observed_*.png`.
- `batch-runner/tests/test_utilization_model.py` and
  `batch-runner/tests/test_replay_simulator.py` — formula correctness,
  capacity lookup, leak monotonicity, hand-computed onset, retry-after
  token-overshoot derivation, reservation release, observed-429
  no-mutation invariant, adapter priority rules, zero-usage demand
  recovery, deterministic calibration, deterministic LOSO folds, and a
  static no-network import guard over the replay module / CLI.
- `benchmarks/10-replay-validation/{README.md, calibration.json,
  validation.md, runs/.gitkeep}` — benchmark scaffold; `calibration.json`
  and `validation.md` are overwritten by each CLI run.
- `results/replay-validation/.gitkeep` plus the two generated charts on
  first CLI run.

Caveats explicitly carried into every report:

- The simulator is an approximate replay / candidate input for capacity
  planning with source-run caveats; it is not a generalized PTU
  predictor across deployments without re-calibration.
- Task 019 is PAYG-throttled-quota proxy evidence, not direct PTU
  validation; Task 013 uses Azure PAYG deployments shaped to expose a
  PTU-like saturation pattern.
- Task 028 canonical schema is not used; inputs are legacy-adapter
  normalized.
- Guide §3 model-specific output-token weighting ratios are not modeled
  in v1; reasoning tokens are reported as observed output composition
  only.
- The numeric `k_leak_tokens_per_ptu_per_second`, the PAYG TPM-quota to
  effective-PTU mapping, the completion reserve / release approximation,
  and the zero-usage-429 demand recovery are operational inference per
  Task 029.

### Added — Task 023: header-driven PTU admission controller (deployable runtime component, 2026-06-02)

Adds `batch_runner.ptu.admission_controller` — a small, auditable
client-side component that honours the Azure OpenAI `retry-after-ms`
response header on 429 as the official admission signal (PTU
Operations Guide §0). Implements the Guide's Option A (SDK Defaults,
made observable) and Option B (PAYG fallback via `max_wait_ms=0` plus
a `fallback` callable). Native spillover (Option C) remains owned by
Task 021. No Azure resources mutated; no live API calls; no
`scripts/*.py` modified.

Deliverables:

- `batch-runner/batch_runner/ptu/admission_controller.py` — controller
  with typed exceptions `AdmissionExhausted`, `WaitExceedsCeiling`,
  `DoubleRetryError`, plus the `ThrottleEvent` dataclass. Parses
  `retry-after-ms` first, falling back to `retry-after` seconds × 1000.
  Enforces the Guide §0 single-owner retry rule by refusing any client
  whose `max_retries > 0` at construction. Surfaces an allow-list of
  safe response headers only (`x-request-id`, `x-ms-region`); never
  logs request body, prompt, `messages`, `Authorization`, `api-key`,
  cache keys, or env-var values.
- `batch-runner/batch_runner/ptu/__init__.py` — public exports.
- `batch-runner/tests/test_admission_controller.py` — 24 pytest cases
  (no network); covers header parsing precedence, missing-header
  zero-wait behaviour, persistent-429 exhaustion, ceiling-with/without
  fallback (pre-jitter admission decision, both shrink-jitter and
  grow-jitter near-ceiling directions), Option B immediate-fallback
  shape, single-owner retry refusal (positive / zero / missing /
  non-integer `max_retries`), safe-header allow-list, observer
  exception isolation, identity-jitter determinism, default-jitter
  bounds, `parsed_wait_ms` preservation under jitter, and construction
  validation.
- `batch-runner/tests/fixtures/retry_after_fixtures.py` — synthetic
  429/200 response objects and a `FakeSDKClient` stand-in.
- `docs/10-ptu-admission-controller.md` (170 lines) — operator-facing
  doc: why, three options A/B/C, sync/async/fallback recipes,
  single-owner retry rule, provisional log keys (Task 028 not yet
  APPROVE'd), non-goals, methodology compliance.

### Added — Task 021 v2.1 Stage 0 only: native-spillover feasibility gate (zero LLM spend, 2026-06-02)

Implements Stage 0a (read-only Azure CLI verification) + Stage 0b
(capped same-API Responses/Foundry v1 preflight, `preflight_hard_ceiling_usd: 0.10`)
of Task 021 v2.1. **No Azure resources mutated. No Stage 1 spillover-fire
proof smoke. No full comparison run. No `spilloverDeploymentName` set.
No PTU deployments created.**

Deliverables:

- `scripts/preflight_native_spillover.py` — Stage 0a + 0b automation.
  Read-only `az` invocations only (mutating verbs `create`/`update`/`set`/
  `delete`/`add`/`remove`/`replace`/`patch` unconditionally refused by
  `run_az_readonly`). Stage 0b enforces the spend ceiling via a
  pessimistic dry-run cost estimate BEFORE any network call. SDK is
  lazy-imported so the script runs in environments without
  `openai` / `azure.identity` installed. Anonymization invariant
  enforced by `assert_no_secrets` over the full PREFLIGHT_LOG content
  pre-write; only env var **names**, derived booleans, SKU aliases,
  and header-name presence are emitted (never endpoint hostnames,
  tenant/sub IDs, RG names, resource IDs, auth headers, bearer
  tokens, API keys, raw `az` JSON, or env-var values).
- `tests/test_preflight_native_spillover.py` — 70 focused tests
  covering: redaction; anonymization-pattern coverage of endpoints,
  UUIDs, ARM resource IDs, bearer tokens, JWT-like strings, api-key
  headers; mutation refusal for all eight tracked verbs; SKU
  normalization; Stage 0a verdict branches (PTU+property →
  `READY_FOR_SMOKE_PROOF`, PAYG+property → `CONFIG-MISSING`,
  PAYG no-property → `INFEASIBLE-AS-SPEC'D`, CLI failure →
  `CONFIG-MISSING`); Stage 0b env-missing → `SAME-API-FAIL` without
  value leak; dry-run skips network; cost-ceiling aborts before
  network I/O; happy-path via seam; non-200 → fail; exception
  message anonymized to class name only; Stage 0c branching matrix;
  PREFLIGHT_LOG append-only writer; writer refuses anonymization
  violations cleanly without leaving partial files; FEASIBILITY_FINDING
  cites the spillover-doc URL + 2026-06-02 access date; CLI smoke.
- `benchmarks/09-native-spillover/README.md` — methodology, scope
  guardrails, baseline caveat (`primary_real_429_count = 0`;
  `spillover_request_fraction ≈ 0.988764`), and the header-absence-is-EXPECTED
  policy stated verbatim.
- `benchmarks/09-native-spillover/PREFLIGHT_LOG.md` — append-only.
  Contains the Stage 0 run records from this local Mac Mini
  environment. The latest run resolves account/RG via read-only `az`
  discovery (values redacted) and records Stage 0a
  **INFEASIBLE-AS-SPEC'D** (`sku_alias: PAYG/GlobalStandard`,
  `spilloverDeploymentName` absent, no owner opt-in to provision a
  PTU primary) and Stage 0b **SAME-API-OK** (one ordinary
  non-spillover Responses API call within the `$0.10` ceiling;
  absence of `x-ms-spillover-from-deployment` is expected on a
  non-spillover preflight and is NOT a `HEADERS-UNSUPPORTED`
  finding). Stage 0c next_action: `PRODUCE_FEASIBILITY_FINDING`,
  kind `INFEASIBLE-AS-SPEC'D`. Sources cited (spillover doc +
  Responses-API doc, last accessed 2026-06-02).
- `benchmarks/09-native-spillover/FEASIBILITY_FINDING.md` — closure
  document for the Stage 0c `INFEASIBLE-AS-SPEC'D` branch. Records,
  per-mode and against the current deployment: Mode A observed
  absent (`spilloverDeploymentName` not set; primary SKU
  `PAYG/GlobalStandard` is not PTU); Mode B not exercised / not
  proven under this feasibility gate (Stage 0b issues only an
  ordinary non-spillover Responses API call and MUST NOT set
  `x-ms-spillover-deployment`; Stage 1 spillover-fire proof smoke
  and the full head-to-head comparison were not executed); Mode C
  not granted (no owner opt-in; no Azure mutation). Cites both
  source URLs with 2026-06-02 access date.
- `benchmarks/09-native-spillover/runs/.gitkeep` — placeholder for
  any future smoke / full-comparison run JSONL, gated on a separate
  approval.
- `results/native-spillover-comparison/.gitkeep` — placeholder for
  any future analysis output, gated on the same approval.

Notes:

- Stage 1 spillover-fire proof smoke and the full head-to-head
  comparison remain BLOCKED per spec until a future commit re-runs
  Stage 0 in an environment where the required env vars are populated,
  the Stage 0a verdict is `READY_FOR_SMOKE_PROOF`, and Stage 0b is
  `SAME-API-OK`.
- `scripts/measure_dual_spillover.py` is unchanged.
- No `prompt_cache_key` / `max_output_tokens` / `retry-after-ms` work
  in this commit (Tasks 018 / 019 / 020 unchanged).

### Added — Task 020: `retry-after-ms` recovery-curve characterization (zero spend, 2026-06-02)

Pure re-aggregation over existing immutable Task 013
(`benchmarks/05-dual-spillover/runs/*.jsonl`) and Task 019
(`benchmarks/07-max-output-tokens-reservation/runs/*.jsonl`) JSONL
streams. **Zero new LLM spend.** No API calls. No network. No client
imports (`openai` / `AzureOpenAI` / `AsyncAzureOpenAI` / `requests` /
`httpx` / `aiohttp` / raw `socket.create_connection` all absent from the
new script).

Deliverables:

- `scripts/retry_after_ms_characterization.py` — source-aware aggregator.
  Allowlisted CLI (`--benchmarks` restricted to `05-dual-spillover` and
  `07-max-output-tokens-reservation`; fails closed otherwise). Source-aware
  429 selectors (`real_429_observed` for Task 013; `429_observed` OR
  `first_429_metadata` for Task 019). Numeric `retry_after_ms` kept as
  ms; numeric `retry_after` converted from seconds × 1000; non-numeric
  HTTP-date `retry_after` skipped **and counted** in
  `counts.http_date_retry_after_skipped` (never silently dropped). Also
  bootstraps `benchmarks/08-retry-after-characterization/README.md` and
  `analysis.md` from embedded templates on first run.
- `tests/test_retry_after_ms_characterization.py` — covers parse rules,
  source-aware selectors, per-source provenance, sparse / imbalanced
  flags, overshoot-not-computable, allowlist guard, no-network static
  check (forbidden imports absent), no-network runtime check (socket /
  http.client / urllib monkeypatched to raise), anonymization grep over
  generated outputs.
- `benchmarks/08-retry-after-characterization/{README.md, analysis.md,
  analysis.json}` — generated by running the script.
- `results/retry-after-characterization/{retry_after_ms_histogram.png,
  retry_after_ms_cdf.png, retry_after_ms_events.csv,
  retry_after_ms_percentiles.csv}` — generated by running the script.

Reporting / methodology:

- Empirical percentiles only (`p10/p50/p90/p99/min/max/count`),
  per-source and combined. **No** inferential-test wording,
  interval-estimate claims, or causal / reset-formula language.
- Per-source counts (`task013_429`, `task019_429`) and HTTP-date skips
  surfaced separately; combined view never erases per-source provenance.
- `distribution_shape` records the observed clustered / integer-ms
  quantized appearance so `analysis.md` explicitly answers whether these
  source-run values look quantized or continuous.
- Sparsity flag set when `total_429 < 50`; imbalance flag set when one
  source contributes ≥ 80% of combined events.
- `correlation_with_overshoot` is emitted as
  `{"status": "not_computable", "reason": ...}` because Task 013 v2
  records expose no numeric per-record projected / admitted utilization
  proxy and Task 019 records expose `arrival_rpm_at_request_time` but no
  calibrated capacity denominator (`selected_peak_tps` is null in the
  available calibration outcomes). No scatter PNG is emitted.

Caveats carried forward into all generated outputs:

- Task 019 source is **PAYG-throttled-quota, not direct PTU evidence**.
- Task 013 source is **workload-shaped, not customer-attributed**.
- Findings are scoped to **these source runs only**; do not generalize
  across tenants, regions, deployments, model versions, or time periods.
- Customer-facing advice limited to: **honor the `retry-after-ms` /
  `retry-after` header Azure returns**.

To regenerate every Task 020 output:

```bash
python -m scripts.retry_after_ms_characterization \
  --benchmarks 05-dual-spillover,07-max-output-tokens-reservation \
  --out benchmarks/08-retry-after-characterization/analysis.json
```

### Fixed — Task 019 v2.7 follow-up: adaptive-summary sidecar writer `_DeploymentBlock` attribute fix (2026-06-02)

**Fresh4 outcome** (run prefix
`benchmarks/07-max-output-tokens-reservation/runs/20260602T022643Z_exp007_max_output_tokens_sweep_calibration`,
artifacts:
`.jsonl` sha256
`050a84e77b3ed519e0b86842ab012f445be06dc718de2a4c894df87b93589867`,
`.result.json` sha256
`11b7954f21cbe93f02915f1cc571f55cfd51341de8381e79fb8b6962af988343`,
`.summary.json` sha256
`5a877b60e9159b1763cf2ed9fa1c445d1eda49054f7148df90fe715309c57a09`):
v2.7 cache-key sanitization (commit `e9faa9c`) was live-verified —
**0 × HTTP 400 / 0 × `BadRequestError`** across 427 HTTP 200 responses
and 2 HTTP 429 responses; 430 JSONL records persisted (4 failed: 2 ×
`rate_limited_observed`, 2 × transient
`transport_exception:APIConnectionError` retries). The calibration
nevertheless terminated as
`outcome=no_promotable_contrast_at_this_prompt_deployment`
(`selected_peak_tps=null`, `selected_via=null`, `n_probes=6`,
`n_bracket_points_evaluated=3`) because the explored TPS envelope
(0.33–0.50 rps at `max_output_tokens=16384`) did not cross the
deployment's PAYG 429 admission ceiling at any depth. Smoke and
evidence promotion were **not attempted** — the v2.4 / v2.5 contract
requires `outcome=selected` with non-null `selected_peak_tps` for
promotion, neither of which holds. This is a **negative finding** for
the prompt+deployment combination, not a wiring bug. PAYG, not PTU.

### Blocked run — Task 019 v2.5

Run prefix:
`benchmarks/07-max-output-tokens-reservation/runs/20260602T022643Z_exp007_max_output_tokens_sweep_calibration`
journaled in
`benchmarks/07-max-output-tokens-reservation/live-v2.5-adaptive-contrast.md`
as `## Blocked run — 2026-06-02 — Fresh4 v2.7 clean live calibration; no promotable contrast (PAYG, not PTU)`.

**Secondary defect observed during Fresh4** (non-fatal to primary
artifacts; this CHANGELOG entry is the fix):

```
ADAPTIVE_SUMMARY_WRITE_FAILED AttributeError:
    '_DeploymentBlock' object has no attribute 'model'
```

The optional adaptive-summary sidecar file (
`<ts>_<exp>_adaptive_calibration_summary.json`) failed to write because
`_write_adaptive_calibration_summary` in
`scripts/measure_max_output_tokens_sweep.py` (~line 8175 in the
payload dict) read `cfg.deployment.model`, but `_DeploymentBlock`
(defined at line 1927) exposes `deployment_name`, `deployment_template`,
`family`, `version`, `endpoint_env`, `deployment_env`, `auth_mode`,
`tpm`, `rpm` — and no `model` attribute. The Fresh4 calibration
caught this in the `except Exception` guard around the writer call;
the authoritative `.result.json` / `.summary.json` / `.jsonl` were
written and verified by sha256, no measurement data was lost.

**Fix:** swap the single buggy reference to `cfg.deployment.family`,
mirroring the convention used by every other "model"-keyed result
payload in this module (calibration result writer, smoke summary
writer, evidence summary writer, planner audit dump — all use
`cfg.deployment.family`). The change is a 1-line semantic correction
plus an explanatory comment tying the choice back to the convention.

**Tests:** 5 new focused regression tests in
`tests/test_measure_max_output_tokens_sweep.py`:

- `TestV27DeploymentBlockHasNoModelAttribute_v27::test_deployment_block_exposes_deployment_name_and_family`
- `TestV27DeploymentBlockHasNoModelAttribute_v27::test_deployment_block_has_no_model_attribute`
- `TestV27AdaptiveSummaryWriterUsesFamilyNotModel_v27::test_writer_does_not_reference_cfg_deployment_model`
- `TestV27AdaptiveSummaryWriterUsesFamilyNotModel_v27::test_writer_uses_cfg_deployment_family_for_model_field`
- `TestV27AdaptiveSummaryWriterUsesFamilyNotModel_v27::test_no_callsite_in_module_reads_deployment_model`

These pin both the dataclass surface (`_DeploymentBlock` has
`deployment_name` + `family`, has no `model`) and the writer's source
(must not read `cfg.deployment.model`; must read `cfg.deployment.family`).
The 5 new tests pass; the broader adaptive-related suite (152 tests
under `-k "adaptive or AdaptiveCalibration or V26 or V27"`) all pass.

**Live re-run:** none required. The fix is deterministic at the
unit-test layer and does not alter v2.4 prompt-identity bytes,
prompt-cache-key bytes, any §10 RFC value, any selection threshold,
or the authoritative `.result.json` / `.summary.json` / `.jsonl`
artifact contents. The only behavioural change is that the optional
adaptive-summary sidecar file now writes successfully on future
adaptive-triggered calibrations (including no-contrast outcomes).

**PAYG, not PTU.** All Fresh4 observations above are PAYG proxy
measurements against the `ptu-deploy-throttled` GlobalStandard deployment.
No PTU-specific causal claim is made or implied.

### Fixed — Task 019 v2.7: Azure-safe adaptive cache-bucket-key + per-record adaptive_step telemetry (2026-06-02)

**Fresh3 outcome** (run `20260602T010212Z_exp007_max_output_tokens_sweep_calibration.jsonl`):
Stage 0.5.C entered cleanly after the v2.6 base-key TPS fix (commit
`302ff8e`), but every adaptive probe call returned HTTP 400
`BadRequestError` from Azure / Foundry v1. The dispatcher terminated
after 133 consecutive BadRequestErrors; final JSONL counts
`records=563, 429=2, failed=142`, `failed_by_reason` dominated by
`transport_exception:BadRequestError`.

**Root cause:** the v2.6 `build_adaptive_cache_bucket_key` composer in
`scripts/task019_v25_adaptive.py` produced `prompt_cache_key` values
of shape
`task019_calib_<hash>_cell16384_tps0676::adaptive::step2_expansion::role=largest::tps=0.676001`
containing provider-hostile punctuation — colons (`::`), equal signs
(`=`), and dots (`.`). Azure's Responses API `prompt_cache_key` field
accepts only `[A-Za-z0-9_-]` and rejects the rest with HTTP 400
before the request reaches model dispatch.

**Fix (v2.7):**

- `build_adaptive_cache_bucket_key` now emits keys of shape
  `{v24_base}_adp_{step_abbr}_{role_abbr}_t{microtps:08d}` (e.g.
  `task019_calib_11090ffe_cell16384_tps0676_adp_s2exp_lg_t00676001`),
  exclusively over the `[A-Za-z0-9_-]` charset, deterministic per
  `(v24_base, step, role, tps)`, with TPS encoded at µTPS precision
  (8-digit zero-padded integer, range 0 < tps < 100). Step and role
  use a short abbreviation table (`s1obs/s2exp/s3brk/c2rep` and
  `lg/sc`) so the composed key stays compact.
- New module-level `ADAPTIVE_BUCKET_KEY_RE = ^[A-Za-z0-9_-]+$` is
  exported and used both as a defensive post-composition assertion
  inside the composer and by regression tests.
- `_assemble_record` and `_run_cell` now accept an optional
  `adaptive_step` kwarg; `_probe_once` forwards it end-to-end so every
  JSONL record dispatched under an adaptive Stage 0.5.C probe — including
  failure-path records (transport exceptions, 429s) — carries the
  originating step. Fresh3 records had `adaptive_step=None` on all 133
  failed transport calls; v2.7 closes that telemetry gap so post-hoc
  triage can attribute failures to the originating adaptive step.

**Tests:** 18 focused tests
(`TestV25AdaptiveCacheKeySuffix_1155`,
`TestV27AdaptiveCacheKeyProviderSafety_v27`,
`TestV27AdaptiveStepTelemetryPlumbing_v27`,
`test_cache_bucket_key_uses_v25_helper_format`) pass; broader
adaptive-related suite (147 tests) all green. Pre-existing unrelated
freshness-window failure
(`TestBracketSelectionSerialization_FixLoop6::test_validate_calibration_result_accepts_bracket_phase`,
calibration fixture timestamp > 24h) is not caused by this change.

**Live retry status:** live calibration retry is recommended after
methodology audit/review of this patch. The fix is deterministic at
the unit-test layer and does not alter v2.4 prompt-identity bytes
(only the cache-bucket-key composer changes); the audit risk is that
Azure's accepted `prompt_cache_key` grammar is undocumented and a
narrower charset may still surface. Mitigation: if v2.7 produces
fresh 400s, the abbreviation table is the single point to tighten
further (e.g. drop hyphens, shorten further).

### Added — Task 019 v2.5: adaptive contrast calibration for unstable / non-separable deployment boundaries (PAYG, not PTU) (2026-05-31)

One-line summary: a pre-registered, auditor-gated adaptive Stage 0.5.C
that role-separates the v2.4 single-bracket search, evaluates a
deterministic C1 (strict separating TPS) / C2 (replicate-gated onset
separation) / C3 (first-class no-promotable-contrast terminal)
criterion, and pins every RFC value under v2.5 microfix #1 + #2 so
loosening any threshold requires a fresh spec revision (v2.6).

v2.5 microfix #1 + #2 PINNED RFC table (§10):

| RFC | PINNED value |
|---|---:|
| `adaptive_expansion_factor` | `1.5` |
| `adaptive_expansion_probes_max_per_role` | `2` |
| `adaptive_bracket_depth_max_per_role` | `3` |
| `adaptive_c2_replicates_max_per_role` | `1` |
| `c2_onset_separation_margin_tps` | `0.05` |
| `adaptive_calibration_max_usd` | `$25` |
| `adaptive_calibration_wall_time_max_minutes` | `45` |
| `adaptive_apiconnectionerror_consecutive_max` | `3` |
| `min_remaining_usd_for_adaptive_entry` | `$8` |
| `min_remaining_usd_for_expansion` | `$3` |

Task 019 total budget cap delta: **+$25** (v2.4 `$405` → v2.5 `$430`)
under the separate adaptive envelope.

§11 test delta: **+32 v2.5 tests** (§11.24–§11.55, including microfix
#1 additions §11.47–§11.55) in
`tests/test_measure_max_output_tokens_sweep.py`. The v2.4 §11 plan is
preserved verbatim.

Added files:

- `scripts/task019_v25_adaptive.py` — single-source-of-truth module
  for §10 RFC PINNED values, v2.5 schema strings, §0.2 onset
  eligibility, §0.8 same-TPS aggregation, §4.1–§4.3 planner helpers,
  §5 C1/C2/C3 evaluators, §9.0/§9.1/§9.2/§9.3 validators, §0.9 + §3.2
  YAML preflight, §0.10 PAYG-proxy wording lint, §0.5 + §11.50
  live-artifact + CHANGELOG lint.
- `benchmarks/07-max-output-tokens-reservation/live-v2.5-adaptive-contrast.md`
  — append-only live-run journal (header section only at PIN time).
- `benchmarks/07-max-output-tokens-reservation/prior-calibrations-disclosure.json`
  — empty JSON list fixture so a future flip of
  `runtime.adaptive_calibration.enabled` to `true` does not surprise
  the operator at preflight (§0.9).
- `experiments/exp007_max_output_tokens_sweep.yaml` — new
  `runtime.adaptive_calibration.*` block (default `enabled: false`).
- `scripts/measure_max_output_tokens_sweep.py` — `load_experiment`
  now invokes the §0.9 + §3.2 preflight validator.

### Live run — Task 019 v2.5

_No live v2.5 calibration attempts yet. Per §0.5 / §11.50, every
future v2.5 attempt MUST land here as one entry per attempt,
referencing the `runs/<ts>_<exp>_calibration*` prefix path, the
terminal artifact sha256, the outcome, the total spend in USD, the
§10 PINNED RFC assumptions in effect, the measurements (per-role
onset intervals, C1/C2/C3 trace, §4.4 caps state), every fix
attempted, every blocker, and an explicit forward-looking statement
("Why execution cannot move forward" or "What the next attempt will
change")._

### Blocked run — Task 019 v2.5

#### 2026-06-02 — first live calibration dispatched; selected via v2.4 bracket_search; smoke promotion gate denied (no v2.5 path engaged)

- Run prefix: `benchmarks/07-max-output-tokens-reservation/runs/20260601T223532Z_exp007_max_output_tokens_sweep_calibration`
- Terminal artifact sha256:
  `88a2afb41418f4bdbe2636ed5e5ea07ffb8eb49d8773f12a5a3c9c5e8ac05805`
  (calibration `result.json`; summary sha256
  `ebc737d69fd9264acc2c553f0d2378cbdda70afce5c34f8b4a40c693f5fe9afa`;
  jsonl per-probe records sha256
  `c34c04643653cb6c4f5e95e4a73076d0664deed7000f2400cf51f56df2f630d4`;
  smoke v2.4 abort-envelope sidecar
  `benchmarks/exp007_max_output_tokens_sweep/runs/20260601T233416Z_exp007_max_output_tokens_sweep_smoke.summary.json`
  sha256
  `9ca5ab170242b18f969dd9a351eed28aaf2442325acf2e454485571f36a55c29`).
- Outcome: calibration `outcome=selected`,
  `selected_via=bracket_search`, `selected_at_phase=bracket`,
  `selected_peak_tps=0.47469318448182934`. Smoke preflight blocked
  with `empirical_promotion_denied_reason=empirical_promotion_disabled_cache_hit_below_floor`
  and cold-cache fallback failure
  `smallest_overshoots_lower_threshold`
  (`exit_reason=TPM_FEASIBILITY_ABORT`,
  schema `task019.v2.4.abort_envelope`).
- Total spend: `$3.057047` calibration (committed `$5.823`); `$0.00`
  incremental smoke (preflight abort before any cell dispatched).
- Pinned §10 RFC assumptions: unchanged from the v2.5 RFC table above.
  The temporary dispatch YAML for this run had
  `runtime.adaptive_calibration.enabled: true` installed under an
  in-file methodology audit gate APPROVE phrase, so the v2.5/v2.6
  adaptive Stage 0.5.C path was *armed* at dispatch. The §3.2
  adaptive Stage 0.5.C runtime trigger predicate did not match the
  v2.4 `bracket_search` selected outcome (operator log:
  `adaptive_trigger_not_matched_outcome_not_in_predicate_set`), so no
  adaptive Stage 0.5.C dispatch fired and no
  `task019.v2.5.adaptive_calibration_summary` / `task019.v2.6.*`
  records were produced. The terminal calibration artifact remains a
  v2.3/v2.4 `calibration_result` selected via `bracket_search`, and
  the downstream smoke / evidence promotion path that was evaluated
  is the v2.4 §10 empirical-promotion gate. Entry is journaled per
  §0.5 / §11.50 as the live-run log of record.
- Measurements: at the selected bracket point the largest probe
  observed `first_429_arrival_rpm=29`, `n_429_records=1`,
  steady-state cache-hit ratio `≈ 0.8829` (above the
  `cache_hit_floor_largest = 0.80` PIN); the smallest control probe
  observed `n_429_records=0` with steady-state cache-hit ratio
  `≈ 0.6544`, **below** the `cache_hit_floor_smallest_control = 0.80`
  PIN. TPM projector (deterministic-conservative) at the selected
  TPS: `smallest_tpm = 68_754.6` overshoots the lower-TPM corridor
  vs deployment cap `60_000`.
- Fixes attempted: none — calibration completed cleanly; the smoke
  block is a methodology-correct denial. Mini-probe revalidation was
  considered as the narrowest forward step and rejected at audit:
  methodology audit gate returned `REQUEST-CHANGES` because v2.4 §7 /
  §3.1 restrict `mini_probe_revalidated` to attempts where
  invariant 12 (freshness) is the **sole** denial; the active
  denial here is invariant 5 (cache-hit floor).
- Blockers:
  1. empirical-promotion gate denial
     `empirical_promotion_disabled_cache_hit_below_floor` on the
     smallest-control probe at the selected TPS;
  2. cold-cache strict fallback denial
     `smallest_overshoots_lower_threshold` — smallest cell already
     exceeds the lower TPM corridor by construction at the selected
     TPS;
  3. mini-probe revalidation is **not** authorized for this denial
     class — methodology audit gate `REQUEST-CHANGES` on file.
- What the next attempt will change: **not** a mini-probe retry
  against this calibration. The next attempt is either (a) a fresh
  Stage 0.5 calibration with a longer steady-state window (or
  smaller bracket step) tuned to lift the smallest-control cache to
  floor at the selected-TPS region, re-spending under the v2.4
  calibration cap; or (b) a separately auditor-approved
  methodology/code change landing a new revalidation path under a
  fresh spec revision (no in-place PIN movement). Either branch
  lands a new attempt as a fresh top-of-list entry above this one,
  with a matching `## ` section in
  `benchmarks/07-max-output-tokens-reservation/live-v2.5-adaptive-contrast.md`.
- PAYG-not-PTU framing: the calibration was dispatched against the
  `ptu-deploy-throttled` GlobalStandard PAYG deployment. The cache-hit
  floor denial and TPM corridor denial are PAYG proxy observations
  relevant to Task 019 hypothesis I (PTU admission-time reservation
  under `max_output_tokens`); they are not direct PTU measurements.

#### 2026-06-01 — final review gate fix-loop #2: test selector, schema validators, production preflight (no live dispatch)

Final review gate (2026-06-01) returned `REQUEST-CHANGES` on
three concrete blockers; all three are resolved in this commit set
on the code surface (no live Azure dispatch).

Fixes:

- **Blocker 1 — spec-required test selector wired.** Added
  `tests/conftest.py` that registers the `adaptive_calibration`
  pytest marker (also declared in `pyproject.toml` under
  `[tool.pytest.ini_options].markers`) and auto-applies it to every
  test whose nodeid contains `TestV25`. The §13(i) gate command
  `pytest tests/test_measure_max_output_tokens_sweep.py -k
  adaptive_calibration -x` now selects 89 tests (was: exit-5, 481
  deselected) and all pass. `-k V25` continues to select the same
  89 tests and is green.

- **Blocker 2 — v2.5 schema validators completed against §9.**
  `validate_adaptive_calibration_summary()` now requires the
  six previously-missing fields per §9.1: `dirty`,
  `calibration_summary_path`, `calibration_summary_sha256`,
  `phase_a_probe_observations`, `phase_b_probe_observations`,
  `adaptive_calibration_total_committed_usd`. The existing test
  fixture in `TestV25AdaptiveCalibrationSummaryValidator_1142_1143`
  was updated to include them, and a new parametrised regression
  class `TestV25AdaptiveSummaryRequiredFieldsFixLoop2` asserts
  every new field trips `missing_required_field` when omitted.
  `validate_smoke_summary_v25()` and `validate_evidence_summary_v25()`
  now REQUIRE the three v2.5 linkage fields
  (`calibration_selected_via`, `calibration_adaptive_summary_path`,
  `calibration_adaptive_summary_sha256`) to be present even on
  non-adaptive summaries (the path/sha must be `null`); both
  validators accept an optional `repo_root` kwarg and, when supplied
  with an adaptive selected_via and non-null path, verify the file
  resolves under `repo_root` and hashes to the recorded sha256.
  New regression class
  `TestV25SmokeEvidenceLinkageRequiredAndHashedFixLoop2` covers
  the missing-field, hash-match, hash-mismatch, and
  path-unresolvable paths.

- **Blocker 3 — production preflight v2.5-compatible.** Per spec
  §6 item 1, the v2.4 `validate_calibration_result()` selected_via
  / selected_at_phase enums are extended to admit
  `{adaptive_strict_separating_tps,
   adaptive_onset_separation_replicate_confirmed}` and
  `selected_at_phase == "adaptive"`. A cross-field invariant
  rejects mismatched pairings (an adaptive selected_via with a
  non-adaptive phase, or vice versa) with
  `calibration_result_invalid_schema`. A new dispatch branch for
  `selected_at_phase == "adaptive"` skips the v2.3 pinned-grid
  membership check (adaptive emits arbitrary positive TPS by
  construction; spec §6 item 4) while still requiring
  `selected_peak_tps > 0`. The empirical-promotion gate's invariant 2
  `allowed_via` and `allowed_phase` sets are extended identically.
  New regression class
  `TestV25ProductionPreflightAdmitsAdaptiveSelectionFixLoop2` proves
  a future C1- or C2-selected v2.5 calibration result is admitted
  unchanged through the v2.4 preflight + that the cross-field
  invariant rejects forged combinations.

Tests run / results:

- `pytest tests/test_measure_max_output_tokens_sweep.py -k
  adaptive_calibration -x` → 89 passed, 412 deselected.
- `pytest tests/test_measure_max_output_tokens_sweep.py -k V25` →
  89 passed, 412 deselected.
- `pytest tests/test_measure_max_output_tokens_sweep.py` →
  497 passed, 1 failed, 3 skipped. The single failure
  (`TestBracketSelectionSerialization_FixLoop6::test_validate_calibration_result_accepts_bracket_phase`)
  is a pre-existing stale-timestamp fixture bug (test hard-codes a
  `completed_at_iso` that is now > 24h old; verified to fail on
  HEAD with the v2.5 fix-loop-#2 changes stashed) and is NOT caused
  by this fix-loop. Not in scope per the "don't fix unrelated
  pre-existing issues" rule.
- `pytest --ignore=tests/test_measure_max_output_tokens_sweep.py
  --ignore=tests/test_tools.py -q` → 249 passed, 1 skipped, 5
  subtests passed.
- `pytest tests/test_tools.py -q` → 46 passed.

Why execution still cannot move forward (live Azure dispatch):
The live 0.5.C dispatcher remains deferred to a follow-up commit
per fix-loop #1's `Blocked run` entry below. This fix-loop touches
only schema validators, the preflight known-set, the test selector,
and test fixtures / regression tests — no production calibration-
runner Step 1/2/3 HTTP wiring is added. The next attempt is the
one that lands the runner loop and consumes spend.

What the next attempt will change: implement the §3.2 Step 1–3
HTTP dispatch path in `_run_calibration_async` (or a sibling helper)
gated on `runtime.adaptive_calibration.enabled`, with §4.4 cap
enforcement and emission of the validator-conforming
`*.adaptive.summary.json` schema this fix-loop hardened.

#### 2026-05-31 — first review gate fix-loop #1: dispatcher wiring deferred; preflight wiring hardened

- Run prefix: _none — no live calibration dispatched; this entry
  records an implementation-side blocker per §0.5 ("preflight-blocked
  / partial / terminal runs are analysis evidence")._
- Terminal artifact sha256: _n/a — no artifact written_
- Outcome: `implementation_blocked_live_dispatcher_not_wired`
- Total spend: `$0.00`
- Pinned §10 RFC assumptions: unchanged from the v2.5 RFC table above.
- Measurements: _n/a — no probes dispatched_
- Fix attempted (landed in this commit set, no live Azure calls):
  1. `scripts/measure_max_output_tokens_sweep.load_experiment` now
     re-raises `AdaptiveCalibrationYAMLPreflightError` as
     `LinkageValidationError` so `main()`'s existing exit-9 branch
     fires deterministically (`LINKAGE_VALIDATION_FAILED reason=...`)
     instead of leaking a Python traceback when an operator flips
     `runtime.adaptive_calibration.enabled: true` with an invalid
     `prior_calibrations_disclosure_path` or auditor comment.
  2. The previous silent `ImportError → no-op` degradation of the v2.5
     helper import has been replaced with a fail-closed
     `LinkageValidationError` carrying the new reason
     `adaptive_calibration_helper_import_failed`. The helper lives in
     the same repo with no optional dependency; the ImportError branch
     is purely a refactor-breakage guard, not an advertised disable
     switch.
  3. Two `main()`-level regression tests
     (`TestV25YAMLPreflightWiringInMain_FixLoop1`) assert exit code +
     stderr token for both the bad disclosure-path branch and the bad
     auditor-comment branch (one invokes `main()` end-to-end, not just
     the pure validator).
- Blocker (remains open, requires a follow-up commit):
  Live HTTP dispatch for v2.5 Stage 0.5.C Steps 1–3 is NOT wired into
  the production calibration loop in `scripts/measure_max_output_tokens_sweep.run_calibration`
  / `_run_calibration_async`. The pure planner / evaluator / validator
  / lint surface (`compute_role_onset_interval`, `plan_step2_expansion`,
  `plan_step3_bracket_midpoint`, `aggregate_observations_same_tps`,
  `evaluate_c1`, `evaluate_c2`, `evaluate_c3_terminal`,
  `build_adaptive_cache_bucket_key`, every §9.x validator, the §0.5 +
  §11.54 lints) is present and fully tested (480 passes in
  `tests/test_measure_max_output_tokens_sweep.py`), but the
  orchestration that — after v2.4 0.5.B emits its inconclusive
  outcome — (i) checks the §3.2 trigger predicate at runtime,
  (ii) calls `compute_role_onset_interval` per role, (iii) dispatches
  the planned Step 2 / Step 3 probes via the existing v2.4
  calibration dispatcher with the §0.12-suffixed `prompt_cache_key`,
  (iv) runs C1 → C2 → C3 in order, and (v) writes the
  `task019.v2.5.adaptive_calibration_summary` alongside the bumped
  `task019.v2.5.calibration_result` is NOT wired. The v2.4 calibration
  loop (10k-line file) requires careful threading of the §0.12
  adaptive cache-key suffix into the async dispatcher path AND a new
  outer state machine for §0.4 separate-replicate-cap bookkeeping;
  doing that wiring in this fix loop without exercising it live would
  itself be an undetectable regression risk.
- Why execution cannot move forward in this commit set: the worker
  context disallows live Azure calls (per session directive); the
  dispatcher wiring is a measurement-bearing change whose only
  meaningful regression evidence is a live calibration attempt
  (planner-step alignment with real 429 timings, §0.12 cache-bucket
  isolation against real prefix-cache behaviour, §4.4 cap-halt timing
  against real wall-time). Landing untested dispatcher code would
  violate Engineering Principle #2 (Silent Failure Is the Enemy).
- What the next attempt will change: a follow-up commit guarded by
  a fresh `methodology audit gate` approval will wire the dispatcher
  inside `_run_calibration_async`, gated by the runtime equivalent of
  the §3.2 trigger predicate (the YAML preflight already enforces the
  pre-dispatch half), preserving v2.4 default behaviour when
  `enabled=false`. That commit will land its first live attempt under
  a new `## ` section in
  `benchmarks/07-max-output-tokens-reservation/live-v2.5-adaptive-contrast.md`
  and a matching entry in `### Live run — Task 019 v2.5` above.

### Fixed — Task 019 v2.4 operational wiring: terminal-report flag computed at production callsite (2026-05-31, follow-up)

Live smoke / evidence runs against the auditor-approved v2.3
calibration (sha
`92126b46ab4320ba38566229292b3b89922d7d58e42a97c43224d67e6a75db81`)
aborted before any HTTP dispatch with
`empirical_promotion_disabled_ptu_evidence_field_missing_and_cannot_infer`
because `run_measurement` invoked `_run_measurement_async` without
passing the §3.1 invariant 11 condition 5 named flag
`v24_terminal_report_lists_calibration_sha_payg_not_ptu`, so the
kwarg silently defaulted to `False` and the five-condition
backward-compatibility inference could never admit even when the
sha IS enumerated by the committed terminal report.

- Added `verify_terminal_report_lists_calibration_sha_payg_not_ptu`
  helper in `scripts/measure_max_output_tokens_sweep.py`. It scans
  the canonical terminal reports
  (`benchmarks/07-max-output-tokens-reservation/live-calibration-smoke-evidence-final.md`,
  then `CHANGELOG.md`) and returns `True` only when ONE file body
  simultaneously enumerates the calibration sha256, an explicit
  PAYG-not-PTU classification phrase (`PAYG-not-PTU` or
  `PAYG, not PTU`), and a Task 019 v2.3/v2.4 context marker.
- `run_measurement` now computes the flag from this helper and
  forwards it to `_run_measurement_async` as
  `v24_terminal_report_lists_calibration_sha_payg_not_ptu=…` (and
  also forwards `v24_repo_root` for deterministic resolution).
- Tests added in `tests/test_measure_max_output_tokens_sweep.py`
  (`TestV24TerminalReportInferenceHelper`,
  `TestV24RunMeasurementCallsiteWiring`) — positive path against the
  real repo, negative paths for missing sha / missing PAYG-not-PTU
  classification / missing task-context marker / unknown sha / `None`
  sha / malformed sha / missing candidate file, and a source-level
  regression guard that pins the kwarg forwarding in the production
  callsite.
- No methodology pins changed. No thresholds changed. No call
  budgets changed. `mini_probe_enabled` remains the YAML-pinned
  default `false`.

### Added — Task 019 v2.4 empirical-calibration-aware promotion (2026-05-31, methodology audit gate APPROVE after microfix #6)

End-to-end implementation of Task 019 v2.4, an additive layer on top
of v2.3 that rescues smoke / evidence runs whose cold-cache TPM
projection would otherwise abort even though the paid live
calibration already proved the deployment passes the contrast
contract at the selected TPS. Spec:
the private Task 019 v2.4 spec (lab-only workspace)
(approved 2026-05-31 by methodology audit gate on local CLI reviewer worker /
Mac Mini local, verdict APPROVE after microfix #6).

- **v2.3 PRESERVED.** Every v2.3 pin survives v2.4: the 0.85 / 1.25
  TPM thresholds, `selected_peak_tps` non-overridability,
  `concurrency=96` / `concurrency_phase_b=512`, Phase A / Phase B
  grids, bracket-search semantics, `max_retries=0`, the
  run-lock, the 24-hour calibration freshness contract, the
  PAYG-not-PTU guardrail string, and every existing `metadata.*`
  invariant.
- **§6 chain step 5 added** in `scripts/measure_max_output_tokens_sweep.py`
  (no new module). The empirical-promotion gate runs BEFORE the v2.1
  cold-cache TPM-feasibility preflight for smoke / evidence stages
  that loaded a v2.3 calibration result; when every §3.1 invariant
  holds, the gate replaces the cold-cache smallest-cell input with
  the calibration's already-paid-for warm-cache observation
  (`projected_tpm_warm = 60 × selected_peak_tps × (max(base × (1 − r), 100) + max_output_tokens)`).
  When the gate denies AND v2.1 cold-cache fallback also denies, the
  runner emits a `task019.v2.4.abort_envelope` artifact and exits
  with the v2.1-PRESERVED `TPM_FEASIBILITY_ABORT` exit reason.
- **§10 PINNED RFC values** (single source of truth in the script;
  YAML may CARRY for operator readback, but the loader enforces
  equality — no per-run loosening):
  `cache_hit_floor_smallest_control = 0.80`,
  `cache_hit_floor_largest = 0.80`,
  `calibration_max_age_hours = 24`,
  `minimum_records_at_selected_tps = 30`,
  `mini_probe_enabled = false` (default),
  `mini_probe_max_usd = $1.00`,
  `mini_probe_max_attempts_per_run = 1`. The
  `_assert_empirical_promotion_pins_match_defaults()` module-load
  assertion (§13(c) auditor checklist) prevents drift between the
  module-level PIN constants and the dataclass defaults.
- **Three promotion paths (§3, §8)** —
  `cold_cache_strict`, `empirical_calibration_aware`,
  `mini_probe_revalidated`. The mini-probe path is OPT-IN ONLY and
  requires an `# auditor-approved-YYYY-MM-DD: <handle>` comment in
  the YAML immediately above `mini_probe_enabled: true`; the loader
  rejects `mini_probe_enabled: true` without that comment
  (`LinkageValidationError` reason
  `mini_probe_yaml_enabled_without_auditor_approved_comment`).
- **§9 schemas:** `task019.v2.4.smoke_summary`,
  `task019.v2.4.evidence_summary`,
  `task019.v2.4.mini_probe_result`,
  `task019.v2.4.mini_probe_summary`,
  `task019.v2.4.abort_envelope`. Path-conditional null discipline
  enforced by `validate_smoke_summary_v24` /
  `validate_evidence_summary_v24` /
  `validate_abort_envelope_v24`. The evidence summary echoes the
  smoke summary's `promotion_path`, `decision_reason`, and
  `largest_cell_projection_formula` byte-for-byte
  (`validate_evidence_summary_smoke_promotion_path_echo`).
- **§9.4 abort-envelope microfix discipline** —
  `empirical_promotion_disabled_*` stable identifiers surface ONLY
  in `empirical_promotion_denied_reason`, never in `exit_reason`
  (microfix #5 blocker 1). Raw `mini_probe_failed_*` strings never
  appear in the abort envelope; the composite identifier
  `empirical_promotion_disabled_mini_probe_failed_and_cold_cache_fails`
  is used instead (microfix #6 blocker 2). The envelope schema's
  forbidden-field list rejects every admitted-summary block on
  presence.
- **§3.1 invariant 11 backward-compatibility PTU inference** for the
  v2.3 fixture (which pre-dates `metadata.ptu_evidence: false`):
  admits PAYG-not-PTU iff all five conditions hold (deployment,
  deployment env, experiment id, pricing snapshot resolves under
  `pricing/` with top-level `source_url` — microfix #4, NOT `source`
  — and a non-empty `pricing_accessed_date`, AND the smoke YAML's
  `metadata.ptu_evidence == false` AND the v2.3 terminal report
  enumerates the calibration's sha256 with explicit PAYG-not-PTU
  classification). The basis dictionary is echoed verbatim into the
  admitted summary's `ptu_evidence_inference_basis` block for audit
  reproducibility.
- **§11.18 frozen-clock fixture outcomes** (the canonical regression
  pair against the v2.3 fixture sha
  `92126b46ab4320ba38566229292b3b89922d7d58e42a97c43224d67e6a75db81`):
  - 18(a) — fresh clock (`completed_at_iso + 1h`):
    PROMOTE → `promotion_path = empirical_calibration_aware`,
    `largest_cell_projection_formula = v2.4_warm_projection`,
    warm smallest TPM ≈ 16,406 (< 51,000), warm largest TPM
    ≈ 473,840 (> 75,000).
  - 18(b) — stale clock (`completed_at_iso + 36h`), mini-probe
    disabled: DENY →
    `empirical_promotion_denied_reason = empirical_promotion_disabled_calibration_stale_and_mini_probe_disabled`,
    cold-cache fallback also denies at the fixture's
    `selected_peak_tps = 0.47469`, abort envelope with
    `exit_reason = TPM_FEASIBILITY_ABORT`.
- **Budget delta** — `+$5` mini-probe spend cap raises the Task 019
  total cap from `$400` (v2.3) to `$405` (v2.4). Mini-probe is
  disabled by default and capped at `$1.00 / attempt × 1 attempt /
  stage × 5 max attempts across a smoke+evidence pair` ≤ `$5`.
- **Files touched:**
  - `scripts/measure_max_output_tokens_sweep.py` (extended;
    no new module per §16 DoD)
  - `experiments/exp007_max_output_tokens_sweep.yaml` (added
    `runtime.empirical_promotion.*` block carrying every §10 PIN)
  - `tests/test_measure_max_output_tokens_sweep.py` (appended
    13 new test classes / 65 tests covering every §11 item
    relevant to implementation, plus the post-microfix #6 closure
    coverage for the two critical wired-not-stub paths below)
  - `benchmarks/07-max-output-tokens-reservation/README.md`
    (prepended v2.4 protocol section + PAYG-not-PTU caveat + spec
    link)
  - `CHANGELOG.md` (this entry)
- **Critical-TODO closure (post-microfix #6, 2026-06-01):** the
  measurement-implementation worker pass that initially landed the v2.4 surface
  reported two open seams; both are now closed end-to-end and
  exercised by the runner-integration tests:
  - **Admitted-summary writer wired into the actual runner.** On the
    admit path (any of the three promotion paths), the post-run
    summary writer in `_run_measurement_async` now overlays the §9.1
    admitted-summary fields onto the existing v2.3 measurement-summary
    dict via the new `apply_v24_admitted_summary_fields` helper,
    bumps `schema_version` to `task019.v2.4.smoke_summary` (smoke)
    or `task019.v2.4.evidence_summary` (evidence), and runs the
    matching v2.4 validator BEFORE writing — a defective overlay
    raises rather than producing a malformed on-disk artifact. For
    evidence stage, the §9.3 `smoke_summary_reference` block
    byte-equal-echoes the source smoke summary's three inheritable
    fields. Existing v2.3 operational fields (cell summaries, pinned
    confounds, run-lock metadata) are PRESERVED — the bump is
    strictly additive at the schema-version literal + named-field
    level. New tests:
    `TestV24RunnerWritesAdmittedSummaryOnDisk` (3 cases including
    smoke + evidence + cold-cache-strict admit via fallback).
  - **Mini-probe production runner.** New `_run_mini_probe_async`
    function executes ONE §7-conformant smallest-control probe
    (cap=256, 12-call pre-warm @ 0.05 TPS, 90 s constant-rate probe @
    `selected_peak_tps`), evaluates the four gates (warm-criterion,
    backlog, all-empty-visible-output, admitted-pressure), bounded-
    retries once on gate failure with a `_retry1` cache-key suffix,
    and writes the §9.2 artifact triplet
    (`mini_probe.result.json` + `mini_probe.summary.json` + `.sha256`
    sidecar). New `build_mini_probe_cache_key` carves a DISTINCT
    `task019_minip_*` namespace (≠ `card1` smoke/evidence ≠ `calib`
    Stage 0.5). `_run_measurement_async` now computes calibration age
    upfront and, when stale AND `mini_probe_enabled=true` is
    auditor-approved-comment-guarded in the YAML, eagerly executes
    the mini-probe before invoking the (sync) gate, exposing the
    cached result via a single-shot closure that raises the typed
    `MiniProbeAttemptedMoreThanOncePerRunError` defensively on any
    second invocation. New tests:
    `TestV24MiniProbeCacheKey`, `TestV24MiniProbeFailedReasonClassifier`,
    `TestV24MiniProbeArtifactWriter`, `TestV24MiniProbeRunnerHappyPath`
    (passed-path + bounded-retry-then-failed),
    `TestV24MiniProbeRunnerDefensive`,
    `TestV24RunnerInvokesMiniProbeWhenStaleAndEnabled`.
- **Test results (post-closure):** module suite 392 passed, 3 skipped
  (the 3rd skip is documented in
  `test_runner_cold_cache_admitted_via_fallback_writes_v24_with_nulls`
  — the cold-cache-fallback-admit scenario requires a different YAML
  fixture combination than the current v2.3 pin). Whole-repo suite
  687 passed, 4 skipped (no regressions).

### Added — Task 019 v2.3 live-run terminal result (2026-05-31, Mac Mini local) — calibration `selected` → smoke `TPM_FEASIBILITY_ABORT`

End-to-end live execution of the v2.3 protocol on the
`ptu-deploy-throttled` PAYG GlobalStandard deployment (60 K TPM / 600 RPM,
`ptu_evidence: false`). Stage 0.5 calibration completed with
`outcome: "selected"`; Stage 1 smoke terminated honestly at the
TPM-feasibility preflight gate; Stage 2 evidence was therefore not
invoked. No spec, gate, threshold, or runtime parameter was mutated to
enable promotion; the analyzer was not invoked because its sole
consumable input is a smoke/evidence summary which was not produced.

- **Calibration outcome (`selected` via bracket search, depth 3):**
  `selected_peak_tps = 0.47469318448182934`, `selected_at_phase =
  "bracket"`, `selected_bracket_root_phase = "A"`,
  `selected_via = "bracket_search"`. The bracket trace recorded all 3
  geometric-midpoint depths between `T_low = 0.330` (Phase A) and
  `T_high = 0.500` (Phase A first-429 candidate); only depth 3 (TPS
  `0.475`) produced the spec-required `largest n_429 ≥ 1 AND smallest
  n_429 = 0` contrast. Calibration result sha256
  `92126b46ab4320ba38566229292b3b89922d7d58e42a97c43224d67e6a75db81`,
  schema `task019.v2.3.calibration_result`. 7 probes total, 501 records
  in the calibration JSONL, 3 real 429s observed (1 each at Phase A /
  largest / 0.500; Phase A / smallest_control / 0.500; bracket d=3 /
  largest / 0.475). The admitted-pressure gate passed on every probe
  with no `_retry1_admp` retries needed.
- **Calibration cost:** `$3.0619` realized vs `$220` hard ceiling
  (`$216.94` headroom, 98.6 % of ceiling unused). The
  deterministic-conservative estimator's pessimistic committed
  reservation was `$5.823` (preserved for auditor traceability only).
- **Smoke terminal verdict (exit `1`, `TPM_FEASIBILITY_ABORT`):** at
  `selected_peak_tps = 0.47469`, the smallest cell
  (`max_output_tokens = 256`) projected `60 × 0.47469 × (2158 + 256)
  = 68 754.6 TPM`, which exceeds the v2.1-pinned smallest-cell
  feasibility ceiling `0.85 × 60 000 = 51 000 TPM` by `+17 755 TPM`
  (+34.8 %). Per spec, the gate refused promotion because the smallest
  cell would saturate at peak ramp, eliminating signal contrast.
  Largest-cell projection `528 105.7 TPM` (≈ 7.04× the `1.25 × quota
  = 75 000 TPM` upper threshold) would have passed. No HTTP call was
  dispatched at the smoke stage; smoke realized spend `$0.00`,
  evidence realized spend `$0.00`. **Task 019 v2.3 live-run total
  realized spend: `$3.0619`**, well under the `$400` task total cap.
- **Calibration-vs-preflight inconsistency (the contract finding this
  run surfaces):** the v2.3 calibration *empirically validated* contrast
  at TPS `0.47469` (smallest-cell control observed 0 / 97 429s with
  85.2 % cache-hit ratio steady-state — canonical
  `probes[6].cache_hit_ratio_steady_state = 0.8517199126857254` —
  i.e. the deployment had real headroom on the smallest cell at this
  TPS once the prompt cache was warm), but the smoke-stage
  TPM-feasibility preflight uses a **cold-cache** projection that
  does not consume realized `cached_tokens` and therefore rejects
  every `selected_peak_tps > ≈0.33` at the current `0.85` threshold.
  This run's terminal exit is the gate operating exactly as the spec
  requires — Hypothesis I is therefore **undetermined** at the proxy
  level on this deployment + prompt identity under v2.3, *not*
  refuted.
- **Headline finding:** monotone reservation-at-cap evidence (the
  `429_onset_rpm` vs `max_output_tokens` sweep that this benchmark
  exists to produce) was NOT collected on this run because Stage 2
  evidence was never invoked. The calibration empirically demonstrated
  that 429 onset at `cell = 16384` occurs at canonical
  `probes[5].first_429_arrival_rpm = 29` (the per-minute rate at the
  instant the first 429 was observed) with
  `probes[5].admitted_pressure.admitted_peak_rpm_observed_last_30s
  = 28.0` (the steady admitted-dispatch peak over the trailing 30s
  window — these are two distinct metrics captured by the runner,
  not synonyms) at candidate TPS `0.475` with prompt cache fully
  warm (`cache_hit_ratio_at_first_429 = 0.883`), and that the same
  admitted-dispatch pressure at `cell = 256` produces zero 429s — a
  clean signal/control separation at a single point in the curve,
  NOT the full monotone sweep that would constitute Hypothesis I
  proxy evidence.
- **Prompt identity SHAs (verified by runner at startup, exit `7` on
  mismatch):** `source_corpus_sha256 =
  6a8ab5a3cb1ad3dace030a82ec1327496b39e65b77a627714a27c39017ca19e3`,
  `user_prompts_source_sha256 =
  45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087`,
  `assembled_system_prompt_sha256 =
  f8a74528164b22eed27d30a5fa089b1d0fbfb38440cc341b043c2cb24e9289c7`.
- **Run-lock hygiene:** a stale `.runlock` left by PID `3882`
  (calibration runner that exited cleanly hours earlier but was killed
  by the host before lock release) was confirmed dead via
  `ps -p 3882` (empty result) and removed; only the `.runlock` file
  was touched, no other run state. Separately, the smoke runner's
  pre-HTTP `TPM_FEASIBILITY_ABORT` exit path was observed to leak
  its own `.runlock` (PID `38681`); **follow-on task to file**:
  "release `.runlock` on pre-HTTP gate aborts in
  `scripts/measure_max_output_tokens_sweep.py`" — owner TBD, no
  spend. Full reproduction in
  `benchmarks/07-max-output-tokens-reservation/live-calibration-smoke-evidence-final.md`
  §10.
- **Artefacts created (durable) and committed in this same change:**
  the three calibration files below were written by the runner on
  2026-05-30 and remained untracked on disk; this commit is what
  versions them. They are NOT pre-existing in git history.
  - `benchmarks/07-max-output-tokens-reservation/runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.result.json`
    (17 105 bytes; written by calibration runner 2026-05-30, committed
    in this change, not rewritten by the report worker),
  - `benchmarks/07-max-output-tokens-reservation/runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.summary.json`
    (1 096 bytes; written by calibration runner 2026-05-30, committed
    in this change),
  - `benchmarks/07-max-output-tokens-reservation/runs/20260530T135125Z_exp007_max_output_tokens_sweep_calibration.jsonl`
    (1 138 674 bytes; written by calibration runner 2026-05-30,
    committed in this change),
  - `benchmarks/07-max-output-tokens-reservation/live-calibration-smoke-evidence-final.md`
    (this run's full report; new in this entry).
- **PAYG-not-PTU caveat:** enforced. `metadata.ptu_evidence: false`,
  `metadata.simulation: false`,
  `metadata.deployment_kind: payg_global_standard_throttled`,
  `metadata.consumption_model_context: paygo_standard_throttled`. This
  result must not be re-cited as direct PTU evidence by Task 022 or any
  downstream artifact; PTU mechanisms (slot routing,
  expected-utilization tiering, capacity-correlated cache effects) are
  NOT measured here.
- **Recommended next step (no runtime override):** spec-revision RFC
  to reconcile the smoke/evidence TPM-feasibility preflight's
  cold-cache projection with the v2.3 calibration's empirical
  contrast finding. See
  `benchmarks/07-max-output-tokens-reservation/live-calibration-smoke-evidence-final.md`
  §8 for the four enumerated options (raise/relax the smallest-cell
  ceiling — i.e. from `≤ 0.85 × quota` to a more permissive bound such
  as `≤ 1.15 × quota` — conditionally on calibration's empirical
  observation; consume
  calibration's per-cell cache_hit_ratio in the projection; tighten
  calibration to `peak_tps ≤ smallest-feasible-cold-cache TPS`; or
  accept the contract as written and ship this run as a documented
  null at the proxy level — option (d) is what this entry records
  today).

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #10 (final review gate REQUEST-CHANGES, Phase A pinned-grid tightening + stale dry-run quarantine)

Final review gate (local CLI reviewer worker, Mac Mini local re-check) RETURNED
`REQUEST-CHANGES` after fix-loop #9 with two remaining blockers — one
on `validate_calibration_result`'s Phase A else-branch, and one on
stale pre-v2.3 dry-run / figures artifacts that would otherwise land in
Task 019 v2.3 commit scope. The narrow fixes are in this commit; no
live Azure call was made.

1. **`validate_calibration_result` Phase A else-branch no longer trusts
   a result-provided `candidate_tps_grid`.** Reviewer finding: pre-fix-
   loop-#10 the Phase A dispatch read
   `phase_a_grid = data.get("candidate_tps_grid", list(CALIBRATION_CANDIDATE_TPS_GRID))`
   and then performed the `selected_peak_tps in phase_a_grid_f`
   membership check against the RESULT-PROVIDED list. The auditor's
   primary forged tuple
   `(selected_via=None, selected_at_phase='A',
   selected_peak_tps=5.0, candidate_tps_grid=[5.0])` therefore passed
   silently — `5.0 in [5.0]` was True even though 5.0 is NOT in the
   pinned 7-member Phase A grid
   `(0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0)`. The same vulnerability
   applied to any forged subset, superset, reordered, duplicated, or
   ad-hoc-value grid the result file might carry on the Phase A path.

   Fix: the Phase A else-branch is rewritten to mirror the existing
   Phase B handling (fix loop #2). The pinned module constant
   `CALIBRATION_CANDIDATE_TPS_GRID` is now the SINGLE SOURCE OF TRUTH
   for Phase A membership. If the result file carries
   `candidate_tps_grid` it MUST equal the pinned grid EXACTLY (same
   length, same members, same ascending order — any forged subset /
   superset / reordering / duplicate / ad-hoc value is rejected); the
   `selected_peak_tps` membership check is ALWAYS performed against
   the pinned constant directly, never the result-provided list. All
   schema rejections carry `reason="calibration_result_invalid_schema"`
   per the v2.2.1 / v2.3 reason-string contract.

   v2.2.1 back-compat is preserved within the auditor's pinned scope:
   `selected_peak_tps` in the pinned Phase A grid AND
   `selected_at_phase` in `{None, "A"}`. Both shapes pass —
   (a) results that echo the pinned grid verbatim (which is what the
   v2.3 runtime always emits via `calib.candidate_tps_grid` in
   `_emit_calibration_result`), and (b) truly archived v2.2.1 results
   that omit the `candidate_tps_grid` key entirely. In both cases the
   fall-through membership check uses the pinned constant directly.

2. **Stale pre-v2.3 dry-run artifacts and figures moved to the
   existing fix-loop-#2 quarantine.** Reviewer finding: the working
   tree carried five untracked artifacts under
   `benchmarks/07-max-output-tokens-reservation/runs/` that pre-date
   the v2.3 microfix fix-loop-#10 code path (and therefore the
   tightened validator + analyzer schema invariants):

     - `20260529T203410Z_exp007_max_output_tokens_sweep_dry-run.jsonl`
     - `20260529T203410Z_exp007_max_output_tokens_sweep_dry-run.jsonl.summary.json`
     - `runs/figures/cache_hit_ratio_vs_cap.png`
     - `runs/figures/first_429_arrival_rpm_vs_cap.png`
     - `runs/figures/visible_output_tokens_p50_vs_cap.png`

   These files would have been silently swept into the Task 019 v2.3
   commit scope when the benchmark directory is first tracked,
   misrepresenting v2.3 schema invariants in the PR diff. The five
   files were moved (preserving original mtimes for forensic
   continuity) into the existing
   `benchmarks/07-max-output-tokens-reservation/runs/_quarantined_pre_v23_microfix/`
   directory (which the repository-root `.gitignore` covers via
   `benchmarks/*/runs/_quarantined_*/`); the now-empty
   `runs/figures/` directory was removed (a fresh v2.3 analyzer run
   will recreate it under the current code path). The quarantine
   `README.md` was updated with a fix-loop-#10 addendum naming each
   moved artifact and the regeneration recipe. The
   `runs/.gitkeep` placeholder remains tracked. After the move,
   `git status --untracked-files=all benchmarks/07-max-output-tokens-reservation/`
   shows only `README.md`, `analysis.md`, and `runs/.gitkeep` as
   commit-scope.

Test deltas: `tests/test_measure_max_output_tokens_sweep.py` gains
**19 new tests** under the new class
`TestPhaseAGridPinnedAgainstForgedResult_FixLoop10` covering the
reviewer's primary forged example (forged `candidate_tps_grid=[5.0]`
with `selected_peak_tps=5.0` on both the `(None, None)` and
`(None, "A")` v2.2.1 back-compat paths) plus the four reviewer-named
schema mutations (missing pinned member, ad-hoc value, reordered,
duplicate), a non-list / non-numeric type-mutation pair, the exact-
pinned-grid happy path (1 named + 7 parametrized sweep over every
pinned Phase A member), the legacy-helper happy path, and the
absent-key v2.2.1 archive back-compat (accepted for in-grid
`selected_peak_tps`, rejected for the 5.0 attack vector even with the
field omitted). The `_make_calibration_result` test helper was
extended with an optional `candidate_tps_grid` kwarg (sentinel-default
preserves all existing callers; `None` omits the key entirely; a list
forges the field) — every pre-existing test continues to pass
unmodified. Full Task 019 pytest suite now passes:
**313 passed, 3 skipped** (baseline before this fix loop was
294 passed, 3 skipped — delta = +19 new deterministic tests).
`ruff check` passes on all four touched files
(`scripts/measure_max_output_tokens_sweep.py`,
`scripts/analyze_max_output_tokens_sweep.py`,
`tests/test_measure_max_output_tokens_sweep.py`,
`tests/test_analyze_max_output_tokens_sweep.py`). No live Azure calls
were made during this fix loop.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #9 (methodology audit gate REQUEST-CHANGES, cross-field `selected_via`/`selected_at_phase` tightening)

Methodology audit gate (local CLI reviewer worker, Mac Mini local re-check) RETURNED
`REQUEST-CHANGES` after fix-loop #8 with one remaining narrow blocker
on `validate_calibration_result`: the validator still accepted a forged
Phase B tuple of the shape `(selected_via=None, selected_at_phase='B',
selected_peak_tps in pinned Phase B grid, bracket markers null)`. The
v2.2.1 unset-via back-compat path was being applied too liberally —
the `elif sel_phase == "B":` Phase B grid dispatch checked pinned-grid
membership but never required the runtime-emitted
`selected_via == "grid_ascending"` marker, so the unset-via path
could silently claim Phase B lineage. The narrow fix is in this
commit; no live Azure call was made.

1. **`validate_calibration_result` now enforces the cross-field
   invariant between `selected_via` and `selected_at_phase`.** Auditor
   finding: pre-fix-loop-#9 the validator accepted four legal values
   of `selected_via` (`None`, `"grid_ascending"`, `"bracket_search"`)
   and four legal values of `selected_at_phase`
   (`None`, `"A"`, `"B"`, `"bracket"`) but did NOT cross-check the
   pair. The forged tuple `(selected_via=None, selected_at_phase='B',
   selected_peak_tps in pinned Phase B grid, bracket markers null)`
   fell through to the `elif sel_phase == "B":` Phase B grid
   dispatch, which only validated pinned-grid membership; downstream
   `phase_b_concurrency_used` and audit fields then claimed Phase B
   lineage on a result that never recorded the v2.3 selection-via
   marker.

   Fix: a new cross-field check is inserted in
   `validate_calibration_result` immediately after the per-field
   `selected_via` and `selected_at_phase` enum validations and
   BEFORE the fix-loop-#8 bracket-marker null check and the
   `bracket_search` / Phase B / Phase A dispatch. The check rejects,
   with `LinkageValidationError(reason="calibration_result_invalid_schema")`,
   any `selected_at_phase == "B"` whose `selected_via != "grid_ascending"`.
   Equivalently: `selected_via is None` is now legal ONLY with
   `selected_at_phase` in `{None, "A"}`. The diagnostic names the
   violated invariant and points at the runtime emit contract.

2. **Selection-path matrix after fix loop #9 (precise legal pairs).**

     * `(selected_via=None, selected_at_phase=None|"A")` — Phase A
       grid (v2.2.1 unset-via back-compat). Bracket markers MUST be
       null. `selected_peak_tps` MUST be in the Phase A grid.
     * `(selected_via="grid_ascending", selected_at_phase=None|"A")`
       — Phase A grid (v2.3 with explicit selection-via marker).
       Bracket markers MUST be null. `selected_peak_tps` MUST be in
       the Phase A grid.
     * `(selected_via="grid_ascending", selected_at_phase="B")` —
       Phase B grid. Bracket markers MUST be null.
       `selected_peak_tps` MUST be in the pinned Phase B grid AND
       `candidate_tps_grid_phase_b` MUST equal the pinned grid
       exactly (fix loop #2).
     * `(selected_via="bracket_search", selected_at_phase="bracket")`
       — bracket search. `selected_bracket_root_phase` MUST be
       `"A"` or `"B"` (fix loop #7) and
       `selected_at_bracket_depth` MUST be an int in
       `1..BRACKET_MAX_DEPTH`. `selected_peak_tps` MUST be > 0.

   All other `(selected_via, selected_at_phase)` pairs are rejected
   as `calibration_result_invalid_schema`. The auditor's primary
   target — `(None, "B")` — is now caught at the schema layer; the
   already-tightened `bracket_search` branch (fix loops #6 + #7) and
   the bracket-marker null check on non-bracket paths (fix loop #8)
   remain unchanged.

Test deltas: `tests/test_measure_max_output_tokens_sweep.py` gains
10 new tests under the new class
`TestValidateCalibrationResultUnsetViaPhaseB_FixLoop9` covering
the auditor's primary forged example (`selected_via=None` +
`selected_at_phase='B'` + `selected_peak_tps=5.0` + pinned
`candidate_tps_grid_phase_b` + bracket markers null) at all six
pinned Phase B grid members (1 named + 6 parametrized sweep) plus
three happy-path regression locks: legacy `(None, None)` via
`_write_calibration_pair`, explicit `(None, "A")` via
`_write_calibration_pair_v23`, and v2.3 `(grid_ascending, "B")` at
TPS=5.0. Full Task 019 pytest suite now passes:
**294 passed, 3 skipped** (baseline before this fix loop was
284 passed, 3 skipped — delta = +10 new deterministic tests).
`ruff check` passes on all four touched files
(`scripts/measure_max_output_tokens_sweep.py`,
`scripts/analyze_max_output_tokens_sweep.py`,
`tests/test_measure_max_output_tokens_sweep.py`,
`tests/test_analyze_max_output_tokens_sweep.py`). No live Azure calls
were made during this fix loop.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #8 (methodology audit gate REQUEST-CHANGES, cross-field bracket-marker schema tightening)

Methodology audit gate (local CLI reviewer worker, Mac Mini local re-check) RETURNED
`REQUEST-CHANGES` after fix-loop #7 with one remaining narrow blocker
on `validate_calibration_result`: the validator permitted
`selected_at_phase='bracket'` globally and only enforced the full
bracket-marker tuple on the `selected_via=='bracket_search'` branch.
Non-bracket selection paths (`selected_via` is `None` or
`'grid_ascending'`) could therefore carry forged bracket lineage
(`selected_at_phase='bracket'`, non-null `selected_bracket_root_phase`,
non-null `selected_at_bracket_depth`) past the schema, satisfy the
Phase-A else branch's grid-membership check on a Phase-A-grid TPS, and
silently misdrive downstream `phase_b_concurrency_used` (which reads
`selected_bracket_root_phase` to recover Phase-A vs Phase-B lineage
when `selected_at_phase=='bracket'` hides it). The narrow fix is in
this commit; no live Azure call was made.

1. **`validate_calibration_result` now enforces cross-field invariants
   between `selected_via` and the three bracket-phase markers.**
   Auditor finding: post-fix-loop-#7 the `selected_via=='bracket_search'`
   branch correctly required `selected_at_phase=='bracket'`,
   `selected_bracket_root_phase ∈ {'A','B'}`, and
   `selected_at_bracket_depth ∈ 1..BRACKET_MAX_DEPTH`, but the
   converse implication — that those markers are reserved EXCLUSIVELY
   for bracket-search — was never enforced. Four classes of forged
   non-bracket result were silently accepted:

     (a) `selected_via='grid_ascending'` with
         `selected_at_phase='bracket'`,
         `selected_bracket_root_phase='B'`, and a Phase-A-grid TPS —
         the Phase-A else branch's grid-membership check passed and
         the forged bracket lineage was carried through to
         `phase_b_concurrency_used`.
     (b) `selected_via=None` (v2.2.1 unset-via back-compat) with
         `selected_at_phase='bracket'` and
         `selected_bracket_root_phase='B'` — same shape as (a) via
         the unset-via path.
     (c) Legitimate Phase A grid selection
         (`selected_via='grid_ascending'` + `selected_at_phase='A'`
         + Phase-A-grid TPS) with a non-null
         `selected_bracket_root_phase` — forged bracket lineage on a
         grid path.
     (d) Legitimate Phase B grid selection
         (`selected_via='grid_ascending'` + `selected_at_phase='B'`
         + Phase-B-grid TPS) with a non-null
         `selected_at_bracket_depth` — forged bracket depth on a
         grid path.

   Fix: a new `if sel_via != "bracket_search":` cross-field block is
   inserted in `validate_calibration_result` BEFORE the
   `bracket_search` / Phase B / Phase A dispatch. It rejects, with
   `LinkageValidationError(reason="calibration_result_invalid_schema")`:

     * Any `selected_at_phase == "bracket"` on a non-bracket path —
       the bracket phase label is meaningful only for bracket-search.
     * Any non-null `selected_bracket_root_phase` on a non-bracket
       path — the root_phase marker is reserved for bracket-search
       and populating it on a grid path misdrives downstream
       `phase_b_concurrency_used` Phase-A vs Phase-B lineage
       recovery.
     * Any non-null `selected_at_bracket_depth` on a non-bracket
       path — the bracket depth marker is reserved for
       bracket-search.

   Each rejection carries a diagnostic that names the violated
   invariant and points at the runtime emit contract.

2. **v2.2.1 back-compat and v2.3 Phase B grid paths are preserved.**
   The new check fires only when at least one bracket-phase marker
   is non-null AND `selected_via != "bracket_search"`. v2.2.1 results
   (`selected_via` absent, `selected_at_phase` in `{None, "A"}`,
   bracket markers absent / null) fall through unchanged to the
   Phase A grid membership check. v2.3 Phase B grid selections
   (`selected_via=='grid_ascending'` + `selected_at_phase=='B'` with
   both bracket markers absent / null) fall through unchanged to the
   Phase B grid membership check. The existing bracket-search branch
   (fix loops #6 + #7) is also untouched.

Test deltas: `tests/test_measure_max_output_tokens_sweep.py` gains
6 new tests under the new class
`TestValidateCalibrationResultCrossFieldInvariants_FixLoop8` covering
the four forged cases named by methodology audit gate plus two happy-path
regression locks (valid Phase A legacy via `_write_calibration_pair`,
valid Phase B grid via `_write_calibration_pair_v23`). Full Task 019
pytest suite now passes: **284 passed, 3 skipped** (baseline before
this fix loop was 278 passed, 3 skipped — delta = +6 new
deterministic tests). `ruff check` passes on all four touched files
(`scripts/measure_max_output_tokens_sweep.py`,
`scripts/analyze_max_output_tokens_sweep.py`,
`tests/test_measure_max_output_tokens_sweep.py`,
`tests/test_analyze_max_output_tokens_sweep.py`). No live Azure calls
were made during this fix loop.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #7 (final review gate REQUEST-CHANGES, bracket-schema tightening)

Final review gate (local CLI reviewer worker, Mac Mini local re-check) RETURNED
`REQUEST-CHANGES` after fix-loop #6 with one remaining schema-tightening
blocker on `validate_calibration_result`: the validator accepted
`selected_via == "bracket_search"` without requiring the bracket-phase
markers the runtime now emits, leaving the door open for pre-fix-loop-#6
stale results and forged/missing root-phase values to validate cleanly.
The narrow fix is in this commit; no live Azure call was made.

1. **`validate_calibration_result` now enforces the full v2.3
   bracket-search invariant set.** Auditor finding: the post-fix-loop-#6
   `_run_calibration_async` bracket success branch ALWAYS sets
   `selected_at_phase = "bracket"` (literal) AND
   `selected_bracket_root_phase = phase_label` (the parent grid that
   rooted the bracket, ∈ `{"A","B"}`), but the validator's
   `selected_via == "bracket_search"` branch only checked
   `selected_at_bracket_depth ∈ 1..BRACKET_MAX_DEPTH` and
   `selected_peak_tps > 0`. Four classes of invalid result were
   silently accepted:

     (a) `selected_via='bracket_search'` with `selected_at_phase='A'`
         (pre-fix-loop-#6 stale variant — bracket conflated with the
         parent Phase A grid label),
     (b) `selected_via='bracket_search'` with `selected_at_phase='B'`
         (pre-fix-loop-#6 stale variant — bracket conflated with the
         parent Phase B grid label),
     (c) `selected_via='bracket_search'` with `selected_at_phase='bracket'`
         but `selected_bracket_root_phase` missing / `null`
         (downstream `phase_b_concurrency_used` cannot recover the
         Phase-A vs Phase-B lineage),
     (d) `selected_via='bracket_search'` with a forged
         `selected_bracket_root_phase='C'` (not in the runtime's
         emit alphabet).

   Fix: the `sel_via == "bracket_search"` branch in
   `validate_calibration_result` now performs three additional checks
   before the existing depth and positivity checks:

     * `selected_at_phase` MUST equal exactly the literal string
       `"bracket"` — anything else (including `"A"`, `"B"`, `None`)
       raises `LinkageValidationError(reason=
       "calibration_result_invalid_schema")` with a diagnostic that
       names the stale variant and points at the runtime emit
       contract.
     * `selected_bracket_root_phase` MUST equal exactly `"A"` or
       `"B"` — missing / `None` / `"C"` / any other value raises
       the same exception with a diagnostic that names the runtime
       emit alphabet.
     * The existing depth (int 1..`BRACKET_MAX_DEPTH`) and
       positive-TPS checks are preserved unchanged.

   The grid-ascending validation paths (Phase A back-compat, Phase B
   pinned-grid) are untouched.

2. **Test helper `_write_calibration_pair_v23` now defaults
   `selected_bracket_root_phase="B"` for bracket-search callers and
   accepts an explicit override (including explicit `None`) via a
   sentinel-based kwarg.** This matches the runtime emit path
   (bracket success branches always set the field) so existing
   bracket-search tests in
   `TestValidateCalibrationResultPhaseBAndBracket_AuditorMicrofix`
   continue to pass, while the fix-loop-#7 invalid-root regression
   tests below can drive `None` / `"A"` / `"C"` explicitly.

Test deltas: `tests/test_measure_max_output_tokens_sweep.py` gains
5 new tests under the new class
`TestValidateCalibrationResultBracketRootPhase_FixLoop7` covering the
four invalid examples named by final review gate plus one valid
Phase-B-rooted bracket case (`bracket_search` +
`selected_at_phase='bracket'` + `selected_bracket_root_phase='B'` +
depth 2 + positive TPS). The test module also gains a small
`_SentinelType` / `_SENTINEL` pair so the helper can distinguish
"omitted" from "explicitly None" on the new optional
`selected_bracket_root_phase` kwarg. Full Task 019 pytest suite now
passes: **278 passed, 3 skipped** (baseline before this fix loop was
273 passed, 3 skipped — delta = +5 new deterministic tests). `ruff
check` passes on all four touched files
(`scripts/measure_max_output_tokens_sweep.py`,
`scripts/analyze_max_output_tokens_sweep.py`,
`tests/test_measure_max_output_tokens_sweep.py`,
`tests/test_analyze_max_output_tokens_sweep.py`). No live Azure calls
were made during this fix loop.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #6 (auditor REQUEST-CHANGES, 3 schema/propagation blockers)

Methodology audit gate (local CLI reviewer worker, Mac Mini local re-check) RETURNED
`REQUEST-CHANGES` after fix-loop #5 with three remaining schema /
propagation blockers covering bracket-selection serialization, the
`phase_b_concurrency_used` JSON type, and v2.3 enforcement of per-cell
`admitted_pressure_passed`. All three are addressed in this commit set
without any live Azure call.

1. **Bracket-search selections now serialize with
   `selected_at_phase='bracket'`, NOT the parent A/B phase label.**
   Auditor finding: the v2.3 fix-loop-#5 bracket success branch in
   `_run_calibration_async` set `selected_via='bracket_search'` but
   `selected_at_phase=phase_label` (i.e. "A" or "B"), conflating the
   bracket with the parent grid that rooted it. The spec / validator
   expect `selected_at_phase='bracket'` so downstream consumers can
   distinguish bracket-rooted from grid-rooted selections without
   re-reading `selected_via`. Fix: the bracket success branch now
   assigns the literal `selected_at_phase = "bracket"` and records
   the parent grid phase separately under the new
   `selected_bracket_root_phase` field (echoed in both
   `calibration_result.json` and the sibling calibration summary, and
   propagated through `run_measurement` → `_run_measurement_async`
   into the smoke / evidence measurement summary). Bracket-aware
   audit consumers can still answer "which Phase-B concurrency
   override (if any) was active?" from
   `selected_bracket_root_phase ∈ {"A", "B"}`. The startup-abort
   artifact gains the same field for consistency. New deterministic
   test class `TestBracketSelectionSerialization_FixLoop6` (5 tests)
   includes: a source-scan regression pinning
   `selected_at_phase = "bracket"` as a string literal AND
   `selected_bracket_root_phase = phase_label`; a source-scan for the
   two echo sites; a fixture round-trip through
   `validate_calibration_result` for a bracket-selected v2.3 result;
   and **two end-to-end regression tests that actually drive
   `_run_calibration_async` through the bracket success path** (with
   stubbed `_run_cell` and `_aggregate_calibration_probe` so no Azure
   is touched) — one with the bracket rooted in Phase B (Phase A
   exhausted clean → Phase B 5.0 clean → Phase B 8.0 contrast-lost →
   bracket midpoint sqrt(5×8) ≈ 6.32 selected) asserting both
   `selected_at_phase == "bracket"` and
   `selected_bracket_root_phase == "B"`, and a second with the
   bracket rooted in Phase A asserting
   `selected_bracket_root_phase == "A"`. These tests explicitly
   exercise the actual runtime path the auditor named, not just
   fixture validation.

2. **`phase_b_concurrency_used` is now a JSON BOOL, not an integer
   concurrency value.** Auditor finding: the v2.3 spec defines
   `phase_b_concurrency_used` as a bool, but the smoke / evidence
   summary writer emitted either the integer concurrency override
   (e.g. `512`) or `None`, and a fixture test pinned the integer. The
   bool semantics: True iff the selection path used the Phase B
   concurrency override (Phase B grid selection OR bracket rooted in
   Phase B); False for Phase A grid selections AND bracket selections
   rooted in Phase A. Fix: the summary writer in
   `_run_measurement_async` now wraps the expression in `bool(...)` so
   the on-disk JSON contains a real boolean. The integer concurrency
   value is preserved separately under the new
   `phase_b_concurrency_value` field (audit-only; null when the Phase
   B override was not exercised) so operators retain access to the
   actual concurrency the runner used. The bool computation references
   the new `calibration_selected_bracket_root_phase` propagation from
   blocker 1 (so a bracket rooted in Phase B correctly reports True
   even though `selected_at_phase == "bracket"`). New deterministic
   test class `TestPhaseBConcurrencyUsedBool_FixLoop6` (7 tests)
   covers: source-scan that the writer emits `bool(...)` AND echoes
   `phase_b_concurrency_value`; source-scan that the bool expression
   references both `calibration_selected_at_phase == "B"` and
   `calibration_selected_bracket_root_phase == "B"`; the four-way
   truth table (Phase A grid → False; Phase B grid → True; bracket
   rooted in B → True; bracket rooted in A → False); and an
   end-to-end runner test that drives `_run_measurement_async` with
   `calibration_selected_at_phase="B"` and inspects the written
   `.summary.json` to confirm the emitted field is a Python `bool`
   (not `int`) with value `True` and `phase_b_concurrency_value=512`.
   The fixture writer
   `_write_smoke_summary_v23_fixture` and the existing
   `test_summary_carries_v23_selection_fields` test were updated to
   the bool contract.

3. **Fresh v2.3 smoke summaries with missing per-cell
   `admitted_pressure_passed` now raise `smoke_admitted_pressure_failed`.**
   Auditor finding: `validate_smoke_summary` skipped any cell whose
   `admitted_pressure_passed` was absent, and a test
   (`test_legacy_smoke_summary_without_admitted_pressure_field_passes`)
   pinned the skip even for fresh v2.3 summaries. The v2.3 runner
   echoes the field unconditionally, so a missing field on a v2.3
   summary indicates a hand-edited / forged / schema-incomplete
   summary that evidence cannot legitimately link against. Fix: the
   per-cell admitted-pressure scan in `validate_smoke_summary` now
   reads `data["schema_version"]` and, when it equals the v2.3 string
   `"task019.v2.3.measurement_summary"`, treats a missing field as a
   linkage failure that raises `LinkageValidationError(reason=
   "smoke_admitted_pressure_failed")` with a diagnostic that names
   the offending cell's `max_output_tokens`. Legacy back-compat is
   preserved ONLY for the explicit older
   `"task019.v2.2.1.measurement_summary"` schema (the v2.2.1 runner
   did not emit the per-cell field); on legacy schema the
   field-absent case is still skipped. Cells with ≥ 1 real 429
   continue to be skipped-by-429 regardless of schema version (the
   429 itself is proof the admission ceiling was crossed). The
   existing legacy test was updated to write the explicit v2.2.1
   schema version into its fixture and to assert the legacy schema
   round-trips. Two new tests cover the v2.3 strict path:
   `test_v23_smoke_summary_missing_admitted_pressure_field_raises`
   (v2.3 schema + zero-429 cell with missing field → raises with the
   cell's `max_output_tokens` in the diagnostic; 429-bearing cells
   with missing fields are NOT named in the diagnostic since they're
   skipped-by-429) and
   `test_v23_smoke_summary_missing_admitted_pressure_on_429_cell_passes`
   (v2.3 schema + 429-bearing cell with missing field → still passes
   because the gate is skipped-by-429). The fixture writer
   `_write_smoke_summary_v23_fixture` gains a `schema_version`
   parameter so tests can write either v2.3 (default) or legacy
   v2.2.1 fixtures.

Test deltas: `tests/test_measure_max_output_tokens_sweep.py` gains
14 new tests (5 in `TestBracketSelectionSerialization_FixLoop6` + 7
in `TestPhaseBConcurrencyUsedBool_FixLoop6` + 2 added to
`TestSmokeSummaryV23Propagation_FixLoop5` for the v2.3 strict
admitted-pressure enforcement). The existing
`test_legacy_smoke_summary_without_admitted_pressure_field_passes`
was updated to use the explicit `task019.v2.2.1.measurement_summary`
schema (the v2.3 path is now caught by the new strict test). The
existing `test_summary_carries_v23_selection_fields` was updated to
assert `phase_b_concurrency_used is True` (bool) and to read the new
`phase_b_concurrency_value` and `selected_bracket_root_phase` fields.
Full Task 019 pytest suite passes: **273 passed, 3 skipped** (baseline
was 259 passed, 3 skipped — delta = +14 new deterministic tests).
`ruff check` passes on both touched files
(`scripts/measure_max_output_tokens_sweep.py` and
`tests/test_measure_max_output_tokens_sweep.py`). No live Azure calls
were made during this fix loop.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #5 (auditor REQUEST-CHANGES, 3 PR-scope blockers)

Methodology audit gate (local CLI reviewer worker, Mac Mini local re-check) RETURNED
`REQUEST-CHANGES` with three new PR-scope blockers covering the
deterministic conservative estimator gating, bracket-probe bounded
retries, and v2.3 calibration field propagation in smoke/evidence
summaries. All three are addressed in this commit set without any live
Azure call.

1. **`_run_cell` now enforces `probe_max_usd` / `total_max_usd_stop_event`
   against the DETERMINISTIC CONSERVATIVE COMMITTED cost, not realized
   billed cost.** Auditor finding: the v2.2.1 realized-cost accounting
   set 429 cost to $0 and zero-usage successes to $0, so a fast 429-only
   or zero-usage stream could dispatch unbounded calls without the cap
   ever firing. Fix: `_run_cell` tracks both `cell_usd` (realized,
   audit-only) and `cell_committed_usd` (deterministic =
   `n_dispatched × DETERMINISTIC_PER_CALL_USD`). The committed counter
   is incremented synchronously BEFORE every `asyncio.create_task` (both
   pre-warm and ramp) so the cap admission check is race-free; the
   concurrent-dispatch invariant (Microfix B) is preserved — no
   `await call()` inserted into the dispatch loop body. `probe_max_usd`
   admission now checks `committed + per_call > cap`. The total-USD
   stop event keys off `total_committed_usd >= 0.85 × total_max_usd`
   (separate accounting coroutine signals; in-flight tasks finish, no
   new dispatch). `_run_cell` return tuple is now 5-tuple
   `(records, cell_usd_realized, cell_committed_usd, max_in_flight,
   halt_reason)`. `MeasurementResult` gains a `total_committed_usd`
   field. Calibration and measurement summaries echo BOTH the realized
   and committed totals (and per-probe / per-cell breakdowns) so an
   operator can audit the deterministic guardrail vs the real spend.
   New deterministic test class `TestDeterministicCommittedCostCap_FixLoop5`
   (5 tests) covers: a 429-only response stream consumes deterministic
   budget and trips `probe_max_usd_hit` at the expected dispatched-call
   count; a zero-usage success stub CANNOT bypass `probe_max_usd` (the
   v2.2.1 failure mode the auditor named); the new 5-tuple return
   contract and `committed >= realized` invariant; the
   `total_max_usd_stop_event` pre-set case blocks all new ramp
   dispatch with `halt_reason='total_max_usd_stop_event_set'`; and a
   line-level source-scan guards against any future regression
   re-introducing a bare `await _admit_and_call(...)` in the dispatch
   loop body. Existing 4-tuple-unpacking call sites in
   `TestEarlyStopOnFirst429_FixLoop4`,
   `TestConcurrentDispatchInvariant`, and
   `TestProbeMaxCallsEnforcement_AuditorMicrofix` updated to the new
   5-tuple shape.

2. **Bracket probes now carry parent-style bounded-retry semantics.**
   Auditor finding: the v2.3 bracket search aborted IMMEDIATELY on a
   warm-criterion / backlog / admitted-pressure failure of either the
   bracket largest-cell probe OR the bracket smallest-cell control
   probe, even though parent calibration probes have a bounded one-shot
   retry for exactly the same eligibility classes. Fix: `_do_bracket_search`
   inside `_run_calibration_async` now retries the failing probe at the
   same bracket TPS exactly ONCE before aborting, using a composite
   `prompt_cache_key` suffix `_bracket{N}_retry1` (warm/backlog) or
   `_bracket{N}_retry1_admp` (admitted-pressure), where N is the
   bracket depth in 1..3. The retry mirrors the parent suffix
   taxonomy without collision and keeps the per-probe artifact trail
   unambiguous about (depth, retry cause). `CALIB_BUCKET_KEY_RE` regex
   grammar extended to admit the new composite suffixes; the explicit
   allowed-suffix set in `build_calibration_cache_key` lists all six
   new variants. `bracket_trace` now records both attempts (initial +
   retry); the new abort outcome is `aborted_eligibility_after_retry`
   and the inconclusive reason detail is
   `bracket_aborted_at_depth_N_eligibility_fail_after_retry`. Maximum
   bracket depth (3) is preserved; the total-USD committed cap is
   re-checked before each retry. New deterministic test class
   `TestBracketBoundedRetrySuffixes_FixLoop5` (10 tests, including
   parametrised depth 1/2/3) covers: parent-suffix-distinct keys for
   all 6 new bracket+retry suffixes; rejection of depth-4 and
   double-retry forgeries on both the builder and the regex; source-
   scan regressions that pin the helper, `largest_attempts /
   control_attempts` tracking lists, and the new outcome /
   reason-detail strings.

3. **Smoke / evidence summaries now propagate v2.3 calibration
   selection fields, and `validate_smoke_summary` enforces two new
   exit-9 linkage reasons.** Auditor finding: the smoke summary
   schema_version was stale at `task019.v2.2.1.measurement_summary`
   and carried only `selected_peak_tps` from calibration — none of
   `selected_at_phase`, `selected_via`, `selected_at_bracket_depth`,
   `phase_b_concurrency_used`, per-cell `first_429_metadata_present`,
   or per-cell `admitted_pressure_passed` were echoed for downstream
   audit / linkage. Fix: `_run_measurement_async` summary writer
   bumped to schema `task019.v2.3.measurement_summary` and now adds
   summary-level `selected_via`, `selected_at_phase`,
   `selected_at_bracket_depth`, `phase_b_concurrency_used`, and
   `total_committed_usd`; per-cell summaries gain `cell_committed_usd`,
   `admitted_pressure` block (via `compute_admitted_pressure_block`),
   `admitted_pressure_passed` bool, and `first_429_metadata_present`
   bool. `run_measurement` threads the v2.3 calibration selection
   metadata from `calibration_result_data` into the measurement helper
   via four new kwargs. `validate_smoke_summary` accepts a new
   `expected_selected_at_phase` kwarg and now enforces:
   `smoke_selected_at_phase_mismatches_calibration` (mismatch between
   smoke and calibration phase labels) and
   `smoke_admitted_pressure_failed` (any per-cell admitted-pressure
   failure with zero 429s observed — gate is skipped-by-429 when ≥ 1
   real 429 is present). New deterministic test class
   `TestSmokeSummaryV23Propagation_FixLoop5` (9 tests) covers: round-
   tripping the v2.3 summary fields through the validator; phase
   mismatch raising the new exit-9 reason; admitted-pressure failure
   with zero 429s raising the new exit-9 reason; admitted-pressure
   failure with ≥ 1 real 429 being correctly skipped-by-429; the
   legacy-summary back-compat path when the per-cell field is absent;
   schema-version round-trip; the bracket-search v2.3 summary-level
   propagation; and the diagnostic-quality requirement that the
   mismatch message names both the smoke value AND the expected
   calibration value.

Also (non-blocking): two `halt_reason` example strings in
`benchmarks/07-max-output-tokens-reservation/README.md` corrected to
their implementation names — `total_max_usd_stop_event` →
`total_max_usd_stop_event_set` and `early_stop_first_429` →
`first_429_observed`.

Total new deterministic tests added in fix loop #5: 24 (5 + 10 + 9).
Full suite now passes: **259 passed, 3 skipped** (baseline was 235
passed, 3 skipped). `ruff check` passes on both touched files. No live
Azure calls were made during this fix loop.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #4 (auditor REQUEST-CHANGES, 3 PR-scope blockers)

Methodology audit gate (local CLI reviewer worker, Mac Mini local re-check) RETURNED
`REQUEST-CHANGES` with three new PR-scope blockers covering preflight
gate semantics, stale active YAML text, and a missing spec-pinned
early-stop behaviour. All three are addressed in this commit set
without any live Azure call.

1. **Smoke/evidence USD preflight gate now uses the deterministic
   conservative estimator (NO 429-no-bill discount, NO cached-token
   discount, NO pricing snapshot consulted).** Auditor finding: the
   smoke/evidence preflight in `_run_measurement_async` was routed
   through `compute_projected_usd(...)` which folds in pricing-driven
   429-no-bill and cached-token discounts. Under the active deterministic
   estimator at $0.009 per dispatched call, the spec/docs require:
   smoke @ selected_peak_tps = 12 → projected $46.31 > $45 ceiling →
   ABORT (`smoke_preflight_over_ceiling_narrow_sweep_or_file_new_spec`);
   evidence @ selected_peak_tps = 5 → projected $96.20 > $90 ceiling →
   ABORT (`evidence_preflight_over_ceiling_narrow_sweep_or_file_new_spec`);
   lower selected_peak_tps values continue to pass. The pricing-driven
   `compute_projected_usd` is retained for dry-run only. New numeric
   regression test class `TestSmokeEvidenceDeterministicPreflight_FixLoop4`
   (10 tests) covers the smoke/evidence pass-and-abort matrix without
   any `compute_projected_usd` monkeypatch; the existing
   `TestSmokeEvidencePreflightOrderingAndReason_AuditorMicrofix`
   smoke/evidence end-to-end tests had their now-redundant
   `compute_projected_usd` monkeypatch removed (the natural
   selected_peak_tps = 12.0 / 5.0 fixture values trip the new estimator
   directly).

2. **`experiments/exp007_max_output_tokens_sweep.yaml` updated from
   stale v2.2.1 to v2.3 active text.** Auditor finding: header comment
   block, `description`, calibration-stage comments, budget comments,
   and the obsolete superseded comment block all still labelled the
   experiment as Task 019 v2.2.1 with the single-grid Stage 0.5
   calibration, the wrong active-cap set, and the wrong preflight
   estimator semantics. The active YAML now carries: v2.3 two-phase
   escalate-until-429 narrative (Phase A predeclared 7-member grid +
   Phase B exact 6-member `candidate_tps_grid_phase_b`
   `[5.0, 8.0, 12.0, 16.0, 24.0, 32.0]`); admitted-pressure validation
   gate (0.70 floor over last 30 s) and bounded geometric-midpoint
   bracket search (depth 3); active caps `calibration $220` /
   `calibration_probe $60` / `smoke $50` / `evidence $100` /
   `contingency $30` / `total $400` (NOT $800); the correct
   preflight statements that smoke selected_peak_tps ≥ 12 aborts and
   evidence selected_peak_tps ≥ 5 aborts under the active
   deterministic conservative estimator; and the explicit PAYG-not-PTU
   caveat is preserved verbatim. Active values (active runtime constants,
   pricing-snapshot path, SHA pins, sweep grid, user-prompts index set)
   are unchanged.

3. **Spec-pinned early-stop-on-first-429 is now implemented for
   calibration probes (`largest` and `smallest`).** Auditor finding:
   the YAML carried `early_stop_on_first_429_largest: true` and
   `early_stop_on_first_429_smallest: true`, but `_run_cell` created
   all ramp tasks and awaited them so the flag was a no-op. v2.3 spec
   pins `halt_reason: first_429_observed` for probes that stop on the
   first real 429. The implementation adds an internal
   `early_stop_429_event: asyncio.Event` inside `_run_cell` (set on
   the first observed 429 from `_admit_and_call` when the new
   `early_stop_on_first_429` kwarg is True), consults the event
   BEFORE `asyncio.create_task` in the dispatch loop and inside
   `_admit_and_call` after the pacer sleep returns (HTTP is NEVER
   awaited inside the dispatch-loop body), filters skipped-call `None`
   records, and records `halt_reason="first_429_observed"` on the cell
   result. `_probe_once` (nested inside `_run_calibration_async`)
   wires the per-role flag from `calib.early_stop_on_first_429_largest`
   and `calib.early_stop_on_first_429_smallest`. The concurrency
   invariant (no sequential-await dispatch under any cap, scheduled
   concurrent dispatch preserved) is verified by both an existing
   source-scan invariant test and the new
   `TestEarlyStopOnFirst429_FixLoop4` (7 tests): first 429 sets the
   stop event, no NEW `create_task` after stop, `halt_reason` recorded
   as `first_429_observed`, concurrent dispatch invariant preserved
   (max_in_flight ≥ 2 with a 500 ms-per-call stub at 8 TPS), legacy
   `early_stop_on_first_429=False` behaviour unchanged, and the
   `_probe_once` per-role flag wiring is honoured.

Test deltas: `tests/test_measure_max_output_tokens_sweep.py` gains
17 new tests (10 preflight + 7 early-stop) appended at file end; two
existing smoke/evidence end-to-end tests have their now-redundant
`compute_projected_usd` monkeypatch removed; full Task 019 pytest
suite continues to pass with the prior count of environmental skips
preserved. No live Azure call.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #3 (auditor REQUEST-CHANGES, 2 PR-scope blockers)

Methodology audit gate (local CLI reviewer worker, Mac Mini local re-check) PASSED the
four prior fix-loop-#2 blockers but RETURNED `REQUEST-CHANGES` on two
remaining PR-scope blockers. Both are addressed in this commit set
without touching code, YAML, or test logic and without any live Azure
call.

1. **PR-facing methodology docs updated from stale v2.2.1 to v2.3.**
   `benchmarks/07-max-output-tokens-reservation/README.md` and
   `benchmarks/07-max-output-tokens-reservation/analysis.md` were
   still labelled v2.2.1 and described the single-grid procedure with
   v2.2.1's 7-member outcome enum, the $20 / $15 / $75 / $120 ceiling
   set, the retired terminal outcome
   `no_largest_cell_429_at_any_candidate_tps`, and the v2.2.1
   description of Phase A as the only phase. They are now fully
   self-consistent with the private Task 019 spec (lab-only workspace)
   v2.3 (with the spec's three microfixes A/B/C), the active
   `experiments/exp007_max_output_tokens_sweep.yaml`, and the runtime
   constants in `scripts/measure_max_output_tokens_sweep.py`.
   Specifically the docs now carry:
   - Two-phase Stage 0.5 narrative (Phase A safe ramp + Phase B
     escalate-until-429),
   - Phase B EXACT pinned six-member grid `[5.0, 8.0, 12.0, 16.0,
     24.0, 32.0]` with the four distinct YAML-load reasons
     (`candidate_tps_grid_phase_b_contains_duplicate_value`,
     `..._contains_ad_hoc_value`, `..._member_missing`,
     `..._not_sorted_ascending`),
   - v2.3 9-member outcome enum (the three NEW outcomes
     `calibration_probe_inconclusive_admitted_pressure_insufficient`,
     `no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling`,
     `no_largest_cell_429_at_any_phase_b_candidate_tps_driver_pressure_insufficient`),
   - Retired `no_largest_cell_429_at_any_candidate_tps` documented as
     intra-calibration signal only (NOT a terminal verdict under v2.3),
   - Admitted-pressure validation gate (0.70 floor, last-30-s window,
     computed from `admitted_dispatch_iso`),
   - Bounded bracket search (`bracket_max_depth: 3`, geometric
     midpoint, same-phase only) with structured `bracket_trace` in
     the calibration result,
   - Three dispatch-time ISO-8601 timestamps per record
     (`intended_dispatch_iso` NEW, `scheduled_dispatch_iso` and
     `admitted_dispatch_iso` preserved),
   - `first_429_metadata` block schema on every probe that observed
     `n_429 ≥ 1`,
   - Concurrent-dispatch invariant requirement (no sequential-await
     dispatch loop under cap enforcement),
   - Cache-key suffixes `_retry1` (preserved), `_retry1_admp` (NEW),
     `_bracketN` (NEW),
   - Phase B concurrency override `runtime.concurrency_phase_b: 512`
     scoped to Phase B and Phase-B-rooted bracket probes only (Phase
     A, smoke, evidence retain `runtime.concurrency: 96`),
   - **Conservative-but-useful Task 019 v2.3 active caps:**
     calibration $220, smoke $50, evidence $100, contingency $30,
     **total $400** (NOT the $800 figure from a superseded v2.3
     draft); per-probe cap $60; preflight at 0.9× ceiling; mid-run
     halt at 0.85× ceiling; calibration total_max_usd stop_event at
     $187 (0.85 × $220),
   - Phase A probe call cap `probe_max_calls: 600` and Phase B
     `probe_max_calls_phase_b: 6624` (the v2.2.1 placeholder caps
     are retired),
   - Admitted-pressure-PASS column added to the headline 429-onset
     table in `analysis.md`,
   - PAYG-not-PTU caveat preserved verbatim (`ptu_evidence: false`
     enforced at YAML load and echoed into every
     `runs/*.summary.json`); the literal guardrail string "Do not
     exhaust shared budget; cap is an accounting guardrail, not a
     spend target" carried in both files,
   - Quarantine paths reflected: the diagnostic v2.1 artifacts
     previously at `runs/20260529T160517Z_*` and
     `runs/20260529T165523Z_*_evidence.partial.*` are now under
     `runs/_quarantined_pre_v23_microfix/` (gitignored via
     `benchmarks/*/runs/_quarantined_*/`),
   - Diagnostic v2.1 block clearly labelled "DIAGNOSTIC ONLY" and
     non-evidence under v2.3.

2. **PR-visible untracked runtime lock excluded from PR scope.** The
   working tree carried `benchmarks/07-max-output-tokens-reservation/.runlock`
   — a stale lock file (`pid=41731` long since exited; `started_at_iso:
   2026-05-30T01:09:40Z` / `expected_completion_iso:
   2026-05-30T01:11:40Z`; > 12 hours old at the time of this fix).
   The file would have entered the Task 019 PR the moment the
   operator ran `git add benchmarks/07-max-output-tokens-reservation/`.
   Fix:
   - Added `benchmarks/*/.runlock` to `.gitignore` (positioned with
     the existing per-benchmark runtime-artifact patterns). Run-lock
     SEMANTICS are fully preserved without the lock file being
     tracked, since `acquire_runlock()` always creates the file on
     demand via `os.open(..., O_CREAT | O_RDWR, 0o600)` when absent
     — the lock contract is the fcntl exclusive flock on the file
     descriptor, not the file's persistence between runs.
   - Deleted the stale local-only `.runlock` (verified stale: PID
     dead, `expected_completion_iso` had passed by > 12 h). No user
     data outside the repo was touched.
   - Verified `git check-ignore -v benchmarks/07-max-output-tokens-reservation/.runlock`
     reports the new `.gitignore` line 59
     (`benchmarks/*/.runlock`).

NO live Azure call was made; the 220 tests in
`tests/test_measure_max_output_tokens_sweep.py` and
`tests/test_analyze_max_output_tokens_sweep.py` all pass; ruff
clean on the touched files.

### Fixed — Task 019 v2.3 microfix 2026-05-30 fix loop #2 (auditor REQUEST-CHANGES, 4 blockers)

Methodology audit gate (local CLI reviewer worker) RETURNED `REQUEST-CHANGES` on the
v2.3 implementation. All four blockers are addressed in this commit set
without altering the v2.3 calibration semantics or pinned values.

1. **Phase B calibration-result grid validation now enforces the pinned
   grid; result-provided grids are never authoritative.** Prior code in
   `validate_calibration_result` fell back to the pinned grid only when
   the result OMITTED `candidate_tps_grid_phase_b`; a forged result
   with `candidate_tps_grid_phase_b=[7.0]` and `selected_peak_tps=7.0`
   silently passed (7.0 ∈ [7.0]). The fix: always membership-check
   against the pinned `CALIBRATION_CANDIDATE_TPS_GRID_PHASE_B`
   constant; if the result file provides
   `candidate_tps_grid_phase_b`, it MUST equal the pinned grid exactly
   (same length, members, ascending order). Missing key on a Phase B
   selection is rejected — no silent default. Regression tests in
   `TestPhaseBGridPinnedAgainstForgedResult_FixLoop2` cover the forged
   `[7.0]` attack vector plus superset, reordered, missing, and
   malformed-member variants.

2. **Malformed experiment YAML now triggers a deterministic startup
   abort + artifact, no stack trace.** Prior code in `main()` only
   caught `FileNotFoundError`, `LinkageValidationError`, `ValueError`
   around `load_experiment`; `yaml.safe_load` raises `yaml.YAMLError`
   on parser failure, which propagated up as an uncaught traceback.
   The fix adds a new top-level `except yaml.YAMLError` handler in
   `main` that returns `EXIT_LINKAGE_FAIL` (exit 9, same as the four
   Phase B grid mutations) and emits a deterministic
   `calibration_startup_abort.result.json` artifact under
   `{benchmarks_root}/unknown_experiment/` (no `experiment_id` can be
   recovered from an unparseable YAML) with the pinned
   `startup_abort_reason="experiment_yaml_malformed"`. The new module
   constant `EXPERIMENT_YAML_MALFORMED_REASON` is exported. Regression
   tests in `TestMalformedYamlStartupAbort_FixLoop2` cover the exit
   code, artifact contents, log format (no `Traceback` header), and
   the no-raise contract for the CLI.

3. **`probe_max_calls` now fully echoed in calibration artifacts.**
   `_probe_once` correctly attached `halt_reason` to each probe agg
   (existing v2.3 fix), but the serialized `probes` block in
   `calibration_result.json` OMITTED the field, and the top-level
   result echoed `calibration_probe_max_usd` only — not the per-phase
   call caps. The fix adds `"halt_reason": p.get("halt_reason")` to
   every entry of the `probes` list and adds top-level
   `calibration_probe_max_calls_phase_a` /
   `calibration_probe_max_calls_phase_b` keys alongside the spend cap.
   Regression tests in
   `TestProbeMaxCallsEchoInCalibrationResult_FixLoop2` verify the
   source-level field emissions, the producer-side
   `agg["halt_reason"] = probe_halt_reason` assignment in
   `_probe_once`, and a JSON round-trip of a synthesised result doc
   with `halt_reason="probe_max_calls_hit"`.

4. **Live Azure run artifacts quarantined out of PR scope.** The
   working tree carried `dry_run=false` smoke / evidence /
   calibration JSONLs under
   `benchmarks/07-max-output-tokens-reservation/runs/` that pre-date
   this fix loop and have not been audited. They are moved to
   `runs/_quarantined_pre_v23_microfix/` (operator forensic data
   retained on disk, README written) and excluded from git scope via
   two new `.gitignore` rules:
   `benchmarks/*/runs/_quarantined_*/` and
   `benchmarks/*/runs/_partial_pre_cost_fix/`. The dry-run JSONL +
   summary, the `figures/` chart directory, and the existing
   `_partial_pre_cost_fix/` quarantine are unaffected.

NO live Azure call was made; all 209 tests in
`tests/test_measure_max_output_tokens_sweep.py` (8 in
`test_analyze_max_output_tokens_sweep.py`) pass; ruff clean on every
changed file.

### Added — Task 019 v2.3: Two-phase (A/B) calibration + admitted-pressure gate + bracket search (2026-05-30)

**Two-phase escalate-until-429 calibration.** v2.3 supersedes v2.2.1's
single-grid procedure with a two-phase calibration:

- **Phase A — safe ramp.** Identical to v2.2.1: iterate the predeclared
  `candidate_tps_grid: [0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]` ascending,
  per-candidate largest-cell probe with bounded retry once, smallest-cell
  control probe on first 429.
- **Phase B — escalate-until-429.** NEW. Entered iff Phase A iterated the
  full grid without observing a largest-cell 429 AND every Phase A largest
  probe passed the admitted-pressure gate. Iterates the v2.3 NEW exact
  full grid `candidate_tps_grid_phase_b: [5.0, 8.0, 12.0, 16.0, 24.0,
  32.0]`. Phase B runs at the dedicated `runtime.concurrency_phase_b: 512`
  (scoped only to Phase B and Phase-B-rooted bracket probes); Phase A
  continues to use `runtime.concurrency: 96`.

**9-member calibration outcome enum (pinned, v2.2.1's 7 minus
`no_largest_cell_429_at_any_candidate_tps` plus 3 v2.3 NEW):**
`selected`, `no_usable_contrast_at_this_prompt_deployment`,
`smallest_cell_control_probe_inconclusive_cap_hit`,
`calibration_total_usd_exhausted`,
`calibration_probe_inconclusive_cache_not_warm`,
`calibration_probe_inconclusive_backlog_excessive`,
`calibration_probe_inconclusive_admitted_pressure_insufficient` (NEW),
`no_largest_cell_429_at_any_phase_b_candidate_tps_endpoint_not_throttling`
(NEW),
`no_largest_cell_429_at_any_phase_b_candidate_tps_driver_pressure_insufficient`
(NEW). The retired `no_largest_cell_429_at_any_candidate_tps` survives
as an internal symbolic Phase A → Phase B transition signal but is no
longer a terminal outcome.

**Admitted-pressure validation gate.** v2.3 adds a fourth blocking
eligibility gate (alongside warm criterion, backlog excessive,
all-empty-visible-output): a probe is admitted-pressure-PASS iff
`admitted_count_last_30s / (candidate_tps × 30) ≥ 0.70` over the rolling
last-30-seconds admitted-dispatch window, where `admitted_dispatch_iso`
records the timestamp at which a request crossed the concurrency
semaphore (NOT the scheduled or intended dispatch time). The gate is
ALWAYS skipped (auto-pass) when the probe observed at least one real
429 — observing throttling supersedes the synthetic floor check.
On the first gate failure the runner retries once with the `_retry1_admp`
cache-key suffix; on a second failure the probe terminates with outcome
`calibration_probe_inconclusive_admitted_pressure_insufficient` (exit 8).

**Three new ISO-8601 per-record timestamps:** every record now carries
`intended_dispatch_iso` (the schedule's intended dispatch time, computed
deterministically at cell start), `scheduled_dispatch_iso` (the time the
record was released by the dispatch pacer), and `admitted_dispatch_iso`
(the time the record crossed the concurrency semaphore). The triplet
makes pacer lag vs. concurrency-saturation lag observably distinct in
the JSONL.

**Concurrent-dispatch invariant (Microfix B).** The v2.2.1 `_run_cell`
sequential-await branch under `probe_max_usd is not None` (one `await
_admit_and_call()` per loop iteration → effective dispatch ≈ 1 call per
HTTP wall time regardless of `candidate_tps`) has been REWRITTEN. All
paths now use `asyncio.create_task` + concurrent `asyncio.gather`. Cap
enforcement uses only non-blocking mechanisms: (a) an `asyncio.Event`
total_max_usd stop sentinel checked synchronously before each
`create_task`; (b) an advisory `cell_usd + DETERMINISTIC_PER_CALL_USD >
probe_max_usd` admission check; (c) a probe-boundary cumulative-spend
re-evaluation. NO sequential await in the dispatch loop body, ever.

**Probe-schedule intended-rate runtime invariant.** Before computing the
admitted-pressure block, the runner asserts that the SCHEDULE itself
intended to dispatch at sufficient rate (counting `intended_dispatch_iso`
within the rolling window). If the intended count is below
`candidate_tps × 30 × 0.70`, the runner aborts with
`ProbeScheduleIntendedRateInsufficientError` (exit 8, reason
`probe_schedule_intended_rate_insufficient`) — this surfaces a true
scheduler-generation bug that would otherwise be masked by Microfix B's
concurrent dispatch.

**Bracket search before terminal no-contrast.** When a same-phase probe
gives a largest-cell 429 AND a smallest-cell control 429 (the
no-usable-contrast trigger), v2.3 attempts up to `bracket_max_depth: 3`
geometric-midpoint bracket probes between the last `T_low` (eligible &
0 largest-429 in the same phase) and the failing `T_high` BEFORE
emitting terminal `no_usable_contrast_at_this_prompt_deployment`. Each
bracket probe uses the `_bracketN` cache-key suffix and respects the
same eligibility gates. Bracket trace is recorded as a structured array
in the result.

**First-429 metadata block.** Every probe with `n_429_records ≥ 1` now
emits a `first_429_metadata` block including
`admitted_peak_rpm_observed_last_30s`,
`admitted_steady_state_rpm_observed_last_30s`,
`dispatch_backlog_ms_at_first_429`, `retry_after_ms`,
`backlog_p50_ms_at_first_429`, `backlog_p95_ms_at_first_429`,
`cache_hit_ratio_at_first_429`,
`visible_output_tokens_of_preceding_success`, `prompt_cache_key_used`,
prompt-identity shas, `candidate_tps`, `probe_phase`, `phase`,
`bracket_depth`. This is the durable observability anchor for the v2.3
two-phase + bracket + admitted-pressure procedure.

**Conservative caps raised (v2.3 calibrated for two-phase
escalate-until-429 worst-case):** calibration $220 / per-probe $60 /
smoke $50 / evidence $100 / contingency $30 / task-total $400. The
active YAML `total_max_usd` is **$220** (NOT the $800 figure that
appeared in a draft v2.3 banner). The `total_max_usd_stop_event` fires
at `0.85 × $220 = $187` (advisory, non-blocking); this is a guardrail,
not a spend target.

**Phase B exact-grid validator (Microfix C, 4 distinct reasons in
order):** the YAML loader now rejects Phase B grids with 4 distinct
`LinkageValidationError` reasons, evaluated in this order (so a grid
like `[5.0, 5.0, 8.0, 12.0, 16.0, 24.0, 32.0]` reports the duplicate,
not a misleading missing-member error):
`candidate_tps_grid_phase_b_contains_duplicate_value`,
`candidate_tps_grid_phase_b_contains_ad_hoc_value`,
`candidate_tps_grid_phase_b_member_missing`,
`candidate_tps_grid_phase_b_not_sorted_ascending`.

**Preserved from v2.2.1 (no regression):** PAYG-only (no PTU fallback);
SDK `max_retries = 0`; the run-lock; pricing-freshness gate; durable
inter-stage linkage via sha256 + paths (no auto-discovery); the
diagnostic-only retention of pre-v2.2 artifacts.

### Added — Task 019 v2.2.1: Stage 0.5 adaptive TPS calibration + durable inter-stage linkage (2026-05-30)

**Calibration-selected `peak_ramp_tps` replaces v2.1's fixed 0.33.** v2.2.1
inserts a new Stage 0.5 (calibration) before every smoke / evidence run.
The runner walks the predeclared candidate-TPS grid
`[0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]` ascending; at each candidate it
runs a 3-minute constant-rate probe on the largest cell
(`max_output_tokens=16 384`). The first candidate that produces ≥ 1 real
429 is followed up by a same-TPS control probe on the smallest cell
(`max_output_tokens=256`); a candidate is `selected` iff the largest cell
saturates AND the smallest control observes zero 429. Bounded retry
once with `_retry1` cache-key suffix on warm-failed / backlog-excessive
probes; no probe selection from all-empty-visible-output records.
Calibration is hard-capped at $20 total / $5 per probe and uses a
deterministic conservative cost estimator that does NOT discount for the
PAYG 429-no-bill quirk.

**7-member calibration outcome enum (pinned):** `selected`,
`no_largest_cell_429_at_any_candidate_tps`,
`no_usable_contrast_at_this_prompt_deployment`,
`smallest_cell_control_probe_inconclusive_cap_hit`,
`calibration_total_usd_exhausted`,
`calibration_probe_inconclusive_cache_not_warm`,
`calibration_probe_inconclusive_backlog_excessive`. Any non-`selected`
outcome is a first-class terminal failure (exit `8`,
`CalibrationTerminalError`) and stops the pipeline; the operator MUST
record the failure honestly rather than weakening the grid or
re-running with ad-hoc parameters.

**Durable inter-stage linkage via sha256 + paths (no auto-discovery).**
Smoke now refuses to start without `--calibration-result <path>` (exit
`9`, `LinkageValidationError`,
`reason=calibration_result_missing`); evidence refuses without BOTH
`--calibration-result` AND `--smoke-summary` (`smoke_summary_missing`).
The calibration result's sha256 is stored in the sibling
`*.calibration.summary.json` (NOT inside the result file itself, so the
result-file bytes have a clean round-trip hash); the smoke summary's
own sha256 is stored in a sibling `.sha256` text sidecar (same reason).
Evidence validates the calibration → smoke triple linkage
(`selected_peak_tps`, `calibration_result_path`,
`calibration_result_sha256`) match before dispatching any HTTP call.
The CLI ALSO refuses any `--peak-ramp-tps` override (exit `9`,
`reason=peak_ramp_tps_override_forbidden_use_calibration_result`); the
TPS value is owned by calibration end-to-end.

**Raised spend ceilings.** Smoke $4 → $15, evidence $25 → $75; added
calibration $20 / per-probe $5; new task-total cap $120. The
deterministic conservative cost estimator projects (no 429-no-bill
discount): calibration ≈ $17.23 worst-case, smoke @ `peak_tps=3.0`
≈ $12.29, evidence @ `peak_tps=3.0` ≈ $58.40 — all well under the
respective ceilings.

**Active YAML gate.** v2.1's single-TPS feasibility math
(`peak_ramp_tps × 60 × (prompt + cap)`) is replaced by
`evaluate_candidate_grid_sanity()`: lowest TPS keeps smallest cell ≤
0.85 × quota (so the control probe has headroom); highest TPS pushes
largest cell ≥ 1.25 × quota (so calibration can produce real 429s).
Grid is validated against the allowed-value frozenset (ad-hoc values
rejected with `reason=candidate_tps_grid_contains_ad_hoc_value`) and
sorted ascending (`candidate_tps_grid_not_sorted_ascending`).

**New exit codes** (additions to the v2.1 0/1/2/3/4/5/6/7 mapping):

| Code | Meaning |
|---:|---|
| `8` | Calibration terminal (any non-`selected` outcome) |
| `9` | Linkage validation (missing / mismatched / forbidden flags) |

Files modified:

- `scripts/measure_max_output_tokens_sweep.py` — added
  `CalibrationTerminalError`, `LinkageValidationError`,
  `CALIBRATION_OUTCOME_ENUM` (7-member frozenset), candidate-grid +
  budget constants, pure helpers (`build_calibration_cache_key`,
  `evaluate_candidate_grid_sanity`,
  `deterministic_conservative_cost_estimator`,
  `compute_calibration_result_sha256`,
  `write_smoke_summary_sidecar_sha256`,
  `validate_calibration_result`, `validate_smoke_summary`),
  `_BudgetBlock` extended with 4 v2.2.1 fields, `_CalibrationBlock` +
  `ExperimentConfig.calibration` dataclass additions, YAML loader
  extended to parse + validate the `calibration:` block, CLI extended
  with `--stage calibration` + `--calibration-result` +
  `--smoke-summary` + `_detect_forbidden_peak_ramp_tps_override`,
  `main()` maps `CalibrationTerminalError` → exit 8 and
  `LinkageValidationError` → exit 9, `run_measurement` validates both
  linkage paths before any HTTP, `run_calibration` +
  `_run_calibration_async` implement the grid walk + bounded retry +
  outcome dispatch, `_run_cell` extended with three v2.2.1-only
  kwargs (`cache_key_override`, `constant_rate`, `probe_max_usd`) that
  default to v2.1 behaviour for smoke / evidence cells.
- `scripts/analyze_max_output_tokens_sweep.py` — version-agnostic
  docstring update; calibration linkage echo (`selected_peak_tps`,
  `calibration_run_id_short`, `calibration_result_sha256`,
  `calibration_result_path`) appended to the run-totals footer when
  the v2.2.1 fields are present on the summary.
- `experiments/exp007_max_output_tokens_sweep.yaml` — added
  `calibration:` block (full 7-member grid + probe params + 24-h
  freshness + $20/$5 ceilings); raised `budget` block to v2.2.1
  ceilings ($15 / $75 / $120 with calibration sub-keys); v2.2.1
  protocol comment in the runtime block noting that `peak_ramp_tps:
  0.33` is now the lowest grid candidate, no longer the active pin.
- `benchmarks/07-max-output-tokens-reservation/README.md` — v2.2.1
  protocol description, calibration outcome enum table, v2.2.1 spend
  ceiling table, v2.2.1 CLI usage with three-stage flow, exit codes
  8/9 added; v2.1 status block preserved verbatim under "DIAGNOSTIC
  ONLY" header.
- `benchmarks/07-max-output-tokens-reservation/analysis.md` — v2.2.1
  protocol section, calibration linkage table, calibration outcome
  interpretation guide; v2.1 status block preserved verbatim under
  "DIAGNOSTIC ONLY" header; v2.1 partial evidence sidecar marked
  "DIAGNOSTIC ONLY" inline.
- `tests/test_measure_max_output_tokens_sweep.py` — added v2.2.1 test
  classes (`TestExitCodes_v221`, `TestCalibrationCandidateGrid`,
  `TestPeakRampTpsOverrideForbidden`, `TestCandidateGridSanityCheck`,
  `TestDeterministicConservativeEstimator`,
  `TestCalibrationOutcomeEnum`, `TestCalibrationCacheKey`,
  `TestSmokeRefusalPaths`, `TestEvidenceRefusalPaths`,
  `TestNoAutoDiscovery`, `TestSmokeSidecarSha256`,
  `TestOldV21SmokeGateFails`, `TestCalibrationDurableLinkage`,
  `TestBoundedWarmRetry`, `TestBoundedBacklogRetry`,
  `TestAllEmptyVisibleOutputPath`); all 83 baseline v2.1 tests
  continue to pass.

Out of scope (deliberate): live calibration → smoke → evidence
results, calibration-selected TPS, realized USD per stage, and v2.2.1
chart artifacts. These populate after the first live calibration run
on the Mac Mini local environment (separate CHANGELOG entry).

### Added — Task 019 v2.1: `max_output_tokens` reservation sweep proxy (2026-05-29) — DIAGNOSTIC ONLY under v2.2.1

> The v2.1 entries below are preserved verbatim for audit reference.
> Under v2.2.1, the v2.1 smoke + partial-evidence artifacts in
> `benchmarks/07-max-output-tokens-reservation/runs/20260529T*` are
> retained as DIAGNOSTIC ONLY: they document the protocol failure that
> motivated the v2.2.1 calibration design and MUST NOT be cited as
> evidence of Hypothesis I.

**Hypothesis I proxy, PAYG-not-PTU.** Adds a behavioural benchmark that
sweeps `max_output_tokens` across `{256, 512, 1024, 2048, 4096, 8192, 16384}`
against the throttled PAYG GlobalStandard deployment `ptu-deploy-throttled`
(60 K TPM) at fixed `reasoning.effort=low`, concurrency 96, and
canonical-formula peak ramp TPS = 0.33. Records per-cell
`first_429_arrival_rpm_at_request_time` together with `visible_output_tokens`,
`reasoning_tokens`, `cached_tokens`, `dispatch_backlog_ms`, and
`prompt_cache_key_used` so reviewers can decide whether the throttled
admission layer reserves quota at the *cap* (predicted: 429-onset RPM
decreases monotonically as `max_output_tokens` rises while
`visible_output_tokens` stays roughly flat). **This is *not* PTU evidence**
(`ptu_evidence=false` is pinned in the experiment YAML and echoed in
`summary.json → pinned_confounds_echo`); the PTU concept is cited only in
`benchmarks/07-max-output-tokens-reservation/README.md` to mark the absence
of PTU instrumentation.

New files:

- `scripts/measure_max_output_tokens_sweep.py` — 3 184-line runner with
  async_scheduled dispatcher (SHA-seeded deterministic inverse-CDF arrival
  schedule), per-cell prewarm + ramp + cooldown phases, `fcntl`-based
  run-lock (`benchmarks/07-max-output-tokens-reservation/.runlock`) with
  stale-PID reclaim, no-retry HTTP layer (`SDK max_retries=0`; 429s captured
  verbatim with `retry-after-ms` *and* `retry-after` headers), pricing
  freshness gate (90 days → exit 5), USD preflight gate (>0.9 × hard
  ceiling → exit 6), mid-run halt at 0.85 × hard ceiling (writes
  `*.partial.summary.json`, exits 0), prompt-identity SHA gate
  (`source_corpus_sha256`, `user_prompts_source_sha256`,
  `assembled_system_prompt_sha256` → exit 7), per-cell warm criterion (≥ 3
  of last 6 prewarm with `cached_tokens > 0`), per-cell backlog-excessive
  gate, and exit-code mapping 0/1/2/3/4/5/6/7 for OK / runtime / auth /
  dataset / runlock / pricing / USD-preflight / SHA-mismatch.
- `experiments/exp007_max_output_tokens_sweep.yaml` — pinned YAML with all
  v2.1 controls; `load_experiment` rejects mutations of
  `sdk.max_retries`, `reasoning.effort`, concurrency, `peak_ramp_tps`,
  `simulation`, `ptu_evidence`, `deployment` env-var, the sweep list, and
  any attempt to set `max_output_tokens` inside `request_template`.
- `tests/test_measure_max_output_tokens_sweep.py` — 67-test deterministic
  suite (1.1 s wall clock) covering: canonical TPM formula values
  (47 797.2 / 367 131.6) and upper/lower gate, prompt-cache-key namespace
  uniqueness per-cell × per-run with regex enforcement, SHA-seeded arrival
  schedule bit-stability, rolling 60-second RpmTracker eviction,
  PAYG-pricing projected-USD math, YAML pinned-control mutation rejection
  (12 cases), prompt-identity SHA contract (4 cases including bad-source /
  bad-assembled / bad-user-prompts SHA and a no-Task-019-corpus-file
  assertion), pricing freshness 89/91 day gate, USD preflight gate at 0.9 ×
  hard, run-lock acquire + release + stale-PID reclaim + cross-process
  subprocess holder → `RunLockHeldError`, warm-criterion 3-of-last-6 pass
  vs 2-of-last-6 fail, `visible_output_tokens == output − reasoning` (and
  saturates to 0 if reasoning > output), Stage 0 dry-run end-to-end with
  full v2.1 schema audit and `sdk_max_retries=0` echo invariant, and 429
  capture path returning verbatim `retry-after-ms=1234` and `retry-after=1`
  with `rate_limited=True` and *no* retry.
- `benchmarks/07-max-output-tokens-reservation/README.md` — PAYG-not-PTU
  framing, pinned-control table, source SHAs, reviewer reproduction
  one-liner (Stage 0 dry-run; zero spend), exit-code table, full
  per-record schema, citations block (Azure quota docs, Azure PTU concept,
  Azure prompt-caching docs, PAYG pricing snapshot URL), and limits &
  honesty section.
- `benchmarks/07-max-output-tokens-reservation/analysis.md` — template
  with TBD-placeholder tables for *visible_output vs cap*, *first-429
  arrival RPM vs cap*, *cache-hit ratio vs cap*; to be populated once
  Stage 2 produces a `summary.json`.

Reviewer reproduction (zero spend):

```bash
python -m scripts.measure_max_output_tokens_sweep \
  --experiment experiments/exp007_max_output_tokens_sweep.yaml \
  --dry-run --allow-dirty
```

Stage 1 smoke ≤ $4 and Stage 2 evidence ≤ $25 are gated on Azure auth
becoming available on the executor. Until then no `runs/*.jsonl` artefact
exists under `benchmarks/07-max-output-tokens-reservation/` and the
`analysis.md` headline tables remain TBD; `summary.json` will carry the
realised spend once Stage 2 lands.

### Added — Task 019 v2.1 protocol-correction: Stage 1 smoke did not confirm 429 contrast — Stage 2 evidence NOT promoted (2026-05-30) — DIAGNOSTIC ONLY under v2.2.1

Stage 1 smoke completed 7/7 cells (warm + backlog OK, total USD ≈ $1.71)
but observed **ZERO real 429s in every cell — including the largest
cell `max_output_tokens=16384`**. The spec requires the largest cell
to observe ≥1 real 429 and the smallest cell to observe zero 429s;
that contrast was not obtained, so the run is **GATE_VERDICT = FAIL,
reason = `no_429_in_largest_cell`** and Stage 2 evidence MUST NOT be
promoted under the current spec. An accidental Stage-2 evidence run
was launched before the gate verdict was confirmed and was halted by
the orchestrator after cells 256 and 512 (253 records, also 0 429s);
that JSONL is preserved as diagnostic-only with a sidecar manifest
that explicitly labels it `partial: true,
reason: "smoke_gate_failed_stage2_aborted"`. Hypothesis I is neither
confirmed nor refuted by these runs.

Files changed in the protocol-correction pass (no commit; orchestrator
review pending):

- `scripts/measure_max_output_tokens_sweep.py`
  - Refactored `_make_robust_token_provider` and `_build_live_client`
    to return the `DefaultAzureCredential` instance alongside the
    `AsyncOpenAI` client; wrapped the runner's main async loop in a
    `try/finally` that closes the client then the credential via a
    new `_aclose_quiet` helper. Eliminates the
    `asyncio Unclosed client session` warning observed at smoke
    completion.
  - Added a public function `evaluate_smoke_gate_block(*,
    cell_summaries, sweep_planned)` returning a deterministic verdict
    dict with `passed`, `reason ∈ {ok, no_cell_summaries,
    largest_cell_not_reached, no_429_in_largest_cell,
    unexpected_429_in_smallest_cell}`, `largest_cell_max_output_tokens`,
    `largest_cell_n_429`, `smallest_cell_max_output_tokens`,
    `smallest_cell_n_429`, `cells_completed`, `cells_planned`, and
    `stage2_promotable`.
  - Summary JSON now carries `n_429_records_per_cell` (always int,
    never None) plus a `smoke_gate` block when `stage=="smoke"` or
    `evidence_429_contrast_gate` block when `stage=="evidence"`.
  - Added a `GATE_VERDICT=...` INFO log line at run end.
- `scripts/analyze_max_output_tokens_sweep.py`
  - Full rewrite. Added `evaluate_smoke_gate(summary)` (uses the
    runner-written block when present; falls back to recomputing
    from `cell_summaries` for older summaries).
  - Markdown output now opens with a "#### 0. SMOKE GATE: **PASS**
    / **FAIL**" section that prints the reason, largest/smallest
    caps with their `n_429_records`, `stage2_promotable`, and an
    explicit "do NOT promote to Stage 2" call-to-action on FAIL.
  - Added an `n_429` column to the first-429-RPM table so counts
    never render as `None`.
  - Added `--require-gate-pass` CLI flag: exits 3 when gate FAILs.
- `tests/test_measure_max_output_tokens_sweep.py`
  - New `TestSmokeGateEvaluation` (PASS, no-429-in-largest FAIL,
    unexpected-429-in-smallest FAIL, largest-not-reached FAIL,
    empty-input FAIL, and a regression test that exercises
    `evaluate_smoke_gate_block` against the real Stage-1 smoke
    summary).
  - New `TestSummaryHasGateBlocks` (dry-run summary embeds the
    `n_429_records_per_cell` dict + the appropriate gate block).
- `tests/test_analyze_max_output_tokens_sweep.py` (NEW)
  - Covers `evaluate_smoke_gate` (uses runner-written block /
    recomputes when missing, smoke and evidence stage labels,
    PASS and FAIL paths) and `render_markdown` (FAIL emits "FAIL"
    + "do NOT promote" text; PASS emits "PASS" with no
    "do NOT promote"; `n_429` column present and never `None`;
    evidence stage uses "EVIDENCE 429-CONTRAST GATE" label) and
    a subprocess-level test that
    `--require-gate-pass` exits 3 against the real failing
    summary.
- `benchmarks/07-max-output-tokens-reservation/runs/20260529T165523Z_exp007_max_output_tokens_sweep_evidence.partial.summary.json` (NEW)
  - Hand-built manifest tagging the killed run as diagnostic-only
    (`partial: true`, `reason: "smoke_gate_failed_stage2_aborted"`,
    `do_not_use_for: ["Hypothesis I evaluation", ...]`).
- `benchmarks/07-max-output-tokens-reservation/README.md`
  - Added a "⚠ Current status — Stage 1 smoke FAILED gate; Stage 2
    NOT promoted" block right under the PAYG-not-PTU banner.
- `benchmarks/07-max-output-tokens-reservation/analysis.md`
  - Replaced the "pending Azure-auth" status template with the
    actual smoke-FAIL finding, the reproducible verdict one-liner,
    a candidate-causes list (peak_ramp_tps too low / quota higher
    than 60 K TPM / admission charges realized rather than reserved
    tokens), and a diagnostic-only partial-evidence table. Kept
    Hypothesis I framing strictly *not evaluated* on this data.

Tests: `pytest tests/test_measure_max_output_tokens_sweep.py
tests/test_analyze_max_output_tokens_sweep.py -q` → **83 passed in
~1.8 s** (67 pre-existing + 11 smoke-gate unit + 5 analyzer + the
real-summary regression). Lint: `ruff check` clean across all four
changed files.

**Blocker / next step:** a v2.x spec revision is required before any
further live run on the throttled deployment. Candidate changes:
raise `peak_ramp_tps` above 0.33 until at least the largest cell
429s; OR extend the sweep beyond 16 384; OR re-verify the
deployment's actual TPM quota (the canonical projection assumed
60 K TPM but the empirical absence of 429s suggests the cap may be
higher, or that the admission layer is charging realized output
rather than the cap — distinguishing those alternatives is itself
the v2.x experimental design question).

### Fixed — Task 018 v2.4 final review gate follow-up: admitted-timestamp authoritative rule now honored for post-admission failures (2026-05-29)

**Blocker.** `scripts/measure_cache_key_bucketing.py` was excluding every
`failed=True` record before computing `p95_dispatch_backlog_ms`,
`max_dispatch_backlog_ms`, `max_in_flight_observed`, and the realized
admitted RPM aggregates, and the `_run_cell` post-cell RPM rebuild loop
skipped every `failed=True` record with the comment "request was never
sent." That premise only holds for pre-admission failures
(`failure_reason="token_cap_exceeded"` — rejected before any HTTP call).
Post-admission failures (`transport_exception:<ExcName>` raised after the
HTTP call left the process, and `rate_limited_after_retries` returned after
the retry budget was exhausted) DID pass `sem.acquire` and DID consume
dispatcher capacity, so excluding them from admission-level aggregates
silently under-reports the realized cadence and contradicts the v2.4
admitted-timestamp authoritative rule documented in
the private Task 018 spec (lab-only workspace) v2.3 telemetry
semantics section ("All per-bucket and common-prefix RPM bookkeeping in
v2.3 uses `admitted_dispatch_cell_elapsed_ms` timestamps").

**Fix.** New module-level helper `_is_pre_admission_failure(record)` +
`PRE_ADMISSION_FAILURE_REASONS = frozenset({"token_cap_exceeded"})`.
`_aggregate_cell` now partitions records into two filters:

- `cache_target` (success-only, warmup-filtered) — drives
  `cache_hit_ratio_steady_state`, `first_token_latency_ms_p50/p95_steady_state`.
- `admitted` (everything except pre-admission failures, full cell — NOT
  warmup-filtered) — drives `p95_dispatch_backlog_ms`,
  `max_dispatch_backlog_ms`, `max_in_flight_observed`, and the
  `backlog_excessive` regression flag. These are operational
  dispatcher-saturation signals that must observe the ramp-up window
  too: if the dispatcher saturates during warmup, that is itself a
  regression and must not be hidden by warmup exclusion.
- `rpm_target` (the warmup-filtered subset of `admitted` — successes plus
  post-admission failures, with the first `warmup_exclusion_s` of each
  cell dropped) — drives `realized_admitted_per_bucket_rpm` and
  `realized_admitted_common_prefix_rpm`. These are steady-state cadence
  numbers and so exclude the warmup window for the same reason
  `cache_target` does.

Three new cell-summary fields surface the partition for downstream audit:
`n_pre_admission_failed_records`, `n_post_admission_failed_records`,
`n_admitted_records`. `n_failed_records` is unchanged (still the full
`failed=True` count). The `_run_cell` post-cell RpmTracker rebuild loop
now skips only pre-admission failures, so post-admission failures receive
the correct trailing-60s RPM count instead of the assembly-time
`per_bucket_running_rpm=0` placeholder. Pre-admission failures
(`token_cap_exceeded`) remain excluded from every admission-level
aggregate as required by the spec.

**Tests added (locks the behavior, including the post-admission failed
record case the reviewer requested).**

`tests/test_measure_cache_key_bucketing.py`:

- `TestPreAdmissionVsPostAdmissionFailedAggregates.test_post_admission_failure_included_in_admitted_aggregates`
  — synthetic cell where a `transport_exception:APIConnectionError` record
  carries `backlog=2500 ms`, `in_flight=7`; asserts these values dominate
  `max_dispatch_backlog_ms` / `max_in_flight_observed` and the failed
  record's `per_bucket_running_rpm` contributes to the realized-RPM mean;
  asserts cache-hit-ratio and P95 latency are computed over the 3
  successful records only.
- `TestPreAdmissionVsPostAdmissionFailedAggregates.test_pre_admission_failure_excluded_from_admitted_aggregates`
  — synthetic cell with a `token_cap_exceeded` record carrying
  deliberately large `backlog=9999 ms`, `in_flight=42`,
  `per_bucket_running_rpm=99` placeholder values; asserts ALL admission
  aggregates ignore them.
- `TestPreAdmissionVsPostAdmissionFailedAggregates.test_rate_limited_after_retries_treated_as_post_admission`
  — synthetic cell with a `rate_limited_after_retries` record carrying
  `backlog=3300 ms`, `in_flight=5`; asserts the values DO contribute to
  admission aggregates and the record is excluded from cache-hit-ratio.
- `TestPreAdmissionVsPostAdmissionFailedAggregates.test_is_pre_admission_failure_helper_classification`
  — direct classifier test covering success records,
  `token_cap_exceeded`, `transport_exception:*`,
  `rate_limited_after_retries`, and `failed=True` with `failure_reason`
  None/missing.
- `TestRunCellPostAdmissionFailureRpmBookkeeping.test_post_admission_failures_count_in_admitted_rpm`
  — integration test: stubbed `_call_with_retry` returns
  `raised=RuntimeError(...)` for arrivals 2/6/10 of 20; asserts each
  failed record receives a non-zero `per_bucket_running_rpm` /
  `common_prefix_running_rpm` from the v2.4 RpmTracker rebuild;
  asserts `_aggregate_cell` reports `n_admitted_records=20`,
  `n_post_admission_failed_records=3`,
  `n_pre_admission_failed_records=0`; asserts the realized
  common-prefix RPM is the mean over all 20 admitted records and differs
  from the success-only mean (the regression signature the missing
  v2.4 fix produces).

Negative-regression check (out-of-band, not committed): reverting both
script changes and re-running the new tests fails on 3 of the new tests,
confirming each lock-test catches the original blocker.

**Live artifacts.** No raw JSONL or run summary regenerated. The
deviation between live-recorded summaries and a re-aggregation under
the v2.4 fix is bounded by `1 / calls_per_cell ≈ 0.2 %` per cell with a
single post-admission failure (the two cells in the working tree that
recorded such a failure each had it carry `dispatch_backlog_ms=0` and
`in_flight_at_dispatch` below the per-cell max, so backlog / max-in-flight
fields are unchanged; only the realized-RPM mean shifts marginally).
Operators re-running Stage 2 evidence under the fix will see admission
aggregates that correctly include any post-admission failures.

**Verification.** `.venv/bin/python -m pytest
tests/test_measure_cache_key_bucketing.py -q` → **65 passed, 5 subtests
passed in 52.73 s** (was 60 + 5 before the fix; +5 new tests).

**No commit, no push, no PR.** This entry is the change record for the
working tree only.

### Added — Task 018 v2.4 hotfix: raise dispatcher semaphore from 8 → 96 to absorb live gpt-5.2 P95 TTFT ≈128 s (2026-05-29)

**Goal.** v2.3's pinned `runtime.concurrency = 8` was sized against an
assumed ~9 s P95 TTFT. The live `gpt-5.2` PAYG deployment delivered
~128 s P95 TTFT in v2.3 Stage 1 smoke (~14× the assumption). At
`TPS = 0.5`, Little's Law gives `in-flight = TPS × TTFT ≈ 64` — already
8× v2.3's sem ceiling. The dispatcher semaphore saturated immediately
and the per-cell `backlog_excessive` regression detector correctly
fired on both YAMLs (inmemory card=1 P95 backlog = 2,398 ms with
`realized_admitted_per_bucket_rpm_card1 = 22.87`; 24h card=1 P95
backlog = 111,238 ms with `realized_admitted_per_bucket_rpm_card1 =
13.23`; both `max_in_flight_observed_card1 = 8` = sem ceiling).
Per the v2.3 spec, Stage 2 evidence was not promoted. The **v2.4
hotfix** is the single permitted remediation: raise sem to 96 (~50 %
headroom over the 64-in-flight Little's-Law steady state), preserve
every other v2.3 pin verbatim, re-run from Stage 0.

**Deliverables landed (working tree, no commit).**

- `scripts/measure_cache_key_bucketing.py` —
  `CONCURRENCY_PINNED = 8 → 96`; module docstring, `CONCURRENCY_PINNED`
  docstring, `load_experiment` reject message, and `argparse` CLI help
  string updated to record the v2.4 rationale (Little's-Law sizing
  against live P95 TTFT ≈128 s + ~50 % headroom). New first-class
  top-level summary field `max_in_flight_observed_run` (run-wide max
  of the per-cell `max_in_flight_observed`) so semaphore-saturation
  regressions of the v2.3 kind are visible without inspecting per-cell
  rows. All v2.3 telemetry preserved verbatim
  (`scheduled_dispatch_cell_elapsed_ms`,
  `admitted_dispatch_cell_elapsed_ms`, `dispatch_backlog_ms`,
  `in_flight_at_dispatch`, per-record echoes, `tpm_feasibility`,
  `backlog_excessive_any`, smoke `*_card1` hoists including
  `max_in_flight_observed_card1`).
- `experiments/exp006_cache_key_bucketing_inmemory.yaml` and
  `experiments/exp006_cache_key_bucketing_24h.yaml` —
  `runtime.concurrency: 8 → 96`; YAML header comments updated to
  record the v2.4 hotfix rationale and reaffirm that TPM math is
  unchanged (`60 × 0.5 × 11000 = 330000 ≤ 0.70 × 500000 = 350000`).
  Every other v2.3 pin is byte-identical (sustain_tps=0.5,
  estimated_processed_tokens_max=11000, deployment_tpm_quota=500000,
  dispatcher=async_scheduled, api_version=preview,
  max_output_tokens=512, reasoning.effort=low, default sweep [1, 8],
  Stage 1 smoke ceiling \$8/YAML, Stage 2 evidence ceiling \$60/YAML,
  PAYG metadata, namespacing, anonymization).
- `tests/test_measure_cache_key_bucketing.py` —
  `TestAsyncCadenceHappyPath` updated for sem=96 (still scaled to
  TPS=10 / TTFT=0.18 s for fast test wall time; max_in_flight bound
  raised to 96). New `TestHeavyStubHappyPathSem96`: deterministic
  heavy stub reproducing the live regime (TTFT=128 s, TPS=0.5, N=120,
  256×-scaled to fit `--timeout=120`) — asserts P95 backlog <1500 ms,
  max backlog <5000 ms, `backlog_excessive=false`, common-prefix RPM
  (rescaled to live time) ∈ [28, 32], steady in-flight ∈ [40, 96]
  with 40 chosen to absorb ramp-up boundary, and
  `max_in_flight_observed < 96` so the semaphore is observably
  non-binding. New `TestCounterfactualSem8HeavyStub`: same heavy stub
  with sem=8 reproduces the v2.3 saturation signature
  (`max_in_flight_observed == 8`, `backlog_excessive=True`, P95
  backlog >1500 ms, realized common-prefix RPM well below 30) — locks
  in the regression test for the bug v2.4 fixes. New
  `test_rejects_v23_concurrency_8` validates that `load_experiment`
  rejects a YAML mutation that sets `concurrency=8`. Existing
  hard-coded `concurrency=8` / `max_in_flight==8` assertions in
  `TestAsyncCadenceHappyPath`, `TestConcurrencyDispatcherEcho`, and
  `TestDryRunEndToEnd` updated to 96. `TestDryRunEndToEnd` adds an
  assertion for the new top-level `max_in_flight_observed_run` field.
- `benchmarks/06-cache-key-bucketing/README.md` — v2.4 status banner,
  v2.4 "one knob" change table, retained v2.1→v2.3→v2.4 historical
  table, v2.4 pinned-controls table (concurrency cell flagged as v2.4
  value), v2.3 Stage 1 outcome section relabeled "superseded by v2.4 —
  historical record" with the failure-number table preserved, and a
  new "v2.4 Stage 1 outcome" placeholder section that defers to
  `analysis.md` for the authoritative numbers. File inventory now
  lists `runs/_v2.3_diagnostic/` alongside `runs/_v2.1_diagnostic/`
  with a DO-NOT-CITE caveat.
- `benchmarks/06-cache-key-bucketing/analysis.md` — rewritten for v2.4.
  Cites only v2.4 artifacts; preserves v2.3 failure numbers in a
  clearly-labelled "what v2.4 fixes" section sourced from
  `runs/_v2.3_diagnostic/`; explicit TODO placeholders for the v2.4
  Stage 1 and Stage 2 evidence sections to be filled in at run time
  (or replaced with a "Stage 1 did not promote — Stage 2 not run"
  block if the v2.4 Stage 1 gates do not clear).
- `benchmarks/06-cache-key-bucketing/runs/_v2.3_diagnostic/` —
  new quarantine directory holding the 8 v2.3 dry-run + Stage 1 smoke
  files (4 JSONL + 4 `.summary.json`) moved verbatim from the top
  level of `runs/`. New `README.md` records the exact failure numbers
  (`realized_admitted_per_bucket_rpm_card1`, `p95`/`max
  dispatch_backlog_ms`, `max_in_flight_observed_card1`,
  `backlog_excessive_card1`) per YAML, the v2.3 → v2.4 design diff
  (one knob), and a DO-NOT-CITE disclaimer. Existing
  `runs/_v2.1_diagnostic/` is preserved untouched.

**Stage 1 gates (v2.4).** All three must hold on both YAMLs for
Stage 2 evidence to run:

1. `realized_admitted_per_bucket_rpm_card1 ≥ 15`
2. `backlog_excessive_card1 == false`
3. `max_in_flight_observed_card1 < 96`

**Budget gates preserved verbatim from v2.3.** Stage 1 smoke
\$8/YAML (\$16 combined). Stage 2 evidence \$60/YAML (\$120
combined). TPM feasibility preflight: `60 × sustain_tps ×
estimated_processed_tokens_max ≤ 0.70 × deployment_tpm_quota`.

**No commit, no push, no PR.** This entry is the change record for
the working tree only; the operator owns the commit/push decision.

### Added — Task 018 v2.3: Benchmark 06 `prompt_cache_key` bucketing (async_scheduled dispatcher; Stage 0 + Stage 1; Stage 2 honestly gated) (2026-05-29)

**Goal.** Upgrade Task 018 from the v2.1 serial dispatcher to the
v2.3 `async_scheduled` dispatcher (concurrency=8, sustain_tps=0.5,
`request_template.estimated_processed_tokens_max=11000`,
`metadata.deployment_tpm_quota=500000`, default sweep `[1, 8]`,
NEW Task-018-specific ~10 K-token system-prompt corpus). The v2.1
serial implementation collapsed the realized arrival cadence to
~7 RPM under per-call TTFTs of 8–13 s, making the docs-stated
~15 req/min per-bucket overflow threshold operationally unreachable
and a 30 K-token corpus at 1.0 TPS over-budget against the
deployment's 500 K TPM quota. v2.3 decouples planned admission rate
from per-call TTFT and adds a TPM feasibility preflight gate so
misconfigured YAMLs never open a live HTTP client.

**Deliverables landed (working tree, no commit).**

- `scripts/measure_cache_key_bucketing.py` — rewritten dispatcher.
  New helper `compute_projected_tpm(sustain_tps,
  estimated_processed_tokens_max)` returns the v2.3 TPM-feasibility
  numerator. New error classes `TpmFeasibilityAbortError` and
  `TokenCapAbortError`. The `_run_cell` coroutine implements the
  `async_scheduled` dispatcher (wall-clock pacer + `asyncio.Semaphore`)
  and per-record captures `scheduled_dispatch_cell_elapsed_ms`
  (pre-acquire), `admitted_dispatch_cell_elapsed_ms` (post-acquire,
  pre-HTTP-send), `dispatch_backlog_ms`, `in_flight_at_dispatch`
  (snapshot pre-increment), `request_concurrency`,
  `request_sustain_tps`, `request_estimated_processed_tokens`,
  `dispatcher_kind="async_scheduled"`. Per-record token cap
  rejection (`failed=true, failure_reason="token_cap_exceeded"`,
  zero HTTP sends). Per-bucket and common-prefix RPM are rebuilt
  post-cell from `admitted_dispatch_cell_elapsed_ms` (admitted
  order, not arrival order); JSONL is sorted by admitted timestamp.
  `_aggregate_cell` exposes `realized_admitted_per_bucket_rpm`,
  `realized_admitted_common_prefix_rpm`,
  `p95_dispatch_backlog_ms`, `max_dispatch_backlog_ms`,
  `backlog_excessive` (p95 > 1500 ms OR max > 5000 ms),
  `max_in_flight_observed`, `n_failed_records`. Failed records are
  excluded from latency and cache aggregates. The run-level summary
  adds `pinned_confounds_echo` (v2.3 control set),
  `tpm_feasibility` (`{projected_tpm, deployment_tpm_quota,
  headroom_fraction, ceiling, passed}`), `backlog_excessive_any`,
  and (smoke runs only) hoisted first-class
  `realized_admitted_per_bucket_rpm_card1`,
  `realized_admitted_common_prefix_rpm_card1`,
  `p95_dispatch_backlog_ms_card1`,
  `max_dispatch_backlog_ms_card1`,
  `max_in_flight_observed_card1`, `backlog_excessive_card1`. The
  Citations block adds `azure_rate_limit_doc`
  (<https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/quota>,
  accessed 2026-05-29). `load_experiment` rejects v2.1/v2.2 mutations
  (concurrency≠8, sustain_tps≠0.5, missing `dispatcher`,
  `dispatcher≠async_scheduled`, missing or wrong
  `estimated_processed_tokens_max`, missing or wrong
  `deployment_tpm_quota`). All v2.1→v2.3 string references updated.
- `experiments/exp006_cache_key_bucketing_inmemory.yaml` and
  `experiments/exp006_cache_key_bucketing_24h.yaml` — v2.3 pins
  (`runtime.dispatcher: async_scheduled`, `runtime.concurrency: 8`,
  `runtime.sustain_tps: 0.5`, `runtime.cell_duration_seconds: 960`,
  `request_template.estimated_processed_tokens_max: 11000`,
  `target_system_prompt_tokens: 9500`, `sweep.bucket_cardinality:
  [1, 8]`, `metadata.deployment_tpm_quota: 500000`). Both YAMLs are
  byte-identical in every v2.3 pinned control; only
  `prompt_cache_retention` and `washout_seconds` differ.
- `benchmarks/06-cache-key-bucketing/system_prompt_corpus.json` —
  **NEW** Task-018-specific ~10 K-token corpus (50 paragraphs on
  corporate-treasury operations, ~21 K characters, SHA-256
  `c169e4d5eb8abff5e1d85289b9f50cd41edd49ad470d5120b19f206bc79762af`).
  Replaces the 30 K Task 012 copy whose TPM projection
  (`60 × 1.0 × 30 512 ≈ 1.83 M tokens/min`) exceeded the 500 K TPM
  quota by 3.7×. Effective `system_prompt` post-`_build_system_prompt`
  is ~9 549 tokens — well under the 11 000-token per-request cap.
- `tests/test_measure_cache_key_bucketing.py` — full rewrite for
  v2.3. **57 tests + 5 subtests pass** under `.venv/bin/python`
  (3.12.4) in ~42 s. New test classes:
  `TestComputeProjectedTpm` (v2.3 pass math, v2.2 fail math,
  rejection of non-positive inputs); `TestTpmFeasibilityPreflight`
  (v2.3 pins pass; v2.2 pins abort BEFORE live-client construction;
  spy on `_build_live_client` proves it was never called);
  `TestMidRunHaltAfterCell` (synthetic stubbed `_run_cell` returns
  oversized USD; gate trips after cell 0; partial summary written;
  cell 1 not executed); `TestTokenCapEnforcement` (oversized prompt
  → all records `failed=true, failure_reason="token_cap_exceeded"`,
  `_call_with_retry` invocation count is zero; undersized prompt
  dispatches normally); `TestAsyncCadenceHappyPath` (sem=8,
  TPS=10.0, TTFT stub 180 ms → admitted tracks scheduled, p95
  backlog < 500 ms, `backlog_excessive=false`);
  `TestSaturatedSemaphoreBacklogRegression` (sem=1, TTFT stub 1.5 s
  → backlog grows monotonically, `backlog_excessive=true`);
  `TestConcurrencyDispatcherEcho` (every record echoes
  `request_concurrency=8`, `request_sustain_tps=0.5`,
  `dispatcher_kind="async_scheduled"`); `TestStartupAbort` (5
  subtest mutations of the YAML each abort at `load_experiment`
  before any HTTP client is built). Existing
  `TestDryRunEndToEnd` rewritten for v2.3 outputs (2 cells, dual
  citations, `tpm_feasibility` block, `backlog_excessive_any`, smoke
  `*_card1` hoists).
- `benchmarks/06-cache-key-bucketing/README.md` — rewritten for
  v2.3 (pinned controls table, dispatcher contract, dual citations,
  `tpm_feasibility` math worked example, Stage 1 outcome summary,
  corpus divergence note + rationale, v2.1 diagnostic quarantine
  note, 5-step anonymization audit).
- `benchmarks/06-cache-key-bucketing/analysis.md` — rewritten to
  cite **only v2.3 artifacts**. Stage 0 + Stage 1 results
  documented; Stage 2 honestly gated.

**Stage 0 dry-run (no network).** Both YAMLs ran end-to-end
(2 cells × 480 records each). All preflight gates passed:
`tpm_feasibility.projected_tpm = 330 000`,
`tpm_feasibility.ceiling = 350 000`, `passed = true`;
`projected_usd = $10.47`, `preflight_threshold = $54.00`.
Artifacts:

- `runs/20260529T103513Z_exp006_cache_key_bucketing_inmemory_dry-run.jsonl[.summary.json]`
- `runs/20260529T103519Z_exp006_cache_key_bucketing_24h_dry-run.jsonl[.summary.json]`

**Stage 1 smoke (live Azure).** Both YAMLs executed cleanly
(zero `failed=true`, no mid-run halts) but **did not clear the
v2.3 backlog gate** under current Azure-deployment TTFT
conditions:

| YAML | `realized_admitted_per_bucket_rpm_card1` | RPM ≥ 15 | `p95_dispatch_backlog_ms_card1` | `backlog_excessive_card1` | cell-0 cache-hit | spend |
|---|---:|:---:|---:|:---:|---:|---:|
| `inmemory` | 22.87 | ✅ | 2 398 | ❌ excessive | 0.781 | $0.83 |
| `24h`      | 13.23 | ❌ | 111 238 | ❌ excessive | 0.765 | $0.85 |

Both runs observed `max_in_flight_observed_card1 = 8` (semaphore
saturated). Per-call TTFTs distributed across roughly 1 s to >100 s
with intermittent 408/500 retries from the deployment.

Artifacts:

- `runs/20260529T103610Z_exp006_cache_key_bucketing_inmemory_smoke.jsonl[.summary.json]`
- `runs/20260529T104555Z_exp006_cache_key_bucketing_24h_smoke.jsonl[.summary.json]`

**Stage 2 evidence.** **NOT RUN.** Per the v2.3 spec ("Stage 2
gating"), Stage 2 evidence requires Stage 1 to pass **both** the
per-bucket-RPM and `backlog_excessive=false` gates for **both**
YAMLs. Neither YAML cleared both gates this session. The
operational implication (the docs-stated 15 RPM per-bucket
threshold may be unreachable against this deployment under
conservative pinned controls; off-peak re-run is the operator's
next decision) is captured in `benchmarks/06-cache-key-bucketing/analysis.md`.

**Quarantined v2.1 diagnostic artifacts.** Pre-existing v2.1
JSONL/summary/chart artifacts are segregated under
`benchmarks/06-cache-key-bucketing/runs/_v2.1_diagnostic/` and
`results/cache-key-bucketing/_v2.1_diagnostic/` with `README.md`
DO-NOT-CITE disclaimers. No v2.2 artifacts existed in the working
tree; `_v2.2_diagnostic/` directories were not created (cleaner
omission). The v2.3 README and analysis cite only v2.3 artifacts.

**Verification.**

- `python -m pytest tests/test_measure_cache_key_bucketing.py -v`
  → **57 passed, 5 subtests passed in 41.56 s**
- `git diff --check` → clean (no whitespace errors)
- Anonymization audit: 5 greps (Bearer/sk- tokens, Azure
  hostnames, literal `AZURE_OPENAI_*=` values, auth header
  names) plus `prompt_cache_key_used` regex check → zero matches

**Carry-forward.**

- Off-peak Stage 1 re-run is the most direct way to determine
  whether the deployment can sustain the planned cadence without
  backlog. If TTFT remains in the tens of seconds, the v2.1
  operational conclusion (threshold unreachable on this deployment
  under conservative pins) is reinforced rather than refuted.
- Task 022 PTU roll-up must continue to inherit the PAYG-not-PTU
  caveat.

### Added — Task 018: Benchmark 06 `prompt_cache_key` bucketing (Stage 0 + Stage 1; Stage 2 honestly deferred) (2026-05-29)

**Goal.** Implement Task 018 v2.1 — a controlled cardinality sweep
of Azure OpenAI's `prompt_cache_key` parameter against a **single
unthrottled gpt-5.2 PAYG (Global Standard) deployment** (not PTU,
not simulation, not the dual-endpoint Task 013/015 rig) to test the
Microsoft Learn docs-stated ~15 req/min per-bucket overflow
threshold for prompt caching. Sweep `[1, 2, 4, 8, 16]` distinct
`prompt_cache_key` values per cell with all other knobs frozen.

**Deliverables landed (working tree, no commit).**
- `scripts/measure_cache_key_bucketing.py` — main measurement
  script (~1300 lines). Exports pure helpers
  `select_bucket(arrival_idx, cardinality, namespace)`,
  `build_namespace(retention_tag, cardinality, run_id_short)`,
  `RpmTracker(window_s=60.0)` with `record(t)` / `count(t)`, plus
  `compute_projected_usd(...)`, `load_experiment(path)` (rejects
  any deployment env-var name containing "THROTTLED"),
  `run_measurement(...)`, and `main(argv)`. Per-bucket and
  common-prefix RPM bookkeeping; preflight gate aborts when
  `projected_usd > 0.9 × hard_ceiling`; mid-run gate halts cleanly
  with a partial summary when `cumulative_usd > 0.85 × hard_ceiling`.
  Public errors: `PreflightBudgetAbortError`,
  `BudgetHaltError`, `EndpointMisconfiguredError`,
  `CorpusMissingError`, `PreflightReachabilityError`,
  `PricingStaleError`. Dry-run artifacts get the `_dry-run` stage
  label in their filename (distinct from Stage 1 `_smoke` and
  Stage 2 `_evidence`).
- `experiments/exp006_cache_key_bucketing_inmemory.yaml` — pinned
  controls: `max_output_tokens=512`, `reasoning.effort=low`,
  `client.api_version="preview"` (copied from Task 013
  `measure_dual_spillover.py`), `runtime.concurrency=1`,
  `runtime.sustain_tps=1.0`,
  `metadata.tier=paygo_standard`,
  `metadata.deployment_type=live_azure_single_deployment`,
  `metadata.sku=GlobalStandard_PAYG`,
  `metadata.simulation=false`,
  `metadata.ptu_evidence=false`, `prompt_cache_retention=in_memory`,
  `washout_seconds=120`.
- `experiments/exp006_cache_key_bucketing_24h.yaml` — sibling of
  inmemory; only differences are `prompt_cache_retention="24h"`
  and `washout_seconds=0`.
- `benchmarks/06-cache-key-bucketing/system_prompt_corpus.json`
  and `benchmarks/06-cache-key-bucketing/user_prompts.json` —
  copied **byte-identical** from `benchmarks/04-spillover-simulation/`.
  SHA-256 verified at copy time:
  - `system_prompt_corpus.json`:
    `6a8ab5a3cb1ad3dace030a82ec1327496b39e65b77a627714a27c39017ca19e3`
  - `user_prompts.json`:
    `45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087`
- `benchmarks/06-cache-key-bucketing/runs/.gitkeep` plus the
  four staged-protocol JSONL + summary JSON pairs produced this
  session (two dry-runs, two smokes; details below).
- `benchmarks/06-cache-key-bucketing/README.md` — PAYG-not-PTU
  framing, pinned-controls table, corpus SHA-256 manifest,
  staged reproduction recipe (Stage 0 / Stage 1 / Stage 2),
  anonymization audit pointer to spec L184-188, Citations block.
- `benchmarks/06-cache-key-bucketing/analysis.md` — opens with
  the PAYG / not-PTU / not-simulation declaration; documents the
  **central methodological finding**: under the v2.1-pinned
  controls (concurrency=1, sustain_tps=1.0, max_output_tokens=512,
  reasoning.effort=low) the measured per-call latency is
  ~9 s p50 / ~13 s p95, which collapses the achieved common-prefix
  cadence to ~7 req/min — well below both the 60 RPM target and the
  docs-stated 15 RPM per-bucket overflow threshold. Includes
  per-cell smoke tables, the under-threshold-monotonic-decrease
  shape (opposite of the above-threshold prediction; consistent
  with "wider spread = more cold pools" at sub-threshold cadence),
  Stage 2 deferral rationale, Citations block.
- `results/cache-key-bucketing/.gitkeep` plus the two charts
  `cache_hit_ratio_vs_cardinality.png` and
  `ttft_p95_vs_cardinality.png` — both annotated **"Stage 1 smoke
  only"** with the achieved common-prefix RPM (~7) overlaid and
  the 15 RPM threshold line shown as unreached.
- `tests/test_measure_cache_key_bucketing.py` — 36 tests across
  6 classes (TestSelectBucket, TestBuildNamespace, TestRpmTracker,
  TestComputeProjectedUsd, TestLoadExperiment, TestPreflightGate,
  TestDryRunEndToEnd). Pure helpers + Stage 0 dry-run
  end-to-end; no network. Includes a Stage 0 namespace-uniqueness
  check across all five cells and a BUCKET_KEY_RE match on every
  generated record. **36 passed in 0.50 s.**

**Staged protocol — what actually executed this session.**
- **Stage 0 dry-run** — passed for both YAMLs. Zero network.
  Preflight gate math validated against
  `pricing/azure-openai-payg-2026-05.yaml`. Artifacts:
  `20260529T092805Z_exp006_cache_key_bucketing_inmemory_dry-run.{jsonl,summary.json}`,
  `20260529T092806Z_exp006_cache_key_bucketing_24h_dry-run.{jsonl,summary.json}`.
- **Stage 1 smoke** — passed for both YAMLs (live Azure /
  Entra auth). Two cells each (card=1, card=8) × 60 calls.
  Steady-state cache-hit and TTFT:
  - `in_memory` smoke (~16 min wall): card=1 cache_hit 95.93%,
    card=8 cache_hit 89.31%; TTFT p50 ~9 s / p95 ~13 s;
    spend $1.27 against $8 ceiling.
  - `24h` smoke (~18 min wall): card=1 cache_hit 94.27%,
    card=8 cache_hit 79.39%; TTFT p50 ~9 s / p95 ~13 s;
    spend $1.50 against $8 ceiling.
  - **Combined Stage 0 + Stage 1 spend: $2.76** against the $16
    combined smoke ceiling. Zero 429s observed; achieved
    common-prefix cadence ≈ 7 req/min (target 60).
- **Stage 2 evidence — intentionally NOT run this session.**
  Reason is operational, not budgetary: at the pinned controls
  the measured TTFT inflates the projected wall time to ~6 h per
  YAML (~12 h combined) for the full 5-cell × 480-call sweep, which
  is beyond an unattended session window. Preflight passes
  (`projected_usd=$46.41` against `hard_ceiling=$60.00`,
  `preflight_threshold=$54.00`) and the script is ready to run.
  `analysis.md` documents this honestly and identifies the
  latency-vs-arrival-rate confound as the central blocker:
  **the docs-stated ~15 RPM per-bucket overflow threshold cannot
  be reached at any cardinality under the v2.1-pinned controls
  without raising `concurrency`**, which the spec explicitly forbids.

**Anonymization audit.** All four greps from spec lines 184-187
(sk-/Bearer tokens, endpoint hostnames, literal `AZURE_OPENAI_*=`
exports, auth-header values) return zero matches against the Task
018 file set. The JSONL schema check (spec line 188) — every
`prompt_cache_key_used` must match
`^benchmark06_(inmemory|24h)_card\d{2}_[a-f0-9]{4,8}_bucket_\d{3}$`
— reports **0 / 5040 bad keys** across all four committed JSONL files
(2 dry-run × 5 cells × 480 records + 2 smoke × 2 cells × 60 records).

**No commit / push / PR.** Per the orchestration contract, this
implementation lands the working-tree changes only; the
commit worker owns the commit, push, and PR steps and runs
after methodology audit gate and final review gate have re-audited.

**Citations.** Microsoft Learn — Prompt caching for Azure OpenAI
in Azure AI Foundry —
`https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching`,
accessed 2026-05-29 (required claim: per-bucket overflow at
≥15 req/min). Pricing snapshot:
`pricing/azure-openai-payg-2026-05.yaml`, source
`https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/`,
accessed 2026-05-19. Both citations appear verbatim in the
README, analysis.md, and every summary JSON.

### Added — Task 016: Customer-scenario synthesis docs (`docs/07-cache-hit-degradation.md`, `docs/08-customer-simulation-findings.md`) (2026-05-29)

**Goal.** Land the two decision-grade synthesis docs called for by
Task 016: `docs/07-cache-hit-degradation.md` (the nine-hypothesis
body, A–I, with per-architecture diagnostic flowchart and per-
hypothesis "how to test" recipes) and
`docs/08-customer-simulation-findings.md` (the narrative payoff doc
for the PTU + single-call ReAct cache / spend pattern, framed as
five leverages with PAYG and PTU lens translations). No new
measurement; pure synthesis over Tasks 011 – 015 outputs already
committed to the repo, plus framing for Task 019's pending
controlled `max_output_tokens` sweep (Hypothesis I) without
fabricating measured results.

**Changed files (working repo).**
- `docs/07-cache-hit-degradation.md` — new file. Twelve sections:
  observation framing → nine hypotheses (A system-prompt
  restructuring, B reasoning-model cache differences, C cache TTL,
  D request pattern change, E tool-definition prefix variance,
  F PTU routing / spillover specifics, G reactive spillover →
  weak form measured, H ReAct planning variance → diagnostic recipe
  only at write time, **I `max_output_tokens` as PTU admission-time
  reservation / concurrency lens — mechanism named, Task 019 owns
  the pending controlled sweep, not yet measured in this repo**) →
  per-architecture diagnostic flowchart (multi-node vs single-call
  PAYG vs single-call PTU+spillover, with the `max_output_tokens`-
  inflation cue routing to I) → in-repo run summary → cross-
  references. Every Hypothesis section carries `Architecture
  applicability:` and `Status:` lines plus a "How to test this
  hypothesis" subsection; G additionally carries a "What this repo
  measured" subsection summarizing Phase 1
  (`benchmarks/04-spillover-simulation/analysis.md`) and Phase 2
  (`benchmarks/05-dual-spillover/runs/*.summary.json` + this
  CHANGELOG's Task 015 entry) headline numbers. Hypothesis C also
  carries an inline **retention truth-table** for
  `prompt_cache_retention` (`in_memory` default vs `24h` extended-
  retention opt-in, with deployment-config verification guidance —
  marked as config check to confirm, not directly measured).
- `docs/08-customer-simulation-findings.md` — new file. Nine
  sections: who-this-is-for → pattern-in-three-sentences → what
  this repo measured (and did not, including I as Task 019's pending
  controlled measurement) → findings summary table (Phase 1,
  Phase 2, H re-analysis pending) → five leverages (L1 first-token
  timeout, L2 proactive vs reactive policy choice, L3 system prompt
  stability, L4 reasoning effort tuning, **L5 `max_output_tokens`
  tightening on PTU — operational rule derived from Hypothesis I
  mechanism plus methodology §2 invariants, direct measurement
  pending under Task 019, with a four-step tightening recipe and
  the PAYG-no-change / PTU-concurrency-gain translation**) → what
  you cannot fix with this evidence → quick diagnostic recipe (now
  five steps + four operational subsections: §7.1 retention truth-
  table pointer, §7.2 `prompt_cache_key` + ~15 req/min overflow
  bucketing taxonomy, §7.3 `retry-after-ms` recovery semantics, §7.4
  native-vs-custom spillover decision guide) → caveats → where to
  go next. Every leverage cites (a) the mechanism Hypothesis letter
  from doc-07, (b) the in-repo evidence source, and (c) the PAYG +
  PTU two-lens translation per Task 011 Principle 6.
- `README.md` — anchored append in the existing "What's Here" TOC
  for `docs/08-customer-simulation-findings.md` only. The existing
  `docs/07-cache-hit-degradation.md` TOC line was already present.
  No restructure. (The "Which Customer Are You?" priority listing
  in `README.md` already enumerates the nine hypotheses including I.)
- `CHANGELOG.md` — this entry.

**Anonymization invariant.** The task anonymization grep (defined in
the Task 016 spec under the lab-only workspace; covers customer organization
names, PTU sizing literals, deployment-specific TPS literals, the
literal throttled-deployment name, and the byte-exact system-prompt
size literal) returns zero matches against the new docs at land time.
No customer name, no PTU sizing, no deployment-specific TPS, and no
literal throttled-deployment name appears in `docs/07` or `docs/08`.
Where the Phase 1 / Phase 2 design needs the workload-size context,
the docs write "long stable system prompt" / "large-prefix workload"
or "the throttled primary deployment" rather than the literal patterns
the spec interdicts. As an anonymization-only redaction to satisfy
the Task 016 grep across all of `docs/`, the prior PTU sizing literal
in [`docs/04-decision-framework.md`](docs/04-decision-framework.md)
Example B is rephrased to "a PTU allocation sized to their workload";
the recommendation, math, and citations are unchanged.

**Claim-to-source mapping.** The implementer kept a per-claim
traceability map listing every substantive claim in `docs/07` and
`docs/08` against the originating `docs/05-methodology.md` section,
`pricing/azure-openai-payg-2026-05.yaml` field, or
`benchmarks/*/analysis.md` / `README.md` / `runs/*.summary.json`
source. The map calls out three evidence gaps explicitly: the
`benchmarks/05-dual-spillover/analysis.md` follow-up is not yet in
the repo (Phase 2 numbers are quoted from `.summary.json` +
the Task 015 CHANGELOG entry); the
`benchmarks/{01,02}-*/HYPOTHESIS_H_REANALYSIS.md` artifacts have not
landed (Hypothesis H is treated as diagnostic recipe only, no
magnitude); and `docs/06-ptu-vs-paygo.md` does not exist (so the
spec-conditional doc-06 cross-reference is skipped per the task
spec's "if the file is not yet written, skip" instruction).

**Out of scope (intentionally not touched).**
`docs/05-methodology.md` (frozen), benchmark `analysis.md` files
(immutable post-APPROVE), `scripts/*.py`, `pricing/*.yaml`,
`experiments/*.yaml`, the lab-only workspace task specs. `results/summary.md`
"Customer-scenario card" is the deferred follow-up the spec puts
out-of-scope for Task 016.

**Verification.** Anonymization grep against `docs/` returns zero
matches; `git diff --check` clean on the working-tree changes; the
in-doc cross-references resolve against extant repo paths
(`benchmarks/04-spillover-simulation/analysis.md`,
`benchmarks/05-dual-spillover/README.md`,
`benchmarks/05-dual-spillover/runs/*.summary.json`,
`benchmarks/01-short-factual/analysis.md`,
`benchmarks/02-multi-step-reasoning/analysis.md`,
`benchmarks/03-tool-using-agent/analysis.md`,
`docs/04-decision-framework.md`,
`docs/05-methodology.md`,
`pricing/azure-openai-payg-2026-05.yaml`,
`results/summary.md`). The deliberately-missing
`docs/06-ptu-vs-paygo.md` link in the README TOC was left untouched
(spec forbids restructuring the README), and `docs/07` / `docs/08`
do not depend on doc-06 content.

<!-- task015-phase2-live: reactive=20260528T135034Z_exp005_dual_spillover_reactive_reactive.jsonl proactive=20260528T183310Z_exp005_dual_spillover_proactive_proactive.jsonl commit=9a266efec53ca9e1e86c8f9a1b45808808d656d8 -->

### Added — Task 015: Phase 2 dual-endpoint spillover live measurement (2026-05-29)

**Goal.** Produce the Phase 2 reactive and proactive dual-endpoint spillover measurement JSONL + summary + recovery-curve charts called for by Task 013 / Task 015, on commit `9a266efec53ca9e1e86c8f9a1b45808808d656d8` from a clean detached worktree. Both policies were dispatched sequentially with a between-policy cooldown; the working tree was reset to the committed state between policies so each policy's `git_commit` field describes the code path that actually executed.

**Measurement scope.**

  - **reactive** — `20260528T135034Z_exp005_dual_spillover_reactive_reactive.jsonl`
    - scheduled: 2136  completed: 2136  halt_reason: `None`
    - primary_real_429: 0  spillover_real_429: 0 (0.00%)
    - cache_hit_overall: 0.9905  spillover_fraction: 0.9888
    - total_usd: $17.8957  pricing: `pricing/azure-openai-payg-2026-05.yaml`
  - **proactive** — `20260528T183310Z_exp005_dual_spillover_proactive_proactive.jsonl`
    - scheduled: 2136  completed: 2303  halt_reason: `None`  (excess 167 records = `primary_real_429_count`; runner logs failed-primary attempt as `sub_request_role=primary_429` before routing the same `request_idx` to spillover. All 2136 unique `request_idx` values present.)
    - primary_real_429: 167  spillover_real_429: 0 (0.00%)
    - cache_hit_overall: 0.9821  spillover_fraction: 0.4333
    - total_usd: $18.8060  pricing: `pricing/azure-openai-payg-2026-05.yaml`

**Validation invariants (both policies).** `dirty=false`, `dry_run=false`, `git_commit=9a266efec53ca9e1e86c8f9a1b45808808d656d8`, and either `halt_reason in (null, '')` with every scheduled `request_idx` present in the JSONL, or a documented halt reason captured in the per-policy summary. Under `halt_reason in (null, '')` the reactive policy satisfies `completed_request_count == scheduled_request_count`; the proactive policy satisfies `completed_request_count == scheduled_request_count + primary_real_429_count` because the runner emits each failed-primary attempt as a `sub_request_role=primary_429` follow-up record before routing the same `request_idx` to spillover (all 2136 unique `request_idx` values must still be present).

**Artifacts copied into the main repo.** `benchmarks/05-dual-spillover/runs/<jsonl + .summary.json>` (both policies), `results/dual-spillover-curves/` charts and CSV companions, and per-run reachability-witness rows appended to `benchmarks/05-dual-spillover/PREFLIGHT_LOG.md` (existing aborted-dirty-attempt rows preserved verbatim).

**No commit / push / PR** is opened by the copyback step — that remains an owner decision after review.

### Fixed — Task 015: Phase 2 dual-spillover long-run Entra ID transient-timeout hotfix

**Goal.** Stop the Phase 2 dual-endpoint spillover reactive run
(`scripts/measure_dual_spillover.py`) from aborting on a single
transient Azure CLI subprocess timeout. The previous live attempt
exited rc=1 at `request_idx≈1006` after ~2h of wall clock with
`azure.identity.CredentialUnavailableError: Timed out waiting for
Azure CLI`, ending the run mid-stream and leaving the proactive
policy never dispatched. No live Azure calls were issued and no
measurement artifacts were generated by this change; this is a
pre-run hotfix only. The partial reactive JSONL captured by the
failed attempt remains valid only as forensic evidence — it does not
satisfy the `scheduled_request_count == completed_request_count`
gate and must not be copied into `runs/`.

**Root cause.** `_build_live_client` passed the raw async callable
returned by `azure.identity.aio.get_bearer_token_provider` directly
into `AsyncOpenAI(api_key=...)`. The OpenAI SDK awaits that callable
before *every* Responses API request, so the underlying
`azure.identity.aio` chain re-spawns `az account get-access-token`
whenever its internal cache nears expiry. Under host load that
subprocess can transiently exceed its 10s timeout and raise
`CredentialUnavailableError`. With no retry or caching layer, a
single transient timeout aborted a multi-thousand-request run.

**Changed files (working repo).**
- `scripts/measure_dual_spillover.py` — added module-level helper
  `_make_robust_token_provider(underlying, *, max_retries=5,
  base_backoff_seconds=1.0, max_backoff_seconds=30.0, sleeper=None)`.
  The returned coroutine function retries
  `CredentialUnavailableError` / `asyncio.TimeoutError` /
  `TimeoutError` with bounded exponential backoff
  (1s → 2s → 4s → 8s → 16s, capped at 30s) up to 5 retries before
  re-raising. Non-transient exceptions propagate immediately.
  Concurrent calls are serialised with an `asyncio.Lock` to prevent
  refresh stampedes (so a single Azure-side cache miss observed by
  N in-flight requests does not spawn N parallel `az` subprocesses;
  one drives the refresh, the rest pick up the freshly-issued
  token via the underlying's in-memory cache). The wrapper
  **intentionally does NOT add an outer fixed-window cache** of
  bearer strings: token reuse + refresh-near-expiry is delegated
  entirely to `azure.identity.aio`'s own internal credential cache,
  which knows the real Entra ID `exp` and rotates within ~5 min of
  expiry. An outer fixed-window cache would risk re-caching an
  aged token that `azure.identity` returned from its internal
  cache while still valid (e.g. minute 40 of a 60-minute Entra
  TTL), then extending the wrapper's perceived freshness past the
  real Azure-side expiry → 401 mid-run. No bearer string, JWT, or
  scope value is ever logged — log lines record only the attempt
  counter, the exception class name, and the backoff seconds.
  `_build_live_client` now wraps the raw provider via this helper
  and re-binds the result to the local `token_provider` symbol so
  the source-pinning regression test
  (`test_build_live_client_uses_aio_identity_not_sync`) still
  matches the `api_key=token_provider)` form. Foundry v1
  `base_url`, audience scope `https://ai.azure.com/.default`, and
  `api_version="preview"` preserved verbatim; no other call sites,
  JSONL record shape, or measurement contract touched.
- `tests/test_measure_dual_spillover.py` — added six focused
  offline regression tests covering the wrapper directly with
  injected fakes (no real Entra ID, no sockets):
  (1) `test_robust_token_provider_retries_on_transient_cli_timeout`
  — transient `CredentialUnavailableError` is retried with the
  expected exponential schedule and the eventually-fetched token is
  returned; (2)
  `test_robust_token_provider_exhausts_after_bounded_retries`
  — re-raises the original exception after `max_retries + 1` total
  attempts (no silent swallow); (3)
  `test_robust_token_provider_retries_on_asyncio_timeout_error`
  — `asyncio.TimeoutError` is also classified transient; (4)
  `test_robust_token_provider_does_not_retry_non_transient_errors`
  — programming/configuration errors propagate on first attempt;
  (5) `test_robust_token_provider_does_not_cache_calls_underlying_every_time`
  — every wrapper call reaches `underlying` (no outer caching at
  this layer); (6)
  `test_robust_token_provider_does_not_recache_aged_underlying_token`
  — reviewer-scenario regression: when `underlying` returns the
  SAME bearer across 25 sequential wrapper calls (mimics
  `azure.identity` serving from its internal cache mid-Entra-TTL),
  the wrapper must call `underlying` 25 times and never extend the
  token's perceived freshness past `underlying`'s say-so;
  (7) `test_robust_token_provider_no_static_token_regression`
  — five sequential wrapper calls with a rotating underlying value
  prove the wrapper never embeds a static one-shot token. The
  existing `test_build_live_client_provider_refreshes_per_call`
  test continues to verify the original long-run safety property
  (three SDK refresh calls → three distinct fresh tokens drawn
  from the underlying provider → `provider_calls == 3`). All
  tests use the existing `_install_fake_aio_identity` monkeypatch
  fixture or inject local fake underlyings; the `_socket_guard`
  fixture continues to enforce zero outbound HTTPS at construction.

**Scope of this commit.** Pre-run hotfix only. No `runs/exp005_*`
JSON, JSONL, or summary files were created, modified, or
overwritten. No frozen file (`pricing/*.yaml`, benchmark datasets,
prior `runs/` artifacts, `docs/*.md`, `experiments/*.yaml`, other
`scripts/*.py`) was touched. The exp005 reactive/proactive YAMLs
are unchanged by this commit. The failed reactive JSONL staged at
`/tmp/task015-phase2-runlogs/artifacts/reactive/runs/` is retained
out-of-tree as forensic evidence only and must not be copied into
`runs/`. The expensive live experiment is NOT re-run by this
commit; the next dispatch is expected to land Phase 2 live-run
artifacts after a clean working tree relative to the recorded
`git_commit`.

### Fixed — Task 015: Phase 2 dual-spillover long-run Entra ID auth hotfix

**Goal.** Eliminate the silent mid-run `401 Access token ... expired`
failure observed during a Phase 2 dual-endpoint spillover live attempt
(`scripts/measure_dual_spillover.py`) at `request_idx=202`, ~23 minutes
into wall-clock execution, after the bearer token captured at process
start exceeded its ~60-minute TTL under the combined reactive +
proactive 22-minute load shape plus SDK 429 backoffs. No live Azure
calls were issued and no measurement artifacts were generated by this
change; this commit only refreshes the auth wiring for the next clean
live dispatch.

**Root cause.** `_build_live_client` imported the *synchronous*
`azure.identity.get_bearer_token_provider`, eagerly called
`token_provider()` at construction time, and embedded the resulting
static JWT string into `AsyncOpenAI(api_key=...)`. The OpenAI SDK then
re-sent that one literal `Authorization: Bearer <static_jwt>` header
on every subsequent Responses API call with no refresh hook, so any
run longer than the remaining token TTL 401'd silently mid-stream.

**Changed files (working repo).**
- `scripts/measure_dual_spillover.py` — `_build_live_client` switched
  from sync `azure.identity` (eager `token_provider()` call embedding
  a one-shot static token) to async `azure.identity.aio` with the
  async callable passed directly into
  `AsyncOpenAI(api_key=token_provider)`. The OpenAI SDK (`AsyncOpenAI`
  ≥ 2.x) awaits the callable before each Responses API call via
  `_refresh_api_key`, and `azure.identity.aio` internally caches and
  renews the underlying access token, so the Bearer header is always
  fresh end-to-end. Foundry v1 `base_url`, audience scope
  `https://ai.azure.com/.default`, and `api_version="preview"`
  preserved verbatim. Mirrors the Task 014 fix already landed in
  `scripts/simulate_spillover.py`; no other call sites or JSONL record
  shape touched.
- `tests/test_measure_dual_spillover.py` — added five focused offline
  regression tests under the "Long-run auth hardening" section that
  parallel the equivalent suite for `simulate_spillover.py`:
  (1) `_build_live_client` passes a callable provider (not a
  pre-resolved string) and uses the async coroutine form,
  (2) the SDK's `_refresh_api_key` hook draws a *fresh* token per call,
  (3) `_build_live_client` does not invoke the provider eagerly at
  construction time, (4) no socket is opened during client
  construction (guarded `socket.socket` traps any connect attempt),
  and (5) the import path is pinned to `azure.identity.aio` with the
  callable form `api_key=token_provider` (not
  `api_key=token_provider()`). All tests use a fake
  `azure.identity.aio` injected via `monkeypatch`; no real Entra ID,
  no real network.

**Scope of this commit.** Pre-run hotfix only. No `runs/exp005_*`
JSON, JSONL, or summary files were created, modified, or overwritten.
No frozen file (`pricing/*.yaml`, benchmark datasets, prior `runs/`
artifacts, `docs/*.md`, non-exp005 `experiments/*.yaml`, other
`scripts/*.py`) was touched. The exp005 reactive/proactive YAMLs are
unchanged by this commit; they were updated by the prior Task 015
Phase 2 pre-run hotfix entry below.

**Reviews.** First review gate APPROVE, final review gate APPROVE.

### Changed — Task 015: Phase 2 dual-spillover exp005 pre-run hotfix

**Goal.** Unblock dispatch for the Phase 2 dual-endpoint spillover
measurement (`experiments/exp005_dual_spillover_reactive.yaml`,
`experiments/exp005_dual_spillover_proactive.yaml`) after the Foundry
v1 Responses API and `gpt-5.2-2025-12-11` rejected the prior preflight
and workload kwargs. No live Azure calls were issued and no measurement
artifacts were generated by this change; this commit only prepares the
clean live-run state.

**Changed files (working repo).**
- `scripts/measure_dual_spillover.py` — `preflight_reachability`
  switched from `max_output_tokens=8` to `max_output_tokens=16`
  (Foundry v1 minimum; previous value 400'd with
  `integer_below_min_value`) and from `reasoning={"effort": "minimal"}`
  to `reasoning={"effort": "low"}` (gpt-5.2-2025-12-11 rejects
  `minimal` with `unsupported_value`; supported set is
  `none / low / medium / high / xhigh`). Inline comments document
  both lower bounds so the fix does not silently regress.
- `experiments/exp005_dual_spillover_reactive.yaml` and
  `experiments/exp005_dual_spillover_proactive.yaml` — `effort:
  minimal` → `effort: low` on both policies, matching the exp004 v3
  HOTFIX resolution so the Phase 1 ↔ Phase 2 recovery-curve comparison
  uses the same model-accepted effort level. Budget envelope aligned
  with the exp004 v3 HOTFIX pattern: `estimated_cost_usd: 6.0` →
  `118.31` (conservative no-cache pre-run upper bound),
  `hard_ceiling_usd: 75.0` → `60.0` (per-policy mid-run guard), and
  `confirmed: false` → `true` (owner-approved Phase 2 live spend for
  Task 015 Phase 2 data generation). Inline comments record the
  rationale and the cached vs no-cache derivation.
- `tests/test_measure_dual_spillover.py` — added
  `test_preflight_reachability_uses_min_legal_kwargs`, a regression
  guard that asserts the preflight call kwargs against both lower
  bounds (`max_output_tokens >= 16`, `reasoning == {"effort": "low"}`)
  for the primary and spillover deployments, so any drift back to the
  rejected values fails offline before any live spend.
- `benchmarks/05-dual-spillover/PREFLIGHT_LOG.md` — clarified the
  "source of truth" language so rows explicitly labelled
  `aborted-dirty-attempt / invalid-for-measurement` are excluded from
  reachability witness selection. Appended two such rows from
  2026-05-28T01:55:14Z and 2026-05-28T01:57:14Z (reactive policy,
  `git_commit d60dbe5d…`, working tree dirty at invocation —
  retained only for the append-only audit trail, not usable as a
  reachability witness for analysis).

**Scope of this commit.** Pre-run only. No `runs/exp005_*` JSON,
JSONL, or summary files were created, modified, or overwritten by this
change. The next commit on this branch is expected to land the clean
Phase 2 live-run artifacts after dispatch with `dry_run=false` and a
clean working tree relative to the recorded `git_commit`.

**Reviews.** First review gate APPROVE, methodology audit gate APPROVE,
final review gate APPROVE.

### Added — Task 014: Phase 1 spillover-simulation live analysis

**Goal.** Analyze the restored live Task 012 Phase 1 exp004 spillover
simulator outputs without re-running the simulator and frame the result as a
weak-form Hypothesis G mitigation comparison.

**Deliverables.**
- Added `benchmarks/04-spillover-simulation/analysis.json`, derived from the
  immutable reactive/proactive raw JSONL streams and summary files.
- Added `benchmarks/04-spillover-simulation/analysis.md` with the Task 014
  12-section narrative, PAYG and PTU translations kept separate, the
  methodology Section 8 descriptive-reporting guardrail, zero confidence
  intervals, and no significance claims.
- Included the restored live run artifacts in the intended commit scope:
  the two raw JSONL files, their `.summary.json` companions, and
  `results/spillover-recovery-curves/*.{png,csv}`.

**Key measured result.** Sustain-phase cache hit ratio was **99.2337%** for
reactive and **99.0680%** for proactive, so proactive was **-0.1657
percentage points** below reactive in sustain. The full-run overall cache hit
ratio was **99.0478%** reactive and **99.0942%** proactive. Both runs completed
2,136 / 2,136 requests with `dry_run=false`, `dirty=false`, matching git commit
`67542910f31cf507f2523c0fba2102df6b622f8a`, matching system prompt SHA-256, no
real 429s, and no halt reason.

**Cost and validation framing.** PAYG totals use the actual measured summaries:
reactive **$17.883347**, proactive **$17.924671**, sourced to
`pricing/azure-openai-payg-2026-05.yaml` accessed 2026-05-19. The PTU section
declares reactive as baseline and reports only a token-pressure proxy
throughput factor, with single-endpoint simulator and no-PTU-billing caveats.
Outlier policy excluded zero cells; high-latency candidates without coincident
logged events were retained.

### Changed — Task 014 / Task 012 Phase 1: exp004 live-run unblock gate (v3 HOTFIX + v3.1 OWNER SIGNOFF)

**Goal.** Unblock dispatch for the Phase 1 spillover-policy simulator
(`experiments/exp004_spillover_baseline_reactive.yaml`,
`experiments/exp004_spillover_proactive.yaml`) after Azure `gpt-5.2`
rejected `effort: minimal` at the Foundry v1 endpoint, while keeping
live Phase 1 data generation explicitly gated behind owner budget
sign-off. No live Azure calls were issued and no measurement
artifacts were generated by this change.

**Changed files (working repo, v3 → v3.1).**
- `experiments/exp004_spillover_baseline_reactive.yaml` — `effort:
  minimal` → `effort: low` (next-lowest value in
  `docs/05-methodology.md` §2 `reasoning_effort` vocabulary
  `{minimal, low, medium, high}`); `budget.estimated_cost_usd: 3.0`
  → `118.31`; `budget.hard_ceiling_usd: 50.0` → `60.0`;
  `budget.confirmed: false` → `true` (v3.1 owner sign-off — see
  the v3.1 OWNER SIGNOFF subsection below). Inline v3 HOTFIX
  comment block added near `effort:` and the `budget:` mapping
  documenting the cached-vs-no-cache derivation and the gating
  contract; inline v3.1 OWNER SIGNOFF comment block added
  alongside the `budget:` mapping recording the in-chat approval.
- `experiments/exp004_spillover_proactive.yaml` — same `effort`,
  `budget.estimated_cost_usd`, `budget.hard_ceiling_usd`, and
  `budget.confirmed: false` → `true` changes; same inline v3
  HOTFIX and v3.1 OWNER SIGNOFF comment blocks.
- `scripts/simulate_spillover.py` — `_build_live_client` switched
  from sync `azure.identity` (eager `token_provider()` call
  embedding a one-shot static token) to async
  `azure.identity.aio` with the async callable passed directly
  into `AsyncOpenAI(api_key=token_provider)`. This makes the
  Entra ID bearer refreshable per Responses API call so runs
  longer than the ~60-minute token TTL (reactive + proactive
  back-to-back plus retry backoffs) no longer 401 silently
  mid-stream. Foundry v1 `base_url` and `api_version="preview"`
  preserved verbatim. (Long-run hardening; landed alongside the
  v3.1 owner sign-off.)
- `tests/test_simulate_spillover.py` — added focused offline
  coverage for the auth wiring above: provider is a callable
  (not a pre-resolved string), `_refresh_api_key` draws a fresh
  token per call, no socket is opened at client construction
  time, the `azure.identity.aio` import path is pinned, and the
  audience scope stays `https://ai.azure.com/.default`. All
  tests use a fake `azure.identity.aio` injected via
  `monkeypatch`; no real Entra ID and no real network.
- `pyproject.toml` — `openai>=1.50.0` → `openai>=2.37.0`.
  Required because the per-request refreshable
  `api_key=Callable[[], Awaitable[str]]` contract used by the
  fixed `_build_live_client` is only available on `AsyncOpenAI`
  in the 2.x line.
- `CHANGELOG.md` — this entry (v3 HOTFIX text updated in place
  to match the v3.1 state, with the v3.1 OWNER SIGNOFF
  subsection appended below).

**Cost envelope (per policy, 22-minute run).**
- Expected cached per-policy spend ≈ **$25 USD**. Phase 1's load
  shape (byte-identical large stable system prompt, warmup → ramp →
  sustain) drives a high prompt-cache hit ratio, so most input
  tokens bill at the cached rate. This is the spend we actually
  expect to incur — it is **not** the value carried in
  `budget.estimated_cost_usd`.
- `budget.estimated_cost_usd: 118.31` is the **conservative
  no-cache pre-run upper bound** assuming every request bills
  input at the full uncached rate (cache-collapse scenario). It
  functions as the YAML approval-gate value, deliberately set
  above the local cap. Source:
  `pricing/azure-openai-payg-2026-05.yaml` (accessed 2026-05-19);
  `gpt-5.2` PAYG bills $1.75 / $0.175 / $14.00 per 1M input /
  cached / output tokens.
- `budget.hard_ceiling_usd: 60.0` is the mid-run no-silent-overrun
  guard, sitting above the cached estimate and below the
  no-cache upper bound so a cache-collapse scenario halts well
  before $118.31.

**Gating contract (confirmed).** `scripts/simulate_spillover.py`
reads `MAX_COST_PER_BENCHMARK_USD` from the runner's process
environment (`os.environ`) and aborts when
`experiment.budget.estimated_cost_usd > MAX_COST_PER_BENCHMARK_USD
AND budget.confirmed == false`. With `MAX_COST_PER_BENCHMARK_USD=100`
exported into the process environment and `estimated_cost_usd=118.31`
in both YAMLs, the gate would trip on `budget.confirmed: false`.
For v3.1, the owner exercised the documented in-YAML override by
flipping `budget.confirmed: true` in both exp004 YAMLs (see the
v3.1 OWNER SIGNOFF subsection below); `MAX_COST_PER_BENCHMARK_USD`
remains at `100` in the runner's process environment. The override
is auditable in the YAMLs themselves and in this CHANGELOG.

**Review gates.** methodology audit gate — APPROVE; first review gate —
APPROVE; final review gate — APPROVE.

**Frozen files untouched.** All `pricing/*.yaml`, all benchmark
datasets / corpora, all prior `runs/` artifacts, all `docs/*.md`,
all non-exp004 `experiments/*.yaml`, and all `scripts/*.py`
other than `scripts/simulate_spillover.py` are unchanged. No prior
benchmark run is re-derived or re-rendered. The Phase 1 simulation
schedule (warmup + ramp + sustain, 22 min) and `corpus_seed` are
byte-identical to v2; only `effort`, the budget envelope,
`budget.confirmed`, and the `_build_live_client` auth wiring
changed (the simulation logic, retry handling, and JSONL record
shape are untouched).

**Live Azure runs.** No live Phase 1 dispatch was performed on
this branch. The pre-run budget gate is now unblocked by the v3.1
owner sign-off (see subsection below); actual live dispatch — if
and when scheduled — will be a separate operational step that
produces its own measurement artifacts and follow-up CHANGELOG
entry.

#### v3.1 OWNER SIGNOFF — 2026-05-27 (Asia/Seoul)

**Approval channel.** Owner approved the exp004 Phase 1 live-run
budget for both policies (reactive + proactive) in chat on
**2026-05-27, timezone Asia/Seoul**. This subsection is the
auditable in-repo record of that conversation-channel approval;
the in-YAML `budget.confirmed: true` flip in both
`experiments/exp004_spillover_baseline_reactive.yaml` and
`experiments/exp004_spillover_proactive.yaml` is the load-bearing
configuration change it authorizes.

**Scope.** Task 012 / Task 014 Phase 1 spillover-policy simulator
only (`exp004_spillover_baseline_reactive` +
`exp004_spillover_proactive`). The byte-identical Phase 1
schedule (warmup + ramp + sustain, 22 min per policy),
`corpus_seed: 4242`, target system prompt token budget,
`reasoning_effort: low` (per v3 HOTFIX), and the
`MAX_COST_PER_BENCHMARK_USD=100` runner-environment gate value
are all preserved verbatim — sign-off is on the budget envelope
only, not on a methodology change.

**Approved budget envelope (per policy, per run).**
- Approval-gate estimate (`budget.estimated_cost_usd`):
  **$118.31 per policy** (conservative no-cache pre-run upper
  bound, unchanged from v3 HOTFIX). Across both policies the
  approval-gate aggregate is **$236.62** ($118.31 × 2).
- Mid-run hard ceiling (`budget.hard_ceiling_usd`):
  **$60.00 per policy** (unchanged from v3 HOTFIX). Across both
  policies the aggregate hard-ceiling guard is **$120.00**
  ($60.00 × 2).
- Expected cached spend ≈ $25 per policy (≈ $50 across both)
  remains the realistic billed figure given Phase 1's high
  prompt-cache hit ratio; the $118.31 / $236.62 figures are the
  pre-run no-cache upper bound that the YAML approval gate
  reads, not the spend the owner expects to incur.

**Override mechanism.** `budget.confirmed: false → true` in both
exp004 YAMLs. With this flip, the pre-run gate in
`scripts/simulate_spillover.py`
(`estimated_cost_usd > MAX_COST_PER_BENCHMARK_USD AND
budget.confirmed == false`) no longer trips — `budget.confirmed`
is now `true`, so the `AND` short-circuits to `False` and live
dispatch is permitted. This is the in-YAML branch of the gating
contract documented in the v3 HOTFIX block above; the alternative
branch (raising `MAX_COST_PER_BENCHMARK_USD` above $118.31) was
**not** taken — the runner-environment ceiling stays at $100.

**Hard ceiling unchanged.** `budget.hard_ceiling_usd` stays at
`60.0` per policy. The mid-run no-silent-overrun guard is not
raised by this sign-off; if a cache-collapse scenario drives
billed spend toward the no-cache upper bound, the run halts at
$60 per policy, well before $118.31.

**Co-landed long-run auth hardening.** Alongside the YAML
sign-off, `scripts/simulate_spillover.py::_build_live_client` was
updated to use `azure.identity.aio` and pass the async token
provider callable directly into `AsyncOpenAI(api_key=...)` so
Entra ID bearer tokens refresh per Responses API call (the
previous embedded-static-token path could 401 mid-stream once
the ~60-minute token TTL elapsed; that risk was elevated by
running reactive + proactive back-to-back under sign-off).
Required `openai>=2.37.0` (`pyproject.toml`) for the
per-request refreshable `api_key` callable contract, and added
offline tests in `tests/test_simulate_spillover.py` proving the
wiring (no real Entra ID, no sockets opened).

### Added — Task 017: live tool-loop cap recovery schema

**Goal.** Tighten the live tool-loop runner in `scripts/run_benchmark.py`
so the iteration-cap recovery leg (1) is forced to emit a final answer
(no `tools=` re-passed on the recovery call) and (2) appears on the
trajectory as a regular per-iteration row with `tool_name=None` /
`tool_args=None`, keeping the audit trail honest about the cap-recovery
call actually happening while still passing the Task 010 per-row schema
contract (`{iteration, tool_name, tool_args, tool_result_summary,
latency_ms, usage}`, `additionalProperties: false`). Adds focused unit
coverage for the three live tool-loop termination shapes (normal,
iteration cap, tool exception recovery) using a scripted fake
`responses.create` so the cap-recovery wiring is locked down without
touching Azure.

**Changed files (working repo).**
- `scripts/run_benchmark.py` — in `_live_tool_loop_call`: drop the
  stray `tool_call_id` key from the per-iteration trajectory row so the
  schema check the new tests enforce stays bit-tight; on the
  `iteration_cap` branch, defensively `pop("tools", None)` from the
  recovery-call kwargs (the loop never adds `tools=` to
  `base_call_kwargs`, but a future refactor of the call-kwargs builder
  could regress this); capture the recovery-call latency and append a
  trajectory row for the cap-recovery leg carrying ONLY the Task 010
  key set and the per-iteration usage already summed into the cell
  total — no audit-only fields, `additionalProperties: false` passes.
  Docstring updated to record the new behavior.
- `tests/test_run_benchmark.py` — adds five Task 017 tests that drive
  `_live_tool_loop_call` directly through a scripted fake
  `responses.create`: normal-termination trajectory schema +
  usage-summation invariant, iteration-cap recovery call omits
  `tools=`, iteration-cap trajectory includes the recovery row with
  `tool_name=None` / `tool_args=None`, tool-exception recovery feeds
  the exception message back as the tool result, and the explicit
  jsonschema (`additionalProperties: false`) check on every
  trajectory row across all three termination shapes.
- `pyproject.toml` — adds `jsonschema>=4.0` to the `dev` extra so the
  test suite can validate the trajectory-row schema with a real
  validator instead of an ad-hoc dict comparison. Intentional dev-only
  dependency; runtime imports are unchanged.
- `CHANGELOG.md` — this entry.

**Frozen files untouched.** `scripts/cost_calculator.py`,
`scripts/run_judge.py`, `scripts/analyze_tokens.py`,
`scripts/plot_results.py`, all `pricing/*.yaml`, all benchmark
datasets / corpora, all prior `runs/` artifacts, and all prior
`experiments/*.yaml` are unchanged. No prior benchmark run is
re-derived or re-rendered.

**Live Azure runs intentionally gated.** Live smoke and live full
tool-loop runs are **not** executed on this branch: the required Azure
env vars (`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_GPT_5_2`,
Entra ID credential) are not configured in this work environment and
the runner's live path is gated behind real-credential resolution. The
change is exercised end-to-end via the scripted-fake unit tests; the
existing benchmark-03 `--skip-existing` resume path remains the
recovery mechanism if and when a live run is launched.

**Reviews.** first review gate **APPROVE**; local CLI reviewer worker final
**APPROVE** after a REQUEST-CHANGES fix round (the cap-recovery
trajectory row + `tools=` defensive pop landed in that fix round).

**Verification.** `git diff --check` clean;
`python3 -m pytest tests/test_run_benchmark.py -q` — **38 passed**;
`python3 -m pytest tests/ -q` — **204 passed**;
`python3 -m ruff check .` — **All checks passed**.

PR: (to be filled on open) — branch `feature/tool-loop-runner-body` → `main`.


### Added — Task 013: Phase 2 dual-endpoint spillover measurement (Hypothesis G_weak)

**Goal.** Replace the Phase 1 internally-simulated PTU throttle and shared
cache pool with two real Azure `gpt-5.2`-family deployments — a low-TPM
throttled primary that produces **real 429s** under the same 22-minute /
large-prefix / 2-TPS sustained workload, and a high-TPM spillover
deployment whose separate deployment name yields a separate Azure cache
pool by design. Reuses Task 012's pure-function `reactive_decide` /
`proactive_decide` primitives verbatim so the Phase 1 ↔ Phase 2
recovery-curve comparison varies only in the throttle and cache-pool
mechanism, not in the policy logic.

**Changed files (working repo).**
- `scripts/measure_dual_spillover.py` — new Phase 2 runner, sibling to
  `scripts/simulate_spillover.py` and `scripts/run_benchmark.py` (does
  not import or modify them at the orchestration layer; imports only the
  Task 012 policy primitives unchanged). Constructs two `AsyncOpenAI`
  clients against the same Foundry v1 endpoint base and the same Entra
  ID credential, differing only in the deployment name passed at request
  time. Mandatory `preflight_reachability()` one-request ping per
  deployment before any policy iteration (skipped only on `--dry-run`,
  which makes zero outbound HTTPS calls); atomic timestamped append to
  `benchmarks/05-dual-spillover/PREFLIGHT_LOG.md` on success using only
  env-var names and the boolean reachability + output-token count (no
  secrets, no endpoint URLs, no resolved deployment names). Per-request
  capture via the OpenAI SDK's `client.responses.with_raw_response.create`
  path so HTTP response headers (`x-ms-spillover-from-deployment`,
  `x-ms-deployment-name`, `x-ms-spillover-error`) are reliably exposed
  on the success path and on errors (`exc.response.headers`). The
  per-request JSONL record adds `endpoint_hit`, `deployment_used`,
  `cache_pool` (= `deployment_used`), `real_429_observed`,
  `primary_429_count_running_total`, `retry_after_ms` /
  `retry_after_seconds` parsed from 429 headers, and the three
  `x_ms_*` header fields; the Phase 1
  `simulated_primary_throttle_state` field is **omitted** (unit-asserted)
  so the Phase 2 schema cannot be mistaken for simulator output. Live
  `--smoke` enforces a strict success-criteria gate (`primary_real_429_count
  >= 1` AND `spillover_real_429_count == 0`); failure exits with
  `EXIT_SMOKE` so a misconfigured workload is caught before any full run
  is launched. Pre-run cost estimate via `scripts.cost_calculator` and a
  mid-run running-USD halt at `budget.hard_ceiling_usd` (per-policy
  ceiling $75) plus the > 1% spillover-side 429-rate halt for full runs.
- `experiments/exp005_dual_spillover_reactive.yaml` — REACTIVE policy
  variant; `architecture_context: single_call_react`,
  `consumption_model_context: ptu`, `benchmark: 05-dual-spillover`,
  `hypothesis_under_test: G_weak`, `phase: 2`,
  `parent_experiment: exp004_spillover_baseline_reactive`,
  `effort: minimal`, `corpus_seed: 4242`,
  target system prompt token budget, 22-minute load profile
  (verbatim Phase 1), primary `tpm: 60000` / `rpm: 600`, spillover
  `tpm: 500000` / `rpm: 5000`, `budget.hard_ceiling_usd: 75.0`,
  `budget.confirmed: false`.
- `experiments/exp005_dual_spillover_proactive.yaml` — PROACTIVE policy
  sibling; single-variable diff from the reactive YAML
  (`policy.type: proactive`); same `corpus_seed` →
  byte-identical large stable system prompt across the two runs so the
  cross-policy recovery-curve comparison stays fair.
- `benchmarks/05-dual-spillover/README.md` — frames the benchmark as
  **not a simulation** (real throttling + real cache-pool separation)
  and **not a replica of any specific deployment** (TPM, corpus, user
  pool, and load pattern are mechanism-exposing workload-shaping
  parameters, never customer-attributed); documents the two-deployment
  / two-cache-pool design, the verbatim Phase 1 policy contract, the
  per-request capture schema with the Phase 1 → Phase 2 migration note,
  the four output charts, and the corpus SHA-256 contract that pins
  byte-identity to Phase 1.
- `benchmarks/05-dual-spillover/system_prompt_corpus.json` and
  `benchmarks/05-dual-spillover/user_prompts.json` — **verbatim copies**
  of the Phase 1 files. SHA-256s match exactly:
  `system_prompt_corpus.json` =
  `6a8ab5a3cb1ad3dace030a82ec1327496b39e65b77a627714a27c39017ca19e3`;
  `user_prompts.json` =
  `45f4a95b5cfe208a3555683fe22c6eb74f1b0e46d621723b1ea1480713ab3087`.
  Byte-identity is the contract that lets Phase 1 ↔ Phase 2 emit
  byte-identical large stable system prompts under the same `corpus_seed`.
- `benchmarks/05-dual-spillover/PREFLIGHT_LOG.md` — owner-completed
  manual pre-flight (deployments exist, scoped to the intended TPM /
  capacity tier, Entra ID auth) plus the implementer reachability-check
  contract that the runner appends to on every live invocation.
- `benchmarks/05-dual-spillover/runs/.gitkeep` and
  `results/dual-spillover-curves/.gitkeep` — directory placeholders so
  the runner's output paths exist in git. No JSONL / PNG / CSV
  artifacts are committed (append-only output discipline; the
  directories are intentionally empty at commit time).
- `tests/test_measure_dual_spillover.py` — 13 tests (zero outbound
  HTTPS, zero Azure credential resolution): YAML validation, dual-client
  construction, `preflight_reachability` happy-path + abort, atomic
  preflight-log append, raw-response header parsing (notably the async
  raw-response parse fix flagged in final review gate),
  `simulated_primary_throttle_state` schema absence,
  smoke success-criteria gate (both halves), policy primitive import
  equivalence to Task 012, and the `--dry-run` socket-mock zero-HTTPS
  assertion. All 13 pass; ruff clean.
- `.env.example` — adds `AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED`
  with comment block documenting the deliberately low TPM cap and that
  the spillover route reuses `AZURE_OPENAI_DEPLOYMENT_GPT_5_2`
  (benchmark 01 unchanged). Scope exception: `.env.example` documents
  the *variable name only*, no secret value.
- `CHANGELOG.md` — this entry.

**Live Azure runs intentionally gated.** Both smoke (≤ $1 per policy,
~3 min) and full (~$3–6 per policy, 22 min, per-policy hard ceiling
$75) live runs are **gated pending owner corpus-review / live-deployment
release** per the README "Owner review gate" and PREFLIGHT_LOG.md owner
pre-flight sections. The PR is opened with this gate in force; no live
Azure smoke or full run has been executed on this branch. The `--dry-run`
path remains available and is exercised by the test suite.

**Zero Azure spend.** Implementation, tests, and review cycles produced
**$0.00** Foundry traffic. No measurement script invoked against a live
endpoint, no judge call, no smoke run. All evidence of correctness comes
from `tests/test_measure_dual_spillover.py` (13 tests, all passing) and
the socket-mock dry-run assertion.

**Reviews.** first review gate **APPROVE**; local CLI reviewer worker final
**APPROVE** after async raw-response parse fix (the success-path
`with_raw_response` await + header-extraction path was tightened so
headers are captured uniformly across success and error paths).

**Anonymization audit.** Added-lines grep on staged files for
customer/organization/specific-bank-name patterns returns **zero
matches**. Corpus and user-prompt strings are byte-identical to Phase 1
(generic financial-services educational content with no institution,
product, app, regulator, or internal team name). The throttled-primary
deployment-name token and the production `gpt-5.2` deployment are
mechanism / deployment-side identifiers only and are not spelled out
verbatim in this entry.

PR: (to be filled on open) — branch `feature/spillover-measurement-phase2`
→ `main`.

### Added — Task 012: Phase 1 spillover-policy simulator (Hypothesis G_weak)

**Goal.** Stand up a single-endpoint, internally-throttled spillover-policy
simulator that produces evidence for **weak-form Hypothesis G** only — the
saturation-sensitive cache-hit dip and the relative shape of the
reactive-vs-proactive recovery curves. The strong form of Hypothesis G was
disproven by customer field measurement (see Task 011 v2 appendix); this
simulator reframes reactive-vs-proactive as a **mitigation** question, not a
root-cause question. Phase 2 (Task 013) replaces the internally-simulated
throttle with a real TPM-throttled second deployment; Task 014 owns the
analysis writeup.

**Changed files (working repo).**
- `scripts/simulate_spillover.py` — new Phase 1 simulator, sibling to
  `scripts/run_benchmark.py` (does not import or modify it). Two pure-function
  policy logic blocks (`reactive_decide`, `proactive_decide`) taking
  `(observation, state, params)` and returning `(decision, new_state)`;
  Foundry v1 client via `AsyncAzureOpenAI` + `DefaultAzureCredential` +
  `api_version="preview"`; deterministic system-prompt construction from
  `system_prompt_corpus.json` + `corpus_seed` with SHA-256 logged at run
  start and embedded in every per-request JSONL record; full per-request
  JSONL capture including `usage.input_tokens_details.cached_tokens`,
  `output_tokens_details.reasoning_tokens`, `prompt_cache_key` (Task 011 v2
  appendix), `retry_after_ms` / `retry_after_seconds` (parsed from 429
  response headers); `--dry-run` produces zero outbound HTTPS calls;
  `--smoke` overrides `duration_seconds` to 180 and
  `sustain_duration_seconds` to 60; pre-run cost estimate via
  `scripts.cost_calculator` and mid-run running-USD halt at
  `budget.hard_ceiling_usd`; real-429-rate halt at 5%; two PNG charts plus
  sibling CSVs per run and a `policy_comparison.png` overlay.
- `experiments/exp004_spillover_baseline_reactive.yaml` — REACTIVE policy
  variant; `architecture_context: single_call_react`,
  `consumption_model_context: ptu`, `benchmark: 04-spillover-simulation`,
  `hypothesis_under_test: G_weak`, `effort: minimal`,
  `corpus_seed: 4242`, target system prompt token budget, 22-minute load
  profile (2-min warmup at 0.3 TPS, 10-min ramp 0.5→2.5 TPS, 10-min sustain
  at 2.0 TPS), `simulated_throttle_threshold_tpm: 90000`,
  `budget.hard_ceiling_usd: 50.0`.
- `experiments/exp004_spillover_proactive.yaml` — PROACTIVE policy sibling;
  single-variable diff from the reactive YAML (`policy.type: proactive`);
  same `corpus_seed` → byte-identical system prompt across the two runs so
  the cross-policy recovery-curve comparison is fair.
- `benchmarks/04-spillover-simulation/README.md` — frames the benchmark as a
  **controlled simulation, not a replica of any specific deployment**,
  states the weak-form-G scope explicitly, documents the deterministic
  system-prompt construction, the two policy contracts verbatim, the load
  profile, the per-request capture schema, the chart artifacts, and the
  owner corpus-review gate that blocks live Azure runs.
- `benchmarks/04-spillover-simulation/system_prompt_corpus.json` — JSON list
  of ~120 neutral instruction snippets covering generic
  financial-services-assistant behavior (scope boundaries, safety guards,
  response format, refusal policies). Every snippet is generic — no bank
  name, product name, app name, regulator, or internal team name. The
  corpus is the most reviewable artifact in this benchmark; the owner
  reviews it before any live Azure run.
- `benchmarks/04-spillover-simulation/user_prompts.json` — 30 short generic
  financial questions (e.g. "Explain compound interest in two sentences",
  "How is APR different from APY?"). No product-specific questions.
- `benchmarks/04-spillover-simulation/runs/.gitkeep` and
  `results/spillover-recovery-curves/.gitkeep` — directory placeholders so
  the simulator's output paths exist in git. No dry-run JSONL / PNG / CSV
  artifacts are committed (append-only output discipline; the directories
  are intentionally empty at commit time).
- `tests/test_simulate_spillover.py` — 9 pure tests (zero outbound HTTPS,
  zero Azure credential resolution): reactive trigger + min-stay,
  proactive p95-driven fraction ramp, proactive `ramp_up_step` YAML
  validation, `--dry-run` socket-mock zero-HTTPS assertion, deterministic
  system-prompt SHA-256 per seed, budget-halt exit code 1, env-var-value
  non-leakage, and two `policy_comparison.png` emission/skip cases. All 9
  pass; ruff clean.
- `CHANGELOG.md` — this entry.

**Live Azure runs intentionally gated.** Both smoke (≤ $1, ~3 min) and full
(~$2–3 per policy, 22 min, hard ceiling $50) live runs are **gated pending
owner corpus review** per the README "Owner review gate" section. No live
Azure smoke or full run has been executed on this branch. The `--dry-run`
path remains available and is exercised by the test suite.

**Zero Azure spend.** Implementation, tests, and review cycles produced
**$0.00** Foundry traffic. No measurement script invoked against a live
endpoint, no judge call, no smoke run. All evidence of correctness comes
from the pure-function unit tests under `tests/test_simulate_spillover.py`
and the socket-mock dry-run assertion.

**Reviews.** first review gate **APPROVE**; local CLI reviewer worker final
**APPROVE**.

**Budget envelope note.** The current `pricing/azure-openai-payg-2026-05.yaml`
snapshot makes the simulator's conservative pre-run cost estimate run
**higher than the old Task 012 prose** ("~$1–$3 per policy"). The hard
ceiling enforcement is unchanged and remains binding —
`budget.hard_ceiling_usd: 50.0` per policy YAML, enforced by both the
pre-run `scripts.cost_calculator` estimate and the mid-run running-USD
check. Live full runs that approach the ceiling will halt before
exceeding it; if the pre-run estimate exceeds the ceiling, the simulator
refuses to start. The owner will reconcile the prose-vs-snapshot delta
against the live run record in Task 014.

**Anonymization audit.** Added-lines grep on staged files for
customer/organization/specific-bank-name patterns returns **zero matches**.
The corpus and user-prompt strings are generic financial-services
educational content with no institution, product, app, regulator, or
internal team name.

PR: (to be filled on open) — branch `feature/spillover-simulator-phase1` → `main`.


### Added — Task 011: Customer-shape framing (architecture context + Hypotheses G/H + v2 HOTFIX hypothesis priority)

**Goal.** Surface `architecture_context` (multi-node orchestration vs single-call
ReAct) as a first-class framing dimension in the experiment template and the
measurement-implementation worker prompt, and align the `README.md` PTU pointer with the v2
HOTFIX hypothesis priority **A / E / C / I / D / G_weak / H′ / B / F** from
`docs/07-cache-hit-degradation.md`. Prepares Tasks 012–016 (customer-scenario
simulation series) without touching any benchmark, script, or pricing snapshot.

**Changed files (working repo).**
- `experiments/_template.yaml` — adds `metadata.architecture_context` (allowed:
  `multi_node_orchestration | single_call_react | mixed`) as a sibling of
  `metadata.consumption_model_context`, plus optional, commented-out
  `call_params.prompt_cache_key` and `call_params.prompt_cache_retention`
  scaffolding (request-scoped key + `in_memory | 24h` retention) for future
  per-run YAMLs to opt into cache hypotheses.
- the measurement-implementation worker prompt file (lab-only) — Principle 6 extended with a
  PAYG-measurement / PTU-hypothesis split paragraph and an architecture-context
  paragraph; Pitfalls list appended with #10 ("Claiming PTU-specific causes
  from PAYG measurements"), #11 ("Generalizing multi-node cache patterns to
  single-call ReAct"), and #12 (`max_output_tokens` as PTU admission-time
  reservation, not a soft cap — Hypothesis I).
- `README.md` — "Short Answer" caching bullet rewritten to point at architecture
  + consumption model as the two axes that explain reasoning-model cache
  behavior; "Which Customer Are You?" PTU paragraph appended with the
  `docs/07-cache-hit-degradation.md` pointer ranking hypotheses in diagnostic
  priority **A / E / C / I / D / G_weak / H′ / B / F**, calling out **I**
  (`max_output_tokens` admission reservation), **G_weak** (transient cache dip
  at near-saturation PTU utilization), and **H′** (input-side architecture
  shift during single-call ReAct migration).
- `CHANGELOG.md` — this entry.

**Intentional skip.** the analyzer-role worker prompt file (lab-only) Principle 6 was NOT added:
an overlapping Principle 6 ("Two Consumption Models, One Set of Measurements")
already exists at lines 57–65, and the Task 011 success criterion explicitly
requires skipping and reporting when an equivalent principle is already
present.

**Not staged here (out of scope for this commit).**
- the private scaffold spec (lab-only meta repo; gitignored; separate
  meta repo commit, not part of the working repo PR).

**Zero API spend.** Framing-only edits. No measurement script invoked, no
benchmark re-run, no judge call, no Foundry traffic.

**Reviews.** first review gate **APPROVE**; local CLI reviewer worker final **APPROVE**.

**Anonymization audit.** Added-lines grep for the Task 011 prohibited
customer-identifying patterns (see Task 011 success criteria for the regex)
returns zero matches. Full-repo baseline (excluding the lab-only workspace) carries ~500
pre-existing generic "PTU" term mentions on `main`; this diff adds none of the
prohibited customer-identifying patterns.

PR: (to be filled on open) — branch `feature/customer-shape-framing` → `main`.

### Added — Task 010: Benchmark 03 (tool-using agent) + cross-benchmark synthesis

**Goal.** Measure whether GPT-5.2 `reasoning_effort` pays off on a tool-using
agent workload (the "tools change the calculus" case), then synthesize
benchmarks 01–03 into a single customer-facing (model, effort) recommendation.

**Deliverables.**
- Benchmark 03 dataset (20 samples, ≥6 tags, 30/40/30 no-tool / single-tool /
  multi-tool split), neutral system prompt, a deterministic `scripts/tools.py`
  calculator + canned `search_kb` knowledge base, and the `exp003` experiment
  YAMLs.
- `scripts/run_benchmark.py` gains a YAML-gated tool-loop mode (Foundry v1
  Responses tool-calling, fail-loudly, `--skip-existing` idempotent resume); the
  benchmarks 01/02 single-shot path is unchanged.
- `scripts/run_judge.py` emits a `tool_efficiency_score ∈ [0.0, 1.0]` field,
  gated on the measurement JSON containing a `tool_calls` key (benchmarks 01/02
  judge output unaffected, byte-identical).
- `scripts/analyze_tokens.py` emits a `tool_efficiency_breakdown` block plus a
  "Tool-efficiency breakdown" sub-section, gated on `tool_efficiency_score`
  presence (benchmarks 01/02 analyzer output unchanged when present-gated off).
- `benchmarks/03-tool-using-agent/analysis.{json,md}`, 10 chart artifacts under
  `results/`, the `results/summary.md` cross-benchmark synthesis, and
  `docs/04-decision-framework.md` (3 worked (model, effort) recommendations).
- Test suites `tests/test_tools.py`, `tests/test_run_benchmark.py`,
  `tests/test_run_judge.py`, `tests/test_analyze_tokens.py` — 177 tests total,
  ruff clean.

**Real Azure cohort.** 6 smoke + 360 headline measurement calls + 360 judge
calls, 100% live Foundry v1 (no `"fixture": true` sentinel in the headline
cohort). Combined spend **≈ $1.51** (measurement ≈ $1.11 + judge ≈ $0.40 +
smoke ≈ $0.008) — three orders of magnitude under the $45 combined hard ceiling.

**Known follow-up (pre-existing, not introduced by Task 010).** Benchmark-01's
committed `analysis.json` predates Task 009's reasoning-token cost fix and is
not byte-reproducible by the current analyzer; its `exp008` fixtures record
`reasoning_tokens` outside `output_tokens`. The drift is confined to the
Task-009 cost/throughput fields (no Task-010 field differs), and benchmarks
02/03 reproduce byte-identical — proving the Task 010 analyzer edits are
additive and gated. A canonical benchmark-01 fixture refresh is tracked
separately. See `benchmarks/03-tool-using-agent/RUN_REPORT.md` → "Pre-existing
benchmark-01 analyzer drift".

PR: #12 (branch `feature/benchmark-03-and-synthesis` → `main`).

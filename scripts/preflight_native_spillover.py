"""scripts/preflight_native_spillover.py — Task 021 v2.1 Stage 0 only.

This script implements the Task 021 v2.1 Stage 0 feasibility gate:

* **Stage 0a** — Read-only Azure CLI verification (zero spend) to
  inspect, on the configured primary deployment:
    - SKU/tier (normalized to a coarse alias: ``PAYG/GlobalStandard``,
      ``PTU/ProvisionedManaged``, or ``OTHER``).
    - Presence (boolean only) of the ``spilloverDeploymentName``
      deployment property.
  Emits exactly one Stage 0a verdict: ``READY_FOR_SMOKE_PROOF``,
  ``CONFIG-MISSING``, or ``INFEASIBLE-AS-SPEC'D``.

* **Stage 0b** — Capped same-API Responses/Foundry v1 preflight (one
  small, ordinary, non-spillover call; the request MUST NOT set
  ``x-ms-spillover-deployment``). Hard ceiling
  ``preflight_hard_ceiling_usd: 0.10``; aborted if the dry-run cost
  estimate would exceed it. Emits exactly one Stage 0b verdict:
  ``SAME-API-OK`` or ``SAME-API-FAIL``. Header observation policy: the
  absence of ``x-ms-spillover-from-deployment`` on this non-spillover
  preflight is EXPECTED and is NOT recorded as ``HEADERS-UNSUPPORTED``;
  header presence is recorded as observational data only (names, not
  values, except for deployment aliases that already appear in
  ``experiments/*.yaml``).

* **Stage 0c** — Branching verdict and append-only write to
  ``benchmarks/09-native-spillover/PREFLIGHT_LOG.md``. Produces
  ``benchmarks/09-native-spillover/FEASIBILITY_FINDING.md`` only if
  Stage 0c branches to ``CONFIG-MISSING`` or ``INFEASIBLE-AS-SPEC'D``.

Strict scope (v2.1, this implementation):
    * Stage 0a + Stage 0b ONLY.
    * **No** mutation of Azure resources. Setting
      ``spilloverDeploymentName``, creating PTU deployments, or any
      ``az ... create / update / set`` is unconditionally refused.
    * **No** Stage 1 spillover-fire proof smoke.
    * **No** full comparison run.

Anonymization invariant (enforced by ``redact()`` and a pre-write
check): committed artifacts MUST NOT contain endpoint hostnames,
tenant/subscription IDs, resource group names, resource IDs, auth
headers, bearer tokens, API keys, raw ``az`` CLI JSON, or any
environment-variable values. Only env var **names**, derived booleans,
SKU aliases, and header-name presence are emitted.

CLI::

    python -m scripts.preflight_native_spillover \\
        [--dry-run] [--skip-stage-0b] [--log-path PATH]

Exit codes:
    0 = Stage 0 ran to completion; verdicts recorded.
    2 = anonymization invariant would be violated (write refused).
    3 = mutation attempt detected (refused by spec).
    4 = invalid CLI args.

This script is intentionally self-contained and does not import the
heavy ``measure_dual_spillover`` machinery so it can run cheaply in
CI/dry-run mode without the OpenAI SDK installed.

Sources (last accessed 2026-06-02):
    * Azure spillover doc — https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management
    * Azure Responses API doc — https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ANONYMIZATION_PATTERNS",
    "MUTATING_AZ_VERBS",
    "PREFLIGHT_HARD_CEILING_USD",
    "PUBLIC_DEPLOYMENT_ALIASES",
    "Stage0aResult",
    "Stage0bResult",
    "Stage0Verdict",
    "MutationRefusedError",
    "AnonymizationViolationError",
    "assert_no_secrets",
    "estimate_preflight_cost_usd",
    "main",
    "mark_feasibility_finding_obsolete",
    "normalize_sku_alias",
    "redact",
    "resolve_env_with_dotenv_fallback",
    "run_stage_0a",
    "run_stage_0b",
    "write_preflight_log",
]

logger = logging.getLogger("scripts.preflight_native_spillover")

# Spec-pinned hard ceiling for any Stage 0b spend (USD).
PREFLIGHT_HARD_CEILING_USD: float = 0.10

# Deployment aliases that are already public in ``experiments/*.yaml`` and
# therefore safe to mention by name in committed artifacts.
PUBLIC_DEPLOYMENT_ALIASES: frozenset[str] = frozenset(
    {"gpt-5.2", "ptu-deploy-throttled", "gpt-4o"}
)

# az verbs that imply mutation of an Azure resource. ANY of these in an
# argv list makes ``run_az_readonly`` refuse to execute.
MUTATING_AZ_VERBS: frozenset[str] = frozenset(
    {"create", "update", "set", "delete", "add", "remove", "replace", "patch"}
)

# Anonymization patterns. ``assert_no_secrets`` runs these against any
# text destined for a committed artifact. They are conservative: a
# match means the writer must redact, not that the match is necessarily
# sensitive.
ANONYMIZATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AZURE_OPENAI_ENDPOINT_HOST", re.compile(
        r"[a-z0-9-]+\.(?:openai\.azure\.com|cognitiveservices\.azure\.com|services\.ai\.azure\.com)",
        re.IGNORECASE,
    )),
    ("UUID_LIKE", re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    )),
    ("ARM_RESOURCE_ID", re.compile(
        r"/subscriptions/[^\s\"',]+",
        re.IGNORECASE,
    )),
    ("BEARER_TOKEN", re.compile(
        r"\bBearer\s+[A-Za-z0-9._\-]{20,}",
    )),
    ("JWT_LIKE", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b",
    )),
    ("API_KEY_HEADER", re.compile(
        r"(?:api-key|Ocp-Apim-Subscription-Key)\s*[:=]\s*\S+",
        re.IGNORECASE,
    )),
)


class MutationRefusedError(RuntimeError):
    """Raised when an attempted ``az`` invocation includes a mutating verb."""


class AnonymizationViolationError(RuntimeError):
    """Raised when text destined for a committed artifact matches a redaction pattern."""


@dataclass(frozen=True)
class Stage0aResult:
    """Outcome of Stage 0a (read-only az CLI verification).

    Attributes:
        verdict: One of ``READY_FOR_SMOKE_PROOF`` / ``CONFIG-MISSING`` /
            ``INFEASIBLE-AS-SPEC'D``.
        sku_alias: Normalized SKU alias (e.g., ``PAYG/GlobalStandard``,
            ``PTU/ProvisionedManaged``, ``OTHER``) or ``UNKNOWN`` if the
            CLI was not invoked.
        spillover_deployment_name_present: Boolean — whether the
            primary deployment has ``spilloverDeploymentName`` set.
            ``None`` if not inspected.
        mode_a_property_configured: Boolean — Mode A (property
            configured at deployment level).
        notes: Free-form short note (anonymized) describing why the
            verdict was chosen.
    """

    verdict: str
    sku_alias: str = "UNKNOWN"
    spillover_deployment_name_present: bool | None = None
    mode_a_property_configured: bool = False
    notes: str = ""


@dataclass(frozen=True)
class Stage0bResult:
    """Outcome of Stage 0b (capped same-API preflight).

    Attributes:
        verdict: ``SAME-API-OK`` or ``SAME-API-FAIL``.
        attempted: Whether the network call was actually attempted.
        observed_header_names: Names (only) of response headers observed
            that are relevant to spillover; values redacted unless they
            are deployment aliases in :data:`PUBLIC_DEPLOYMENT_ALIASES`.
        spillover_from_header_present: Whether
            ``x-ms-spillover-from-deployment`` was observed on the
            response. Absence on a non-spillover preflight is EXPECTED
            and is NOT a failure.
        dry_run_cost_estimate_usd: The pre-call cost estimate compared
            against :data:`PREFLIGHT_HARD_CEILING_USD`.
        failure_reason: Anonymized short string when ``verdict``
            is ``SAME-API-FAIL``; empty otherwise.
    """

    verdict: str
    attempted: bool = False
    observed_header_names: tuple[str, ...] = ()
    spillover_from_header_present: bool = False
    dry_run_cost_estimate_usd: float = 0.0
    failure_reason: str = ""


@dataclass(frozen=True)
class Stage0Verdict:
    """Combined Stage 0a + 0b verdict produced by :func:`run_stage_0`."""

    stage_0a: Stage0aResult
    stage_0b: Stage0bResult
    next_action: str  # One of: PROCEED_STAGE_1, PRODUCE_FEASIBILITY_FINDING, FIX_AND_RERUN_STAGE_0B
    feasibility_finding_kind: str = ""  # CONFIG-MISSING | INFEASIBLE-AS-SPEC'D | ""


# ---------------------------------------------------------------------------
# .env loader (in-memory only; values never logged)
# ---------------------------------------------------------------------------


# Env var names that this Stage 0 module is allowed to consider when
# overlaying a local ``.env`` onto the process environment. Other keys
# in the .env file are ignored, so a stray secret cannot accidentally
# enter our in-memory env even if the file gains new entries.
_DOTENV_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "AZURE_OPENAI_FOUNDRY_ENDPOINT",
        "AZURE_OPENAI_RESOURCE_GROUP",
        "AZURE_OPENAI_ACCOUNT_NAME",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2",
        "AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED",
        "AZURE_OPENAI_DEPLOYMENT_GPT_4O",
        "AZURE_AUTH_MODE",
    }
)


def _parse_dotenv_text(text: str) -> dict[str, str]:
    """Parse a minimal subset of .env syntax (KEY=VALUE per line).

    Quoted values (single or double) are unquoted. Lines that are
    blank, start with ``#``, or contain ``export`` prefixes are
    handled. This intentionally does NOT support multi-line values,
    variable expansion, or command substitution — the goal is a small,
    auditable parser, not a full dotenv runtime.

    Args:
        text: Raw file contents.

    Returns:
        Dict of KEY → VALUE for KEYs in :data:`_DOTENV_ALLOWED_KEYS`.
        Values are NEVER logged by this function.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in _DOTENV_ALLOWED_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _load_dotenv_into_memory(path: pathlib.Path) -> dict[str, str]:
    """Load .env file contents into a dict (in-memory only).

    Returns an empty dict if the file is absent or unreadable. Values
    are NEVER printed, logged, or written to disk by this module; they
    only flow into :class:`Stage0aResult` / :class:`Stage0bResult`
    derivations via the merged-env path.
    """
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return _parse_dotenv_text(text)


def resolve_env_with_dotenv_fallback(
    *,
    process_env: dict[str, str] | None = None,
    dotenv_path: pathlib.Path | None = None,
) -> dict[str, str]:
    """Build the in-memory env used by Stage 0a/0b.

    Process environment values WIN over .env values when set to a
    non-empty string. Missing/empty process-env entries are filled
    from the (allow-listed) .env. Values are never logged.

    Args:
        process_env: Defaults to ``os.environ``.
        dotenv_path: Defaults to ``./.env`` relative to CWD.

    Returns:
        Merged env dict containing only the union of process_env and
        allow-listed .env keys. Use this dict everywhere Stage 0 needs
        env input.
    """
    proc = dict(process_env) if process_env is not None else dict(os.environ)
    dot_path = dotenv_path if dotenv_path is not None else pathlib.Path(".env")
    overlay = _load_dotenv_into_memory(dot_path)
    merged: dict[str, str] = {}
    # Start with overlay (low priority).
    for k, v in overlay.items():
        merged[k] = v
    # Process env overrides for the same keys when set non-empty.
    for k, v in proc.items():
        if v:
            merged[k] = v
    # Booleans only — never log values. Record which sources contributed
    # via env-var NAMES only.
    overlay_contributed = sorted(
        k for k in overlay if not proc.get(k)
    )
    if overlay_contributed:
        logger.info(
            "DOTENV_OVERLAY_APPLIED keys=%s",
            ",".join(overlay_contributed),
        )
    return merged


# ---------------------------------------------------------------------------
# Anonymization
# ---------------------------------------------------------------------------


def redact(value: str | None, *, allowlist: frozenset[str] = PUBLIC_DEPLOYMENT_ALIASES) -> str:
    """Reduce a potentially sensitive value to a non-leaking token.

    Rules:
        * ``None`` / empty → ``"<absent>"``.
        * If ``value`` (case-insensitive exact match) is in the
          allowlist of deployment aliases that already appear in
          ``experiments/*.yaml``, return it unchanged.
        * Otherwise → ``"<redacted>"``.

    This function deliberately does NOT hash, truncate, or partially
    reveal the input — partial reveals leak information in aggregate.

    Args:
        value: The raw value (env var content, az JSON field, header
            value, etc.).
        allowlist: Set of strings explicitly safe to reveal.

    Returns:
        A redaction-safe string.
    """
    if value is None or value == "":
        return "<absent>"
    if value in allowlist:
        return value
    return "<redacted>"


def assert_no_secrets(text: str, *, where: str) -> None:
    """Raise :class:`AnonymizationViolationError` if ``text`` matches a redaction pattern.

    Args:
        text: The complete string about to be written to a committed
            artifact.
        where: Short label naming the destination (used in the error
            message; never leaked into the artifact).

    Raises:
        AnonymizationViolationError: If any pattern in
            :data:`ANONYMIZATION_PATTERNS` matches.
    """
    for label, pattern in ANONYMIZATION_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            # Do NOT include the matched substring in the exception —
            # the exception itself may be logged.
            raise AnonymizationViolationError(
                f"anonymization pattern {label!r} matched in text destined for "
                f"{where!r}; refusing to write."
            )


# ---------------------------------------------------------------------------
# az helpers
# ---------------------------------------------------------------------------


def _refuse_if_mutating(argv: list[str]) -> None:
    """Refuse to invoke ``az`` if the argv contains a mutating verb.

    The check is positional-agnostic: any token in ``argv`` whose
    lowercase form is in :data:`MUTATING_AZ_VERBS` triggers refusal.
    This is intentionally conservative — Stage 0 must NEVER mutate.

    Raises:
        MutationRefusedError: If a mutating verb appears.
    """
    for tok in argv:
        if tok.lower() in MUTATING_AZ_VERBS:
            raise MutationRefusedError(
                f"refusing to invoke az: mutating verb {tok!r} present in argv. "
                "Stage 0 is read-only by spec."
            )


def run_az_readonly(
    args: list[str],
    *,
    timeout_seconds: float = 30.0,
    _runner: Any = None,
) -> dict[str, Any] | None:
    """Invoke ``az`` with a read-only argv and return parsed JSON (or None).

    Adds ``--only-show-errors -o json`` if not already present. Refuses
    to run if any token in ``args`` is a mutating verb.

    The returned dict is the parsed CLI JSON; callers are responsible
    for extracting ONLY the booleans/SKU aliases they need and MUST NOT
    write the raw dict to any committed artifact (see
    :func:`assert_no_secrets`).

    Args:
        args: argv after the leading ``az`` (e.g., ``["cognitiveservices",
            "account", "deployment", "show", ...]``).
        timeout_seconds: subprocess timeout.
        _runner: Test seam; if provided, called as
            ``_runner(full_argv, timeout=...)`` and expected to return
            an object with ``returncode``, ``stdout``, ``stderr``.

    Returns:
        Parsed JSON dict, or None if the CLI errored / returned empty
        / parsing failed. The function never raises on CLI failure;
        callers branch on ``None``.

    Raises:
        MutationRefusedError: If a mutating verb is in ``args``.
    """
    _refuse_if_mutating(args)
    full_argv: list[str] = ["az", *args]
    if "-o" not in args and "--output" not in args:
        full_argv += ["-o", "json"]
    if "--only-show-errors" not in args:
        full_argv += ["--only-show-errors"]

    runner = _runner
    if runner is None:
        if shutil.which("az") is None:
            logger.info("AZ_CLI_ABSENT — skipping read-only inspection")
            return None
        runner = lambda argv, timeout: subprocess.run(  # noqa: E731
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    try:
        proc = runner(full_argv, timeout=timeout_seconds)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info("AZ_INVOCATION_FAILED type=%s", type(exc).__name__)
        return None

    if getattr(proc, "returncode", 1) != 0:
        # Do NOT log proc.stderr — may contain identifiers.
        logger.info("AZ_NONZERO_RETURN rc=%d", proc.returncode)
        return None
    stdout = getattr(proc, "stdout", "") or ""
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        logger.info("AZ_JSON_PARSE_FAILED")
        return None


def normalize_sku_alias(sku_field: Any) -> str:
    """Reduce an az ``sku`` field to one of a small set of coarse aliases.

    The az response shape for ``cognitiveservices account deployment
    show`` varies across CLI versions; this function accepts either a
    dict (``{"name": ..., "tier": ...}``) or a string and returns a
    short, low-cardinality alias safe to write to a committed artifact.

    Returns:
        One of: ``PAYG/GlobalStandard``, ``PAYG/Standard``,
        ``PTU/ProvisionedManaged``, ``PTU/GlobalProvisionedManaged``,
        ``OTHER``, ``UNKNOWN``.
    """
    if sku_field is None:
        return "UNKNOWN"
    if isinstance(sku_field, dict):
        name = str(sku_field.get("name") or sku_field.get("tier") or "").strip()
    else:
        name = str(sku_field).strip()
    if not name:
        return "UNKNOWN"
    lname = name.lower()
    if lname in {"globalstandard", "global-standard", "global_standard"}:
        return "PAYG/GlobalStandard"
    if lname == "standard":
        return "PAYG/Standard"
    if lname in {"provisionedmanaged", "provisioned-managed", "provisioned_managed"}:
        return "PTU/ProvisionedManaged"
    if lname in {
        "globalprovisionedmanaged",
        "global-provisioned-managed",
        "global_provisioned_managed",
    }:
        return "PTU/GlobalProvisionedManaged"
    return "OTHER"


# ---------------------------------------------------------------------------
# Stage 0a
# ---------------------------------------------------------------------------


def _discover_deployment_location(
    deployment_name: str,
    *,
    az_runner: Any = None,
    max_accounts_scanned: int = 64,
) -> tuple[str, str] | None:
    """Find the (resource_group, account_name) hosting ``deployment_name``.

    Strategy (all read-only):
        1. ``az cognitiveservices account list`` — enumerate accounts.
        2. For each account, ``az cognitiveservices account deployment
           list --resource-group RG --name ACCOUNT`` and check whether
           any deployment ``name`` matches ``deployment_name`` exactly.

    Returns the pair ONLY if exactly one account hosts a deployment
    with the requested name. Returns ``None`` if there are zero or
    multiple matches, if the CLI is absent, or if any call errors —
    callers MUST treat ``None`` as "discovery failed; record derived
    failure only" and never reveal account/RG names.

    The returned tuple values themselves are sensitive (resource group
    + account name) and MUST be consumed only as parameters to a
    further read-only ``az`` call. They MUST NOT be written to any
    committed artifact.
    """
    if not deployment_name:
        return None
    accounts = run_az_readonly(
        ["cognitiveservices", "account", "list"], _runner=az_runner
    )
    if not isinstance(accounts, list) or not accounts:
        return None
    matches: list[tuple[str, str]] = []
    scanned = 0
    for acct in accounts:
        if scanned >= max_accounts_scanned:
            break
        scanned += 1
        if not isinstance(acct, dict):
            continue
        rg = str(acct.get("resourceGroup") or "").strip()
        name = str(acct.get("name") or "").strip()
        if not rg or not name:
            continue
        deps = run_az_readonly(
            [
                "cognitiveservices",
                "account",
                "deployment",
                "list",
                "--resource-group",
                rg,
                "--name",
                name,
            ],
            _runner=az_runner,
        )
        if not isinstance(deps, list):
            continue
        for d in deps:
            if isinstance(d, dict) and str(d.get("name") or "") == deployment_name:
                matches.append((rg, name))
                break
        if len(matches) > 1:
            # Ambiguous — bail early without revealing anything.
            return None
    if len(matches) == 1:
        return matches[0]
    return None


def run_stage_0a(
    *,
    env: dict[str, str] | None = None,
    az_runner: Any = None,
) -> Stage0aResult:
    """Execute Stage 0a (read-only az CLI verification).

    Reads env vars to identify the deployment to inspect:

        * ``AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED`` — primary
          deployment alias (the same env var the Task 013 runner uses).
          REQUIRED. Without it, Stage 0a cannot proceed.
        * ``AZURE_OPENAI_RESOURCE_GROUP`` — resource group name.
        * ``AZURE_OPENAI_ACCOUNT_NAME`` — Cognitive Services account
          name.

    If RG / account name are absent but the deployment alias is
    present, Stage 0a performs a read-only Azure CLI **discovery**:
    enumerates Cognitive Services accounts (``cognitiveservices
    account list``) and looks for exactly one account hosting a
    deployment with that alias (``cognitiveservices account
    deployment list``). If discovery returns exactly one match, the
    derived (rg, account) flows ONLY into the subsequent ``az ...
    deployment show`` call and never into any committed artifact. If
    discovery is ambiguous (0 or ≥2 matches) or fails, Stage 0a
    returns ``CONFIG-MISSING`` with a redacted note.

    Verdict rules are unchanged: only Mode A (deployment-level
    ``spilloverDeploymentName`` set) + PTU SKU yields
    ``READY_FOR_SMOKE_PROOF``.

    Args:
        env: Process environment (defaults to ``os.environ``).
        az_runner: Test seam forwarded to :func:`run_az_readonly`.

    Returns:
        :class:`Stage0aResult`.
    """
    src = env if env is not None else os.environ

    rg = src.get("AZURE_OPENAI_RESOURCE_GROUP", "").strip()
    account = src.get("AZURE_OPENAI_ACCOUNT_NAME", "").strip()
    deployment = src.get("AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED", "").strip()

    if not deployment:
        missing_envs: list[str] = []
        if not rg:
            missing_envs.append("AZURE_OPENAI_RESOURCE_GROUP")
        if not account:
            missing_envs.append("AZURE_OPENAI_ACCOUNT_NAME")
        missing_envs.append("AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED")
        return Stage0aResult(
            verdict="CONFIG-MISSING",
            sku_alias="UNKNOWN",
            spillover_deployment_name_present=None,
            mode_a_property_configured=False,
            notes=(
                "missing env vars (names only): "
                + ",".join(sorted(missing_envs))
                + "; cannot run read-only az cognitiveservices account deployment show; "
                "Modes A/B/C unverifiable from this environment."
            ),
        )

    discovery_used = False
    if not rg or not account:
        discovered = _discover_deployment_location(deployment, az_runner=az_runner)
        if discovered is None:
            return Stage0aResult(
                verdict="CONFIG-MISSING",
                sku_alias="UNKNOWN",
                spillover_deployment_name_present=None,
                mode_a_property_configured=False,
                notes=(
                    "AZURE_OPENAI_ACCOUNT_NAME and/or AZURE_OPENAI_RESOURCE_GROUP "
                    "absent; read-only az discovery (cognitiveservices account list + "
                    "deployment list) did not return exactly one match for the "
                    "configured deployment alias; identifiers redacted; "
                    "Modes A/B/C unverifiable."
                ),
            )
        rg, account = discovered
        discovery_used = True

    payload = run_az_readonly(
        [
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--resource-group",
            rg,
            "--name",
            account,
            "--deployment-name",
            deployment,
        ],
        _runner=az_runner,
    )

    if payload is None:
        return Stage0aResult(
            verdict="CONFIG-MISSING",
            sku_alias="UNKNOWN",
            spillover_deployment_name_present=None,
            mode_a_property_configured=False,
            notes=(
                "az read-only inspection did not return a parsable payload "
                "(CLI absent, nonzero exit, or empty output); "
                "Mode A/B/C unverifiable. Identifiers redacted."
            ),
        )

    # Extract ONLY the two fields we need; never retain the raw payload.
    sku_alias = normalize_sku_alias(payload.get("sku"))
    properties = payload.get("properties") or {}
    raw_spillover = properties.get("spilloverDeploymentName")
    mode_a = bool(raw_spillover)
    is_ptu = sku_alias.startswith("PTU/")

    discovery_suffix = (
        " (account/RG resolved via read-only az discovery; values redacted)"
        if discovery_used
        else ""
    )

    if mode_a and is_ptu:
        verdict = "READY_FOR_SMOKE_PROOF"
        notes = (
            f"Mode A satisfied (spilloverDeploymentName present, value redacted); "
            f"primary SKU alias={sku_alias} satisfies the spillover doc's "
            "PTU-primary requirement." + discovery_suffix
        )
    elif mode_a and not is_ptu:
        verdict = "CONFIG-MISSING"
        notes = (
            f"Mode A satisfied but primary SKU alias={sku_alias} is not PTU; "
            "the spillover doc (accessed 2026-06-02) requires PTU primary → "
            "standard target. Owner OPTIN would be required to relax."
            + discovery_suffix
        )
    elif sku_alias == "UNKNOWN":
        verdict = "CONFIG-MISSING"
        notes = (
            "primary SKU not parsable from az response; Modes A/B/C "
            "unverifiable. Identifiers redacted." + discovery_suffix
        )
    elif not is_ptu:
        verdict = "INFEASIBLE-AS-SPEC'D"
        notes = (
            f"primary SKU alias={sku_alias} is not PTU and spilloverDeploymentName "
            "not configured; per the spillover doc (accessed 2026-06-02) "
            "native spillover requires PTU primary → standard target. "
            "No owner OPTIN to provision a PTU primary." + discovery_suffix
        )
    else:
        verdict = "CONFIG-MISSING"
        notes = (
            f"primary SKU alias={sku_alias} but spilloverDeploymentName not set; "
            "Modes A/B unconfigured and no Mode C OPTIN." + discovery_suffix
        )

    return Stage0aResult(
        verdict=verdict,
        sku_alias=sku_alias,
        spillover_deployment_name_present=mode_a,
        mode_a_property_configured=mode_a,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Stage 0b
# ---------------------------------------------------------------------------


# Header names (lowercased) that the preflight may observe; only names
# (not values) are written to PREFLIGHT_LOG.md, except deployment-alias
# values in PUBLIC_DEPLOYMENT_ALIASES.
RELEVANT_HEADER_NAMES: tuple[str, ...] = (
    "x-ms-spillover-from-deployment",
    "x-ms-deployment-name",
    "x-ms-spillover-error",
)


def estimate_preflight_cost_usd(
    *,
    input_tokens: int = 16,
    output_tokens: int = 16,
    input_per_1m_usd: float = 2.0,
    output_per_1m_usd: float = 10.0,
) -> float:
    """Pessimistic dry-run cost estimate for the single Stage 0b call.

    Uses an intentionally conservative upper-bound rate (defaults
    chosen to be above current gpt-5.2 PAYG list rates). The full
    cost-calculator pipeline is not invoked at preflight time because
    Stage 0 must remain runnable without the pricing snapshot loaded
    in memory; this estimate exists solely to enforce the
    :data:`PREFLIGHT_HARD_CEILING_USD` halt.

    Note:
        This estimator is a conservative budget guard, not an exact
        token-accounting model. The actual single Stage 0b call (a
        16-token "ping" with ``effort="low"`` and ``max_output_tokens=16``)
        runs far below the ``$0.10`` ceiling at any plausible list rate;
        the over-estimate exists to fail-closed if list rates ever spike
        beyond the defaults baked in here.

    Args:
        input_tokens: Pessimistic input-token budget for the one call.
        output_tokens: Pessimistic output-token budget (includes any
            reasoning tokens).
        input_per_1m_usd: Per-million-token input rate.
        output_per_1m_usd: Per-million-token output rate.

    Returns:
        USD cost estimate for the single preflight call.
    """
    return (
        input_tokens * input_per_1m_usd + output_tokens * output_per_1m_usd
    ) / 1_000_000.0


def run_stage_0b(
    *,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    _call_responses: Any = None,
) -> Stage0bResult:
    """Execute Stage 0b (capped same-API Responses/Foundry v1 preflight).

    Issues exactly ONE small, ordinary, non-spillover Responses API
    call against the primary deployment via the same code path the
    smoke would use. The request MUST NOT set
    ``x-ms-spillover-deployment``.

    Args:
        env: Process environment (defaults to ``os.environ``).
        dry_run: If True, skip the network call and return
            ``SAME-API-FAIL`` with ``attempted=False`` and a
            ``failure_reason`` noting dry-run.
        _call_responses: Test seam. If provided, called as
            ``await _call_responses(endpoint_redacted, deployment)``
            and expected to return a dict with keys ``headers`` (case-
            insensitive header mapping) and ``status_code``.

    Returns:
        :class:`Stage0bResult`.

    Notes:
        * If env/auth is missing, returns ``SAME-API-FAIL`` with a
          short anonymized reason (env var **names** only).
        * If the dry-run cost estimate exceeds
          :data:`PREFLIGHT_HARD_CEILING_USD`, the call is aborted
          before any network I/O and ``SAME-API-FAIL`` is returned.
        * Absence of ``x-ms-spillover-from-deployment`` on a
          non-spillover preflight is EXPECTED and is NOT recorded as
          ``HEADERS-UNSUPPORTED`` (spec §Stage 0b header observation
          policy).
    """
    src = env if env is not None else os.environ
    endpoint = src.get("AZURE_OPENAI_FOUNDRY_ENDPOINT", "").strip()
    deployment = src.get("AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED", "").strip()

    missing: list[str] = []
    if not endpoint:
        missing.append("AZURE_OPENAI_FOUNDRY_ENDPOINT")
    if not deployment:
        missing.append("AZURE_OPENAI_DEPLOYMENT_GPT_5_2_THROTTLED")
    if missing:
        return Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=False,
            failure_reason=(
                "required env vars absent (names only): "
                + ",".join(sorted(missing))
                + "; no network call attempted; no identifier values leaked."
            ),
        )

    estimate = estimate_preflight_cost_usd()
    if estimate > PREFLIGHT_HARD_CEILING_USD:
        return Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=False,
            dry_run_cost_estimate_usd=estimate,
            failure_reason=(
                f"dry-run cost estimate ${estimate:.4f} exceeds "
                f"preflight_hard_ceiling_usd=${PREFLIGHT_HARD_CEILING_USD:.4f}; "
                "aborted before any network I/O."
            ),
        )

    if dry_run:
        return Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=False,
            dry_run_cost_estimate_usd=estimate,
            failure_reason="dry_run=True; preflight network call deliberately skipped.",
        )

    # ---- Network call path ----
    try:
        if _call_responses is not None:
            response_info: dict[str, Any] = _call_responses(endpoint, deployment)
        else:
            response_info = _live_responses_call(endpoint, deployment)
    except Exception as exc:  # noqa: BLE001
        # Anonymize: include only the exception class name, never repr/args
        # (which may contain endpoint hostnames or tokens).
        return Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=True,
            dry_run_cost_estimate_usd=estimate,
            failure_reason=f"exception_class={type(exc).__name__}; details suppressed for anonymization.",
        )

    status_code = int(response_info.get("status_code") or 0)
    raw_headers = response_info.get("headers") or {}
    # Normalize header names to lowercase for membership check.
    observed = tuple(
        sorted(
            name.lower()
            for name in raw_headers
            if name.lower() in RELEVANT_HEADER_NAMES
        )
    )
    spillover_from_present = "x-ms-spillover-from-deployment" in observed

    if status_code != 200:
        return Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=True,
            observed_header_names=observed,
            spillover_from_header_present=spillover_from_present,
            dry_run_cost_estimate_usd=estimate,
            failure_reason=f"non_200_status_code={status_code}",
        )

    return Stage0bResult(
        verdict="SAME-API-OK",
        attempted=True,
        observed_header_names=observed,
        spillover_from_header_present=spillover_from_present,
        dry_run_cost_estimate_usd=estimate,
        failure_reason="",
    )


def _live_responses_call(endpoint: str, deployment: str) -> dict[str, Any]:
    """Issue exactly one small non-spillover Responses API call (live).

    Uses ``AsyncOpenAI`` + ``DefaultAzureCredential`` via
    ``azure.identity.aio.get_bearer_token_provider``, mirroring
    :func:`scripts.measure_dual_spillover.preflight_reachability`'s
    call shape exactly so this preflight exercises the SAME code path
    the runner would. The endpoint string is NOT logged.

    Args:
        endpoint: Foundry v1 base endpoint (read locally, never logged).
        deployment: Deployment alias (already in
            :data:`PUBLIC_DEPLOYMENT_ALIASES`).

    Returns:
        A dict ``{"status_code": int, "headers": dict[str, str]}``
        suitable for :func:`run_stage_0b` consumption.

    Raises:
        Whatever the SDK raises; caller anonymizes.
    """
    import asyncio

    from azure.identity.aio import (  # noqa: PLC0415
        DefaultAzureCredential,
        get_bearer_token_provider,
    )
    from openai import AsyncOpenAI  # noqa: PLC0415

    async def _call() -> dict[str, Any]:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://ai.azure.com/.default",
        )
        base_url = endpoint.rstrip("/") + "/openai/v1/"
        client = AsyncOpenAI(base_url=base_url, api_key=token_provider)
        raw = client.responses.with_raw_response
        # Foundry v1 rejects max_output_tokens < 16; effort="low" is the
        # smallest non-zero effort accepted by gpt-5.2.
        resp = await raw.create(
            model=deployment,
            input="ping",
            max_output_tokens=16,
            reasoning={"effort": "low"},
        )
        headers = {k: v for k, v in (resp.headers or {}).items()}
        status_code = int(getattr(resp, "status_code", 0) or 0)
        return {"status_code": status_code, "headers": headers}

    return asyncio.run(_call())


# ---------------------------------------------------------------------------
# Stage 0c — combine + branch
# ---------------------------------------------------------------------------


def _decide_next_action(a: Stage0aResult, b: Stage0bResult) -> Stage0Verdict:
    """Combine Stage 0a + 0b results into the Stage 0c branching verdict."""
    if a.verdict == "READY_FOR_SMOKE_PROOF" and b.verdict == "SAME-API-OK":
        return Stage0Verdict(
            stage_0a=a, stage_0b=b, next_action="PROCEED_STAGE_1"
        )
    if a.verdict == "INFEASIBLE-AS-SPEC'D":
        return Stage0Verdict(
            stage_0a=a,
            stage_0b=b,
            next_action="PRODUCE_FEASIBILITY_FINDING",
            feasibility_finding_kind="INFEASIBLE-AS-SPEC'D",
        )
    if a.verdict == "CONFIG-MISSING" and b.verdict == "SAME-API-OK":
        return Stage0Verdict(
            stage_0a=a,
            stage_0b=b,
            next_action="PRODUCE_FEASIBILITY_FINDING",
            feasibility_finding_kind="CONFIG-MISSING",
        )
    # CONFIG-MISSING + SAME-API-FAIL is not explicitly defined by the
    # spec; the spec says SAME-API-FAIL alone must not produce a
    # FEASIBILITY_FINDING, but we still surface the CONFIG-MISSING
    # finding because the same-API issue may be a separate fix.
    if a.verdict == "CONFIG-MISSING" and b.verdict == "SAME-API-FAIL":
        return Stage0Verdict(
            stage_0a=a,
            stage_0b=b,
            next_action="PRODUCE_FEASIBILITY_FINDING",
            feasibility_finding_kind="CONFIG-MISSING",
        )
    # READY_FOR_SMOKE_PROOF + SAME-API-FAIL → fix and rerun Stage 0b.
    return Stage0Verdict(
        stage_0a=a, stage_0b=b, next_action="FIX_AND_RERUN_STAGE_0B"
    )


# ---------------------------------------------------------------------------
# PREFLIGHT_LOG.md writer
# ---------------------------------------------------------------------------


PREFLIGHT_LOG_HEADER = """# benchmarks/09-native-spillover/PREFLIGHT_LOG.md

> Task 021 v2.1 — Stage 0 pre-flight log. **Append-only.** Each Stage 0
> run appends one timestamped section. No mutation of Azure resources is
> performed by this log or the script that writes it. No endpoint
> hostnames, tenant/subscription IDs, resource group names, resource IDs,
> auth tokens, bearer tokens, API keys, raw `az` CLI JSON, or
> environment-variable values are recorded — only derived booleans, SKU
> aliases, and header-name presence.

> Sources (last accessed 2026-06-02):
> - Azure spillover doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management>
> - Azure Responses API doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses>
"""


def _format_section(verdict: Stage0Verdict, *, timestamp_iso: str, git_commit: str) -> str:
    """Render one append-only PREFLIGHT_LOG.md section for a Stage 0 run."""
    a = verdict.stage_0a
    b = verdict.stage_0b
    header_names = ", ".join(b.observed_header_names) if b.observed_header_names else "<none>"
    spillover_from_note = (
        "present (preflight unexpectedly traversed a spillover path)"
        if b.spillover_from_header_present
        else "absent (EXPECTED on a non-spillover preflight; NOT a HEADERS-UNSUPPORTED finding)"
    )
    # CONFIG-MISSING + SAME-API-FAIL is not explicitly defined by the
    # spec, but Stage 0c still emits a CONFIG-MISSING feasibility
    # finding here. Annotate the log so a reader does not mistake the
    # Stage 0b failure for the sole basis of the feasibility verdict.
    same_api_fail_clarifier = ""
    if (
        verdict.next_action == "PRODUCE_FEASIBILITY_FINDING"
        and verdict.feasibility_finding_kind == "CONFIG-MISSING"
        and b.verdict == "SAME-API-FAIL"
    ):
        same_api_fail_clarifier = (
            "- note: Stage 0b SAME-API-FAIL is a separate "
            "reachability/config fix item (env/auth/network) and is NOT "
            "the basis of the CONFIG-MISSING feasibility finding; the "
            "feasibility verdict is grounded in Stage 0a Mode-A/B/C status.\n"
        )
    return (
        f"\n## Stage 0 run — {timestamp_iso}\n\n"
        f"- git_commit: `{git_commit}`\n"
        f"- preflight_hard_ceiling_usd: `${PREFLIGHT_HARD_CEILING_USD:.2f}`\n\n"
        "### Stage 0a — read-only az CLI verification\n\n"
        f"- verdict: **{a.verdict}**\n"
        f"- sku_alias: `{a.sku_alias}`\n"
        f"- spillover_deployment_name_present: `{a.spillover_deployment_name_present}`\n"
        f"- mode_a_property_configured: `{a.mode_a_property_configured}`\n"
        f"- notes: {a.notes}\n\n"
        "### Stage 0b — capped same-API Responses/Foundry v1 preflight\n\n"
        f"- verdict: **{b.verdict}**\n"
        f"- attempted: `{b.attempted}`\n"
        f"- dry_run_cost_estimate_usd: `${b.dry_run_cost_estimate_usd:.4f}` "
        f"(ceiling `${PREFLIGHT_HARD_CEILING_USD:.2f}`)\n"
        f"- observed_relevant_header_names: `{header_names}`\n"
        f"- x-ms-spillover-from-deployment: {spillover_from_note}\n"
        f"- failure_reason: {b.failure_reason or '<none>'}\n\n"
        "### Stage 0c — branching verdict\n\n"
        f"- next_action: **{verdict.next_action}**\n"
        f"- feasibility_finding_kind: `{verdict.feasibility_finding_kind or '<none>'}`\n"
        f"{same_api_fail_clarifier}"
    )


def write_preflight_log(
    log_path: pathlib.Path,
    verdict: Stage0Verdict,
    *,
    timestamp_iso: str,
    git_commit: str,
) -> None:
    """Append (or create) the PREFLIGHT_LOG.md with this run's Stage 0 section.

    Runs :func:`assert_no_secrets` against the FULL post-write content
    before any disk write, so a redaction failure aborts cleanly
    without leaving a partial committed artifact.

    Args:
        log_path: Destination path
            (``benchmarks/09-native-spillover/PREFLIGHT_LOG.md``).
        verdict: Combined Stage 0c verdict.
        timestamp_iso: UTC ISO-8601 ``Z``-suffixed timestamp.
        git_commit: Short or long git commit SHA captured at run start.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    section = _format_section(verdict, timestamp_iso=timestamp_iso, git_commit=git_commit)
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")
        new_content = existing.rstrip("\n") + "\n" + section
    else:
        new_content = PREFLIGHT_LOG_HEADER.rstrip("\n") + "\n" + section
    assert_no_secrets(new_content, where=str(log_path.name))
    tmp = log_path.with_name(log_path.name + f".tmp.{os.getpid()}")
    tmp.write_text(new_content, encoding="utf-8")
    os.replace(tmp, log_path)


def mark_feasibility_finding_obsolete(
    path: pathlib.Path,
    *,
    timestamp_iso: str,
    git_commit: str,
    reason: str = "Stage 0c subsequently reached READY_FOR_SMOKE_PROOF + SAME-API-OK.",
) -> bool:
    """Rewrite a stale FEASIBILITY_FINDING.md as an obsolete-note marker.

    Called when Stage 0c on a subsequent run resolves to
    ``PROCEED_STAGE_1`` (i.e., the previously written CONFIG-MISSING /
    INFEASIBLE-AS-SPEC'D verdict is no longer the latest finding). The
    file is rewritten — never deleted — so the git history retains the
    transition. Stage 1 is NOT executed by this function.

    Returns:
        True if the file existed and was rewritten, False if no
        action was needed (file absent).
    """
    if not path.exists():
        return False
    body = (
        f"# Task 021 v2.1 — Feasibility Finding (OBSOLETE)\n\n"
        f"**Timestamp (UTC):** {timestamp_iso}\n"
        f"**Git commit:** `{git_commit}`\n"
        f"**Status:** OBSOLETE — superseded by a later Stage 0 run.\n\n"
        f"## Why this file is obsolete\n\n"
        f"{reason}\n\n"
        "See `PREFLIGHT_LOG.md` (append-only) for the latest Stage 0 "
        "run's verdicts. The most recent run's Stage 0c branch resolves "
        "to `PROCEED_STAGE_1`, meaning the prior CONFIG-MISSING / "
        "INFEASIBLE-AS-SPEC'D finding no longer reflects the observed "
        "environment.\n\n"
        "Stage 1 (spillover-fire proof smoke) has NOT been executed by "
        "this update. Task 021 remains under Stage 0 control until "
        "explicitly promoted.\n\n"
        "## Sources (last accessed 2026-06-02)\n\n"
        "- Azure spillover doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management>\n"
        "- Azure Responses API doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses>\n"
    )
    assert_no_secrets(body, where=str(path.name))
    path.write_text(body, encoding="utf-8")
    return True


def write_feasibility_finding(
    path: pathlib.Path,
    verdict: Stage0Verdict,
    *,
    timestamp_iso: str,
    git_commit: str,
) -> None:
    """Produce FEASIBILITY_FINDING.md when Stage 0c branches accordingly.

    The finding cites the spillover doc URL + 2026-06-02 access date
    (and the Responses-API URL only where API path/context is the
    source). No identifiers are leaked.
    """
    kind = verdict.feasibility_finding_kind
    a = verdict.stage_0a
    b = verdict.stage_0b

    # ---- Per-mode polished wording (derived from data, not free-form) ----
    # Mode A: deployment-level property + PTU-primary requirement.
    sku_is_ptu = a.sku_alias.startswith("PTU/")
    if a.mode_a_property_configured and sku_is_ptu:
        mode_a_line = (
            "**Mode A** — observed **present**: `spilloverDeploymentName` "
            "is set on the inspected deployment resource and the primary "
            f"SKU alias (`{a.sku_alias}`) is a PTU primary as required by "
            "the spillover doc."
        )
    elif a.mode_a_property_configured and not sku_is_ptu:
        mode_a_line = (
            "**Mode A** — observed **absent**: `spilloverDeploymentName` "
            "is set on the inspected deployment resource, but the primary "
            f"SKU alias (`{a.sku_alias}`) is not a PTU primary as required "
            "by the spillover doc."
        )
    elif a.spillover_deployment_name_present is None:
        mode_a_line = (
            "**Mode A** — **unverifiable** under this preflight: read-only "
            "discovery did not return a deployment object (likely missing "
            f"env/config); SKU alias `{a.sku_alias}`."
        )
    else:
        mode_a_line = (
            "**Mode A** — observed **absent**: `spilloverDeploymentName` "
            "is not set on the inspected deployment resource, and the "
            f"primary SKU alias (`{a.sku_alias}`) is not a PTU primary as "
            "required by the spillover doc."
        )

    # Mode B: never exercised under the feasibility gate.
    mode_b_line = (
        "**Mode B** — **not exercised / not proven** under this feasibility "
        "gate: Stage 0b issues exactly one ordinary non-spillover Responses "
        "API call and MUST NOT set `x-ms-spillover-deployment`; Stage 1 "
        "spillover-fire proof smoke and the full head-to-head comparison "
        "were not executed. Absence here is a scope statement, not a claim "
        "that Mode B is impossible."
    )

    # Mode C: owner-approved Azure mutation; never granted by this preflight.
    mode_c_line = (
        "**Mode C** — **not granted**: no owner opt-in has been granted to "
        "provision a PTU primary or to set `spilloverDeploymentName`; no "
        "Azure resources were mutated by this preflight."
    )

    # ---- CONFIG-MISSING + SAME-API-FAIL clarifier ----
    # Per Stage 0c branching, this combination still emits a
    # FEASIBILITY_FINDING for the CONFIG-MISSING side, but the
    # SAME-API-FAIL is a separate reachability/config fix item and
    # NOT the basis of the feasibility finding.
    same_api_fail_clarifier = ""
    if kind == "CONFIG-MISSING" and b.verdict == "SAME-API-FAIL":
        same_api_fail_clarifier = (
            "\n> **Note on Stage 0b SAME-API-FAIL:** the Stage 0b "
            "reachability failure above is a *separate* "
            "reachability/config fix item (e.g., missing env vars, auth, "
            "or transient network/SDK error). It is NOT the basis of this "
            "feasibility finding — the CONFIG-MISSING verdict is grounded "
            "in the Stage 0a Mode-A/B/C status below.\n"
        )

    body = f"""# Task 021 v2.1 — Feasibility Finding ({kind})

**Timestamp (UTC):** {timestamp_iso}
**Git commit:** `{git_commit}`
**Outcome class:** {kind}
**Task 021 status:** closed at Stage 0 (feasibility-closed DoD per spec §"Definition of Done" option A).

## Stage 0a (read-only)

- Verdict: **{a.verdict}**
- SKU alias: `{a.sku_alias}`
- `spilloverDeploymentName` present: `{a.spillover_deployment_name_present}`
- Notes: {a.notes}

## Stage 0b (capped same-API preflight)

- Verdict: **{b.verdict}**
- Network call attempted: `{b.attempted}`
- Failure reason: {b.failure_reason or '<none>'}
{same_api_fail_clarifier}
## Configuration modes (per spillover doc)

Per the Azure spillover doc (accessed 2026-06-02), native spillover
fires when one of:

- **Mode A** — the target deployment has `spilloverDeploymentName` set
  on the deployment resource (deployment-level default), OR
- **Mode B** — the request explicitly sets the
  `x-ms-spillover-deployment` header to a valid sibling deployment
  alias, OR
- **Mode C** — owner-approved mutation provisioning Mode A for the
  experiment.

Status against the current deployment, derived from Stage 0a evidence
above and Stage 0b scope:

- {mode_a_line}
- {mode_b_line}
- {mode_c_line}

## Sources (last accessed 2026-06-02)

- Azure spillover doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management>
- Azure Responses API doc — <https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses> (cited only for API path/context, not for native-spillover semantics)

## Closure

Task 021 v2.1 closes at this finding. Task 022 may cite this file
instead of a head-to-head comparison.
"""
    assert_no_secrets(body, where=str(path.name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _git_commit_redacted() -> str:
    """Return short git commit SHA (12 chars) or ``UNKNOWN``. Safe to log."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "UNKNOWN"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = argparse.ArgumentParser(
        prog="preflight_native_spillover",
        description="Task 021 v2.1 Stage 0 (read-only + capped same-API preflight).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the Stage 0b network call; still write log.",
    )
    parser.add_argument(
        "--skip-stage-0b",
        action="store_true",
        help="Skip Stage 0b entirely; record SAME-API-FAIL with reason 'skipped'.",
    )
    parser.add_argument(
        "--log-path",
        type=pathlib.Path,
        default=pathlib.Path("benchmarks/09-native-spillover/PREFLIGHT_LOG.md"),
    )
    parser.add_argument(
        "--finding-path",
        type=pathlib.Path,
        default=pathlib.Path("benchmarks/09-native-spillover/FEASIBILITY_FINDING.md"),
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Suppress third-party INFO logs that may include endpoint hostnames,
    # bearer-token request URLs, or other identifiers. These libraries
    # are noisy at INFO level and their messages bypass this script's
    # own redaction discipline. Raised to WARNING regardless of whether
    # the SDK actually fires a request.
    for noisy in (
        "azure",
        "azure.identity",
        "azure.identity.aio",
        "azure.core",
        "azure.core.pipeline.policies.http_logging_policy",
        "httpx",
        "httpcore",
        "openai",
        "asyncio",
        "aiohttp",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    resolved_env = resolve_env_with_dotenv_fallback()

    a = run_stage_0a(env=resolved_env)
    if args.skip_stage_0b:
        b = Stage0bResult(
            verdict="SAME-API-FAIL",
            attempted=False,
            failure_reason="--skip-stage-0b CLI flag set; no network call attempted.",
        )
    else:
        b = run_stage_0b(env=resolved_env, dry_run=args.dry_run)

    verdict = _decide_next_action(a, b)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_commit = _git_commit_redacted()

    try:
        write_preflight_log(args.log_path, verdict, timestamp_iso=ts, git_commit=git_commit)
    except AnonymizationViolationError as exc:
        logger.error("ANONYMIZATION_REFUSED %s", exc)
        return 2

    feasibility_finding_obsoleted = False
    if verdict.next_action == "PRODUCE_FEASIBILITY_FINDING":
        try:
            write_feasibility_finding(
                args.finding_path,
                verdict,
                timestamp_iso=ts,
                git_commit=git_commit,
            )
        except AnonymizationViolationError as exc:
            logger.error("ANONYMIZATION_REFUSED %s", exc)
            return 2
    elif verdict.next_action == "PROCEED_STAGE_1":
        # Latest Stage 0c is feasibility-OPEN (READY + SAME-API-OK).
        # Mark any stale FEASIBILITY_FINDING.md as obsolete; do NOT
        # execute Stage 1 — promotion to Stage 1 remains an explicit
        # human/CLI step out of this script's scope.
        try:
            feasibility_finding_obsoleted = mark_feasibility_finding_obsolete(
                args.finding_path,
                timestamp_iso=ts,
                git_commit=git_commit,
            )
        except AnonymizationViolationError as exc:
            logger.error("ANONYMIZATION_REFUSED %s", exc)
            return 2

    # Final stdout summary (anonymized). The CLI summary is the only
    # place this script uses print() per repo style.
    print(
        json.dumps(
            {
                "stage_0a_verdict": a.verdict,
                "stage_0a_sku_alias": a.sku_alias,
                "stage_0a_mode_a_property_configured": a.mode_a_property_configured,
                "stage_0b_verdict": b.verdict,
                "stage_0b_attempted": b.attempted,
                "stage_0b_dry_run_cost_estimate_usd": b.dry_run_cost_estimate_usd,
                "stage_0b_spillover_from_header_present": b.spillover_from_header_present,
                "next_action": verdict.next_action,
                "feasibility_finding_kind": verdict.feasibility_finding_kind,
                "feasibility_finding_obsoleted": feasibility_finding_obsoleted,
                "preflight_hard_ceiling_usd": PREFLIGHT_HARD_CEILING_USD,
                "stage_1_proof_smoke_executed": False,
                "full_comparison_executed": False,
                "azure_mutation_performed": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

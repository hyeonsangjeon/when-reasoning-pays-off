"""scripts/task019_v25_adaptive.py — Task 019 v2.5 adaptive Stage 0.5.C
calibration contrast (microfix #1 + microfix #2 PINNED).

Single source of truth for v2.5 RFC values, schema strings, validators,
and pure planner / evaluator helpers. See
``.internal/tasks/019-v2.5-adaptive-contrast.md`` §0–§16 for the
binding spec; every PINNED value below cites its §10 row.

This module is import-safe (no network, no environment-variable reads,
no fork into subprocesses at import) so that
``scripts/measure_max_output_tokens_sweep.py`` can import it for a
runtime constants-regression assertion (§11.41).

PAYG, NOT PTU — every PAYG observation produced by this benchmark is
PAYG-proxy evidence for a PTU hypothesis and MUST NOT be framed as
PTU evidence (§0.10 lint, §11.54).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import pathlib
import re
import statistics
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# §10 RFC values — PINNED by v2.5 microfix #1 (§0.1). Loosening any value
# below requires a fresh spec revision (v2.6) and a fresh auditor APPROVE
# record. §11.41 (adaptive_rfc_values_pinned_regression) asserts these
# match the §10 table exactly.
# ---------------------------------------------------------------------------

ADAPTIVE_EXPANSION_FACTOR: float = 1.5
"""§10 — Geometric expansion factor for Step 2 expansion probes."""

ADAPTIVE_EXPANSION_PROBES_MAX_PER_ROLE: int = 2
"""§10 — Max Step 2 expansion probes per role."""

ADAPTIVE_BRACKET_DEPTH_MAX_PER_ROLE: int = 3
"""§10 — Max Step 3 geometric-midpoint bracket depth per role."""

ADAPTIVE_C2_REPLICATES_MAX_PER_ROLE: int = 1
"""§10 / §0.4 — Separate per-role cap for C2 replicate probes. NOT
shared with the bracket-depth cap; the FIRST replicate is binding (no
best-of-N)."""

C2_ONSET_SEPARATION_MARGIN_TPS: float = 0.05
"""§10 / §0.14 microfix #2 — Absolute TPS margin between
``onset_lower[smallest_control]`` and ``onset_upper[largest]`` required
for C2 admission. PINNED (no longer a candidate)."""

ADAPTIVE_CALIBRATION_MAX_USD: float = 25.0
"""§10 / §12 — Hard USD halt for the 0.5.C envelope. Independent of
v2.4 ``calibration.total_max_usd``."""

ADAPTIVE_CALIBRATION_WALL_TIME_MAX_MINUTES: int = 45
"""§10 — Hard wall-time halt for the 0.5.C envelope."""

ADAPTIVE_APICONNECTIONERROR_CONSECUTIVE_MAX: int = 3
"""§10 — Halt-on-third consecutive APIConnectionError per role."""

MIN_REMAINING_USD_FOR_ADAPTIVE_ENTRY: float = 8.0
"""§10 — Minimum remaining USD after 0.5.A + 0.5.B to admit 0.5.C."""

MIN_REMAINING_USD_FOR_EXPANSION: float = 3.0
"""§10 — Minimum remaining USD to dispatch a Step 2 expansion probe."""

# v2.4-pinned floors (§0.2 / §5.1) re-exported here so v2.5 evaluators
# never drift from v2.4. Loosening still requires a fresh spec revision.
CACHE_HIT_FLOOR_LARGEST: float = 0.80
CACHE_HIT_FLOOR_SMALLEST_CONTROL: float = 0.80
MINIMUM_RECORDS_AT_SELECTED_TPS: int = 30

RFC_PINNED_VALUES: dict[str, Any] = {
    "adaptive_expansion_factor": ADAPTIVE_EXPANSION_FACTOR,
    "adaptive_expansion_probes_max_per_role": ADAPTIVE_EXPANSION_PROBES_MAX_PER_ROLE,
    "adaptive_bracket_depth_max_per_role": ADAPTIVE_BRACKET_DEPTH_MAX_PER_ROLE,
    "adaptive_c2_replicates_max_per_role": ADAPTIVE_C2_REPLICATES_MAX_PER_ROLE,
    "c2_onset_separation_margin_tps": C2_ONSET_SEPARATION_MARGIN_TPS,
    "adaptive_calibration_max_usd": ADAPTIVE_CALIBRATION_MAX_USD,
    "adaptive_calibration_wall_time_max_minutes": ADAPTIVE_CALIBRATION_WALL_TIME_MAX_MINUTES,
    "adaptive_apiconnectionerror_consecutive_max": ADAPTIVE_APICONNECTIONERROR_CONSECUTIVE_MAX,
    "min_remaining_usd_for_adaptive_entry": MIN_REMAINING_USD_FOR_ADAPTIVE_ENTRY,
    "min_remaining_usd_for_expansion": MIN_REMAINING_USD_FOR_EXPANSION,
}
"""§11.41 — Regression-asserted table; any divergence requires a v2.6
revision."""

# ---------------------------------------------------------------------------
# Schema versions — v2.5 bumps (§0.6, §9.1, §9.2, §9.3).
# ---------------------------------------------------------------------------

SCHEMA_VERSION_CALIBRATION_RESULT_V25: str = (
    "task019.v2.5.calibration_result"
)
SCHEMA_VERSION_ADAPTIVE_CALIBRATION_SUMMARY_V25: str = (
    "task019.v2.5.adaptive_calibration_summary"
)
SCHEMA_VERSION_SMOKE_SUMMARY_V25: str = "task019.v2.5.smoke_summary"
SCHEMA_VERSION_EVIDENCE_SUMMARY_V25: str = "task019.v2.5.evidence_summary"

# ---------------------------------------------------------------------------
# Outcome / selected_via enums (§0.6, §8.1, §8.2).
# ---------------------------------------------------------------------------

# v2.4 pre-existing outcomes that v2.5 calibration result MUST still
# accept under the v2.5 schema version (no destructive migration).
_V24_OUTCOMES_BASE: frozenset[str] = frozenset({
    "selected",
    "no_usable_contrast_at_this_prompt_deployment",
    "no_largest_cell_429_at_any_phase_b_candidate_tps_after_admitted_pressure_block",
    "no_largest_cell_429_at_any_phase_b_candidate_tps_after_phase_a_grid_exhausted",
    "calibration_probe_inconclusive_admitted_pressure_floor_violation",
    "calibration_probe_inconclusive_request_rate_target_undershoot",
    "calibration_probe_inconclusive_zero_admitted_requests",
    "calibration_probe_inconclusive_429_in_both_roles",
    "calibration_probe_inconclusive_429_only_in_smallest_control",
})

V25_NEW_OUTCOMES: frozenset[str] = frozenset({
    "adaptive_calibration_wall_time_exhausted",
    "adaptive_calibration_api_connection_unstable",
    "adaptive_calibration_budget_exhausted",
    "no_promotable_contrast_at_this_prompt_deployment",
    "adaptive_calibration_auditor_approval_missing_or_invalid",
})

CALIBRATION_OUTCOMES_V25_EXTENDED: frozenset[str] = (
    _V24_OUTCOMES_BASE | V25_NEW_OUTCOMES
)

_V24_SELECTED_VIA_BASE: frozenset[str] = frozenset({
    "phase_a",
    "phase_b",
    "bracket_search",
})

V25_NEW_SELECTED_VIA: frozenset[str] = frozenset({
    "adaptive_strict_separating_tps",
    "adaptive_onset_separation_replicate_confirmed",
})

SELECTED_VIA_V25_EXTENDED: frozenset[str] = (
    _V24_SELECTED_VIA_BASE | V25_NEW_SELECTED_VIA
)

ADAPTIVE_CAP_TERMINAL_OUTCOMES: frozenset[str] = frozenset({
    "adaptive_calibration_budget_exhausted",
    "adaptive_calibration_wall_time_exhausted",
    "adaptive_calibration_api_connection_unstable",
})
"""§0.3 / §8.1 — hard-cap halt outcomes; NEVER C3."""

C3_OUTCOME: str = "no_promotable_contrast_at_this_prompt_deployment"

# ---------------------------------------------------------------------------
# §0.2 onset-bound eligibility enums.
# ---------------------------------------------------------------------------

ONSET_BOUND_ELIGIBILITY_REASONS: frozenset[str] = frozenset({
    "cache_hit_floor_violation",
    "pressure_admission_failed",
    "backlog_ceiling_exceeded",
    "prompt_identity_mismatch",
    "pricing_snapshot_mismatch",
    "network_error_terminal",
})

# ---------------------------------------------------------------------------
# §3.2 / §0.9 YAML preflight validators.
# ---------------------------------------------------------------------------

AUDITOR_APPROVAL_COMMENT_REGEX: re.Pattern[str] = re.compile(
    r"^methodology-auditor approved v2\.5 adaptive — [a-z0-9-]+ — "
    r"\d{4}-\d{2}-\d{2}$"
)
"""§3.2 trigger predicate 3 regex (verbatim from spec)."""

# ---------------------------------------------------------------------------
# §9.1 PAYG-not-PTU caveat literal banner. The validator REJECTS any
# adaptive summary whose ``payg_not_ptu_caveat`` field is not this exact
# string. v2.4 microfix #1 banner verbatim (single source of truth).
# ---------------------------------------------------------------------------

PAYG_NOT_PTU_CAVEAT_BANNER: str = (
    "PAYG, not PTU. This benchmark runs against a PAYG GlobalStandard "
    "deployment (ptu-deploy-throttled, 60K TPM, ptu_evidence=false). "
    "Every observation is PAYG proxy evidence for a PTU hypothesis; "
    "it is NOT direct PTU evidence."
)

# ---------------------------------------------------------------------------
# §0.10 PAYG-proxy live-report wording lint config (used by §11.54 and
# the CI lint script). Forbidden phrases MUST NOT appear in a v2.5
# live-report markdown file outside an explicitly quoted counter-example
# block (markdown blockquote prefixed with ``> COUNTER-EXAMPLE:``).
# ---------------------------------------------------------------------------

FORBIDDEN_PAYG_PROXY_PHRASES: tuple[str, ...] = (
    "PTU evidence",
    "evidence of PTU",
    "demonstrates PTU behaviour",
    "demonstrates PTU behavior",
    "proves PTU",
)


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class AdaptiveCalibrationYAMLPreflightError(RuntimeError):
    """Raised by the YAML preflight when the adaptive block is enabled
    but a §0.9 / §3.2 requirement is not met. Carries an enumerated
    ``reason`` string so call sites can branch on it."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class V25SchemaValidationError(RuntimeError):
    """Raised when a v2.5 artefact payload fails its §9.x validator."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# §0.12 adaptive cache-bucket-key composer.
# ---------------------------------------------------------------------------


ADAPTIVE_STEP_NAMES: frozenset[str] = frozenset({
    "step1_observation_only",
    "step2_expansion",
    "step3_bracket",
    "c2_replicate",
})


# Task 019 v2.7 — Azure-safe adaptive cache-bucket-key abbreviations.
# Azure / Foundry v1 ``prompt_cache_key`` rejects keys containing
# colons (``:``), equal signs (``=``), or dots (``.``) — those characters
# trigger ``BadRequestError`` (HTTP 400) from the Responses API. The
# v2.6 composer used ``::adaptive::step::role=largest::tps=0.676001``
# which surfaced 133 BadRequestErrors in Fresh3 (run
# 20260602T010212Z_exp007_max_output_tokens_sweep_calibration.jsonl).
#
# v2.7 emits keys that match ``[A-Za-z0-9_-]+`` exclusively and remain
# deterministic per ``(v24_base, step, role, tps)``. Steps and roles are
# abbreviated so the suffix stays compact (≤24 chars) and the composed
# key tends to stay under 64 chars even for the longest v2.4 base.
_ADAPTIVE_STEP_ABBR: dict[str, str] = {
    "step1_observation_only": "s1obs",
    "step2_expansion": "s2exp",
    "step3_bracket": "s3brk",
    "c2_replicate": "c2rep",
}
_ADAPTIVE_ROLE_ABBR: dict[str, str] = {
    "largest": "lg",
    "smallest_control": "sc",
}
# v2.7 — bucket-key regex, used by tests and by callers wanting to
# pre-validate an adaptive key against the provider-safe charset.
ADAPTIVE_BUCKET_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def build_adaptive_cache_bucket_key(
    *,
    v24_base: str,
    step: str,
    role: str,
    tps: float,
) -> str:
    """Compose the §0.12 adaptive cache-bucket key (v2.7, Azure-safe).

    The v2.4 prompt-identity contract is PRESERVED VERBATIM: this
    function appends an ``_adp_…`` suffix at the cache-bucket layer
    ONLY; the caller is responsible for not altering any byte that
    contributes to ``prompt_identity_sha256``.

    Format (v2.7):
        ``{v24_base}_adp_{step_abbr}_{role_abbr}_t{microtps:08d}``

    Where ``microtps = int(round(tps * 1_000_000))`` is zero-padded to
    8 digits (sufficient for TPS up to 99 with µTPS precision).
    The result matches ``[A-Za-z0-9_-]+`` so Azure / Foundry v1 does
    NOT reject it as it did the v2.6 ``::adaptive::…`` form.

    Args:
        v24_base: The v2.4 cache-bucket key for this probe (already a
            function of ``prompt_identity_sha256``). Must itself be
            provider-safe (no ``:``/``=``/``.``).
        step: One of ``ADAPTIVE_STEP_NAMES``.
        role: ``"largest"`` or ``"smallest_control"``.
        tps: The TPS the probe will dispatch at (must be > 0 and
            < 100; µTPS resolution).

    Returns:
        A composed bucket key string distinct per
        ``(step, role, tps)`` triple, matching
        ``ADAPTIVE_BUCKET_KEY_RE``.

    Raises:
        ValueError: invalid ``step`` or ``role``, non-positive /
            out-of-range ``tps``, empty/unsafe ``v24_base``.
    """
    if step not in ADAPTIVE_STEP_NAMES:
        raise ValueError(
            f"step must be one of {sorted(ADAPTIVE_STEP_NAMES)}; "
            f"got {step!r}"
        )
    if role not in _ADAPTIVE_ROLE_ABBR:
        raise ValueError(
            f"role must be 'largest' or 'smallest_control'; got {role!r}"
        )
    if not isinstance(tps, (int, float)) or tps <= 0 or math.isnan(float(tps)):
        raise ValueError(f"tps must be a positive number; got {tps!r}")
    if float(tps) >= 100.0:
        raise ValueError(
            f"tps must be < 100 for µTPS-resolution 8-digit encoding; "
            f"got {tps!r}"
        )
    if not v24_base:
        raise ValueError("v24_base must be a non-empty string")
    if not ADAPTIVE_BUCKET_KEY_RE.fullmatch(v24_base):
        raise ValueError(
            f"v24_base must match [A-Za-z0-9_-]+ (provider-safe); "
            f"got {v24_base!r}"
        )
    microtps = int(round(float(tps) * 1_000_000))
    step_abbr = _ADAPTIVE_STEP_ABBR[step]
    role_abbr = _ADAPTIVE_ROLE_ABBR[role]
    composed = f"{v24_base}_adp_{step_abbr}_{role_abbr}_t{microtps:08d}"
    # Defensive — internal invariant; the abbreviation table above is
    # the only authority that can break this. Fail loudly rather than
    # silently emit a key Azure would reject.
    if not ADAPTIVE_BUCKET_KEY_RE.fullmatch(composed):
        raise ValueError(
            f"composed adaptive key is not provider-safe: {composed!r}"
        )
    return composed


# ---------------------------------------------------------------------------
# §0.2 onset-eligibility evaluator.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class OnsetEligibilityResult:
    """Closed-form §0.2 eligibility verdict for a single probe."""

    eligibility: str  # "eligible" | "ineligible"
    reason: str | None  # None when eligible; one of ONSET_BOUND_ELIGIBILITY_REASONS


def compute_onset_eligibility(
    *,
    probe: dict[str, Any],
    pinned_prompt_identity_sha256: str,
    pinned_pricing_snapshot_path: str,
    cache_hit_floor_for_role: float,
    backlog_ceiling_seconds: float,
) -> OnsetEligibilityResult:
    """Evaluate the §0.2 six-condition eligibility predicate.

    Checks are evaluated in the §0.2 numerical order so the FIRST
    failing condition is surfaced (deterministic).

    Args:
        probe: a probe-observation dict carrying at minimum the keys
            ``cache_hit_ratio_steady_state``, ``admitted``,
            ``backlog_pre_dispatch_seconds``, ``prompt_identity_sha256``,
            ``pricing_snapshot_path``, ``terminal_status``.
        pinned_prompt_identity_sha256: the calibration root's pinned
            prompt-identity sha256 (byte-identical contract).
        pinned_pricing_snapshot_path: the calibration root's pricing
            snapshot path.
        cache_hit_floor_for_role: the v2.4 pinned floor for this role
            (``CACHE_HIT_FLOOR_LARGEST`` or
            ``CACHE_HIT_FLOOR_SMALLEST_CONTROL``).
        backlog_ceiling_seconds: the v2.4 pinned backlog ceiling in
            seconds.

    Returns:
        ``OnsetEligibilityResult``.
    """
    cache_hit = float(probe.get("cache_hit_ratio_steady_state", 0.0))
    if cache_hit < cache_hit_floor_for_role:
        return OnsetEligibilityResult(
            "ineligible", "cache_hit_floor_violation"
        )
    if not probe.get("admitted", False):
        return OnsetEligibilityResult(
            "ineligible", "pressure_admission_failed"
        )
    backlog = float(probe.get("backlog_pre_dispatch_seconds", 0.0))
    if backlog > backlog_ceiling_seconds:
        return OnsetEligibilityResult(
            "ineligible", "backlog_ceiling_exceeded"
        )
    if probe.get("prompt_identity_sha256") != pinned_prompt_identity_sha256:
        return OnsetEligibilityResult(
            "ineligible", "prompt_identity_mismatch"
        )
    if probe.get("pricing_snapshot_path") != pinned_pricing_snapshot_path:
        return OnsetEligibilityResult(
            "ineligible", "pricing_snapshot_mismatch"
        )
    terminal_status = probe.get("terminal_status", "")
    if terminal_status in {
        "openai.APIConnectionError",
        "socket_error",
        "dns_error",
        "tls_handshake_error",
        "network_error",
    }:
        return OnsetEligibilityResult(
            "ineligible", "network_error_terminal"
        )
    return OnsetEligibilityResult("eligible", None)


# ---------------------------------------------------------------------------
# §0.8 aggregation for repeated same-TPS probes.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AggregatedObservation:
    """§0.8 aggregated paired observation used by C1 / C2 evaluators."""

    role: str
    tps: float
    n_429_aggregated: int
    n_records_aggregated: int
    cache_hit_ratio_steady_state_aggregated: float
    contributing_probe_indices: tuple[int, ...]


def aggregate_observations_same_tps(
    *,
    probes: Sequence[dict[str, Any]],
    role: str,
    tps: float,
    tps_tolerance: float = 1e-9,
) -> AggregatedObservation:
    """Aggregate every eligible probe at the same ``(role, tps)``.

    Args:
        probes: list of probe-observation dicts. Each MUST carry
            ``role``, ``tps_dispatched``, ``n_429``, ``n_records``,
            ``cache_hit_ratio_steady_state`` and ``eligible`` (bool).
            Ineligible probes are silently excluded (§0.2).
        role: ``"largest"`` or ``"smallest_control"``.
        tps: the TPS value the aggregator pairs probes on.
        tps_tolerance: float comparison tolerance.

    Returns:
        ``AggregatedObservation`` with summed counts and the
        cache-hit weighted mean per §0.8.

    Raises:
        ValueError: no eligible probes match ``(role, tps)``.
    """
    indices: list[int] = []
    matched: list[dict[str, Any]] = []
    for i, p in enumerate(probes):
        if p.get("role") != role:
            continue
        if not p.get("eligible", False):
            continue
        if abs(float(p["tps_dispatched"]) - float(tps)) > tps_tolerance:
            continue
        matched.append(p)
        indices.append(i)
    if not matched:
        raise ValueError(
            f"aggregate_observations_same_tps: no eligible probes for "
            f"(role={role!r}, tps={tps!r})"
        )
    n_429 = sum(int(p["n_429"]) for p in matched)
    n_records = sum(int(p["n_records"]) for p in matched)
    if n_records <= 0:
        # Avoid divide-by-zero; cache-hit is undefined and falls to 0.
        cache_hit_wm = 0.0
    else:
        cache_hit_wm = sum(
            float(p["cache_hit_ratio_steady_state"]) * int(p["n_records"])
            for p in matched
        ) / n_records
    return AggregatedObservation(
        role=role,
        tps=float(tps),
        n_429_aggregated=n_429,
        n_records_aggregated=n_records,
        cache_hit_ratio_steady_state_aggregated=cache_hit_wm,
        contributing_probe_indices=tuple(indices),
    )


# ---------------------------------------------------------------------------
# §4.1 role onset interval computation.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RoleOnsetInterval:
    """§4.1 per-role onset bounds + classification state."""

    role: str
    onset_lower_tps: float | None
    onset_upper_tps: float | None
    state: str  # "bracketed" | "right_open" | "left_open" | "open"


def compute_role_onset_interval(
    *,
    probes: Sequence[dict[str, Any]],
    role: str,
) -> RoleOnsetInterval:
    """Compute the §4.1 onset interval from eligible probes for one role.

    Probe dicts MUST carry ``role``, ``tps_dispatched``, ``n_429``, and
    ``eligible``. Ineligible probes are silently excluded per §0.2.

    State classification (§4.1):

    - ``bracketed`` — both bounds non-null AND ``upper > lower``.
    - ``right_open`` — lower non-null, upper null.
    - ``left_open`` — upper non-null, lower null.
    - ``open`` — both null (no eligible observations at all).
    """
    zero_tps: list[float] = []
    pos_tps: list[float] = []
    for p in probes:
        if p.get("role") != role:
            continue
        if not p.get("eligible", False):
            continue
        tps = float(p["tps_dispatched"])
        if int(p["n_429"]) == 0:
            zero_tps.append(tps)
        else:
            pos_tps.append(tps)
    lower = max(zero_tps) if zero_tps else None
    upper = min(pos_tps) if pos_tps else None
    if lower is not None and upper is not None and upper > lower:
        state = "bracketed"
    elif lower is not None and upper is None:
        state = "right_open"
    elif upper is not None and lower is None:
        state = "left_open"
    else:
        # Either both None (no eligible observations) OR upper<=lower
        # (degenerate; treat as "open" so the planner does not present
        # this as bracketed). §4.1 does not contemplate upper<=lower
        # under aggregation but we surface it defensively.
        state = "open"
    return RoleOnsetInterval(
        role=role,
        onset_lower_tps=lower,
        onset_upper_tps=upper,
        state=state,
    )


# ---------------------------------------------------------------------------
# §4.2 / §4.3 expansion and bracket TPS planners.
# ---------------------------------------------------------------------------


def adaptive_tps_hard_max(phase_b_grid_tps: Sequence[float]) -> float:
    """§4.4 — ``1.20 × max(phase_b_grid_tps)``."""
    if not phase_b_grid_tps:
        raise ValueError("phase_b_grid_tps must be non-empty")
    return 1.20 * float(max(phase_b_grid_tps))


def adaptive_tps_hard_min(phase_a_grid_tps: Sequence[float]) -> float:
    """§4.4 — ``0.5 × min(phase_a_grid_tps)``."""
    if not phase_a_grid_tps:
        raise ValueError("phase_a_grid_tps must be non-empty")
    return 0.5 * float(min(phase_a_grid_tps))


@dataclasses.dataclass(frozen=True)
class ExpansionPlan:
    """One planned Step 2 expansion probe."""

    role: str
    side: str  # "right_open" | "left_open"
    tps_next: float
    clamped_to_cap: str | None  # None | "hard_max" | "hard_min"


def plan_step2_expansion(
    *,
    interval: RoleOnsetInterval,
    phase_a_grid_tps: Sequence[float],
    phase_b_grid_tps: Sequence[float],
) -> ExpansionPlan | None:
    """Compute the §4.2 ``tps_next`` for a single expansion probe.

    Returns ``None`` if the role is already ``bracketed`` (no
    expansion needed) or fully ``open`` (cannot decide a side).
    """
    if interval.state == "right_open":
        assert interval.onset_lower_tps is not None
        raw = interval.onset_lower_tps * ADAPTIVE_EXPANSION_FACTOR
        cap = adaptive_tps_hard_max(phase_b_grid_tps)
        if raw > cap:
            return ExpansionPlan(
                role=interval.role,
                side="right_open",
                tps_next=cap,
                clamped_to_cap="hard_max",
            )
        return ExpansionPlan(
            role=interval.role,
            side="right_open",
            tps_next=raw,
            clamped_to_cap=None,
        )
    if interval.state == "left_open":
        assert interval.onset_upper_tps is not None
        raw = interval.onset_upper_tps / ADAPTIVE_EXPANSION_FACTOR
        floor = adaptive_tps_hard_min(phase_a_grid_tps)
        if raw < floor:
            return ExpansionPlan(
                role=interval.role,
                side="left_open",
                tps_next=floor,
                clamped_to_cap="hard_min",
            )
        return ExpansionPlan(
            role=interval.role,
            side="left_open",
            tps_next=raw,
            clamped_to_cap=None,
        )
    return None


def plan_step3_bracket_midpoint(interval: RoleOnsetInterval) -> float | None:
    """Compute the §4.3 geometric-midpoint TPS for a bracket probe.

    Returns ``None`` if the role is not ``bracketed``.
    """
    if interval.state != "bracketed":
        return None
    assert interval.onset_lower_tps is not None
    assert interval.onset_upper_tps is not None
    # Geometric midpoint matches v2.4 bracket-search semantics.
    return math.sqrt(
        interval.onset_lower_tps * interval.onset_upper_tps
    )


# ---------------------------------------------------------------------------
# §5 contrast criteria.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AdmissionDecision:
    """Result of evaluating one criterion (C1 / C2 / C3)."""

    criterion: str  # "C1" | "C2" | "C3"
    decision: str  # "ADMIT" | "DENY"
    reason: str
    selected_peak_tps: float | None = None
    selected_via: str | None = None


def evaluate_c1(
    *,
    aggregated_observations: Sequence[AggregatedObservation],
    minimum_records_at_selected_tps: int = MINIMUM_RECORDS_AT_SELECTED_TPS,
    cache_hit_floor_largest: float = CACHE_HIT_FLOOR_LARGEST,
    cache_hit_floor_smallest_control: float = CACHE_HIT_FLOOR_SMALLEST_CONTROL,
) -> AdmissionDecision:
    """Evaluate C1 (§5.1) with §0.7 lowest-qualifying-TPS tie-break.

    Args:
        aggregated_observations: aggregated paired observations
            produced by :func:`aggregate_observations_same_tps`,
            covering BOTH roles at every candidate ``t*``.
        minimum_records_at_selected_tps: §5.1 v2.4 pinned floor (30).
        cache_hit_floor_largest: v2.4-pinned floor for largest role.
        cache_hit_floor_smallest_control: v2.4-pinned floor for
            smallest_control role.

    Returns:
        ``AdmissionDecision`` — ADMIT with the lowest qualifying TPS,
        else DENY with a reason enum.
    """
    # Group observations by tps.
    by_tps: dict[float, dict[str, AggregatedObservation]] = {}
    for obs in aggregated_observations:
        by_tps.setdefault(obs.tps, {})[obs.role] = obs
    qualifying_tps: list[float] = []
    for tps in sorted(by_tps):
        roles = by_tps[tps]
        if "largest" not in roles or "smallest_control" not in roles:
            continue
        largest = roles["largest"]
        small = roles["smallest_control"]
        if largest.n_429_aggregated < 1:
            continue
        if small.n_429_aggregated != 0:
            continue
        if (
            largest.cache_hit_ratio_steady_state_aggregated
            < cache_hit_floor_largest
        ):
            continue
        if (
            small.cache_hit_ratio_steady_state_aggregated
            < cache_hit_floor_smallest_control
        ):
            continue
        if small.n_records_aggregated < minimum_records_at_selected_tps:
            continue
        qualifying_tps.append(tps)
    if not qualifying_tps:
        return AdmissionDecision(
            criterion="C1",
            decision="DENY",
            reason="no_tps_satisfies_strict_separating_predicate",
        )
    # §0.7 tie-break: lowest qualifying TPS wins.
    chosen = min(qualifying_tps)
    return AdmissionDecision(
        criterion="C1",
        decision="ADMIT",
        reason="strict_separating_tps_found_lowest_tie_break",
        selected_peak_tps=chosen,
        selected_via="adaptive_strict_separating_tps",
    )


def evaluate_c2(
    *,
    largest_interval: RoleOnsetInterval,
    smallest_interval: RoleOnsetInterval,
    aggregated_observations_at_t_star: dict[str, AggregatedObservation] | None,
    c2_margin_tps: float = C2_ONSET_SEPARATION_MARGIN_TPS,
    minimum_records_at_selected_tps: int = MINIMUM_RECORDS_AT_SELECTED_TPS,
    cache_hit_floor_largest: float = CACHE_HIT_FLOOR_LARGEST,
    cache_hit_floor_smallest_control: float = CACHE_HIT_FLOOR_SMALLEST_CONTROL,
) -> AdmissionDecision:
    """Evaluate C2 (§5.2, microfix #2 PINNED margin).

    Args:
        largest_interval / smallest_interval: §4.1 role onset intervals
            after Steps 1–3.
        aggregated_observations_at_t_star: aggregated (largest +
            smallest_control) observations at the C2 candidate
            ``t* = geometric_mean(largest.upper, smallest.lower)``,
            INCLUDING the C2 replicate per §0.8. ``None`` when no
            replicate has been dispatched yet (DENY: no replicate).
        c2_margin_tps: §10 PINNED 0.05 absolute TPS margin.

    Returns:
        ``AdmissionDecision``.
    """
    if largest_interval.state != "bracketed":
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="largest_role_not_bracketed",
        )
    if smallest_interval.state != "bracketed":
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="smallest_control_role_not_bracketed",
        )
    assert smallest_interval.onset_lower_tps is not None
    assert largest_interval.onset_upper_tps is not None
    separation = (
        smallest_interval.onset_lower_tps
        - largest_interval.onset_upper_tps
    )
    if separation < c2_margin_tps:
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="onset_separation_below_margin",
        )
    if aggregated_observations_at_t_star is None:
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="no_replicate_dispatched_at_t_star",
        )
    if (
        "largest" not in aggregated_observations_at_t_star
        or "smallest_control" not in aggregated_observations_at_t_star
    ):
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="replicate_missing_one_role_at_t_star",
        )
    largest_obs = aggregated_observations_at_t_star["largest"]
    small_obs = aggregated_observations_at_t_star["smallest_control"]
    if largest_obs.n_429_aggregated < 1:
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="replicate_largest_no_429_at_t_star",
        )
    if small_obs.n_429_aggregated != 0:
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="replicate_smallest_control_429_at_t_star",
        )
    if (
        largest_obs.cache_hit_ratio_steady_state_aggregated
        < cache_hit_floor_largest
    ):
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="replicate_largest_cache_hit_floor_violation",
        )
    if (
        small_obs.cache_hit_ratio_steady_state_aggregated
        < cache_hit_floor_smallest_control
    ):
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="replicate_smallest_control_cache_hit_floor_violation",
        )
    if small_obs.n_records_aggregated < minimum_records_at_selected_tps:
        return AdmissionDecision(
            criterion="C2",
            decision="DENY",
            reason="replicate_smallest_control_n_records_below_floor",
        )
    t_star = math.sqrt(
        largest_interval.onset_upper_tps
        * smallest_interval.onset_lower_tps
    )
    return AdmissionDecision(
        criterion="C2",
        decision="ADMIT",
        reason="onset_separation_replicate_confirmed",
        selected_peak_tps=t_star,
        selected_via="adaptive_onset_separation_replicate_confirmed",
    )


def evaluate_c3_terminal(
    *,
    c1: AdmissionDecision,
    c2: AdmissionDecision,
    adaptive_caps_state: Sequence[dict[str, Any]],
) -> AdmissionDecision:
    """Evaluate the §5.3 / §0.3 C3 terminal predicate.

    C3 is emitted IFF (a) neither C1 nor C2 admitted AND (b) NO §4.4
    hard cap halted. Any halted cap MUST instead surface the
    corresponding adaptive-cap terminal outcome (§0.3); the caller
    enforces this by inspecting ``adaptive_caps_state`` itself.
    """
    if c1.decision == "ADMIT" or c2.decision == "ADMIT":
        return AdmissionDecision(
            criterion="C3",
            decision="DENY",
            reason="c1_or_c2_already_admitted",
        )
    for cap in adaptive_caps_state:
        if cap.get("halted_on_cap"):
            return AdmissionDecision(
                criterion="C3",
                decision="DENY",
                reason=(
                    f"cap_halted_must_use_cap_terminal_outcome:"
                    f"{cap.get('cap_name')}"
                ),
            )
    return AdmissionDecision(
        criterion="C3",
        decision="ADMIT",
        reason="no_promotable_contrast_search_complete_within_caps",
        selected_peak_tps=None,
        selected_via=None,
    )


# ---------------------------------------------------------------------------
# §9.x schema validators.
# ---------------------------------------------------------------------------


_CALIBRATION_RESULT_REQUIRED_TOP_LEVEL: tuple[str, ...] = (
    "schema_version",
    "outcome",
)


def validate_calibration_result_v25(data: dict[str, Any]) -> None:
    """§0.6 / §9.0 validator for ``task019.v2.5.calibration_result``.

    Backward compatibility: payloads with
    ``schema_version == "task019.v2.4.calibration_result"`` (or earlier
    v2.3/v2.2.1) are NOT this validator's concern; the v2.4 validator
    in ``measure_max_output_tokens_sweep.py`` handles them. This
    validator REJECTS any v2.4 payload mixed with v2.5-only enum
    values.

    Raises:
        V25SchemaValidationError: with an enumerated ``reason``.
    """
    for k in _CALIBRATION_RESULT_REQUIRED_TOP_LEVEL:
        if k not in data:
            raise V25SchemaValidationError(
                f"v2.5 calibration result missing required key {k!r}",
                reason="missing_required_field",
            )
    schema = data["schema_version"]
    outcome = data["outcome"]
    selected_via = data.get("selected_via")
    if schema == SCHEMA_VERSION_CALIBRATION_RESULT_V25:
        if outcome not in CALIBRATION_OUTCOMES_V25_EXTENDED:
            raise V25SchemaValidationError(
                f"v2.5 calibration result outcome {outcome!r} not in "
                f"extended enum",
                reason="outcome_not_in_v25_extended_enum",
            )
        if outcome == "selected":
            if selected_via not in SELECTED_VIA_V25_EXTENDED:
                raise V25SchemaValidationError(
                    f"v2.5 calibration result selected_via "
                    f"{selected_via!r} not in extended enum",
                    reason="selected_via_not_in_v25_extended_enum",
                )
        return
    # If schema is the v2.4 version, the v2.5 validator's job is ONLY
    # to reject v2.5-only enum mixing (§0.6 third bullet). Other v2.4
    # fields are out of scope here.
    if schema == "task019.v2.4.calibration_result":
        if outcome in V25_NEW_OUTCOMES:
            raise V25SchemaValidationError(
                f"v2.4 calibration result MUST NOT use v2.5-only outcome "
                f"{outcome!r}",
                reason="v24_payload_uses_v25_only_outcome",
            )
        if selected_via in V25_NEW_SELECTED_VIA:
            raise V25SchemaValidationError(
                f"v2.4 calibration result MUST NOT use v2.5-only "
                f"selected_via {selected_via!r}",
                reason="v24_payload_uses_v25_only_selected_via",
            )
        return
    raise V25SchemaValidationError(
        f"v2.5 calibration validator only accepts schema_version "
        f"task019.v2.5.calibration_result or task019.v2.4.calibration_result; "
        f"got {schema!r}",
        reason="unsupported_schema_version",
    )


def validate_smoke_summary_v25(
    data: dict[str, Any],
    *,
    repo_root: pathlib.Path | None = None,
) -> None:
    """§9.2 validator for ``task019.v2.5.smoke_summary``.

    Per §9.2 (final reviewer follow-up), the three v2.5 linkage fields
    — ``calibration_selected_via``, ``calibration_adaptive_summary_path``,
    ``calibration_adaptive_summary_sha256`` — are REQUIRED to be
    present in the payload (even for non-adaptive results, where the
    path/sha must be ``null``). When ``repo_root`` is supplied AND the
    selected_via is adaptive AND the path is non-null, the file at
    that path MUST hash to the recorded sha256.
    """
    schema = data.get("schema_version")
    if schema != SCHEMA_VERSION_SMOKE_SUMMARY_V25:
        raise V25SchemaValidationError(
            f"v2.5 smoke summary schema_version must be "
            f"{SCHEMA_VERSION_SMOKE_SUMMARY_V25!r}; got {schema!r}",
            reason="schema_version_mismatch",
        )
    for field in (
        "calibration_selected_via",
        "calibration_adaptive_summary_path",
        "calibration_adaptive_summary_sha256",
    ):
        if field not in data:
            raise V25SchemaValidationError(
                f"v2.5 smoke summary missing required field {field!r}",
                reason="missing_required_field",
            )
    sv = data["calibration_selected_via"]
    if sv not in SELECTED_VIA_V25_EXTENDED:
        raise V25SchemaValidationError(
            f"v2.5 smoke summary calibration_selected_via {sv!r} not in "
            f"extended enum",
            reason="selected_via_not_in_v25_extended_enum",
        )
    needs_linkage = sv in V25_NEW_SELECTED_VIA
    path = data["calibration_adaptive_summary_path"]
    sha = data["calibration_adaptive_summary_sha256"]
    if needs_linkage:
        if path is None or sha is None:
            raise V25SchemaValidationError(
                "v2.5 smoke summary with adaptive selected_via MUST carry "
                "non-null calibration_adaptive_summary_path and "
                "calibration_adaptive_summary_sha256",
                reason="missing_adaptive_summary_linkage",
            )
        if repo_root is not None:
            _verify_adaptive_summary_hash(
                repo_root=repo_root, path=path, expected_sha256=sha,
            )
    else:
        if path is not None or sha is not None:
            raise V25SchemaValidationError(
                "v2.5 smoke summary with non-adaptive selected_via MUST "
                "carry null calibration_adaptive_summary_path and "
                "calibration_adaptive_summary_sha256",
                reason="unexpected_adaptive_summary_linkage_present",
            )


def _verify_adaptive_summary_hash(
    *,
    repo_root: pathlib.Path,
    path: str,
    expected_sha256: str,
) -> None:
    """Read the file referenced by ``path`` (resolved under
    ``repo_root``) and require its sha256 to equal
    ``expected_sha256``. Raises :class:`V25SchemaValidationError` with
    ``reason="adaptive_summary_sha256_mismatch"`` on mismatch, or
    ``reason="adaptive_summary_path_unresolvable"`` when the file does
    not exist or is not readable.
    """
    candidate = (pathlib.Path(repo_root) / path).resolve()
    try:
        if not candidate.is_file():
            raise V25SchemaValidationError(
                f"v2.5 adaptive summary path {path!r} does not resolve to "
                f"a file under {repo_root!s}",
                reason="adaptive_summary_path_unresolvable",
            )
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as exc:
        raise V25SchemaValidationError(
            f"v2.5 adaptive summary path {path!r} unreadable: {exc!s}",
            reason="adaptive_summary_path_unresolvable",
        ) from exc
    if actual != expected_sha256:
        raise V25SchemaValidationError(
            f"v2.5 adaptive summary sha256 mismatch for {path!r}: "
            f"recorded {expected_sha256!r}, actual {actual!r}",
            reason="adaptive_summary_sha256_mismatch",
        )


def validate_evidence_summary_v25(
    data: dict[str, Any],
    *,
    source_smoke_summary: dict[str, Any] | None = None,
    repo_root: pathlib.Path | None = None,
) -> None:
    """§9.3 validator for ``task019.v2.5.evidence_summary``.

    When ``source_smoke_summary`` is provided, evaluates the §9.3
    echo-validation contract on the three new fields. Mismatch raises
    a :class:`V25SchemaValidationError` with reason
    ``smoke_summary_reference_echo_mismatch:<field>`` so the caller
    can map to v2.4's existing
    ``evidence_summary_missing_smoke_promotion_path_echo`` exit reason.

    Per §9.2 / §9.3 (final reviewer follow-up), the three v2.5 linkage
    fields are REQUIRED to be present even on non-adaptive evidence
    summaries (where the path/sha are ``null``), and when ``repo_root``
    is supplied and the path is non-null, the file at that path MUST
    hash to the recorded sha256.
    """
    schema = data.get("schema_version")
    if schema != SCHEMA_VERSION_EVIDENCE_SUMMARY_V25:
        raise V25SchemaValidationError(
            f"v2.5 evidence summary schema_version must be "
            f"{SCHEMA_VERSION_EVIDENCE_SUMMARY_V25!r}; got {schema!r}",
            reason="schema_version_mismatch",
        )
    for field in (
        "calibration_selected_via",
        "calibration_adaptive_summary_path",
        "calibration_adaptive_summary_sha256",
    ):
        if field not in data:
            raise V25SchemaValidationError(
                f"v2.5 evidence summary missing required field {field!r}",
                reason="missing_required_field",
            )
    sv = data["calibration_selected_via"]
    if sv not in SELECTED_VIA_V25_EXTENDED:
        raise V25SchemaValidationError(
            f"v2.5 evidence summary calibration_selected_via {sv!r} not "
            f"in extended enum",
            reason="selected_via_not_in_v25_extended_enum",
        )
    needs_linkage = sv in V25_NEW_SELECTED_VIA
    path = data["calibration_adaptive_summary_path"]
    sha = data["calibration_adaptive_summary_sha256"]
    if needs_linkage and (path is None or sha is None):
        raise V25SchemaValidationError(
            "v2.5 evidence summary with adaptive selected_via MUST carry "
            "non-null calibration_adaptive_summary_path / sha256",
            reason="missing_adaptive_summary_linkage",
        )
    if not needs_linkage and (path is not None or sha is not None):
        raise V25SchemaValidationError(
            "v2.5 evidence summary with non-adaptive selected_via MUST "
            "carry null calibration_adaptive_summary_path / sha256",
            reason="unexpected_adaptive_summary_linkage_present",
        )
    if needs_linkage and repo_root is not None:
        _verify_adaptive_summary_hash(
            repo_root=repo_root, path=path, expected_sha256=sha,
        )
    if source_smoke_summary is not None:
        ref = data.get("smoke_summary_reference") or {}
        for field in (
            "calibration_selected_via",
            "calibration_adaptive_summary_path",
            "calibration_adaptive_summary_sha256",
        ):
            if ref.get(field) != source_smoke_summary.get(field):
                raise V25SchemaValidationError(
                    f"v2.5 evidence summary echo mismatch on {field}",
                    reason=f"smoke_summary_reference_echo_mismatch:{field}",
                )


def validate_adaptive_calibration_summary(data: dict[str, Any]) -> None:
    """§9.1 validator for ``task019.v2.5.adaptive_calibration_summary``."""
    schema = data.get("schema_version")
    if schema != SCHEMA_VERSION_ADAPTIVE_CALIBRATION_SUMMARY_V25:
        raise V25SchemaValidationError(
            f"adaptive summary schema_version must be "
            f"{SCHEMA_VERSION_ADAPTIVE_CALIBRATION_SUMMARY_V25!r}; "
            f"got {schema!r}",
            reason="schema_version_mismatch",
        )
    required = (
        "git_commit",
        "dirty",
        "run_id_short",
        "experiment_id",
        "started_at_iso",
        "completed_at_iso",
        "model",
        "deployment_used",
        "calibration_result_path",
        "calibration_result_sha256",
        "calibration_summary_path",
        "calibration_summary_sha256",
        "pricing_source_url",
        "pricing_accessed_date",
        "pricing_snapshot_path",
        "payg_not_ptu_caveat",
        "prompt_identity_sha256",
        "phase_a_probe_observations",
        "phase_b_probe_observations",
        "adaptive_search_trace",
        "role_onset_intervals",
        "contrast_criterion_evaluation",
        "adaptive_caps_state",
        "adaptive_calibration_total_usd",
        "adaptive_calibration_total_committed_usd",
        "auditor_approval_comment_verbatim",
        "disclosed_prior_calibrations",
    )
    for k in required:
        if k not in data:
            raise V25SchemaValidationError(
                f"adaptive summary missing required key {k!r}",
                reason="missing_required_field",
            )
    if data["payg_not_ptu_caveat"] != PAYG_NOT_PTU_CAVEAT_BANNER:
        raise V25SchemaValidationError(
            "adaptive summary payg_not_ptu_caveat does not match v2.5 "
            "banner verbatim",
            reason="payg_not_ptu_caveat_not_verbatim",
        )
    if not AUDITOR_APPROVAL_COMMENT_REGEX.match(
        str(data["auditor_approval_comment_verbatim"])
    ):
        raise V25SchemaValidationError(
            "adaptive summary auditor_approval_comment_verbatim does not "
            "match required regex",
            reason="auditor_approval_comment_invalid",
        )


# ---------------------------------------------------------------------------
# §3.2 + §0.9 YAML preflight.
# ---------------------------------------------------------------------------


def validate_adaptive_calibration_yaml_block(
    yaml_dict: dict[str, Any],
    *,
    repo_root: pathlib.Path,
) -> dict[str, Any]:
    """Validate the ``runtime.adaptive_calibration`` YAML block.

    Returns the resolved adaptive-calibration sub-block (a dict). Does
    NOT mutate the YAML.

    Behaviour:

    - If the block is absent OR ``enabled`` is missing OR
      ``enabled == false``, returns the block (or
      ``{"enabled": False}``) UNCHANGED. No preflight enforcement.
    - If ``enabled: true`` (§3.2 trigger predicate 3), this function
      enforces:

      * ``prior_calibrations_disclosure_path`` (§0.9) is set AND
        resolves to a readable JSON file under ``repo_root``;
      * ``adaptive_calibration_auditor_approval.comment`` matches
        :data:`AUDITOR_APPROVAL_COMMENT_REGEX`.

    Raises:
        AdaptiveCalibrationYAMLPreflightError: with an enumerated
        ``reason`` per §0.9 / §3.2.
    """
    runtime = yaml_dict.get("runtime") or {}
    block = runtime.get("adaptive_calibration") or {"enabled": False}
    if not block.get("enabled", False):
        return block
    # §0.9 — disclosure path required and resolvable when enabled.
    disclosure = block.get("prior_calibrations_disclosure_path")
    if not disclosure:
        raise AdaptiveCalibrationYAMLPreflightError(
            "runtime.adaptive_calibration.enabled=true requires "
            "runtime.adaptive_calibration.prior_calibrations_disclosure_path",
            reason="adaptive_calibration_prior_disclosure_path_required",
        )
    resolved = (repo_root / disclosure).resolve()
    try:
        rel = resolved.relative_to(repo_root.resolve())
    except ValueError:
        raise AdaptiveCalibrationYAMLPreflightError(
            f"prior_calibrations_disclosure_path {disclosure!r} resolves "
            f"outside repo_root",
            reason="adaptive_calibration_prior_disclosure_path_required",
        )
    if not resolved.is_file():
        raise AdaptiveCalibrationYAMLPreflightError(
            f"prior_calibrations_disclosure_path {disclosure!r} "
            f"({rel}) does not resolve to a file",
            reason="adaptive_calibration_prior_disclosure_path_required",
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdaptiveCalibrationYAMLPreflightError(
            f"prior_calibrations_disclosure_path {disclosure!r} unreadable: {exc}",
            reason="adaptive_calibration_prior_disclosure_path_required",
        )
    if not isinstance(payload, list):
        raise AdaptiveCalibrationYAMLPreflightError(
            f"prior_calibrations_disclosure_path {disclosure!r} payload "
            f"must be a JSON list",
            reason="adaptive_calibration_prior_disclosure_path_required",
        )
    # §3.2 trigger predicate 3 — auditor approval comment regex.
    approval = block.get("adaptive_calibration_auditor_approval") or {}
    comment = approval.get("comment") if isinstance(approval, dict) else None
    if not comment or not AUDITOR_APPROVAL_COMMENT_REGEX.match(str(comment)):
        raise AdaptiveCalibrationYAMLPreflightError(
            "runtime.adaptive_calibration.adaptive_calibration_auditor_"
            "approval.comment is missing or fails the §3.2 regex",
            reason="adaptive_calibration_auditor_approval_missing_or_invalid",
        )
    return block


# ---------------------------------------------------------------------------
# §0.10 PAYG-proxy wording lint.
# ---------------------------------------------------------------------------


_COUNTER_EXAMPLE_PREFIX: str = "> COUNTER-EXAMPLE:"


def lint_payg_proxy_wording(text: str) -> list[tuple[int, str, str]]:
    """Return a list of ``(line_number, phrase, line)`` tuples for any
    forbidden PAYG-proxy phrase that appears OUTSIDE a markdown
    blockquote line beginning with ``"> COUNTER-EXAMPLE:"``.

    Args:
        text: full file contents.

    Returns:
        Empty list when the file passes the lint.
    """
    findings: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith(_COUNTER_EXAMPLE_PREFIX):
            continue
        for phrase in FORBIDDEN_PAYG_PROXY_PHRASES:
            if phrase in line:
                findings.append((i, phrase, line))
                break
    return findings


# ---------------------------------------------------------------------------
# §0.5 / §11.50 live-run / blocked-run artifact lint.
# ---------------------------------------------------------------------------

LIVE_V25_MARKDOWN_PATH: str = (
    "benchmarks/07-max-output-tokens-reservation/"
    "live-v2.5-adaptive-contrast.md"
)
CHANGELOG_PATH: str = "CHANGELOG.md"

_LIVE_SECTION_REQUIRED_TOKENS: tuple[str, ...] = (
    "Run prefix:",
    "Terminal artifact sha256:",
    "Outcome:",
    "Total spend:",
    "Pinned §10 RFC assumptions:",
    "Measurements:",
    "Fixes attempted:",
    "Blockers:",
)


def check_v25_live_artifacts(
    *,
    repo_root: pathlib.Path,
    expected_run_prefixes: Iterable[str],
) -> list[str]:
    """§11.50 lint over the v2.5 live artifacts.

    Returns a list of human-readable findings. Empty list means PASS.

    For each expected v2.5 run prefix:
      (a) live markdown file MUST contain a section whose body cites
          the prefix and every §0.5 required field;
      (b) CHANGELOG MUST contain an entry under
          ``### Live run — Task 019 v2.5`` or
          ``### Blocked run — Task 019 v2.5`` mentioning the same prefix.

    Always runs the §11.54 PAYG-proxy wording lint over the markdown
    file itself.
    """
    findings: list[str] = []
    md_path = repo_root / LIVE_V25_MARKDOWN_PATH
    cl_path = repo_root / CHANGELOG_PATH
    md_text = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    cl_text = cl_path.read_text(encoding="utf-8") if cl_path.is_file() else ""
    if not md_path.is_file():
        findings.append(f"missing live markdown file: {LIVE_V25_MARKDOWN_PATH}")
        return findings
    # Split the markdown into sections by `## ` headers so we can
    # locate per-attempt sections.
    sections = _split_markdown_sections(md_text)
    for prefix in expected_run_prefixes:
        matching_section: str | None = None
        for sec in sections:
            if prefix in sec:
                matching_section = sec
                break
        if matching_section is None:
            findings.append(
                f"live markdown missing section citing run prefix "
                f"{prefix!r}"
            )
            continue
        for token in _LIVE_SECTION_REQUIRED_TOKENS:
            if token not in matching_section:
                findings.append(
                    f"section for {prefix!r} missing required token "
                    f"{token!r}"
                )
        if "Why execution cannot move forward" not in matching_section and (
            "What the next attempt will change" not in matching_section
        ):
            findings.append(
                f"section for {prefix!r} missing forward-looking "
                f"statement (one of 'Why execution cannot move forward' "
                f"or 'What the next attempt will change' required)"
            )
        if prefix not in cl_text:
            findings.append(
                f"CHANGELOG.md missing entry citing run prefix "
                f"{prefix!r}"
            )
        if (
            "### Live run — Task 019 v2.5" not in cl_text
            and "### Blocked run — Task 019 v2.5" not in cl_text
        ):
            findings.append(
                "CHANGELOG.md missing required v2.5 section header "
                "('### Live run — Task 019 v2.5' or "
                "'### Blocked run — Task 019 v2.5')"
            )
    # §11.54 PAYG-proxy wording lint over the markdown file.
    wording = lint_payg_proxy_wording(md_text)
    for line_no, phrase, line in wording:
        findings.append(
            f"forbidden PAYG-proxy phrase {phrase!r} at "
            f"{LIVE_V25_MARKDOWN_PATH}:{line_no}: {line.strip()!r}"
        )
    return findings


def _split_markdown_sections(text: str) -> list[str]:
    """Split a markdown document by top-level ``## `` headers."""
    sections: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") and buf:
            sections.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        sections.append("\n".join(buf))
    return sections


__all__ = [
    # constants
    "ADAPTIVE_EXPANSION_FACTOR",
    "ADAPTIVE_EXPANSION_PROBES_MAX_PER_ROLE",
    "ADAPTIVE_BRACKET_DEPTH_MAX_PER_ROLE",
    "ADAPTIVE_C2_REPLICATES_MAX_PER_ROLE",
    "C2_ONSET_SEPARATION_MARGIN_TPS",
    "ADAPTIVE_CALIBRATION_MAX_USD",
    "ADAPTIVE_CALIBRATION_WALL_TIME_MAX_MINUTES",
    "ADAPTIVE_APICONNECTIONERROR_CONSECUTIVE_MAX",
    "MIN_REMAINING_USD_FOR_ADAPTIVE_ENTRY",
    "MIN_REMAINING_USD_FOR_EXPANSION",
    "CACHE_HIT_FLOOR_LARGEST",
    "CACHE_HIT_FLOOR_SMALLEST_CONTROL",
    "MINIMUM_RECORDS_AT_SELECTED_TPS",
    "RFC_PINNED_VALUES",
    # schema versions
    "SCHEMA_VERSION_CALIBRATION_RESULT_V25",
    "SCHEMA_VERSION_ADAPTIVE_CALIBRATION_SUMMARY_V25",
    "SCHEMA_VERSION_SMOKE_SUMMARY_V25",
    "SCHEMA_VERSION_EVIDENCE_SUMMARY_V25",
    # enums
    "CALIBRATION_OUTCOMES_V25_EXTENDED",
    "SELECTED_VIA_V25_EXTENDED",
    "V25_NEW_OUTCOMES",
    "V25_NEW_SELECTED_VIA",
    "ADAPTIVE_CAP_TERMINAL_OUTCOMES",
    "C3_OUTCOME",
    "ONSET_BOUND_ELIGIBILITY_REASONS",
    "ADAPTIVE_STEP_NAMES",
    "ADAPTIVE_BUCKET_KEY_RE",
    # banners / regexes
    "AUDITOR_APPROVAL_COMMENT_REGEX",
    "PAYG_NOT_PTU_CAVEAT_BANNER",
    "FORBIDDEN_PAYG_PROXY_PHRASES",
    # exceptions
    "AdaptiveCalibrationYAMLPreflightError",
    "V25SchemaValidationError",
    # dataclasses
    "OnsetEligibilityResult",
    "AggregatedObservation",
    "RoleOnsetInterval",
    "ExpansionPlan",
    "AdmissionDecision",
    # planners + evaluators
    "build_adaptive_cache_bucket_key",
    "compute_onset_eligibility",
    "aggregate_observations_same_tps",
    "compute_role_onset_interval",
    "adaptive_tps_hard_max",
    "adaptive_tps_hard_min",
    "plan_step2_expansion",
    "plan_step3_bracket_midpoint",
    "evaluate_c1",
    "evaluate_c2",
    "evaluate_c3_terminal",
    # validators
    "validate_calibration_result_v25",
    "validate_smoke_summary_v25",
    "validate_evidence_summary_v25",
    "validate_adaptive_calibration_summary",
    "validate_adaptive_calibration_yaml_block",
    # lint
    "lint_payg_proxy_wording",
    "check_v25_live_artifacts",
    "LIVE_V25_MARKDOWN_PATH",
    "CHANGELOG_PATH",
]

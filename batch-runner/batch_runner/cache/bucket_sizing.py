"""Sizing helper for ``prompt_cache_key`` buckets (Task 026).

Implements the sizing formula from the Azure OpenAI PTU Operations
Guide §1 ("prompt_cache_key Bucketing Guide"):

    common_prefix_rpm = common_prefix_tps × 60
    minimum_buckets_at_threshold = ceil(common_prefix_rpm / 15)
    recommended_buckets = ceil(common_prefix_rpm / target_rpm_per_bucket)

* The threshold ``15 req/min`` is **official spec** (Guide §1,
  citing Microsoft Learn) — it is the documented per-(prefix+key)
  point above which requests overflow to additional machines.
* The default ``target_rpm_per_bucket = 10`` is **operational
  inference** per Task 029. The Guide §1 publishes a recommended range
  of ``[8, 12]``; this library picks the midpoint, and the value is
  meant to be reviewed once Task 018's measurement closes the loop.

Worked example reproduced from Guide §1 verbatim::

    common_prefix_tps = 1.4         # measured
    common_prefix_rpm = 84          # 1.4 * 60
    minimum_buckets_at_threshold = 6   # ceil(84 / 15)
    recommended_buckets = 9            # ceil(84 / 10)

This module performs only arithmetic. It does not call out to any
network, file system, or clock; the result is a function of the
inputs alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Guide §1 official-spec threshold: requests per minute per
# (prefix + cache_key) above which routing overflow begins.
THRESHOLD_RPM_PER_BUCKET: int = 15

# Operational-inference default (Task 029 classification). Midpoint of
# the Guide §1 recommended range [8, 12]. Callers should override once
# Task 018's measured curve refines the value.
DEFAULT_TARGET_RPM_PER_BUCKET: int = 10


@dataclass(frozen=True)
class BucketSizingResult:
    """Result of :func:`recommended_bucket_count`.

    Attributes
    ----------
    common_prefix_tps:
        Input — measured request rate sharing the same prefix, in
        requests per second.
    common_prefix_rpm:
        Derived — ``common_prefix_tps * 60``.
    target_rpm_per_bucket:
        Input — the per-bucket target rate the caller is sizing to.
    threshold_rpm_per_bucket:
        Constant — the Guide §1 official-spec overflow threshold (15).
    minimum_buckets_at_threshold:
        ``ceil(common_prefix_rpm / 15)``. Below this count, the bucket
        rate exceeds the documented overflow point on average.
    recommended_buckets:
        ``ceil(common_prefix_rpm / target_rpm_per_bucket)``. The
        operational target, providing headroom above the threshold.
    """

    common_prefix_tps: float
    common_prefix_rpm: float
    target_rpm_per_bucket: int
    threshold_rpm_per_bucket: int
    minimum_buckets_at_threshold: int
    recommended_buckets: int


def recommended_bucket_count(
    *,
    common_prefix_tps: float,
    target_rpm_per_bucket: int = DEFAULT_TARGET_RPM_PER_BUCKET,
) -> BucketSizingResult:
    """Compute the minimum and recommended bucket counts for a prefix.

    Parameters
    ----------
    common_prefix_tps:
        Measured request rate, in requests per second, sharing the
        same cacheable prefix. Must be ``>= 0``.
    target_rpm_per_bucket:
        The per-bucket target rate. Must be strictly positive. The
        Guide §1 recommended range is ``[8, 12]``; values outside this
        range are accepted (the caller may have measured cause to
        deviate) but the default of 10 is the documented operational
        inference.

    Returns
    -------
    BucketSizingResult
        A frozen dataclass exposing both the official-spec minimum
        (computed against the 15 req/min threshold) and the
        recommended count (computed against ``target_rpm_per_bucket``),
        along with the inputs for logging.
    """
    if not isinstance(common_prefix_tps, (int, float)):
        raise TypeError("common_prefix_tps must be a number")
    if math.isnan(common_prefix_tps) or math.isinf(common_prefix_tps):
        raise ValueError("common_prefix_tps must be a finite number")
    if common_prefix_tps < 0:
        raise ValueError("common_prefix_tps must be >= 0")

    if not isinstance(target_rpm_per_bucket, int) or isinstance(
        target_rpm_per_bucket, bool
    ):
        raise TypeError("target_rpm_per_bucket must be an int")
    if target_rpm_per_bucket <= 0:
        raise ValueError("target_rpm_per_bucket must be > 0")

    common_prefix_rpm = common_prefix_tps * 60.0

    minimum = math.ceil(common_prefix_rpm / THRESHOLD_RPM_PER_BUCKET)
    recommended = math.ceil(common_prefix_rpm / target_rpm_per_bucket)

    # When traffic is zero the sizing question is undefined operationally,
    # but the formula yields 0 buckets; preserve that as-is so callers
    # see the input echoed back rather than a synthesized "1".
    return BucketSizingResult(
        common_prefix_tps=float(common_prefix_tps),
        common_prefix_rpm=common_prefix_rpm,
        target_rpm_per_bucket=target_rpm_per_bucket,
        threshold_rpm_per_bucket=THRESHOLD_RPM_PER_BUCKET,
        minimum_buckets_at_threshold=minimum,
        recommended_buckets=recommended,
    )

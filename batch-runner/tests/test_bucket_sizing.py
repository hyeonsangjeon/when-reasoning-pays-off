"""Tests for batch_runner.cache.bucket_sizing (Task 026)."""

from __future__ import annotations

import pytest

from batch_runner.cache.bucket_sizing import (
    DEFAULT_TARGET_RPM_PER_BUCKET,
    THRESHOLD_RPM_PER_BUCKET,
    BucketSizingResult,
    recommended_bucket_count,
)


def test_guide_worked_example_1_4_tps():
    """Guide §1 worked example: 1.4 TPS -> 84 RPM -> min 6, recommended 9."""
    r = recommended_bucket_count(common_prefix_tps=1.4)
    assert isinstance(r, BucketSizingResult)
    assert r.common_prefix_tps == 1.4
    assert r.common_prefix_rpm == pytest.approx(84.0)
    assert r.threshold_rpm_per_bucket == THRESHOLD_RPM_PER_BUCKET == 15
    assert r.target_rpm_per_bucket == DEFAULT_TARGET_RPM_PER_BUCKET == 10
    assert r.minimum_buckets_at_threshold == 6
    assert r.recommended_buckets == 9


def test_zero_tps_yields_zero_buckets():
    r = recommended_bucket_count(common_prefix_tps=0)
    assert r.minimum_buckets_at_threshold == 0
    assert r.recommended_buckets == 0


def test_custom_target_rpm_per_bucket():
    # 1.4 TPS = 84 RPM. With target=12, recommended = ceil(84/12) = 7.
    r = recommended_bucket_count(
        common_prefix_tps=1.4, target_rpm_per_bucket=12
    )
    assert r.recommended_buckets == 7
    # Minimum is independent of target — always uses the 15-RPM threshold.
    assert r.minimum_buckets_at_threshold == 6


def test_ceiling_rounds_up_for_fractional_result():
    # 0.1 TPS -> 6 RPM -> ceil(6/15) = 1, ceil(6/10) = 1.
    r = recommended_bucket_count(common_prefix_tps=0.1)
    assert r.minimum_buckets_at_threshold == 1
    assert r.recommended_buckets == 1


def test_higher_tps_scales_linearly():
    r = recommended_bucket_count(common_prefix_tps=5.0)
    # 5 * 60 = 300 RPM -> min ceil(300/15)=20, rec ceil(300/10)=30.
    assert r.minimum_buckets_at_threshold == 20
    assert r.recommended_buckets == 30


def test_rejects_negative_tps():
    with pytest.raises(ValueError):
        recommended_bucket_count(common_prefix_tps=-1.0)


def test_rejects_non_numeric_tps():
    with pytest.raises(TypeError):
        recommended_bucket_count(common_prefix_tps="1.4")  # type: ignore[arg-type]


def test_rejects_nan_tps():
    with pytest.raises(ValueError):
        recommended_bucket_count(common_prefix_tps=float("nan"))


def test_rejects_zero_target_rpm():
    with pytest.raises(ValueError):
        recommended_bucket_count(
            common_prefix_tps=1.4, target_rpm_per_bucket=0
        )


def test_rejects_bool_target_rpm():
    with pytest.raises(TypeError):
        recommended_bucket_count(
            common_prefix_tps=1.4,
            target_rpm_per_bucket=True,  # type: ignore[arg-type]
        )


def test_result_is_frozen():
    r = recommended_bucket_count(common_prefix_tps=1.4)
    with pytest.raises(Exception):
        r.recommended_buckets = 99  # type: ignore[misc]

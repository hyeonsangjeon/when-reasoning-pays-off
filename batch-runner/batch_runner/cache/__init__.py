"""Public surface for the prompt_cache_key policy library (Task 026).

Re-exports the deterministic key composition function, sizing helper,
and retention-policy helpers so callers can write::

    from batch_runner.cache import cache_key, recommended_bucket_count
    from batch_runner.cache import default_retention, ensure_explicit

The library is grounded in the Azure OpenAI PTU Operations Guide §1
(bucketing + composition) and §2 (retention defaults). It is a pure
composition + sizing helper; it does NOT own the Azure-side cache, it
does NOT measure hit rate (Task 018 owns measurement), and it does NOT
predict per-bucket hit rate (Task 024 replay-simulator territory).
"""

from .bucket_sizing import BucketSizingResult, recommended_bucket_count
from .key_composition import (
    anti_pattern_reasons,
    assert_deterministic,
    cache_key,
)
from .retention_policy import (
    EXTENDED_RETENTION_SUPPORTED_MODELS,
    ImplicitInMemoryError,
    UnknownRetentionValueError,
    default_retention,
    ensure_explicit,
)

__all__ = [
    "BucketSizingResult",
    "EXTENDED_RETENTION_SUPPORTED_MODELS",
    "ImplicitInMemoryError",
    "UnknownRetentionValueError",
    "anti_pattern_reasons",
    "assert_deterministic",
    "cache_key",
    "default_retention",
    "ensure_explicit",
    "recommended_bucket_count",
]

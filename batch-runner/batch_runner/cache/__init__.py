"""Public surface for the prompt_cache_key policy library (Task 026).

Re-exports the deterministic key composition function, sizing helper,
and retention-policy helpers so callers can write::

    from batch_runner.cache import cache_key, recommended_bucket_count
    from batch_runner.cache import default_retention, ensure_explicit

The library is grounded in Microsoft Learn, "Prompt caching with Azure
OpenAI in Microsoft Foundry Models":

    https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching
    2026-05-19 snapshot: https://web.archive.org/web/20260519140800/https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching

The 2026-05-19 snapshot gives ``prompt_cache_key`` routing, the ~15
requests per minute per prefix-and-key threshold above which some
requests overflow to additional machines, and the eleven-model
``prompt_cache_retention`` list with its ``in_memory`` default. The
current page (checked 2026-11-23) scopes the 15 req/min threshold to
models before GPT-5.6 only, and its retention list has twelve models
(it adds gpt-5.5); this library's table stays at the snapshot's
eleven.

The key format and the bucket-sizing formula and target are
operational inference; the "must be explicit" retention rule is
operational policy.

It is a pure composition + sizing helper; it does NOT own the
Azure-side cache, it does NOT measure hit rate (Task 018 owns
measurement), and it does NOT predict per-bucket hit rate (Task 024
replay-simulator territory).
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

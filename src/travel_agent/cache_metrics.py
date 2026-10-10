"""Safe token observations. Unknown usage is never converted into zero usage."""

from collections.abc import Iterable

TOKEN_FIELDS = (
    "prompt_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "completion_tokens",
)


def cache_observation(usage: dict | None) -> dict:
    usage = usage or {}
    counts = {
        field: value if type(value := usage.get(field)) is int and value >= 0 else None
        for field in TOKEN_FIELDS
    }
    prompt, hit, miss = (counts[field] for field in TOKEN_FIELDS[:3])
    consistent = (
        prompt is not None and hit is not None and miss is not None and hit + miss == prompt
    )
    return {
        **counts,
        "cache_counts_consistent": consistent,
        "cache_hit_ratio": hit / prompt if consistent and prompt else None,
    }


def aggregate_cache_observations(records: Iterable[dict]) -> dict:
    """Records represent unique completed effects, not recovery replays.

    Aggregate each available count with its coverage; the weighted cache ratio
    uses only complete, internally consistent observations.
    """
    records = list(records)
    valid = [r for r in records if r.get("cache_counts_consistent")]
    denominator = sum(r["prompt_tokens"] for r in valid)
    return {
        "completed_requests": len(records),
        "cache_observed_requests": len(valid),
        "totals": {
            f: sum(values) if (values := [r[f] for r in records if r.get(f) is not None]) else None
            for f in TOKEN_FIELDS
        },
        "coverage": {f: sum(r.get(f) is not None for r in records) for f in TOKEN_FIELDS},
        "cache_ratio_prompt_tokens": denominator,
        "token_weighted_cache_hit_ratio": (
            sum(r["prompt_cache_hit_tokens"] for r in valid) / denominator if denominator else None
        ),
    }

import json
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from travel_agent.cache_metrics import aggregate_cache_observations, cache_observation
from travel_agent.runtime_context import (
    RUNTIME_MARKER,
    build_policy_message,
    build_runtime_message,
    require_closed_tool_batch,
    runtime_state,
)


def test_delta_clock_crosses_day_and_zone_without_rewriting_old_snapshots():
    first_time = datetime(2026, 10, 10, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    first = build_runtime_message(first_time, "Asia/Shanghai", [], {"current": None})
    history = [{"role": "user", "content": "已订，不要改"}, first]
    original = deepcopy(history)
    next_time = datetime(2026, 10, 11, 0, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    update = build_runtime_message(
        next_time, "Asia/Shanghai", [], {"current": None}, history=history
    )
    delta = json.loads(update["content"][len(RUNTIME_MARKER) :])
    assert set(delta) == {"current_time", "current_date", "current_weekday"}
    assert history == original
    state = runtime_state([*history, update])
    assert state["current_date"] == "2026-10-11" and state["current_weekday"] == "星期日"
    assert state["timezone"] == "Asia/Shanghai" and state["planning"]["current"] is None
    overseas = next_time.astimezone(ZoneInfo("America/Chicago"))
    changed = build_runtime_message(overseas, "America/Chicago", [], history=[*history, update])
    state = runtime_state([*history, update, changed])
    assert state["current_date"] == "2026-10-10" and state["timezone"] == "America/Chicago"


def test_index_updates_replace_fields_preserve_null_and_never_create_user_messages():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    planning = {
        "user_message_references": [{"message_id": "user1", "user_message_number": 1}],
        "available_sources": [{"id": "source1"}],
        "run_budget": {"remaining": 11},
    }
    first = build_runtime_message(now, "Asia/Shanghai", [], planning)
    changed = {
        **planning,
        "available_sources": [],
        "run_budget": {"remaining": 10},
        "current": None,
    }
    second = build_runtime_message(now, "Asia/Shanghai", [], changed, history=[first])
    delta = json.loads(second["content"][len(RUNTIME_MARKER) :])
    assert "user_message_references" not in delta["planning"]
    assert runtime_state([first, second])["planning"] == changed
    assert first["role"] == second["role"] == "system"
    assert build_policy_message() == build_policy_message()
    assert "current_time" not in build_policy_message()["content"]


def test_runtime_requires_aware_clock():
    with pytest.raises(ValueError, match="timezone-aware"):
        build_runtime_message(datetime(2026, 10, 10), "Asia/Shanghai", [])


@pytest.mark.parametrize("remaining", [[], [{"role": "tool", "tool_call_id": "a"}]])
def test_snapshots_reject_unfinished_or_split_tool_batches(remaining):
    assistant = {
        "role": "assistant",
        "reasoning_content": "private",
        "tool_calls": [{"id": "a"}, {"id": "b"}],
    }
    with pytest.raises(ValueError, match="tool"):
        require_closed_tool_batch([assistant, *remaining])
    with pytest.raises(ValueError, match="tool"):
        require_closed_tool_batch([assistant, {"role": "system", "content": "invalid"}])
    require_closed_tool_batch(
        [assistant, {"role": "tool", "tool_call_id": "a"}, {"role": "tool", "tool_call_id": "b"}]
    )


def test_usage_unknown_inconsistent_and_boolean_counts_are_not_zero_hit_rates():
    for usage in [
        None,
        {},
        {"prompt_tokens": True},
        {"prompt_tokens": 10, "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 9},
    ]:
        observation = cache_observation(usage)
        assert observation["cache_hit_ratio"] is None
        assert not observation["cache_counts_consistent"]


def test_aggregate_uses_token_weighting_and_reports_partial_coverage():
    records = [
        cache_observation(
            {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 90,
                "prompt_cache_miss_tokens": 10,
                "completion_tokens": 20,
            }
        ),
        cache_observation(
            {"prompt_tokens": 900, "prompt_cache_hit_tokens": 90, "prompt_cache_miss_tokens": 810}
        ),
        cache_observation({}),
    ]
    report = aggregate_cache_observations(records)
    assert report["completed_requests"] == 3 and report["cache_observed_requests"] == 2
    assert report["token_weighted_cache_hit_ratio"] == 0.18
    assert report["coverage"]["completion_tokens"] == 1
    assert aggregate_cache_observations([])["token_weighted_cache_hit_ratio"] is None

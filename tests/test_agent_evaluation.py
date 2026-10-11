"""Evaluation observes the durable API without mistaking replay for LLM quality."""

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from travel_agent import evaluation
from travel_agent.evaluation import (
    EvalCase,
    EvalSuite,
    ScriptedModel,
    _metrics,
    _result_id_index,
    _usage,
    assertion_results,
    load_suite,
    pointer_value,
    replay_registry,
    run_case,
    run_evaluation,
)
from travel_tools.config import Settings

SUITE = Path(__file__).resolve().parents[1] / "evals" / "agent_cases.json"


def minimal_case(**changes):
    return EvalCase.model_validate(
        {
            "id": "minimal",
            "description": "评测契约演示，不判定模型语义。",
            "turns": [{"content": "你好"}],
            "scripted_replies": [{"kind": "final", "content": "你好。"}],
            "assertions": [
                {
                    "id": "completed",
                    "metric": "task_completion",
                    "pointer": "/conversation/status",
                    "operator": "equals",
                    "expected": "completed",
                    "note": "只检查运行结束。",
                }
            ],
            **changes,
        }
    )


async def test_versioned_chinese_suite_uses_isolated_api_sse_and_no_network(tmp_path, monkeypatch):
    user_database = tmp_path / "real-user.db"
    user_database.write_bytes(b"do not touch real user sessions")
    config = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///" + user_database.as_posix(),
        deepseek_api_key="model-secret",
        amap_api_key="amap-secret",
        browser_queries_enabled=True,
        flyai_enable_demo=True,
    )
    original_send = httpx.AsyncClient.send

    async def forbid_network(client, request, **kwargs):
        assert request.url.host == "evaluation", "offline evaluation attempted network I/O"
        return await original_send(client, request, **kwargs)

    def forbid_live_model(*args, **kwargs):
        pytest.fail("offline evaluation tried to select a live model")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbid_network)
    monkeypatch.setattr(evaluation, "_live_model", forbid_live_model)
    suite = load_suite(SUITE)
    report = await run_evaluation(suite, settings=config)
    assert report["mode"] == "offline"
    assert len(report["cases"]) == len(suite.cases)
    assert user_database.read_bytes() == b"do not touch real user sessions"
    assert "model-secret" not in str(report) and "amap-secret" not in str(report)
    assert str(user_database) not in str(report)
    for case in report["cases"]:
        assert case["run_outcome"] == "completed"
        assert case["versions"]["engine"] == "langgraph-v1"
        assert case["versions"]["context_version"] == "append-only-v1"
        assert len(case["versions"]["prompt_version"]) == 64
        assert len(case["versions"]["tools_version"]) == 64
        assert case["model"]["model"] == "scripted-runtime-replay-v1"
        assert case["usage"]["status"] == "unknown" and case["usage"]["totals"] is None
        assert all(record["usage"] is None for record in case["model_requests"])
        assert all(
            metric["agent_quality_status"] == "unknown" for metric in case["metrics"].values()
        )
        assert not any(check["status"] == "failed" for check in case["assertions"])
        assert case["elapsed_seconds"] > 0
        assert case["preferences"] == case["initial_preferences_profile"]
        assert case["context_user_preferences"]["entries"] == case["preferences"]["entries"]
        assert case["runs"]
        assert all(run["active_seconds"] >= 0 for run in case["runs"])
        assert sum(run["model_requests"] for run in case["runs"]) == len(case["model_requests"])
        assert "final_saved_before_end" in {mark["name"] for mark in case["runtime_marks"]}
        delivered = [event for turn in case["turns"] for event in turn.get("events", [])]
        replay = case["replayed_events"]
        by_sequence = {event["event_seq"]: event for event in replay}
        assert delivered == [by_sequence[event["event_seq"]] for event in delivered]
        assert [event["event_seq"] for event in replay] == list(range(1, len(replay) + 1))
    edit = next(
        case for case in report["cases"] if case["case_id"] == "locked_local_edit_unknown_cost"
    )
    assert edit["tool_results"]["review_itinerary"]["latest"]["data"]["state"] == "read_only"
    assert edit["conversation"]["itinerary"]["revision"] == 2
    compare = next(
        case for case in report["cases"] if case["case_id"] == "common_basis_candidate_comparison"
    )
    assert compare["tool_results"]["compare_itinerary_options"]["latest"]["data"][
        "pareto_option_ids"
    ] == ["cheap"]
    atomic = next(case for case in report["cases"] if case["case_id"] == "atomic_claim_review")
    reviewed = atomic["tool_results"]["review_itinerary"]["latest"]["data"]
    assert {claim["id"]: claim["status"] for claim in reviewed["claims"]} == {
        "wrong-party": "fail",
        "planned-amount": "pass",
        "within-budget": "unknown",
    }
    assert atomic["conversation"]["itinerary"] is None
    assert all(tool["name"] == "review_itinerary" for tool in atomic["tools"])


def test_initial_preferences_are_typed_and_old_cases_keep_empty_default():
    assert minimal_case().initial_preferences == []
    case = minimal_case(
        initial_preferences=[
            {
                "id": "transport",
                "label": "交通偏好",
                "value": "通常优先坐高铁",
            }
        ]
    )
    assert case.initial_preferences[0].id == "transport"
    with pytest.raises(ValidationError):
        minimal_case(
            initial_preferences=[
                {
                    "id": "invalid id",
                    "label": "交通偏好",
                    "value": "通常优先坐高铁",
                }
            ]
        )


@pytest.mark.parametrize(
    "case_id",
    [
        "saved_preference_current_override",
        "explicit_preference_suggestion_pending",
    ],
)
async def test_preferences_use_formal_seed_api_without_approving_suggestions(case_id, monkeypatch):
    import json

    requests = []
    original_send = httpx.AsyncClient.send

    async def observe(client, request, **kwargs):
        assert request.url.host == "evaluation"
        requests.append((request.method, request.url.path, request.content))
        return await original_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", observe)
    case = next(item for item in load_suite(SUITE).cases if item.id == case_id)
    result = await run_case(case, mode="offline", settings=Settings(_env_file=None))
    assert result["preferences"] == result["initial_preferences_profile"]
    assert result["context_user_preferences"]["entries"] == result["preferences"]["entries"]
    assert result["run_outcome"] == "completed"
    assert not any(check["status"] == "failed" for check in result["assertions"])
    assert not any(path.endswith("/apply") for _, path, _ in requests)
    puts = [
        (index, json.loads(content))
        for index, (method, path, content) in enumerate(requests)
        if method == "PUT" and path == "/preferences"
    ]
    if case.initial_preferences:
        assert len(puts) == 1
        assert puts[0][1] == {
            "base_revision": 0,
            "upsert": [item.model_dump(mode="json") for item in case.initial_preferences],
        }
        new_conversation = next(
            index
            for index, (method, path, _) in enumerate(requests)
            if method == "POST" and path == "/conversations"
        )
        assert puts[0][0] < new_conversation
        assert result["preferences"]["entries"][0]["source"]["kind"] == "manual"
        assert result["preference_proposals"]["items"] == []
    else:
        assert puts == [] and result["preferences"]["revision"] == 0
        pending = result["preference_proposals"]["items"][0]
        assert pending["status"] == "pending"
        assert pending["user_reference"]["message_id"] == (
            result["conversation"]["id"] + "/eval-" + case.id + "-0"
        )
        assert pending["user_reference"]["quote"] in case.turns[0].content
        assert pending["base_revision"] == result["context_user_preferences"]["revision"]


def test_atomic_claim_index_preserves_identity_when_result_order_changes():
    result = {
        "data": {
            "claims": [
                {"id": "wrong-party", "status": "fail"},
                {"id": "planned-amount", "status": "pass"},
                {"id": "within-budget", "status": "unknown"},
            ]
        }
    }
    indexed = _result_id_index(result)
    result["data"]["claims"].reverse()
    assert _result_id_index(result) == indexed
    case = next(c for c in load_suite(SUITE).cases if c.id == "atomic_claim_review")
    checks = case.model_copy(
        update={"assertions": [check for check in case.assertions if check.id.endswith("-status")]}
    )
    assertions = assertion_results(
        checks,
        {
            "tool_results": {
                "review_itinerary": {
                    "latest_by_id": indexed,
                }
            }
        },
    )
    assert all(assertion["status"] == "passed" for assertion in assertions)


async def test_provider_replays_match_schema_normalized_arguments_without_order():
    def fixture(query, label):
        return {
            "tool_name": "search_web",
            "arguments": {"query": query},
            "data": {"query": query, "results": [], "warnings": [label]},
        }

    case = minimal_case(provider_replays=[fixture("first", "alpha"), fixture("second", "beta")])
    config = Settings(
        _env_file=None,
        amap_api_key=None,
        deepseek_api_key=None,
        browser_queries_enabled=False,
        flyai_enable_demo=False,
    )
    async with httpx.AsyncClient() as client:
        registry = replay_registry(config, client, case)
        try:
            second = await registry.dispatch("search_web", {"query": "second"})
            first = await registry.dispatch("search_web", {"query": "first", "count": 10})
            assert second.status == first.status == "ok"
            assert second.data["warnings"] == ["beta"]
            assert first.data["warnings"] == ["alpha"]
            unmatched = await registry.dispatch("search_web", {"query": "first", "count": 3})
            assert unmatched.status == "error" and unmatched.error.code == "evaluation_replay_miss"
            missing = await registry.dispatch("fetch_webpage", {"url": "https://example.com"})
            assert missing.status == "error" and missing.error.code == "tool_unavailable"
        finally:
            await registry.close()


def test_assertions_do_not_score_keywords_or_absence_as_null():
    case = minimal_case(
        assertions=[
            {
                "id": "substring",
                "metric": "clarification_relevance",
                "operator": "collection_contains",
                "pointer": "/text",
                "expected": "日期",
                "note": "文本关键词不是语义证明。",
            },
            {
                "id": "null",
                "metric": "evidence_support",
                "operator": "equals",
                "pointer": "/missing",
                "expected": None,
                "note": "字段不存在不能当作明确未知。",
            },
            {
                "id": "semantic",
                "metric": "task_completion",
                "operator": "unknown",
                "note": "无独立语义oracle。",
            },
        ]
    )
    results = assertion_results(case, {"text": "日期在哪里？"})
    assert [item["status"] for item in results] == ["unknown", "failed", "unknown"]
    assert _metrics(results, "offline")["evidence_support"]["agent_quality_status"] == "unknown"
    completed = assertion_results(minimal_case(), {"conversation": {"status": "completed"}})
    assert _metrics(completed, "live_model")["task_completion"]["agent_quality_status"] == "unknown"
    assert pointer_value({"a/b": {"~x": [None]}}, "/a~1b/~0x/0") is None
    with pytest.raises(KeyError):
        pointer_value({"a": []}, "/a/-1")


def test_real_usage_is_aggregated_and_missing_fields_stay_unknown():
    usage = _usage(
        [
            {"usage": {"prompt_tokens": 10}},
            {"usage": {"prompt_tokens": 20, "completion_tokens": 3}},
        ],
        scripted=False,
    )
    assert usage["status"] == "partial"
    assert usage["totals"] == {"completion_tokens": None, "prompt_tokens": 30}
    assert _usage([{"usage": None}], scripted=False)["status"] == "unknown"
    assert _usage([{"usage": {"prompt_tokens": 30}}], scripted=True)["status"] == "unknown"


async def test_live_model_mode_records_observed_usage_with_replay_providers(monkeypatch):
    case = minimal_case()

    class ObservedModel(ScriptedModel):
        model, provider = "configured-test-model", "test_provider"

        async def complete(self, messages, tools):
            reply = await super().complete(messages, tools)
            reply.usage = {"prompt_tokens": 17, "completion_tokens": 5}
            return reply

    monkeypatch.setattr(
        evaluation, "_live_model", lambda settings, client: ObservedModel(case.scripted_replies)
    )
    result = await run_case(case, mode="live_model", settings=Settings(_env_file=None))
    assert result["provider_mode"] == "recorded_replay"
    assert result["model"]["model"] == "configured-test-model"
    assert result["usage"]["totals"] == {"prompt_tokens": 17, "completion_tokens": 5}
    assert result["run_outcome"] == "completed"
    assert result["metrics"]["task_completion"]["agent_quality_status"] == "unknown"


async def test_model_failure_and_waiting_end_are_recorded_without_invented_usage():
    failure = minimal_case(
        scripted_replies=[{"kind": "error", "error_code": "timeout"}],
        assertions=[
            {
                "id": "stopped",
                "metric": "task_completion",
                "pointer": "/conversation/status",
                "operator": "equals",
                "expected": "error",
                "note": "记录实际错误终态。",
            }
        ],
    )
    result = await run_case(failure, mode="offline")
    assert result["run_outcome"] == "error"
    assert result["model_requests"][0]["error_code"] == "timeout"
    assert result["usage"]["status"] == "unknown"
    waiting = minimal_case(
        scripted_replies=[{"kind": "ask", "content": "你从哪里出发？"}],
        assertions=[
            {
                "id": "waiting",
                "metric": "task_completion",
                "pointer": "/conversation/status",
                "operator": "equals",
                "expected": "waiting_user",
                "note": "追问等待不等于任务完成。",
            }
        ],
    )
    result = await run_case(waiting, mode="offline")
    assert result["run_outcome"] == "waiting_user"
    assert result["conversation"]["question_id"]


async def test_unknown_case_ids_invalid_schema_and_safe_harness_failure(monkeypatch):
    suite = EvalSuite(
        schema_version="1.0", suite_version="test", description="test", cases=[minimal_case()]
    )
    with pytest.raises(ValueError, match="Unknown evaluation case"):
        await run_evaluation(suite, case_ids=["not-in-suite"])
    with pytest.raises(ValueError, match="Unknown evaluation mode"):
        await run_evaluation(suite, mode="invalid")
    with pytest.raises(ValidationError, match="case IDs must be unique"):
        EvalSuite(suite_version="test", description="test", cases=[minimal_case(), minimal_case()])

    async def fail(*args, **kwargs):
        raise RuntimeError("private-provider-payload-with-secret")

    monkeypatch.setattr(evaluation, "run_case", fail)
    report = await run_evaluation(suite)
    assert report["cases"][0]["run_outcome"] == "harness_error"
    assert report["cases"][0]["error_type"] == "RuntimeError"
    assert "private-provider-payload" not in str(report)

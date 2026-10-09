import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass

import pytest

from travel_agent.models import ModelError
from travel_agent.runner import AgentRunner, RunLimits, repair_history
from travel_agent.tool_encoding import decode_tool_result
from travel_tools.common import StrictModel, ToolPayload
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.schemas.quotes import SearchTrainsInput, SearchTrainsOutput


class Input(StrictModel):
    query: str


class Output(ToolPayload):
    value: str


def make_registry(handler=None):
    async def search(arguments):
        return Output(value=arguments.query)

    registry = ToolRegistry()
    registry.register(
        ToolSpec("search_places", "search", Input, Output, handler or search, "ready")
    )
    registry.register(
        ToolSpec("search_coaches", "coaches", Input, Output, handler or search, "ready")
    )
    return registry


@dataclass
class FakeReply:
    message: dict
    finish_reason: str = "stop"
    usage: dict | None = None


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def call(name="search_places", arguments=None, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"query": "上海"}, ensure_ascii=False),
        },
    }


def action(*calls, reasoning="internal secret reasoning"):
    return FakeReply(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": reasoning,
            "tool_calls": list(calls),
        },
        "tool_calls",
    )


def answer(content="这是行程安排。"):
    return FakeReply({"role": "assistant", "content": content, "reasoning_content": "hidden final"})


def emitter():
    events = []

    async def emit(event):
        events.append(event)

    return events, emit


def tool_payloads(history):
    return [json.loads(message["content"]) for message in history if message["role"] == "tool"]


async def test_lossless_tool_tables_are_persisted_and_replayed_without_history_changes():
    class ManyResults(ToolPayload):
        records: list[dict]

    records = [{"id": i, "inventory_status": "unknown", "duration_s": None} for i in range(30)]

    async def search(arguments):
        return ManyResults(records=records)

    registry = ToolRegistry()
    registry.register(ToolSpec("search_places", "search", Input, ManyResults, search, "ready"))
    model = FakeModel(action(call()), answer())
    checkpoints = []

    async def checkpoint(history):
        checkpoints.append(deepcopy(history))

    events, emit = emitter()
    outcome = await AgentRunner(model, registry).run([], emit, checkpoint=checkpoint)
    tool = outcome.history[1]
    assert json.loads(tool["content"])["data"]["encoding"] == "travel-table-v1"
    assert decode_tool_result(tool["content"])["data"]["records"] == records
    assert model.requests[1][0][1:] == outcome.history[:-1]
    assert checkpoints[-1] == outcome.history
    assert "inventory_status" not in json.dumps(events)


async def test_question_resume_full_history_and_reasoning_private():
    first_model = FakeModel(action(call("ask_user", {"message": "从哪里出发，哪天出行？"})))
    events, emit = emitter()
    initial = [{"role": "user", "content": "帮我安排一次出游"}]
    result = await AgentRunner(first_model, make_registry()).run(initial, emit)
    assert result.status == "waiting_user"
    assert len(initial) == 1
    assert result.history[1]["reasoning_content"] == "internal secret reasoning"
    assert tool_payloads(result.history)[0]["data"]["state"] == "waiting_user"
    assert events[-1] == {
        "type": "message",
        "role": "assistant",
        "kind": "question",
        "content": "从哪里出发，哪天出行？",
    }
    resumed = result.history + [{"role": "user", "content": "上海出发，下周六，两个人"}]
    second_model = FakeModel(answer())
    final = await AgentRunner(second_model, make_registry()).run(resumed, emit)
    assert final.status == "completed"
    assert second_model.requests[0][0][1:] == resumed
    assert final.history[: len(resumed)] == resumed
    assert final.history[-1]["reasoning_content"] == "hidden final"
    assert "reasoning" not in json.dumps(events)
    assert "hidden final" not in json.dumps(events)


async def test_question_protocol_does_not_limit_business_question_count():
    question = "从哪出发？何时出发？可用几天？同行几人？"
    model = FakeModel(action(call("ask_user", {"message": question})))
    events, emit = emitter()
    outcome = await AgentRunner(model, make_registry()).run(
        [{"role": "user", "content": "帮我计划一次出行"}], emit
    )
    assert outcome.status == "waiting_user" and outcome.content == question
    assert events[-1]["content"] == question
    assert tool_payloads(outcome.history)[0]["data"]["message"] == question


async def test_later_tool_evidence_can_trigger_another_question_in_same_conversation():
    _, emit = emitter()
    first = await AgentRunner(
        FakeModel(action(call("ask_user", {"message": "哪天出行？"}, "q1"))), make_registry()
    ).run([{"role": "user", "content": "帮我安排出游"}], emit)
    resumed = first.history + [{"role": "user", "content": "周一，一个人，必须去该场馆。"}]
    second_model = FakeModel(
        action(call(call_id="evidence")),
        action(call("ask_user", {"message": "查询发现周一闭馆，要改日期还是换场馆？"}, "q2")),
    )
    second = await AgentRunner(second_model, make_registry()).run(resumed, emit)
    assert second.status == "waiting_user"
    assert second.history[: len(resumed)] == resumed
    assert len([r for r in tool_payloads(second.history) if r["tool_name"] == "ask_user"]) == 2
    third_model = FakeModel(answer("按你选择更换场馆，继续安排。"))
    third = await AgentRunner(third_model, make_registry()).run(
        second.history + [{"role": "user", "content": "日期不变，换场馆。"}], emit
    )
    assert third.status == "completed"
    assert third_model.requests[0][0][1:-1] == second.history


async def test_multiple_tools_then_autonomous_answer_and_environment():
    model = FakeModel(action(call(call_id="a"), call(call_id="b")), answer())
    events, emit = emitter()
    result = await AgentRunner(model, make_registry()).run(
        [{"role": "user", "content": "安排上海一天"}], emit
    )
    assert result.status == "completed"
    assert len(model.requests) == 2
    assert [payload["call_id"] for payload in tool_payloads(result.history)] == ["a", "b"]
    assert model.requests[1][0][1:] == result.history[:-1]
    system = model.requests[0][0][0]["content"]
    assert "Asia/Shanghai" in system and "current_time" in system
    assert "search_coaches" in system
    assert [event["status"] for event in events if event["type"] == "tool_finished"] == ["ok", "ok"]
    assert all("arguments" not in event and "data" not in event for event in events)


async def test_ready_ordinary_coach_tool_is_exported_and_dispatched():
    model = FakeModel(action(call("search_coaches")), answer("这是普通大巴候选。"))
    _, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit)
    assert result.status == "completed"
    assert "search_coaches" in [tool["function"]["name"] for tool in model.requests[0][1]]
    assert tool_payloads(result.history)[0]["status"] == "ok"


async def test_blocked_ticket_source_is_observation_then_model_can_ask_for_choice():
    from travel_tools.common import ToolFailure

    async def blocked(arguments):
        raise ToolFailure("provider_access_blocked", "Official source blocked; no bypass.")

    registry = ToolRegistry()
    registry.register(
        ToolSpec("search_trains", "query", SearchTrainsInput, SearchTrainsOutput, blocked, "ready")
    )
    model = FakeModel(
        action(
            call(
                "search_trains",
                {
                    "origin": {"query": "上海虹桥"},
                    "destination": {"query": "杭州东"},
                    "departure_date": "2026-10-16",
                    "travelers": {"adults": 1},
                    "provider": "12306",
                },
                "train",
            )
        ),
        action(call("ask_user", {"message": "官方查询受阻，要改查库存未知的候选吗？"}, "choice")),
    )
    _, emit = emitter()
    outcome = await AgentRunner(model, registry).run([], emit)
    assert outcome.status == "waiting_user" and outcome.error is None
    assert tool_payloads(outcome.history)[0]["error"]["code"] == "provider_access_blocked"


async def test_mixed_question_rejected_without_executing_any_tool():
    async def forbidden(arguments):
        pytest.fail("Mixed batch must not execute")

    model = FakeModel(
        action(call("ask_user", {"message": "何时？"}, "a"), call(call_id="b")),
        action(call("ask_user", {"message": "请提供出行日期。"}, "c")),
    )
    events, emit = emitter()
    result = await AgentRunner(model, make_registry(forbidden)).run([], emit)
    assert result.status == "waiting_user"
    payloads = tool_payloads(result.history)
    assert [item["error"]["code"] for item in payloads[:2]] == ["ask_user_must_be_alone"] * 2
    assert len([event for event in events if event["type"] == "message"]) == 1


@pytest.mark.parametrize(
    "arguments", [{}, {"message": " "}, {"message": 123}, {"message": "日期？", "extra": 1}]
)
async def test_invalid_question_can_be_corrected(arguments):
    invalid = call("ask_user")
    invalid["function"]["arguments"] = json.dumps(arguments)
    model = FakeModel(action(invalid), answer())
    _, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit)
    assert result.status == "completed"
    assert tool_payloads(result.history)[0]["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("bad_json", ["{", "[]", "null", '{"query": NaN}'])
async def test_invalid_json_and_unknown_tools_are_observations(bad_json):
    invalid = call(call_id="bad")
    invalid["function"]["arguments"] = bad_json
    model = FakeModel(action(invalid, call("missing", call_id="unknown")), answer())
    _, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit)
    assert result.status == "completed"
    assert [item["error"]["code"] for item in tool_payloads(result.history)] == [
        "invalid_arguments",
        "unknown_tool",
    ]


async def test_step_limit_stops_loop_preserving_results():
    model = FakeModel(action(call(call_id="a")), action(call(call_id="b")))
    _, emit = emitter()
    result = await AgentRunner(model, make_registry(), RunLimits(max_steps=2)).run([], emit)
    assert result.status == "error" and result.error["code"] == "step_limit"
    assert len(tool_payloads(result.history)) == 2


async def test_tool_calls_above_previous_limits_all_execute_and_history_is_complete():
    # Exceed both the former default (24) and former configurable maximum (100).
    first = [call(call_id=f"first-{i}") for i in range(60)]
    second = [call(call_id=f"second-{i}") for i in range(60)]
    model = FakeModel(action(*first), action(*second), answer())
    _, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit)
    assert result.status == "completed" and result.error is None
    assert len(tool_payloads(result.history)) == 120
    assert all(item["status"] == "ok" for item in tool_payloads(result.history))
    assert model.requests[-1][0][1:] == result.history[:-1]


async def test_large_history_and_answer_are_passed_through_without_length_limits():
    history = [{"role": "user", "content": "行程" * 300000}]
    content = "完整方案" * 100000
    model = FakeModel(answer(content))
    _, emit = emitter()
    result = await AgentRunner(model, make_registry()).run(history, emit)
    assert result.status == "completed"
    assert model.requests[0][0][1:] == history
    assert result.history[:1] == history
    assert result.content == content


async def test_question_above_previous_length_limit_is_returned_in_full():
    question = "请确认出行安排。" * 1000
    model = FakeModel(action(call("ask_user", {"message": question})))
    events, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit)
    assert result.status == "waiting_user" and result.content == question
    assert events[-1]["content"] == question
    definition = model.requests[0][1][0]
    assert "maxLength" not in definition["function"]["parameters"]["properties"]["message"]


@pytest.mark.parametrize(
    "error", [ModelError("rate_limit", "provider secret", True), RuntimeError("private details")]
)
async def test_model_failure_is_not_retried_or_leaked(error):
    model = FakeModel(error)
    events, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit)
    assert result.status == "error" and len(model.requests) == 1
    assert "secret" not in result.content and "private details" not in result.content
    assert not events


@pytest.mark.parametrize(
    ("code", "explanation"),
    [
        ("output_truncated", "长度上限"),
        ("timeout", "请求超时"),
        ("rate_limited", "过于频繁"),
        ("authentication_error", "鉴权"),
    ],
)
async def test_model_failure_explains_cause_and_preserves_completed_queries(code, explanation):
    model = FakeModel(action(call()), ModelError(code, "provider secret"))
    events, emit = emitter()
    result = await AgentRunner(model, make_registry()).run(
        [{"role": "user", "content": "安排四天出行"}], emit
    )
    assert result.status == "error" and result.error["code"] == code
    assert explanation in result.content
    assert "provider secret" not in result.content
    assert len(model.requests) == 2
    assert len(tool_payloads(result.history)) == 1
    assert tool_payloads(result.history)[0]["status"] == "ok"
    assert not any(event["type"] == "message" for event in events)


async def test_tool_exception_is_safe_and_model_can_continue():
    registry = make_registry()

    async def broken(*args, **kwargs):
        raise RuntimeError("provider credential")

    registry.dispatch = broken
    model = FakeModel(action(call()), answer())
    _, emit = emitter()
    result = await AgentRunner(model, registry).run([], emit)
    assert result.status == "completed"
    assert tool_payloads(result.history)[0]["error"]["code"] == "tool_exception"
    assert "credential" not in json.dumps(result.history)


async def test_timeout_repairs_all_pending_tool_results():
    async def slow(arguments):
        await asyncio.sleep(1)
        return Output(value="late")

    model = FakeModel(action(call(call_id="a"), call(call_id="b")))
    _, emit = emitter()
    result = await AgentRunner(model, make_registry(slow), RunLimits(max_run_seconds=0.01)).run(
        [], emit
    )
    assert result.status == "error" and result.error["code"] == "run_timeout"
    assert [item["error"]["code"] for item in tool_payloads(result.history)] == ["run_timeout"] * 2


async def test_external_cancellation_repairs_history():
    async def cancel(arguments):
        raise asyncio.CancelledError

    model = FakeModel(action(call()))
    _, emit = emitter()
    result = await AgentRunner(model, make_registry(cancel)).run([], emit)
    assert result.error["code"] == "interrupted"
    assert tool_payloads(result.history)[0]["error"]["code"] == "interrupted"


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "unknown"])
async def test_model_truncation_never_becomes_success(finish_reason):
    model = FakeModel(FakeReply({"role": "assistant", "content": "未完成"}, finish_reason))
    events, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit)
    assert result.error["code"] == "model_incomplete"
    assert result.history[-1]["content"] == "未完成" and not events


def test_repair_preserves_original_messages_and_only_fills_missing_results():
    assistant = action(call(call_id="a"), call(call_id="b")).message
    already = {"role": "tool", "tool_call_id": "a", "content": '{"status":"ok"}'}
    original = [{"role": "user", "content": "安排"}, assistant, already]
    repaired = repair_history(original)
    assert repaired[:3] == original and len(original) == 3
    assert repaired[3]["tool_call_id"] == "b"
    assert json.loads(repaired[3]["content"])["error"]["code"] == "interrupted"
    assert repair_history(repaired) == repaired
    with_next_user = original + [{"role": "user", "content": "继续"}]
    fixed = repair_history(with_next_user)
    assert fixed[-1] == with_next_user[-1] and fixed[-2]["tool_call_id"] == "b"


async def test_unfinished_history_is_repaired_before_next_request():
    initial = [action(call()).message]
    model = FakeModel(answer())
    _, emit = emitter()
    result = await AgentRunner(model, make_registry()).run(initial, emit)
    assert result.status == "completed"
    assert model.requests[0][0][-1]["role"] == "tool"
    assert tool_payloads(result.history)[0]["error"]["code"] == "interrupted"


async def test_unknown_timezone_and_invalid_limits_fail_explicitly():
    model = FakeModel()
    _, emit = emitter()
    result = await AgentRunner(model, make_registry()).run([], emit, timezone_name="Not/APlace")
    assert result.error["code"] == "invalid_timezone" and not model.requests
    with pytest.raises(ValueError):
        AgentRunner(model, make_registry(), RunLimits(max_steps=0))


async def test_checkpoint_saves_assistant_before_execution_and_each_tool_result():
    snapshots = []

    async def checkpoint(history):
        snapshots.append(deepcopy(history))

    async def checked_handler(arguments):
        assert snapshots[-1][-1]["role"] == "assistant"
        return Output(value=arguments.query)

    model = FakeModel(action(call()), answer())
    _, emit = emitter()
    result = await AgentRunner(model, make_registry(checked_handler)).run(
        [], emit, checkpoint=checkpoint
    )
    assert result.status == "completed"
    assert [snapshot[-1]["role"] for snapshot in snapshots if snapshot] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert snapshots[-1] == result.history
    assert snapshots[1][0]["reasoning_content"] == "internal secret reasoning"

import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from test_graph_runtime import Model, api_client, final, question, send

from scripts.report_context_cache import report
from travel_agent import durable_storage
from travel_agent.durable_storage import DurableStore, StaleOwner
from travel_agent.graph_runtime import GraphRuntime
from travel_agent.runner import AgentRunner, RunLimits
from travel_agent.runtime_context import require_closed_tool_batch, runtime_state
from travel_agent.storage import ConversationStore
from travel_tools.registry import ToolRegistry


async def test_diagnostics_do_not_create_a_missing_sqlite_database(tmp_path):
    from sqlalchemy.exc import OperationalError

    path = tmp_path / "must-not-create.db"
    with pytest.raises(OperationalError):
        await report("sqlite+aiosqlite:///" + path.as_posix())
    assert not path.exists()


async def test_graph_requests_extend_exact_prefix_across_tools_questions_and_new_turn(tmp_path):
    replies = [question(), question("哪天？"), final(), final("局部修改完成")]
    model = Model(*replies)
    original_tools = []
    complete = model.complete

    async def observe(messages, tools):
        original_tools.append(deepcopy(tools))
        require_closed_tool_batch(messages)
        return await complete(messages, tools)

    model.complete = observe
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        qid = None
        for index, content in enumerate(["想出游", "上海", "你决定日期", "只修改第二天"], 1):
            await send(client, cid, content, f"r{index}", qid)
            public = (await client.get(f"/conversations/{cid}")).json()
            qid = public["question_id"]
        assert public["status"] == "completed"
        for previous, following in zip(model.requests, model.requests[1:], strict=False):
            assert following[: len(previous)] == previous
        assert all(tools == original_tools[0] for tools in original_tools)
        history = await service.facts.history(cid)
        assert [m for m in history if m["role"] == "assistant"] == [r.message for r in replies]
        assert len([m for m in history if m["role"] == "user"]) == 4
        refs = runtime_state(history)["planning"]["user_message_references"]
        assert [r["user_message_number"] for r in refs] == [1, 2, 3, 4]
        assert len({r["message_id"] for r in refs}) == 4
        assert (
            runtime_state(history)["planning"]["run_budget"]["model_requests_remaining_after_this"]
            == 11
        )
        assert "运行状态快照" not in json.dumps(public, ensure_ascii=False)
        assert "reasoning_content" not in json.dumps(public, ensure_ascii=False)


async def test_legacy_checkpoint_contains_exact_request_prefix_before_model_call(tmp_path):
    snapshots, requests = [], []

    async def persist(history):
        snapshots.append(deepcopy(history))

    class CheckModel:
        async def complete(self, messages, tools):
            assert messages[1:] == snapshots[-1]
            requests.append(deepcopy(messages))
            return question() if len(requests) == 1 else final()

    async def emit(event):
        assert "reasoning_content" not in event

    runner = AgentRunner(CheckModel(), ToolRegistry())
    first = await runner.run([{"role": "user", "content": "去哪"}], emit, checkpoint=persist)
    second = await runner.run(
        [*first.history, {"role": "user", "content": "你决定"}], emit, checkpoint=persist
    )
    assert second.status == "completed"
    assert requests[1][: len(requests[0])] == requests[0]


async def test_snapshot_attempt_idempotence_budget_fence_and_completed_replay(
    tmp_path, monkeypatch
):
    url = "sqlite+aiosqlite:///" + (tmp_path / "cache.db").as_posix()
    store = ConversationStore(url)
    await store.initialize()
    facts = DurableStore(store)
    cid = (await store.create("Asia/Shanghai"))["id"]
    lease = await facts.accept(cid, "已订，请保持", "r1")
    limits = RunLimits()
    key = lease["run_id"] + "/model/0"
    try:
        await facts.begin_effect(lease, key, "model", 12)
        await facts.spend_time(lease, 0.7, 300)

        class FirstClock:
            @staticmethod
            def now(zone):
                return datetime(2026, 10, 10, 23, 59, 59, tzinfo=zone)

        monkeypatch.setattr(durable_storage, "datetime", FirstClock)
        history, snapshot = await facts.append_runtime_context(lease, key, [], {}, limits)
        duplicate, duplicate_snapshot = await facts.append_runtime_context(
            lease, key, [], {"different": True}, limits
        )
        assert duplicate == history and duplicate_snapshot == snapshot
        state = runtime_state(history)
        assert state["planning"]["run_budget"]["model_requests_remaining_after_this"] == 11
        assert state["planning"]["run_budget"]["active_seconds_committed"] == 0.7
        fresh = (await facts.recover())[0]
        with pytest.raises(StaleOwner):
            await facts.append_runtime_context(lease, key, [], {}, limits)
        await facts.begin_effect(fresh, key, "model", 12)

        class NextClock:
            @staticmethod
            def now(zone):
                return datetime(2026, 10, 11, 0, 0, 1, tzinfo=zone)

        monkeypatch.setattr(durable_storage, "datetime", NextClock)
        history2, _ = await facts.append_runtime_context(fresh, key, [], {}, limits)
        assert history2[: len(history)] == history
        assert runtime_state(history2)["current_date"] == "2026-10-11"
        assert (
            runtime_state(history2)["planning"]["run_budget"]["model_requests_remaining_after_this"]
            == 10
        )
        usage = {
            "prompt_tokens": 1000,
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 100,
            "completion_tokens": 20,
        }
        reply = final()
        payload = {"message": reply.message, "finish_reason": "stop", "usage": usage}
        await facts.commit_effect(fresh, key, payload, reply.message)
        saved_history = await facts.history(cid)
        runtime = GraphRuntime(facts, Model(), ToolRegistry(), limits, None)
        result = await runtime.model_node(
            {"step": 0}, SimpleNamespace(context=SimpleNamespace(lease=fresh))
        )
        assert result["route"] == "final" and not runtime.model.requests
        assert await facts.history(cid) == saved_history
        assert (await facts.run(fresh["run_id"])).model_requests == 2
        diagnostics = await report(url)
        assert diagnostics["aggregate"]["completed_requests"] == 1
        assert diagnostics["aggregate"]["token_weighted_cache_hit_ratio"] == 0.9
        assert diagnostics["unknown_outcome_attempts"] == 1
        assert "已订" not in json.dumps(diagnostics, ensure_ascii=False)
        assert "保留推理" not in json.dumps(diagnostics, ensure_ascii=False)
    finally:
        await store.close()

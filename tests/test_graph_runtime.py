"""Durable graph/API acceptance; provider and tool fixtures remain offline."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy

import httpx
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from travel_agent.checkpoints import FencedSaver
from travel_agent.durable_service import DurableService
from travel_agent.durable_storage import DurableStore, StaleOwner
from travel_agent.graph_runtime import GraphRuntime
from travel_agent.models import ModelReply
from travel_agent.runner import RunLimits
from travel_agent.storage import ConversationStore
from travel_tools.api import create_app
from travel_tools.config import Settings
from travel_tools.registry import ToolRegistry


def question(text="想从哪里出发？", call_id="same-provider-call-id"):
    return ModelReply(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "秘密推理",
            "vendor_extra": {"nested": [1, {"opaque": True}]},
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "ask_user", "arguments": json.dumps({"message": text})},
                }
            ],
        },
        "tool_calls",
        {"prompt_tokens": 17},
    )


def final(text="按你的偏好安排。"):
    return ModelReply(
        {"role": "assistant", "content": text, "reasoning_content": "保留推理"}, "stop", {}
    )


class Model:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        return self.replies.pop(0)


@asynccontextmanager
async def api_client(tmp_path, model, engine="langgraph"):
    settings = Settings(
        _env_file=None,
        agent_engine=engine,
        deepseek_api_key=None,
        database_url="sqlite+aiosqlite:///" + (tmp_path / "app.db").as_posix(),
    )
    app = create_app(settings, ToolRegistry(), model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
        ) as client:
            yield client, app.state.agent.durable


async def send(client, cid, text, rid, qid=None):
    payload = {"content": text, "request_id": rid}
    if qid:
        payload["question_id"] = qid
    return await client.post(f"/conversations/{cid}/messages", json=payload)


async def test_multiple_questions_and_raw_fields_and_events(tmp_path):
    first, second = question(), question("日期不确定，可以让我选吗？")
    model = Model(first, second, final())
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        assert (await send(client, cid, "想出去玩，不知道去哪", "req-1")).status_code == 200
        public = (await client.get(f"/conversations/{cid}")).json()
        assert public["status"] == "waiting_user"
        q1 = public["question_id"]
        assert q1
        assert (await send(client, cid, "上海，日期还不确定", "req-2", q1)).status_code == 200
        public = (await client.get(f"/conversations/{cid}")).json()
        q2 = public["question_id"]
        assert q1 != q2  # Provider tool IDs may repeat on different model steps.
        assert (await send(client, cid, "你决定", "req-3", q2)).status_code == 200
        raw = await service.facts.history(cid)
        assert raw[1] == first.message and raw[4] == second.message
        assert json.loads(raw[2]["content"])["data"]["state"] == "waiting_user"
        assert model.requests[-1][1:] == raw[:-1]
        response = await client.get(f"/conversations/{cid}/events")
        events = [json.loads(s[6:]) for s in response.text.splitlines() if s.startswith("data: ")]
        seqs = [e["event_seq"] for e in events]
        assert seqs == list(range(1, len(seqs) + 1))
        assert "秘密推理" not in response.text and "reasoning_content" not in response.text
        replay = await client.get(
            f"/conversations/{cid}/events", headers={"Last-Event-ID": str(seqs[-2])}
        )
        assert len([s for s in replay.text.splitlines() if s.startswith("data: ")]) == 1


async def test_idempotent_answer_and_conflicting_requests(tmp_path):
    model = Model(question(), final())
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        await send(client, cid, "出去玩", "r1")
        qid = (await client.get(f"/conversations/{cid}")).json()["question_id"]
        assert (await send(client, cid, "杭州", "r2", qid)).status_code == 200
        duplicate = await send(client, cid, "杭州", "r2", qid)
        assert duplicate.status_code == 200 and len(model.requests) == 2
        assert (await send(client, cid, "上海", "r2", qid)).status_code == 409
        assert (await send(client, cid, "杭州", "r3", qid)).status_code == 409
        assert len([m for m in await service.facts.history(cid) if m["role"] == "user"]) == 2


async def test_all_writes_including_native_checkpoint_and_pending_writes_fenced(tmp_path):
    store = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "fence.db").as_posix())
    await store.initialize()
    facts = DurableStore(store)
    cid = (await store.create("Asia/Shanghai"))["id"]
    old = await facts.accept(cid, "测试", "r1")
    async with AsyncSqliteSaver.from_conn_string((tmp_path / "fence.graph").as_posix()) as native:
        graph = DurableService(
            store, Model(final()), ToolRegistry(), RunLimits(), native
        ).runtime.graph(old)
        config = {"configurable": {"thread_id": cid + "/langgraph-v1"}}
        await graph.aupdate_state(config, {"step": 0, "route": "model"}, as_node="__start__")
        snapshot = await graph.aget_state(config)
        current = (await facts.recover())[0]
        fenced = FencedSaver(native, facts, old)
        checkpoint = await fenced.aget_tuple(config)
        canonical = await facts.cursor(cid)
        with pytest.raises(StaleOwner):
            await fenced.aput(
                snapshot.config,
                checkpoint.checkpoint,
                checkpoint.metadata,
                checkpoint.checkpoint["channel_versions"],
            )
        with pytest.raises(StaleOwner):
            await fenced.aput_writes(snapshot.config, [("step", 999)], "stale-task")
        with pytest.raises(StaleOwner):
            await facts.begin_effect(old, "old-model", "model", 12)
        with pytest.raises(StaleOwner):
            await facts.emit(old, "stale-event", {"type": "tool_started", "tool_name": "x"})
        assert await facts.cursor(cid) == canonical
        fresh = await FencedSaver(native, facts, current).aget_tuple(config)
        assert not any(w[0] == "stale-task" for w in fresh.pending_writes)
    await store.close()


async def test_graph_thread_remains_pinned_when_switch_returns_to_legacy(tmp_path):
    async with api_client(tmp_path, Model(question())) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        await send(client, cid, "去哪玩", "r1")
        qid = (await client.get(f"/conversations/{cid}")).json()["question_id"]
    async with api_client(tmp_path, Model(final()), engine="legacy") as (client, service):
        assert (await send(client, cid, "你决定", "r2", qid)).status_code == 200
        assert (await client.get(f"/conversations/{cid}")).json()["status"] == "completed"
        assert len(service.runtime.model.requests) == 1


async def test_active_time_budget_retained_and_next_message_starts_new_run(tmp_path):
    store = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "time.db").as_posix())
    await store.initialize()
    facts = DurableStore(store)
    cid = (await store.create("Asia/Shanghai"))["id"]
    old = await facts.accept(cid, "测试时间预算", "r1")
    await facts.spend_time(old, 0.8, 1)
    lease = (await facts.recover())[0]

    class Slow:
        async def complete(self, messages, tools):
            await asyncio.sleep(0.4)
            return final()

    async with AsyncSqliteSaver.from_conn_string((tmp_path / "time.graph").as_posix()) as saver:
        runtime = GraphRuntime(facts, Slow(), ToolRegistry(), RunLimits(12, 1), saver)
        await runtime.execute(lease)
        assert (await store.get(cid))["last_error"]["code"] == "run_timeout"
        failed = await facts.run(lease["run_id"])
        assert failed.model_requests == 1 and failed.active_seconds >= 1
        new = await facts.accept(cid, "继续", "r2")
        fresh = GraphRuntime(facts, Model(final()), ToolRegistry(), RunLimits(12, 1), saver)
        await fresh.execute(new)
        assert (await store.get(cid))["status"] == "completed"
        assert (await facts.run(new["run_id"])).model_requests == 1
    await store.close()


async def test_concurrent_request_copies_return_same_accepted_run(tmp_path):
    store = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "copies.db").as_posix())
    await store.initialize()
    facts = DurableStore(store)
    cid = (await store.create("Asia/Shanghai"))["id"]
    first, second = await asyncio.gather(
        facts.accept(cid, "需求", "r1"), facts.accept(cid, "需求", "r1")
    )
    assert first["run_id"] == second["run_id"]
    assert len(await facts.history(cid)) == 1
    await store.close()


async def test_large_tool_batch_has_no_graph_or_tool_count_limit(tmp_path):
    calls = [
        {
            "id": str(i),
            "type": "function",
            "function": {"name": "missing_readonly_tool", "arguments": "{}"},
        }
        for i in range(40)
    ]
    model = Model(
        ModelReply({"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls", {}),
        final(),
    )
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        await send(client, cid, "批量查询", "r1")
        raw = await service.facts.history(cid)
        assert [m["tool_call_id"] for m in raw if m["role"] == "tool"] == [
            str(i) for i in range(40)
        ]
        assert (await client.get(f"/conversations/{cid}")).json()["status"] == "completed"

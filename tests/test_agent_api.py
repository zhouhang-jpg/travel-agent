"""Offline API, durable history, run isolation and SSE lifecycle integration tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy

import httpx
import pytest

from travel_agent.models import ModelReply
from travel_agent.runner import RunLimits, RunOutcome, repair_history
from travel_agent.service import AgentService, stream_events
from travel_agent.storage import Conversation, ConversationStore
from travel_tools.api import create_app
from travel_tools.config import Settings
from travel_tools.registry import ToolRegistry


def reply(content="这是行程安排。"):
    return ModelReply(
        message={
            "role": "assistant",
            "content": content,
            "reasoning_content": "private-final-reasoning",
        },
        finish_reason="stop",
        usage={},
    )


def tool_call(name="ask_user", arguments=None, call_id="question-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"message": "请提供出发地和出行日期。"}),
        },
    }


def question():
    return ModelReply(
        message={
            "role": "assistant",
            "content": None,
            "reasoning_content": "private-question-reasoning",
            "tool_calls": [tool_call()],
        },
        finish_reason="tool_calls",
        usage={},
    )


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        return self.responses.pop(0)


class BlockingModel:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []
        self.cancelled = False

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        content = next(item["content"] for item in reversed(messages) if item["role"] == "user")
        if content == "等待处理":
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return reply("已完成：" + content)


def database_url(path):
    return "sqlite+aiosqlite:///" + path.as_posix()


def settings():
    return Settings(
        _env_file=None,
        deepseek_api_key=None,
        llm_api_key=None,
        llm_provider="deepseek",
    )


@asynccontextmanager
async def client_for(tmp_path, model=None, *, raise_app_exceptions=True):
    store = ConversationStore(database_url(tmp_path / "conversations.sqlite"))
    app = create_app(settings(), registry=ToolRegistry(), model=model, store=store)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app, raise_app_exceptions=raise_app_exceptions),
                base_url="http://test",
            ) as client:
                yield client, app.state.agent, store
    finally:
        await store.close()


async def internal_history(store, conversation_id):
    async with store.sessions() as session:
        row = await session.get(Conversation, conversation_id)
        return deepcopy(row.history)


def events(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


async def create_conversation(client, **body):
    response = await client.post("/conversations", json=body)
    assert response.status_code == 201
    return response.json()["id"]


async def test_question_answer_roundtrip_keeps_internal_history_but_hides_it_publicly(tmp_path):
    model = FakeModel(question(), reply("上海出发的两天行程如下。"))
    async with client_for(tmp_path, model) as (client, service, store):
        conversation_id = await create_conversation(client)
        url = f"/conversations/{conversation_id}"
        first = await client.post(url + "/messages", json={"content": "安排一次出行"})
        first_events = events(first)
        assert first_events[-1] == {"type": "done", "status": "waiting_user"}
        assert (
            next(item for item in first_events if item["type"] == "message")["kind"] == "question"
        )
        saved_question_history = await internal_history(store, conversation_id)
        assert [item["role"] for item in saved_question_history] == ["user", "assistant", "tool"]
        assert saved_question_history[1]["reasoning_content"] == "private-question-reasoning"
        question_result = json.loads(saved_question_history[2]["content"])
        assert question_result["data"]["state"] == "waiting_user"

        second = await client.post(url + "/messages", json={"content": "上海出发，10月17日到18日"})
        assert events(second)[-1] == {"type": "done", "status": "completed"}
        final_history = await internal_history(store, conversation_id)
        assert final_history[:3] == saved_question_history
        assert model.requests[1][1:] == final_history[:-1]
        assert final_history[-1]["reasoning_content"] == "private-final-reasoning"
        public_response = await client.get(url)
        public = public_response.json()
        assert public["status"] == "completed"
        assert [item["role"] for item in public["transcript"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert [item["kind"] for item in public["transcript"] if item["role"] == "assistant"] == [
            "question",
            "answer",
        ]
        listing = await client.get("/conversations")
        assert listing.json()["items"][0]["id"] == conversation_id
        for surface in (first.text, second.text, public_response.text, listing.text):
            assert "reasoning_content" not in surface
            assert "private-question-reasoning" not in surface
            assert "private-final-reasoning" not in surface
            assert "tool_calls" not in surface
            assert "arguments" not in surface
        assert "history" not in public
        assert (await client.get("/agent/health")).json()["model_configured"] is True


async def test_running_conversation_rejects_second_message_without_appending_it(tmp_path):
    model = BlockingModel()
    async with client_for(tmp_path, model) as (client, service, store):
        conversation_id = await create_conversation(client)
        url = f"/conversations/{conversation_id}"
        active = asyncio.create_task(client.post(url + "/messages", json={"content": "等待处理"}))
        try:
            await asyncio.wait_for(model.entered.wait(), timeout=2)
            rejected = await client.post(url + "/messages", json={"content": "不应追加的插话"})
            assert rejected.status_code == 409
            public = (await client.get(url)).json()
            assert public["status"] == "running"
            assert len(public["transcript"]) == 1
            assert await internal_history(store, conversation_id) == [
                {"role": "user", "content": "等待处理"}
            ]
        finally:
            model.release.set()
            response = await asyncio.wait_for(active, timeout=2)
        assert events(response)[-1]["status"] == "completed"
        assert len(model.requests) == 1 and not model.cancelled


async def test_different_conversations_run_independently_without_sharing_history(tmp_path):
    model = BlockingModel()
    async with client_for(tmp_path, model) as (client, service, store):
        first_id, second_id = await create_conversation(client), await create_conversation(client)
        first = asyncio.create_task(
            client.post(f"/conversations/{first_id}/messages", json={"content": "等待处理"})
        )
        try:
            await asyncio.wait_for(model.entered.wait(), timeout=2)
            second = await asyncio.wait_for(
                client.post(f"/conversations/{second_id}/messages", json={"content": "独立行程"}),
                timeout=2,
            )
            assert events(second)[-1]["status"] == "completed"
            assert (await store.get(first_id))["status"] == "running"
            assert "等待处理" not in json.dumps(
                await internal_history(store, second_id), ensure_ascii=False
            )
            assert "独立行程" not in json.dumps(
                await internal_history(store, first_id), ensure_ascii=False
            )
        finally:
            model.release.set()
            await asyncio.wait_for(first, timeout=2)
        assert len(model.requests) == 2
        assert all(
            len([m for m in request if m["role"] == "user"]) == 1 for request in model.requests
        )


async def test_missing_model_key_returns_503_without_writing_user_message(tmp_path):
    async with client_for(tmp_path) as (client, service, store):
        conversation_id = await create_conversation(client)
        response = await client.post(
            f"/conversations/{conversation_id}/messages", json={"content": "帮我安排"}
        )
        assert response.status_code == 503
        assert (await client.get("/agent/health")).json()["model_configured"] is False
        public = await store.get(conversation_id)
        assert public["status"] == "idle" and public["transcript"] == []
        assert await internal_history(store, conversation_id) == []
        assert not service.tasks


async def test_closing_sse_iterator_does_not_cancel_background_run(tmp_path):
    model = BlockingModel()
    store = ConversationStore(database_url(tmp_path / "disconnect.sqlite"))
    await store.initialize()
    service = AgentService(store, model, ToolRegistry(), RunLimits())
    try:
        conversation_id = (await store.create("Asia/Shanghai"))["id"]
        queue = await service.start(conversation_id, "等待处理")
        stream = stream_events(queue)
        assert '"status": "running"' in await asyncio.wait_for(anext(stream), timeout=2)
        await asyncio.wait_for(model.entered.wait(), timeout=2)
        active_tasks = list(service.tasks)
        assert active_tasks and all(not task.done() for task in active_tasks)
        await stream.aclose()
        assert all(not task.done() for task in active_tasks)
        assert (await store.get(conversation_id))["status"] == "running"
        model.release.set()
        await asyncio.wait_for(asyncio.gather(*active_tasks), timeout=2)
        public = await store.get(conversation_id)
        assert public["status"] == "completed"
        assert public["transcript"][-1]["content"] == "已完成：等待处理"
        assert (await internal_history(store, conversation_id))[-1]["reasoning_content"]
        assert not model.cancelled
    finally:
        model.release.set()
        await service.close()
        await store.close()


async def test_store_reopen_keeps_complete_history_and_public_transcript(tmp_path):
    url = database_url(tmp_path / "durable.sqlite")
    store = ConversationStore(url)
    await store.initialize()
    conversation_id = (await store.create("Asia/Shanghai"))["id"]
    lease = await store.acquire(conversation_id, "安排上海周末", repair_history)
    history = lease["history"] + [reply().message]
    await store.save_history(conversation_id, lease["run_id"], history)
    await store.append_message(conversation_id, lease["run_id"], "这是行程安排。", "answer")
    await store.finish(conversation_id, lease["run_id"], RunOutcome("completed", history, "回答"))
    await store.close()

    reopened = ConversationStore(url)
    try:
        await reopened.initialize()
        await reopened.recover_runs(repair_history)
        assert await internal_history(reopened, conversation_id) == history
        public = await reopened.get(conversation_id)
        assert public["status"] == "completed"
        assert [message["role"] for message in public["transcript"]] == ["user", "assistant"]
        assert "reasoning_content" not in json.dumps(public)
    finally:
        await reopened.close()


async def test_recover_running_lease_repairs_missing_tools_and_allows_next_turn(tmp_path):
    url = database_url(tmp_path / "recover.sqlite")
    store = ConversationStore(url)
    await store.initialize()
    conversation_id = (await store.create("Asia/Shanghai"))["id"]
    lease = await store.acquire(conversation_id, "安排出行", repair_history)
    raw_assistant = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "private-before-restart",
        "tool_calls": [
            tool_call("search_places", {"query": "上海"}, "finished"),
            tool_call("search_places", {"query": "杭州"}, "pending"),
        ],
    }
    finished = {"role": "tool", "tool_call_id": "finished", "content": '{"status":"ok"}'}
    before = lease["history"] + [raw_assistant, finished]
    await store.save_history(conversation_id, lease["run_id"], before)
    await store.close()

    reopened = ConversationStore(url)
    try:
        await reopened.initialize()
        await reopened.recover_runs(repair_history)
        public = await reopened.get(conversation_id)
        assert public["status"] == "error" and public["last_error"]["code"] == "interrupted"
        assert public["transcript"][-1]["kind"] == "error"
        repaired = await internal_history(reopened, conversation_id)
        assert repaired[: len(before)] == before
        assert repaired[-1]["tool_call_id"] == "pending"
        assert json.loads(repaired[-1]["content"])["error"]["code"] == "interrupted"
        await reopened.recover_runs(repair_history)
        assert await internal_history(reopened, conversation_id) == repaired
        assert (await reopened.get(conversation_id))["transcript"] == public["transcript"]
        resumed = await reopened.acquire(conversation_id, "继续", repair_history)
        assert resumed["run_id"] != lease["run_id"]
        assert resumed["history"][:-1] == repaired
        assert resumed["history"][-1] == {"role": "user", "content": "继续"}
        await reopened.finish(
            conversation_id, resumed["run_id"], RunOutcome("completed", resumed["history"], "完成")
        )
    finally:
        await reopened.close()


@pytest.mark.parametrize("content", ["", "   \n\t", "x" * 16001, None, 123])
async def test_invalid_message_does_not_start_run_or_echo_payload(tmp_path, content):
    model = FakeModel()
    async with client_for(tmp_path, model) as (client, service, store):
        conversation_id = await create_conversation(client)
        response = await client.post(
            f"/conversations/{conversation_id}/messages", json={"content": content}
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid conversation request."}
        assert await internal_history(store, conversation_id) == []
        assert not model.requests and not service.tasks


async def test_oversized_message_envelope_returns_413_before_start(tmp_path):
    model = FakeModel()
    async with client_for(tmp_path, model) as (client, service, store):
        conversation_id = await create_conversation(client)
        response = await client.post(
            f"/conversations/{conversation_id}/messages", content=b"x" * 65537
        )
        assert response.status_code == 413
        assert await internal_history(store, conversation_id) == []
        assert not model.requests


@pytest.mark.parametrize("timezone", ["Invalid/Zone", "", "/etc/passwd", None])
async def test_invalid_timezone_rejected_without_creating_conversation(tmp_path, timezone):
    async with client_for(tmp_path, FakeModel()) as (client, service, store):
        response = await client.post("/conversations", json={"timezone": timezone})
        assert response.status_code == 422
        assert (await client.get("/conversations")).json() == {"items": []}


async def test_unknown_conversation_returns_404_for_get_and_send(tmp_path):
    model = FakeModel()
    async with client_for(tmp_path, model) as (client, service, store):
        assert (await client.get("/conversations/not-found")).status_code == 404
        response = await client.post("/conversations/not-found/messages", json={"content": "安排"})
        assert response.status_code == 404
        assert not model.requests and not service.tasks


async def test_temporary_finish_failure_reports_error_then_read_repairs_lease(
    tmp_path, monkeypatch
):
    model = FakeModel(reply("首个方案"), reply("后续修改"))
    async with client_for(tmp_path, model) as (client, service, store):
        conversation_id = await create_conversation(client)
        url = f"/conversations/{conversation_id}"
        original_finish = store.finish
        attempts = 0

        async def flaky_finish(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("database-password-private")
            return await original_finish(*args, **kwargs)

        monkeypatch.setattr(store, "finish", flaky_finish)
        response = await client.post(url + "/messages", json={"content": "安排周末"})
        streamed = events(response)
        assert streamed[-1] == {"type": "done", "status": "error"}
        assert any(item["type"] == "error" and item["code"] == "storage_error" for item in streamed)
        assert not any(item.get("status") == "completed" for item in streamed)
        assert "database-password-private" not in response.text
        assert conversation_id in service.failed_saves
        assert (await store.get(conversation_id))["status"] == "running"

        refreshed = await client.get(url)
        assert refreshed.status_code == 200
        assert refreshed.json()["status"] == "error"
        assert refreshed.json()["last_error"]["code"] == "storage_error"
        assert not service.failed_saves
        assert (await internal_history(store, conversation_id))[-1]["content"] == "首个方案"
        resumed = await client.post(url + "/messages", json={"content": "修改第二天"})
        assert events(resumed)[-1] == {"type": "done", "status": "completed"}
        assert len(model.requests) == 2
        assert "首个方案" in json.dumps(model.requests[-1], ensure_ascii=False)


async def test_persistent_finish_failure_never_signals_success_or_exposes_database_error(
    tmp_path, monkeypatch
):
    model = FakeModel(reply("已生成的方案"))
    async with client_for(tmp_path, model, raise_app_exceptions=False) as (client, service, store):
        conversation_id = await create_conversation(client)
        url = f"/conversations/{conversation_id}"

        async def broken_finish(*args, **kwargs):
            raise RuntimeError("postgres://admin:private-password@internal-host")

        monkeypatch.setattr(store, "finish", broken_finish)
        response = await client.post(url + "/messages", json={"content": "安排周末"})
        streamed = events(response)
        assert streamed[-1] == {"type": "done", "status": "error"}
        assert not any(item.get("status") == "completed" for item in streamed)
        assert conversation_id in service.failed_saves
        cached_run, cached_outcome = service.failed_saves[conversation_id]
        assert cached_run and cached_outcome.status == "error"
        assert cached_outcome.history[-1]["content"] == "已生成的方案"
        failures = [
            await client.get(url),
            await client.get("/conversations"),
            await client.post(url + "/messages", json={"content": "不应写入"}),
        ]
        assert all(500 <= failure.status_code < 600 for failure in failures)
        for surface in [response.text, *(failure.text for failure in failures)]:
            assert "private-password" not in surface
            assert "internal-host" not in surface
            assert "reasoning_content" not in surface
        assert conversation_id in service.failed_saves
        assert len(model.requests) == 1
        saved = await internal_history(store, conversation_id)
        assert [item["content"] for item in saved if item["role"] == "user"] == ["安排周末"]

"""Public progress/replay boundaries and validated contextual tool descriptions."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime

import httpx
import pytest

from travel_agent.models import ModelReply
from travel_agent.public_progress import ACTIVITY_TYPES, tool_summary
from travel_agent.runner import AgentRunner
from travel_agent.storage import Conversation
from travel_tools.api import create_app
from travel_tools.common import ToolPayload
from travel_tools.config import Settings
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.schemas.places import SearchPlacesInput


def call(name="search_places", arguments=None, call_id="places"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"keywords": "街巷", "region": "成都"}),
        },
    }


def update(
    calls=None, finish_reason="tool_calls", content="先查成都街巷，再结合路线安排轻松行程。"
):
    return ModelReply(
        {
            "role": "assistant",
            "content": content,
            "tool_calls": calls if calls is not None else [call()],
            "reasoning_content": "private-hidden-reasoning",
            "vendor_fields": {"api_key": "private-vendor-secret"},
        },
        finish_reason,
        {},
    )


def final():
    return ModelReply({"role": "assistant", "content": "查询完成，具体安排仍需核实。"}, "stop", {})


class Model:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        return self.replies.pop(0)


@asynccontextmanager
async def api_client(tmp_path, model, registry=None):
    app = create_app(
        Settings(
            _env_file=None,
            deepseek_api_key=None,
            agent_engine="langgraph",
            database_url="sqlite+aiosqlite:///" + (tmp_path / "progress.db").as_posix(),
        ),
        registry or ToolRegistry(),
        model=model,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield client, app.state.agent.durable


def events(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


async def test_live_progress_order_refresh_all_runs_and_private_raw_history(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Output(ToolPayload):
        value: str

    async def query(arguments):
        started.set()
        await release.wait()
        return Output(value="private-raw-tool-result")

    registry = ToolRegistry()
    registry.register(
        ToolSpec("search_places", "查地点", SearchPlacesInput, Output, query, "ready")
    )
    first = update()
    model = Model(first, final(), update(content="再查询街巷候选。"), final())
    async with api_client(tmp_path, model, registry) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        sending = asyncio.create_task(
            client.post(
                f"/conversations/{cid}/messages", json={"content": "去成都玩", "request_id": "r1"}
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        running = (await client.get(f"/conversations/{cid}")).json()
        assert running["status"] == "running" and running["active_run_id"]
        commentary = [
            event for event in running["activity_events"] if event["type"] == "commentary"
        ]
        assert [event["content"] for event in commentary] == [first.message["content"]]
        assert all(call["summary"] == "成都 · 街巷" for call in running["tool_progress"].values())
        assert "private-" not in json.dumps(running)
        release.set()
        streamed = events(await sending)
        update_index = next(i for i, event in enumerate(streamed) if event["type"] == "commentary")
        tool_index = next(i for i, event in enumerate(streamed) if event["type"] == "tool_started")
        answer_index = next(i for i, event in enumerate(streamed) if event["type"] == "message")
        assert update_index < tool_index < answer_index
        for event in streamed:
            assert event["run_id"] == running["active_run_id"]
            datetime.fromisoformat(event["created_at"])
        completed = (await client.get(f"/conversations/{cid}")).json()
        assert completed["active_run_id"] is None
        assert completed["tool_progress"] == {} and completed["model_progress"] is None
        assert completed["activity_events"] == [
            event for event in streamed if event["type"] in ACTIVITY_TYPES
        ]
        await client.post(
            f"/conversations/{cid}/messages", json={"content": "再看看", "request_id": "r2"}
        )
        refreshed = (await client.get(f"/conversations/{cid}")).json()
        assert len({event["run_id"] for event in refreshed["activity_events"]}) == 2
        assert [event["event_seq"] for event in refreshed["activity_events"]] == sorted(
            event["event_seq"] for event in refreshed["activity_events"]
        )
        replay = await client.get(f"/conversations/{cid}/events")
        assert refreshed["activity_events"] == [
            event for event in events(replay) if event["type"] in ACTIVITY_TYPES
        ]
        assert "private-" not in replay.text
        assert "private-" not in json.dumps(refreshed)
        history = await service.facts.history(cid)
        assert first.message in history
        assert "private-raw-tool-result" in json.dumps(history)
        assert "private-hidden-reasoning" in json.dumps(model.requests[-1])


async def test_old_transcript_public_backfill_does_not_rewrite_storage(tmp_path):
    async with api_client(tmp_path, Model(final())) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        await client.post(
            f"/conversations/{cid}/messages", json={"content": "看看", "request_id": "r1"}
        )
        async with service.store.sessions.begin() as session:
            row = await session.get(Conversation, cid)
            old = [
                {key: value for key, value in item.items() if key not in {"run_id", "event_seq"}}
                for item in row.transcript
            ]
            row.transcript = deepcopy(old)
        refreshed = (await client.get(f"/conversations/{cid}")).json()
        assert all(item["run_id"] and item["event_seq"] for item in refreshed["transcript"])
        async with service.store.sessions() as session:
            assert (await session.get(Conversation, cid)).transcript == old


@pytest.mark.parametrize(
    "reply",
    [
        update(finish_reason="length"),
        update(finish_reason=None),
        update(calls=[]),
        update(calls=[{"id": "missing-function"}]),
        update(content=None),
        update(content={"reasoning_content": "not-public"}),
    ],
)
async def test_invalid_incomplete_or_nontext_reply_has_no_commentary(tmp_path, reply):
    async with api_client(tmp_path, Model(reply, final())) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        response = await client.post(
            f"/conversations/{cid}/messages", json={"content": "查询", "request_id": "r1"}
        )
        assert not any(event["type"] == "commentary" for event in events(response))
        assert "private-hidden-reasoning" not in response.text
        assert "private-vendor-secret" not in response.text


async def test_legacy_also_emits_only_assistant_content_before_tool():
    first = update()
    emitted = []

    async def emit(event):
        emitted.append(event)

    outcome = await AgentRunner(Model(first, final()), ToolRegistry()).run(
        [{"role": "user", "content": "查询"}], emit
    )
    assert outcome.status == "completed"
    assert emitted[0]["type"] == "commentary" and emitted[0]["content"] == first.message["content"]
    assert emitted[1]["type"] == "tool_started"
    assert "private-" not in json.dumps(emitted)
    assert first.message in outcome.history


async def test_legacy_repeated_names_and_provider_ids_keep_distinct_progress():
    emitted = []

    async def emit(event):
        emitted.append(event)

    model = Model(update(calls=[call(call_id="same"), call(call_id="second")]), update(), final())
    await AgentRunner(model, ToolRegistry()).run([{"role": "user", "content": "查询"}], emit)
    starts = [event["call_id"] for event in emitted if event["type"] == "tool_started"]
    finishes = [event["call_id"] for event in emitted if event["type"] == "tool_finished"]
    assert len(starts) == len(set(starts)) == 3
    assert starts == finishes


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        (
            "search_flights",
            {
                "origin": {"query": "上海"},
                "destination": {"query": "成都"},
                "departure_date": "2026-10-20",
                "travelers": {"adults": 1},
            },
            "上海 → 成都 · 2026-10-20",
        ),
        (
            "search_hotels",
            {
                "destination": {"query": "成都"},
                "check_in": "2026-10-20",
                "check_out": "2026-10-23",
                "travelers": {"adults": 1},
                "rooms": 1,
            },
            "成都 · 2026-10-20 至 2026-10-23 · 1 间房",
        ),
        ("search_places", {"region": "成都", "keywords": "街巷", "extra_secret": "credential"}, ""),
        (
            "search_places",
            {"region": "成都", "keywords": "街巷", "force_refresh": True},
            "成都 · 街巷",
        ),
        ("search_web", {"query": "api_key=must-not-show"}, ""),
        (
            "fetch_webpage",
            {"url": "https://user:password@example.com/path/secret?token=credential#secret"},
            "example.com",
        ),
        ("search_flights", {"origin": {"query": "上海"}}, ""),
        ("unknown_tool", {"query": "secret"}, ""),
    ],
)
def test_summary_projects_only_validated_known_fields(name, arguments, expected):
    assert tool_summary(call(name, arguments)) == expected


def test_summary_malformed_arguments_has_no_private_json_echo():
    candidate = call()
    candidate["function"]["arguments"] = '{"api_key":"secret"'
    assert tool_summary(candidate) == ""

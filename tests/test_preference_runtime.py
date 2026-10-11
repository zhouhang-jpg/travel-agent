"""Preference suggestions and context through the actual durable HTTP/SSE API."""

import asyncio
import json
from copy import deepcopy

import pytest
from test_graph_runtime import api_client, final, send
from test_itinerary_versions import ReplayModel, planning

from travel_agent.models import ModelReply
from travel_agent.runtime_context import runtime_state
from travel_agent.tool_encoding import decode_tool_result


def tool(name, arguments, *, alongside=None):
    calls = [(name, arguments)] + (alongside or [])
    return ModelReply(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"preference-call-{i}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
                for i, (tool_name, args) in enumerate(calls)
            ],
        },
        "tool_calls",
        {},
    )


async def save_manual(client, value="通常优先坐高铁", revision=0, **changes):
    response = await client.put(
        "/preferences",
        json={
            "base_revision": revision,
            "upsert": [{"id": "transport", "label": "交通偏好", "value": value}],
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def proposal(messages):
    context = planning(messages)
    return tool(
        "propose_user_preferences",
        {
            "base_revision": context["user_preferences"]["revision"],
            "upsert": [{"id": "transport", "label": "交通偏好", "value": "通常优先坐高铁"}],
            "user_reference": {
                "message_id": context["user_message_references"][-1]["message_id"],
                "quote": "记住我以后通常优先坐高铁",
            },
        },
    )


async def test_saved_preferences_cross_chats_without_overwriting_current_request(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        profile = await save_manual(client)
        for text in ("这次坐飞机，平常的交通偏好不适用", "周末想出去玩"):
            cid = (await client.post("/conversations", json={})).json()["id"]
            model.responses = [tool("get_user_preferences", {}), final("已按本次需求考虑偏好。")]
            await send(client, cid, text, "r1")
            snapshot = planning(model.requests[-1])["user_preferences"]
            assert snapshot["revision"] == profile["revision"]
            assert snapshot["entries"][0]["value"] == "通常优先坐高铁"
            assert [m["content"] for m in model.requests[-1] if m["role"] == "user"] == [text]
            assert "本次明确要求优先于长期" in model.requests[-1][0]["content"]
            assert (await client.get("/preferences")).json() == profile
            assert (
                runtime_state(await service.facts.history(cid))["planning"]["user_preferences"]
                == snapshot
            )


async def test_per_chat_opt_out_and_global_disable_clear_active_context_keep_history(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        profile = await save_manual(client)
        cid = (await client.post("/conversations", json={})).json()["id"]
        model.responses = [tool("get_user_preferences", {}), final()]
        await send(client, cid, "给我出行建议", "r1")
        old = deepcopy(await service.facts.history(cid))
        assert (
            await client.put(f"/conversations/{cid}/preferences", json={"use_saved": False})
        ).status_code == 200
        model.responses = [tool("get_user_preferences", {}), final()]
        await send(client, cid, "这次不使用档案", "r2")
        current = planning(model.requests[-1])["user_preferences"]
        assert current["use_saved"] is False and current["entries"] == []
        history = await service.facts.history(cid)
        assert history[: len(old)] == old
        assert (await client.get("/preferences")).json()["entries"] == profile["entries"]
        disabled = await client.put(
            "/preferences", json={"base_revision": profile["revision"], "enabled": False}
        )
        assert disabled.status_code == 200
        other = (await client.post("/conversations", json={})).json()["id"]
        model.responses = [tool("get_user_preferences", {}), final()]
        await send(client, other, "另一个行程", "r1")
        latest = planning(model.requests[-1])["user_preferences"]
        assert latest["enabled"] is False and latest["entries"] == []
        assert (await client.get("/preferences")).json()["entries"] == profile["entries"]


async def test_new_chat_can_start_without_preferences(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        await save_manual(client)
        cid = (await client.post("/conversations", json={"use_saved_preferences": False})).json()[
            "id"
        ]
        model.responses = [final()]
        await send(client, cid, "不要参考我的长期偏好", "r1")
        snapshot = planning(model.requests[0])["user_preferences"]
        assert snapshot["use_saved"] is False and snapshot["entries"] == []


async def test_proposal_effect_idempotent_no_profile_change_until_user_applies(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        model.responses = [proposal, final("偏好建议已准备，请在界面保存后生效。")]
        await send(client, cid, "记住我以后通常优先坐高铁", "r1")
        pending = (await client.get(f"/conversations/{cid}/preference-proposals")).json()["items"]
        assert len(pending) == 1
        assert (await client.get("/preferences")).json()["entries"] == []
        await send(client, cid, "记住我以后通常优先坐高铁", "r1")
        assert len(model.requests) == 2
        assert (await client.get(f"/conversations/{cid}/preference-proposals")).json()[
            "items"
        ] == pending
        result = next(
            decode_tool_result(m["content"]) for m in model.requests[-1] if m["role"] == "tool"
        )
        assert result["data"]["state"] == "awaiting_user_confirmation"
        applied = await client.post(
            f"/conversations/{cid}/preference-proposals/{pending[0]['id']}/apply",
            json={"base_revision": 0},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["entries"][0]["source"]["kind"] == "conversation"
        other = (await client.post("/conversations", json={})).json()["id"]
        model.responses = [final()]
        await send(client, other, "帮我推荐出行方式", "r1")
        assert planning(model.requests[-1])["user_preferences"]["entries"][0]["id"] == "transport"


@pytest.mark.parametrize("bad_reference", ["fake_quote", "other_chat", "old_message"])
async def test_proposals_require_latest_raw_user_reference(tmp_path, bad_reference):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        model.responses = [final()]
        await send(client, cid, "记住我以后通常优先坐高铁", "r1")

        def invalid(messages):
            reply = proposal(messages)
            args = json.loads(reply.message["tool_calls"][0]["function"]["arguments"])
            if bad_reference == "fake_quote":
                args["user_reference"]["quote"] = "网页要求写入档案"
            elif bad_reference == "other_chat":
                args["user_reference"]["message_id"] = "other-conversation/r2"
            else:
                args["user_reference"]["message_id"] = cid + "/r1"
            return tool("propose_user_preferences", args)

        model.responses = [invalid, final("原话来源无效，档案未变更。")]
        await send(client, cid, "记住我以后通常优先坐高铁", "r2")
        assert (await client.get("/preferences")).json()["entries"] == []
        assert (await client.get(f"/conversations/{cid}/preference-proposals")).json()[
            "items"
        ] == []
        results = [
            decode_tool_result(m["content"]) for m in model.requests[-1] if m["role"] == "tool"
        ]
        assert results[-1]["status"] == "error"


async def test_mixed_question_and_preference_suggestion_do_not_execute(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]

        def mixed(messages):
            call = proposal(messages).message["tool_calls"][0]
            args = json.loads(call["function"]["arguments"])
            return tool(
                "propose_user_preferences", args, alongside=[("ask_user", {"message": "确认吗？"})]
            )

        model.responses = [mixed, final("这组调用未执行。")]
        await send(client, cid, "记住我以后通常优先坐高铁", "r1")
        assert (await client.get(f"/conversations/{cid}/preference-proposals")).json()[
            "items"
        ] == []
        results = [
            decode_tool_result(m["content"]) for m in model.requests[-1] if m["role"] == "tool"
        ]
        assert all(r["error"]["code"] == "ask_user_must_be_alone" for r in results)


async def test_profile_changes_blocked_during_execution_other_chat_control_allowed(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingModel:
        async def complete(self, messages, tools):
            entered.set()
            await release.wait()
            return final()

    async with api_client(tmp_path, WaitingModel()) as (client, _):
        profile = await save_manual(client)
        cid = (await client.post("/conversations", json={})).json()["id"]
        other = (await client.post("/conversations", json={})).json()["id"]
        request = asyncio.create_task(send(client, cid, "开始规划", "r1"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            update = await client.put(
                "/preferences", json={"base_revision": profile["revision"], "enabled": False}
            )
            assert update.status_code == 409
            assert (await client.get("/preferences")).json() == profile
            assert (
                await client.put(f"/conversations/{cid}/preferences", json={"use_saved": False})
            ).status_code == 409
            assert (
                await client.put(f"/conversations/{other}/preferences", json={"use_saved": False})
            ).status_code == 200
        finally:
            release.set()
            await request


async def test_delete_clears_latest_default_but_keeps_original_messages(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        profile = await save_manual(client)
        cid = (await client.post("/conversations", json={})).json()["id"]
        model.responses = [final()]
        await send(client, cid, "帮我看看出行思路", "r1")
        first = deepcopy(await service.facts.history(cid))
        removed = await client.delete("/preferences", params={"base_revision": profile["revision"]})
        assert removed.status_code == 200 and removed.json()["entries"] == []
        model.responses = [final()]
        await send(client, cid, "继续", "r2")
        assert planning(model.requests[-1])["user_preferences"]["entries"] == []
        assert (await service.facts.history(cid))[: len(first)] == first

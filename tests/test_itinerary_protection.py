"""Protected arrangements: compact source references are not edits, and real
edits are reported field by field so the model can restore them."""

import json
from copy import deepcopy

from test_graph_runtime import api_client, final, send
from test_itinerary_save_optimization import reply
from test_itinerary_versions import SOURCE, ReplayModel, planning, setup_plan

from travel_tools.schemas.itinerary import arrangement_view


async def booked_plan(client, model, cid):
    """Mirror the evaluation case: a booked train whose duration cites a source
    both inline and by catalog ID, which is stored in compact form."""
    first = await setup_plan(client, model, cid)

    def lock(messages):
        data = deepcopy(first["document"])
        data["items"][-1]["protection"] = {
            "state": "user_reported_booked",
            "user_reference": {
                "message_id": planning(messages)["user_message_references"][-1]["message_id"],
                "quote": "返程G123已经买好",
            },
        }
        data["items"][-1]["travel_duration"] = {
            "basis": "estimate",
            "minutes": 120,
            "explanation": "按时刻表估计",
            "sources": [SOURCE],
            "source_ids": ["estimate"],
        }
        return reply(
            {"base_version_id": first["id"], "document": data, "change_reason": "标记已购返程"},
            "lock",
        )

    model.responses = [lock, final("已标记返程为已购")]
    await send(client, cid, "返程G123已经买好，不能改", "lock-request")
    locked = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
    # The stored version keeps the compact reference that used to trip the check.
    assert locked["document"]["items"][-1]["travel_duration"]["source_ids"] == ["estimate"]
    return locked


async def save_results(service, cid, count):
    raw = await service.facts.history(cid)
    return [json.loads(m["content"]) for m in raw if m.get("role") == "tool"][-count:]


async def test_unrelated_patch_and_resent_document_keep_booked_item(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        locked = await booked_plan(client, model, cid)
        model.responses = [
            reply(
                {
                    "base_version_id": locked["id"],
                    "patch": {"items": [{"id": "day2", "title": "第二天下午博物馆"}]},
                    "change_reason": "第二天下午改室内",
                },
                "indoor",
            ),
            final("已改为室内"),
        ]
        await send(client, cid, "第二天下午改成室内，其他不动", "edit-request")
        patched = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert patched["revision"] == locked["revision"] + 1
        assert patched["document"]["items"][1]["title"] == "第二天下午博物馆"
        assert patched["document"]["items"][-1] == locked["document"]["items"][-1]

        # A full document that cites the source only by ID is also unchanged.
        resent = deepcopy(patched["document"])
        resent["items"][-1]["travel_duration"]["sources"] = []
        resent["items"][1]["title"] = "第二天下午美术馆"
        model.responses = [
            reply(
                {"base_version_id": patched["id"], "document": resent, "change_reason": "换场馆"},
                "resend",
            ),
            final("已换场馆"),
        ]
        await send(client, cid, "换成美术馆", "resend-request")
        current = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert current["revision"] == patched["revision"] + 1
        assert (await save_results(service, cid, 1))[0]["status"] == "ok"


async def test_conflict_names_item_and_changed_fields(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        locked = await booked_plan(client, model, cid)
        model.responses = [
            reply(
                {
                    "base_version_id": locked["id"],
                    "patch": {
                        "items": [
                            {"id": "day2", "title": "第二天下午博物馆"},
                            {"id": "return", "travel_duration": {"minutes": 90}},
                        ]
                    },
                    "change_reason": "顺手改了返程时长",
                },
                "changed",
            ),
            reply(
                {
                    "base_version_id": locked["id"],
                    "patch": {"remove_items": ["return"]},
                    "change_reason": "删除返程",
                },
                "removed",
            ),
            final("返程已购，不能修改"),
        ]
        await send(client, cid, "第二天下午改室内", "edit-request")
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"] == locked
        changed, removed = await save_results(service, cid, 2)
        assert changed["error"]["code"] == "locked_item_conflict"
        assert changed["data"]["conflicts"] == [
            {
                "section": "items",
                "id": "return",
                "title": "返程 G123",
                "change": "modified",
                "fields": [{"field": "travel_duration.minutes", "before": 120.0, "after": 90.0}],
            }
        ]
        assert "before" in changed["error"]["message"]
        # Only the protected entry is reported; the unrelated day2 edit is not echoed.
        assert "第二天下午博物馆" not in json.dumps(changed["data"], ensure_ascii=False)
        assert removed["data"]["conflicts"] == [
            {
                "section": "items",
                "id": "return",
                "title": "返程 G123",
                "change": "removed",
                "fields": [],
            }
        ]


def test_arrangement_view_resolves_references_but_keeps_real_differences():
    item = {
        "id": "train",
        "title": "G1",
        "kind": "transport",
        "start": "2026-10-13T19:00:00+08:00",
        "end": "2026-10-13T21:00:00+08:00",
        "travel_duration": {
            "basis": "queried",
            "minutes": 120,
            "explanation": "时刻表",
            "sources": [SOURCE],
            "source_ids": ["s"],
        },
    }
    compact = arrangement_view("items", item, {"s": SOURCE})
    resolved = deepcopy(item)
    resolved["travel_duration"].update(sources=[SOURCE, SOURCE], source_ids=[])
    assert arrangement_view("items", resolved) == compact
    other = {**SOURCE, "url": "https://example.test/other"}
    assert arrangement_view("items", item, {"s": other}) != compact
    assert arrangement_view("items", item, {}) != compact  # unknown ID stays visible


async def test_missing_base_and_wrapped_arguments_are_not_reported_as_stale(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        edit = {
            "base_version_id": first["id"],
            "patch": {"items": [{"id": "day2", "title": "第二天下午博物馆"}]},
            "change_reason": "第二天下午改室内",
        }
        missing = {k: v for k, v in edit.items() if k != "base_version_id"}
        wrapped = {"arguments": json.dumps(edit, ensure_ascii=False)}
        old = {**edit, "base_version_id": "older-version"}
        model.responses = [
            reply(missing, "missing"),
            reply(wrapped, "wrapped"),
            reply(old, "old"),
            final("未能保存"),
        ]
        await send(client, cid, "第二天下午改室内", "edit-request")
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"] == first
        missing_result, wrapped_result, old_result = await save_results(service, cid, 3)
        assert missing_result["error"]["code"] == "missing_base_version_id"
        assert missing_result["data"]["conflicts"] == [{"current_version_id": first["id"]}]
        assert wrapped_result["error"]["code"] == "invalid_save_arguments"
        assert wrapped_result["data"]["conflicts"] == [
            {"unexpected_fields": ["arguments"], "current_version_id": first["id"]}
        ]
        assert old_result["error"]["code"] == "stale_itinerary_version"
        assert old_result["data"]["conflicts"] == [
            {"base_version_id": "older-version", "current_version_id": first["id"]}
        ]

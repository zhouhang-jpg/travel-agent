"""Repair saves from durable history without weakening validation or provenance."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_graph_runtime import api_client, final, send
from test_itinerary_versions import ReplayModel, document, planning, save_reply, setup_plan

from travel_agent.itineraries import PlanConflict, public_version, repair_rejected_candidate
from travel_agent.itinerary_schema import SaveItineraryInput


def reply(arguments, call_id):
    result = save_reply(arguments)
    result.message["tool_calls"][0]["id"] = call_id
    return result


async def test_initial_save_repairs_schema_then_time_conflict_with_small_patches(tmp_path):
    model = ReplayModel()
    candidate = document()
    candidate["costs"][0]["total"] = None  # reference requires a numeric total.
    candidate["items"][-1]["start"] = "2026-10-13T15:45:00+08:00"  # overlaps day2.
    original = reply({"document": candidate, "change_reason": "首次规划"}, "initial")
    cost_fix = reply(
        {
            "retry_from_call_id": "initial",
            "patch": {"costs": [{"id": "hotel-cost", "kind": "unknown"}]},
            "change_reason": "住宿金额未知",
        },
        "cost-fix",
    )
    time_fix = reply(
        {
            "retry_from_call_id": "cost-fix",
            "patch": {"items": [{"id": "return", "start": "2026-10-13T19:00:00+08:00"}]},
            "change_reason": "修正返程时间冲突",
        },
        "time-fix",
    )
    model.responses = [original, cost_fix, time_fix, final("修正后交付，费用仍有未知。")]
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        await send(client, cid, "请规划两天行程", "r1")
        view = (await client.get(f"/conversations/{cid}")).json()
        assert view["status"] == "completed"
        saved = view["itinerary"]
        assert saved["revision"] == 1
        assert saved["document"]["costs"][0]["total"] is None
        assert saved["document"]["costs"][0]["kind"] == "unknown"
        assert saved["document"]["items"][-1]["start"].startswith("2026-10-13T19:00")
        assert saved["document"]["items"][0]["title"] == candidate["items"][0]["title"]
        raw = await service.facts.history(cid)
        original_messages = [m for m in raw if m["role"] != "system"]
        assert original_messages[1] == original.message
        assert original_messages[3] == cost_fix.message
        assert original_messages[5] == time_fix.message
        assert json.loads(original_messages[2]["content"])["error"]["code"] == "invalid_itinerary"
        assert json.loads(original_messages[4]["content"])["error"]["code"] == "itinerary_conflict"
        assert json.loads(original_messages[6]["content"])["status"] == "ok"
        assert model.requests[-1][1:] == raw[:-1]  # Complete provider messages stay intact.
        index = planning(model.requests[-1])["pending_itinerary"]
        canonical = await service.itineraries.current(cid)
        assert index["document"] == canonical.document
        assert all("after" not in change for change in index["diff"]["changes"])
        assert any("after" in change for change in saved["diff"]["changes"])
        full_size = len(original.message["tool_calls"][0]["function"]["arguments"])
        assert len(cost_fix.message["tool_calls"][0]["function"]["arguments"]) < full_size / 5


def records(arguments, *, run="run", status="error", call_id="save", offset=0):
    return [
        SimpleNamespace(
            id=f"{run}/model/{offset}",
            payload={
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "function": {
                            "name": "save_itinerary",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            },
        ),
        SimpleNamespace(
            id=f"{run}/model/{offset}/tool/0",
            payload={
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(
                    {
                        "status": status,
                        "tool_name": "save_itinerary",
                        "call_id": call_id,
                    }
                ),
            },
        ),
    ]


@pytest.mark.parametrize("case", ["other_run", "success", "older_failure", "wrong_base"])
def test_retry_cannot_reference_another_run_success_or_stale_save(case):
    rejected = {"document": document(), "change_reason": "initial"}
    history = records(
        rejected,
        run="other" if case == "other_run" else "run",
        status="ok" if case == "success" else "error",
    )
    if case == "older_failure":
        history += records(rejected, offset=1, call_id="latest")
    request = {"retry_from_call_id": "save", "patch": {}, "change_reason": "repair"}
    if case == "wrong_base":
        request["base_version_id"] = "invented-version"
    with pytest.raises(PlanConflict) as exc:
        repair_rejected_candidate(request, history, "run", None)
    assert exc.value.code == "invalid_itinerary_retry"


def test_retry_inherits_source_imports_and_does_not_mutate_rejected_history():
    rejected = {
        "document": document(),
        "available_source_ids": ["real-source"],
        "change_reason": "initial",
    }
    history = records(rejected)
    before = deepcopy(history)
    repaired = repair_rejected_candidate(
        {
            "retry_from_call_id": "save",
            "patch": {
                "costs": [{"id": "train-cost", "basis": {"calculation": "未知费用，不按零计"}}]
            },
            "change_reason": "repair",
        },
        history,
        "run",
        None,
    )
    assert repaired["available_source_ids"] == ["real-source"]
    assert repaired["document"]["source_catalog"] == rejected["document"]["source_catalog"]
    assert repaired["document"]["costs"][-1]["total"] is None
    assert history == before


async def test_repair_still_enforces_locked_items_and_published_version(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)

        def lock(messages):
            data = deepcopy(first["document"])
            data["items"][0]["protection"] = {
                "state": "locked",
                "user_reference": {
                    "message_id": planning(messages)["user_message_references"][-1]["message_id"],
                    "quote": "第一天散步锁定",
                },
            }
            return reply(
                {"base_version_id": first["id"], "document": data, "change_reason": "锁定首日"},
                "lock",
            )

        model.responses = [lock, final("首日锁定")]
        await send(client, cid, "第一天散步锁定", "lock-request")
        locked = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        model.responses = [
            reply(
                {
                    "base_version_id": locked["id"],
                    "patch": {
                        "items": [
                            {
                                "id": "day1",
                                "title": "更换已锁定活动",
                            }
                        ]
                    },
                    "change_reason": "尝试改活动",
                },
                "bad-edit",
            ),
            reply(
                {
                    "base_version_id": locked["id"],
                    "retry_from_call_id": "bad-edit",
                    "patch": {"costs": [{"id": "train-cost", "title": "仍未知"}]},
                    "change_reason": "仅修正费用，锁定冲突尚在",
                },
                "bad-repair",
            ),
            final("无法更改锁定安排，保留原版本"),
        ]
        await send(client, cid, "只更新返程费用说明", "edit-request")
        current = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert current == locked
        raw = await service.facts.history(cid)
        failed = [json.loads(m["content"]) for m in raw if m.get("role") == "tool"][-2:]
        assert all(r["error"]["code"] == "locked_item_conflict" for r in failed)


def test_internal_index_avoids_duplicate_diff_but_keeps_canonical_document_and_review():
    row = SimpleNamespace(
        id="v",
        base_id=None,
        revision=1,
        created_at="now",
        document=document(),
        review={"status": "unknown"},
        reason="initial",
        diff={
            "changes": [
                {
                    "section": "items",
                    "id": "day1",
                    "operation": "added",
                    "before": None,
                    "after": document()["items"][0],
                    "fields": ["title"],
                }
            ],
            "cost_deltas": [{"currency": "CNY", "after": "600"}],
        },
    )
    full = public_version(row, internal=False)
    index = public_version(row, internal=True)
    assert index["document"] == full["document"]
    assert index["review"] == full["review"]
    assert index["diff"]["cost_deltas"] == full["diff"]["cost_deltas"]
    assert index["diff"]["changes"][0]["fields"] == ["title"]
    assert "after" in row.diff["changes"][0]


def test_retry_schema_rejects_ambiguous_document_and_patch():
    with pytest.raises(ValidationError):
        SaveItineraryInput.model_validate(
            {
                "retry_from_call_id": "x",
                "document": document(),
                "patch": {},
                "change_reason": "ambiguous",
            }
        )


async def test_rejected_candidate_can_be_repaired_after_runtime_restart(tmp_path):
    candidate = document()
    candidate["costs"][0]["total"] = None
    initial = reply({"document": candidate, "change_reason": "首次保存"}, "initial")
    first_model = ReplayModel()
    first_model.responses = [initial]
    async with api_client(tmp_path, first_model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        lease = await service.facts.accept(
            cid, "请规划", "r1", runtime_spec=service.runtime.manifest
        )

        async def crash(name):
            if name == "tool_saved_before_checkpoint":
                raise asyncio.CancelledError

        service.runtime.failpoint = crash
        with pytest.raises(asyncio.CancelledError):
            await service.runtime.execute(lease)
        assert (
            await service.facts.effect(lease["run_id"] + "/model/0/tool/0")
        ).status == "complete"

    restored_model = ReplayModel()
    restored_model.responses = [
        reply(
            {
                "retry_from_call_id": "initial",
                "patch": {"costs": [{"id": "hotel-cost", "kind": "unknown"}]},
                "change_reason": "重启后局部修正",
            },
            "repair",
        ),
        final("恢复并交付"),
    ]
    async with api_client(tmp_path, restored_model) as (client, service):
        await asyncio.gather(*service.tasks)
        view = (await client.get(f"/conversations/{cid}")).json()
        assert view["status"] == "completed"
        assert view["itinerary"]["revision"] == 1
        assert view["itinerary"]["document"]["costs"][0]["kind"] == "unknown"
        assert len(restored_model.requests) == 2  # The original model call is reused.
        raw = await service.facts.history(cid)
        assert len([m for m in raw if m.get("role") == "tool"]) == 2
        assert restored_model.requests[-1][1:] == raw[:-1]

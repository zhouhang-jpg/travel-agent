"""JSON syntax repairs and model progress preserve the normal validation boundary."""

import asyncio
import json

import pytest
from test_graph_runtime import api_client, final, send
from test_itinerary_save_optimization import reply
from test_itinerary_versions import ReplayModel, document

from travel_agent.itinerary_schema import ArgumentEdit
from travel_agent.model_progress import model_phase
from travel_agent.tool_arguments import ArgumentJSONError, edit_arguments, parse_arguments


@pytest.mark.parametrize("shape", ["missing_closure", "missing_comma"])
async def test_syntax_error_is_located_and_repaired_without_rewriting_document(tmp_path, shape):
    original = reply({"document": document(), "change_reason": "首次规划"}, "broken")
    function = original.message["tool_calls"][0]["function"]
    good = function["arguments"]
    bad = (
        good[:-1]
        if shape == "missing_closure"
        else good.replace(', "change_reason"', ' "change_reason"')
    )
    function["arguments"] = bad

    def repair(messages):
        error = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        location = error["data"]["json_error"]
        assert location["source_call_id"] == "broken"
        assert location["character_count"] == len(bad)
        return reply(
            {
                "retry_from_call_id": "broken",
                "argument_edits": [
                    {
                        "start": location["character_offset"],
                        "delete_count": 0,
                        "expected": "",
                        "insert": "}" if shape == "missing_closure" else ",",
                    }
                ],
                "change_reason": "仅修正JSON语法",
            },
            "repair",
        )

    model = ReplayModel()
    model.responses = [original, repair, final("行程已交付")]
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        await send(client, cid, "请规划两天", "r1")
        view = (await client.get(f"/conversations/{cid}")).json()
        assert view["itinerary"]["revision"] == 1
        raw = await service.facts.history(cid)
        original_saved = next(m for m in raw if m.get("tool_calls"))
        assert original_saved == original.message
        assert model.requests[-1][1:] == raw[:-1]
        events = await service.facts.events(cid)
        phases = [
            e["phase"] for e in events if e["type"] == "model_progress" and e["status"] == "running"
        ]
        assert phases == ["deciding", "repairing_itinerary", "reviewing_saved_itinerary"]
        assert "private-eval" not in json.dumps(events)
        assert "argument_edits" not in json.dumps(events)


@pytest.mark.parametrize(
    "edits",
    [
        [{"start": 100, "delete_count": 0, "expected": "", "insert": "}"}],
        [{"start": 0, "delete_count": 1, "expected": "x", "insert": "{"}],
        [
            {"start": 0, "delete_count": 2, "expected": "{}", "insert": "{}"},
            {"start": 1, "delete_count": 0, "expected": "", "insert": "}"},
        ],
    ],
)
def test_argument_edits_require_exact_non_overlapping_original_positions(edits):
    with pytest.raises(ValueError):
        edit_arguments("{}", [ArgumentEdit.model_validate(e) for e in edits])


def test_json_position_counts_unicode_characters_and_does_not_echo_input():
    bad = '{"地点":"中文秘密"'
    with pytest.raises(ArgumentJSONError) as caught:
        parse_arguments(bad, "call")
    details = caught.value.details
    assert details["character_offset"] == len(bad)
    assert details["column"] == len(bad) + 1
    assert "中文秘密" not in json.dumps(details, ensure_ascii=False)


def test_new_user_turn_does_not_inherit_old_save_repair_phase():
    old = {
        "role": "tool",
        "content": json.dumps({"tool_name": "save_itinerary", "status": "error"}),
    }
    assert model_phase([old, {"role": "user", "content": "新的查询"}]) == "deciding"


async def test_active_model_progress_is_available_in_refresh_snapshot(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingModel:
        async def complete(self, messages, tools):
            entered.set()
            await release.wait()
            return final()

    async with api_client(tmp_path, BlockingModel()) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        sending = asyncio.create_task(send(client, cid, "出去走走", "r1"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            snapshot = (await client.get(f"/conversations/{cid}")).json()
            assert snapshot["model_progress"]["status"] == "running"
            assert snapshot["model_progress"]["phase"] == "deciding"
            assert snapshot["model_progress"]["request_number"] == 1
        finally:
            release.set()
            await sending


async def test_two_syntax_errors_can_be_fixed_in_successive_small_edits(tmp_path):
    original = reply({"document": document(), "change_reason": "首次保存"}, "broken")
    function = original.message["tool_calls"][0]["function"]
    function["arguments"] = function["arguments"][:-1].replace(
        ', "change_reason"', ' "change_reason"'
    )

    def correction(call_id, insert):
        def respond(messages):
            error = json.loads(
                next(m["content"] for m in reversed(messages) if m["role"] == "tool")
            )
            location = error["data"]["json_error"]
            return reply(
                {
                    "retry_from_call_id": location["source_call_id"],
                    "argument_edits": [
                        {
                            "start": location["character_offset"],
                            "delete_count": 0,
                            "expected": "",
                            "insert": insert,
                        }
                    ],
                    "change_reason": "继续修正语法",
                },
                call_id,
            )

        return respond

    model = ReplayModel()
    model.responses = [original, correction("comma", ","), correction("closure", "}"), final()]
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        await send(client, cid, "规划两天", "r1")
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"]["revision"] == 1

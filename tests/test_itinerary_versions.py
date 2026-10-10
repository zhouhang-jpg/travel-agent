"""Chinese multi-turn replay against the actual durable API; no production fixtures."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest
from test_graph_runtime import api_client, final, question, send

from travel_agent.durable_storage import DurableStore
from travel_agent.itineraries import ItineraryService, PlanConflict
from travel_agent.models import ModelError, ModelReply
from travel_agent.runner import _tool_result
from travel_agent.storage import ConversationStore
from travel_tools.itinerary import validate_itinerary
from travel_tools.schemas.itinerary import ValidateItineraryInput

SOURCE = {"provider": "planning-assumption", "retrieved_at": "2026-10-09T12:00:00Z"}


def document():
    return {
        "title": "两天轻松游",
        "planning_window": {
            "start": "2026-10-12T08:00:00+08:00",
            "end": "2026-10-13T22:00:00+08:00",
        },
        "party": {"travelers": 2, "rooms": 1},
        "items": [
            {
                "id": "day1",
                "title": "第一天公园散步",
                "start": "2026-10-12T10:00:00+08:00",
                "end": "2026-10-12T11:00:00+08:00",
                "opening_hours_required": False,
            },
            {
                "id": "day2",
                "title": "第二天下午公园",
                "start": "2026-10-13T14:00:00+08:00",
                "end": "2026-10-13T16:00:00+08:00",
                "opening_hours_required": False,
            },
            {
                "id": "return",
                "title": "返程 G123",
                "kind": "transport",
                "start": "2026-10-13T19:00:00+08:00",
                "end": "2026-10-13T21:00:00+08:00",
                "opening_hours_required": False,
            },
        ],
        "lodging": [
            {
                "id": "hotel",
                "title": "酒店A",
                "hotel_id": "hotel-A",
                "check_in": "2026-10-12",
                "check_out": "2026-10-13",
            }
        ],
        "required_lodging_nights": [{"night": "2026-10-12"}],
        "required_cost_ids": ["hotel-cost", "activity-cost", "train-cost"],
        "costs": [
            {
                "id": "hotel-cost",
                "title": "住宿",
                "total": {"amount": "600", "currency": "CNY"},
                "kind": "reference",
                "source_ids": ["estimate"],
                "lodging_ids": ["hotel"],
                "basis": {
                    "travelers": 2,
                    "rooms": 1,
                    "nights": 1,
                    "calculation": "1间房×1晚的参考总额",
                },
            },
            {
                "id": "activity-cost",
                "title": "第二天下午活动",
                "total": {"amount": "0", "currency": "CNY"},
                "kind": "reference",
                "source_ids": ["estimate"],
                "item_ids": ["day2"],
                "basis": {"travelers": 2, "calculation": "免费散步假设，仍需核实"},
            },
            {"id": "train-cost", "title": "返程费用", "kind": "unknown", "total": None},
        ],
        "source_catalog": {"estimate": SOURCE},
        "evidence": {"estimate": {"origin": "assumption"}},
        "budget_scope": "limited",
        "budget": {"amount": "2000", "currency": "CNY"},
        "fixed_commitments_complete": True,
    }


def save_reply(arguments):
    return ModelReply(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "private-eval",
            "tool_calls": [
                {
                    "id": "same-id",
                    "type": "function",
                    "function": {
                        "name": "save_itinerary",
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        },
        "tool_calls",
        {},
    )


class ReplayModel:
    def __init__(self):
        self.responses, self.requests = [], []

    async def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        next_reply = self.responses.pop(0)
        if isinstance(next_reply, Exception):
            raise next_reply
        if callable(next_reply):
            return next_reply(messages)
        return next_reply


def planning(messages):
    return json.loads(
        messages[0]["content"].split("【当前行程与原话引用，仅作本会话索引，完整历史仍保留】\n")[1]
    )


async def setup_plan(client, model, cid):
    def initial(messages):
        data = document()
        data["requirements"] = [
            {
                "id": "budget",
                "statement": "两人总预算2000元",
                "origin": "user",
                "user_reference": {
                    "message_id": planning(messages)["user_message_references"][-1]["message_id"],
                    "quote": "两人总预算2000元",
                },
            }
        ]
        return save_reply({"document": data, "change_reason": "首次规划"})

    model.responses = [initial, final("两天方案已保存，返程费用未知，不把已知小计当总价。")]
    await send(client, cid, "请安排两天游，两人总预算2000元", "r1")
    return (await client.get(f"/conversations/{cid}")).json()["itinerary"]


async def test_generate_lock_modify_preserve_and_show_versions_budget_sources(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        assert first["revision"] == 1
        assert first["document"]["items"][0]["protection"] is None
        assert first["document"]["costs"][0]["source_ids"] == ["estimate"]

        def lock(messages):
            context = planning(messages)
            data = deepcopy(context["current_itinerary"]["document"])
            reference = {
                "message_id": context["user_message_references"][-1]["message_id"],
                "quote": "我已订好酒店A和返程G123，两项锁定",
            }
            for item in [data["lodging"][0], data["items"][-1]]:
                item["protection"] = {"state": "user_reported_booked", "user_reference": reference}
            return save_reply(
                {
                    "base_version_id": first["id"],
                    "document": data,
                    "change_reason": "保留用户告知已订的住宿和返程，未做供应商核验",
                }
            )

        model.responses = [
            lock,
            final("酒店和返程已保留为固定项；预订状态是你提供的信息，尚未核验。"),
        ]
        await send(client, cid, "我已订好酒店A和返程G123，两项锁定", "r2")
        fixed = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert fixed["revision"] == 2

        def modify(messages):
            data = deepcopy(planning(messages)["current_itinerary"]["document"])
            data["items"][1].update(title="第二天下午室内展览", opening_hours_required=True)
            data["costs"][1]["total"]["amount"] = "50"
            data["costs"][1]["basis"]["calculation"] = "两人门票参考合计50元，需核实"
            return save_reply(
                {
                    "base_version_id": fixed["id"],
                    "document": data,
                    "change_reason": "仅调整第二天下午为室内活动，给返程保留原时间",
                }
            )

        model.responses = [
            modify,
            final(
                "第二天下午改为室内展览，酒店与G123保留。已知小计增加50元；门票、开放时间和转场仍待核实，返程费用未知。"
            ),
        ]
        response = await send(
            client, cid, "只把第二天下午改室内活动，其他不变，这个活动你决定", "r3"
        )
        latest = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert latest["revision"] == 3
        assert latest["document"]["lodging"] == fixed["document"]["lodging"]
        assert latest["document"]["items"][0] == fixed["document"]["items"][0]
        assert latest["document"]["items"][-1] == fixed["document"]["items"][-1]
        assert latest["document"]["requirements"] == first["document"]["requirements"]
        changed = [c["id"] for c in latest["diff"]["changes"] if c["section"] == "items"]
        assert changed == ["day2"]
        assert Decimal(latest["diff"]["cost_deltas"][0]["delta"]) == 50
        assert latest["review"]["unknown_cost_ids"] == ["train-cost"]
        assert latest["review"]["status"] == "unknown"
        assert (
            latest["document"]["source_catalog"]["estimate"]["retrieved_at"]
            == first["document"]["source_catalog"]["estimate"]["retrieved_at"]
        )
        assert "itinerary_updated" in response.text and "private-eval" not in response.text
        versions = (await client.get(f"/conversations/{cid}/itineraries")).json()
        assert len(versions["items"]) == 3
        old = (await client.get(f"/conversations/{cid}/itineraries/{first['id']}")).json()
        assert old == first
        raw = await service.facts.history(cid)
        assert [m["content"] for m in raw if m["role"] == "user"][
            -1
        ] == "只把第二天下午改室内活动，其他不变，这个活动你决定"


@pytest.mark.parametrize("case", ["drop_condition", "fake_user", "overlap", "old_base"])
async def test_invalid_edits_do_not_replace_completed_version(tmp_path, case):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        data = deepcopy(first["document"])
        base = first["id"]
        if case == "drop_condition":
            data["requirements"] = []
        if case == "fake_user":
            data["items"][0]["protection"] = {
                "state": "locked",
                "user_reference": {"message_id": "fake", "quote": "用户同意"},
            }
        if case == "overlap":
            data["items"][-1].update(start="2026-10-13T15:00:00+08:00")
        if case == "old_base":
            base = "wrong-version"
        model.responses = [
            save_reply({"document": data, "base_version_id": base, "change_reason": "修改"}),
            question("存在冲突，请选择如何处理。"),
        ]
        await send(client, cid, "尝试修改", "r2")
        public = (await client.get(f"/conversations/{cid}")).json()
        assert public["status"] == "waiting_user" and public["itinerary"] == first
        assert len((await client.get(f"/conversations/{cid}/itineraries")).json()["items"]) == 1


async def test_draft_is_not_published_during_question_or_model_failure(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        changed = deepcopy(first["document"])
        changed["items"][1]["title"] = "室内候选草案"
        candidate = save_reply(
            {"base_version_id": first["id"], "document": changed, "change_reason": "等待确认"}
        )
        model.responses = [candidate, question("门票未知，是否继续考虑？")]
        await send(client, cid, "考虑第二天下午室内", "r2")
        public = (await client.get(f"/conversations/{cid}")).json()
        assert public["itinerary"] == first
        model.responses = [candidate, ModelError("timeout", "safe", True)]
        await send(client, cid, "可以", "r3", public["question_id"])
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"] == first


async def test_buffer_estimates_and_actual_insufficient_time_are_distinct():
    data = document()
    data.pop("title")
    data.pop("party")
    data.pop("evidence")
    data["items"] = [data["items"][1], data["items"][2]]
    data["items"][1]["start"] = "2026-10-13T16:40:00+08:00"
    data["transfers"] = [
        {
            "from_item_id": "day2",
            "to_item_id": "return",
            "minimum_minutes": 20,
            "source_ids": ["estimate"],
            "buffers_complete": True,
            "buffers": [
                {
                    "kind": "station_entry",
                    "minutes": 30,
                    "basis": "estimate",
                    "explanation": "本次进站缓冲假设，非实查规定",
                }
            ],
        }
    ]
    result = await validate_itinerary(ValidateItineraryInput.model_validate(data))
    assert any(c.category == "transfers" and c.status == "fail" for c in result.checks)
    data["items"][1]["start"] = "2026-10-13T17:00:00+08:00"
    result = await validate_itinerary(ValidateItineraryInput.model_validate(data))
    assert any(c.category == "transfers" and c.status == "unknown" for c in result.checks)


@pytest.mark.parametrize("release", ["none", "old_quote", "explicit"])
async def test_locked_return_changes_require_new_user_authorization(tmp_path, release):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        data = deepcopy(first["document"])
        old_reference = {"message_id": cid + "/r2", "quote": "返程G123已订，锁定"}
        data["items"][-1]["protection"] = {
            "state": "user_reported_booked",
            "user_reference": old_reference,
        }
        model.responses = [
            save_reply(
                {"base_version_id": first["id"], "document": data, "change_reason": "记录已订返程"}
            ),
            final("保留已订返程"),
        ]
        await send(client, cid, "返程G123已订，锁定", "r2")
        fixed = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        changed = deepcopy(fixed["document"])
        changed["items"][-1]["title"] = "候选G456"
        changed["items"][-1]["protection"] = None
        args = {"base_version_id": fixed["id"], "document": changed, "change_reason": "尝试换返程"}
        if release != "none":
            args["release_protections"] = [
                {
                    "section": "items",
                    "id": "return",
                    "user_reference": old_reference
                    if release == "old_quote"
                    else {"message_id": cid + "/r3", "quote": "我明确解除G123锁定，可以改返程"},
                }
            ]
        model.responses = [
            save_reply(args),
            final(
                "已按授权调整候选返程"
                if release == "explicit"
                else "原返程保留，不能在未授权情况下解除"
            ),
        ]
        await send(
            client,
            cid,
            "我明确解除G123锁定，可以改返程"
            if release == "explicit"
            else "第二天下午活动你决定，返程不动",
            "r3",
        )
        result = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert result["revision"] == (3 if release == "explicit" else 2)
        assert result["document"]["items"][-1]["title"] == (
            "候选G456" if release == "explicit" else "返程 G123"
        )


@pytest.mark.parametrize(
    "case",
    ["observed", "changed_date", "party_changed", "invented_amount", "expired", "missing_tax"],
)
async def test_quotes_preserve_source_time_and_reject_false_price_scope(tmp_path, case):
    store = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "evidence.db").as_posix())
    await store.initialize()
    facts = DurableStore(store)
    cid = (await store.create("Asia/Shanghai"))["id"]
    lease = await facts.accept(cid, "两人出行", "r1")
    arguments = {"departure_date": "2026-10-13", "travelers": {"adults": 2, "children_ages": []}}
    call = {"id": "q", "function": {"name": "search_trains", "arguments": json.dumps(arguments)}}
    raw_model = {"role": "assistant", "content": None, "tool_calls": [call]}
    await facts.begin_effect(lease, "model-fixture", "model", 12)
    await facts.commit_effect(lease, "model-fixture", {}, raw_model)
    price = {
        "kind": "query_quote",
        "money": {"amount": "50", "currency": "CNY"},
        "unit": "whole_party",
        "priced_persons": 2,
        "tax_basis": "included",
        "fee_basis": "included",
    }
    if case == "expired":
        price["valid_until"] = "2026-10-09T12:00:00Z"
    if case == "missing_tax":
        price["tax_basis"] = "unknown"
    quote_source = {"provider": "synthetic-quote-fixture", "retrieved_at": "2026-10-10T01:00:00Z"}
    raw_tool = _tool_result(call, data={"sources": [quote_source], "price": price})
    await facts.begin_effect(lease, "tool-fixture", "tool", 12)
    await facts.commit_effect(lease, "tool-fixture", {}, raw_tool)
    data = document()
    data["source_catalog"]["quote"] = quote_source
    data["evidence"]["quote"] = {
        "kind": "quote",
        "origin": "tool",
        "travel_dates": ["2026-10-13"],
        "travelers": 2,
        "query_conditions": arguments,
        "item_ids": ["day2"],
        "cost_ids": ["activity-cost"],
    }
    cost = data["costs"][1]
    cost.update(
        kind="query_quote", tax_basis="included", fee_basis="included", source_ids=["quote"]
    )
    cost["total"]["amount"] = "50"
    if case == "changed_date":
        data["planning_window"]["end"] = "2026-10-14T22:00:00+08:00"
        data["items"][1].update(start="2026-10-14T14:00:00+08:00", end="2026-10-14T16:00:00+08:00")
    if case == "party_changed":
        data["party"]["travelers"] = 3
        cost["basis"]["travelers"] = 3
    if case == "invented_amount":
        cost["total"]["amount"] = "500"
    candidate = await ItineraryService(facts).prepare(
        lease, "save-fixture", {"document": data, "change_reason": "报价口径验收"}
    )
    normalized = candidate["document"]["costs"][1]
    assert normalized["kind"] == (
        "query_quote" if case in {"observed", "missing_tax"} else "reference"
    )
    if case == "missing_tax":
        assert normalized["tax_basis"] == "unknown"
    assert candidate["document"]["source_catalog"]["quote"]["retrieved_at"].startswith(
        "2026-10-10T01:00:00"
    )
    assert candidate["review"]["status"] == "unknown"  # Rail cost remains unknown.
    await store.close()


async def test_patch_preserves_unmentioned_items_conditions_sources_and_unknowns(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, _):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        model.responses = [
            save_reply(
                {
                    "base_version_id": first["id"],
                    "change_reason": "局部修改下午",
                    "patch": {
                        "items": [{"id": "day2", "title": "室内展览"}],
                        "costs": [{"id": "activity-cost", "total": {"amount": "50"}}],
                    },
                }
            ),
            final("仅改第二天下午，其他安排保留，费用仍有未知项。"),
        ]
        await send(client, cid, "只改第二天下午", "r2")
        result = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert result["revision"] == 2
        assert result["document"]["items"][0] == first["document"]["items"][0]
        assert result["document"]["items"][-1] == first["document"]["items"][-1]
        assert result["document"]["requirements"] == first["document"]["requirements"]
        assert result["document"]["source_catalog"] == first["document"]["source_catalog"]
        assert result["document"]["costs"][1]["total"] == {"currency": "CNY", "amount": "50"}
        assert result["review"]["unknown_cost_ids"] == ["train-cost"]


async def test_actual_source_import_and_transport_block_cannot_shorten_queried_duration(tmp_path):
    store = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "routes.db").as_posix())
    await store.initialize()
    facts = DurableStore(store)
    cid = (await store.create("Asia/Shanghai"))["id"]
    lease = await facts.accept(cid, "请核实时长", "r1")
    call = {"id": "route", "function": {"name": "get_routes", "arguments": '{"mode":"transit"}'}}
    await facts.begin_effect(lease, "route-model", "model", 12)
    await facts.commit_effect(lease, "route-model", {}, {"role": "assistant", "tool_calls": [call]})
    source = {"provider": "amap-test", "retrieved_at": "2026-10-10T02:13:14.123456Z"}
    raw = _tool_result(call, data={"sources": [source], "routes": [{"duration_s": 2700}]})
    await facts.begin_effect(lease, "route-tool", "tool", 12)
    await facts.commit_effect(lease, "route-tool", {}, raw)
    service = ItineraryService(facts)
    sources = (await service.context(cid))["available_sources"]
    sid = next(iter(sources))
    data = document()
    data["evidence"][sid] = {"origin": "tool", "query_conditions": {}}
    item = data["items"][1]
    item.update(
        kind="transport",
        start="2026-10-13T14:00:00+08:00",
        end="2026-10-13T14:30:00+08:00",
        travel_duration={
            "minutes": 30,
            "basis": "queried",
            "explanation": "地图来源时长",
            "source_ids": [sid],
        },
    )
    arguments = {"document": data, "available_source_ids": [sid], "change_reason": "核实时长"}
    with pytest.raises(PlanConflict, match="已知冲突"):
        await service.prepare(lease, "save-short", arguments)
    item["end"] = "2026-10-13T14:50:00+08:00"
    candidate = await service.prepare(lease, "save-fitting", arguments)
    assert candidate["document"]["items"][1]["travel_duration"]["minutes"] == 45
    assert candidate["document"]["items"][1]["travel_duration"]["source_ids"] == [sid]
    assert candidate["document"]["source_catalog"][sid]["retrieved_at"].endswith("123456Z")
    assert candidate["review"]["evidence"][sid]["state"] == "observed"
    arguments.pop("available_source_ids")
    data["source_catalog"][sid] = source
    data["evidence"][sid]["query_conditions"] = {"narrative": "not original arguments"}
    fixed = await service.prepare(lease, "save-pinned-id", arguments)
    assert fixed["review"]["evidence"][sid]["state"] == "observed"
    assert fixed["document"]["evidence"][sid]["query_conditions"] == {"mode": "transit"}
    await store.close()

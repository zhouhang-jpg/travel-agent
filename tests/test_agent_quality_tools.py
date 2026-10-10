"""Quality tools use the durable API without changing planning authority.

All model replies and comparison evidence are offline, explicitly supplied test
data. Tests check public behavior, saved versions and effects, not tool order as
a product policy or natural-language requirement interpretation.
"""

import json
from copy import deepcopy

import pytest
import test_graph_runtime
from sqlalchemy import select
from test_graph_runtime import api_client, final, question, send
from test_itinerary_versions import ReplayModel, document, planning, save_reply, setup_plan

from travel_agent.durable_storage import Effect
from travel_agent.itineraries import ItineraryVersion, available_sources, source_bank
from travel_agent.models import ModelReply
from travel_agent.runner import _tool_result
from travel_agent.tool_encoding import decode_tool_result
from travel_tools.plan_comparison import compare_itinerary_options
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.schemas.itinerary import ValidateItineraryInput
from travel_tools.schemas.plan_comparison import (
    CompareItineraryOptionsInput,
    CompareItineraryOptionsOutput,
)


def tool_reply(name, arguments, call_id="quality-call"):
    return ModelReply(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "quality-private-reasoning",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        },
        "tool_calls",
        {},
    )


def review_arguments(version):
    return {
        "base_version_id": version["id"],
        "requirement_assessments": [
            {
                "requirement_id": requirement["id"],
                "assessment": "satisfied",
                "explanation": "模型判断，仍需按原话和费用范围核对",
                "item_ids": [version["document"]["items"][0]["id"]],
            }
            for requirement in version["document"]["requirements"]
            if requirement["state"] == "active"
        ],
        "claims": [{"id": "party", "kind": "party_size", "travelers": 2}],
    }


async def results_for(service, cid, name):
    history = await service.facts.history(cid)
    return [
        result
        for message in history
        if message.get("role") == "tool"
        and (result := decode_tool_result(message["content"])).get("tool_name") == name
    ]


async def versions_for(service, cid):
    async with service.store.sessions() as session:
        return list(
            (
                await session.scalars(
                    select(ItineraryVersion)
                    .where(ItineraryVersion.conversation_id == cid)
                    .order_by(ItineraryVersion.journal_seq)
                )
            ).all()
        )


def comparison_arguments(saved_document):
    plan = {
        key: deepcopy(value)
        for key, value in saved_document.items()
        if key in ValidateItineraryInput.model_fields
    }
    return {
        "basis": {
            "planning_window": deepcopy(plan["planning_window"]),
            "travelers": 2,
            "rooms": 1,
            "budget_scope": plan["budget_scope"],
            "budget": deepcopy(plan["budget"]),
            "required_cost_ids": deepcopy(plan["required_cost_ids"]),
            "required_lodging_nights": deepcopy(plan["required_lodging_nights"]),
            "fixed_commitments_complete": True,
        },
        "options": [
            {"id": "preserved", "label": "保留原安排", "itinerary": deepcopy(plan)},
            {"id": "changed", "label": "另一候选", "itinerary": deepcopy(plan)},
        ],
        "objectives": ["activity_minutes"],
    }


def install_comparison_registry(monkeypatch):
    """Supply the real deterministic handler before the shared API helper starts."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "compare_itinerary_options",
            "Compare explicitly supplied offline alternatives.",
            CompareItineraryOptionsInput,
            CompareItineraryOptionsOutput,
            compare_itinerary_options,
            "ready",
            execution="parallel_read",
        )
    )
    monkeypatch.setattr(test_graph_runtime, "ToolRegistry", lambda: registry)


@pytest.mark.parametrize("target", ["current", "candidate"])
async def test_review_is_read_only_and_request_retry_reuses_one_effect(tmp_path, target):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        arguments = review_arguments(first)
        if target == "candidate":
            arguments.pop("base_version_id")
            arguments["document"] = deepcopy(first["document"])
            arguments["document"]["items"][1]["title"] = "尚未保存的室内候选"
        model.responses = [tool_reply("review_itinerary", arguments), final("仅提供复核结果")]
        response = await send(client, cid, "检查方案，不修改", "review-only")
        assert response.status_code == 200
        before_retry = len(model.requests)
        assert (await send(client, cid, "检查方案，不修改", "review-only")).status_code == 200
        assert len(model.requests) == before_retry
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"] == first
        rows = await versions_for(service, cid)
        assert len(rows) == 1 and rows[0].status == "published"
        results = await results_for(service, cid, "review_itinerary")
        assert len(results) == 1 and results[0]["status"] == "ok"
        report = results[0]["data"]
        assert report["state"] == "read_only"
        assert report["checked_version_id"] == (first["id"] if target == "current" else None)
        assert report["claims"][0]["status"] == "pass"
        assert report["requirements"][0]["status"] == "unknown"
        async with service.store.sessions() as session:
            effects = list(
                (await session.scalars(select(Effect).where(Effect.kind == "tool"))).all()
            )
        reviews = [
            effect
            for effect in effects
            if decode_tool_result(effect.payload["raw"]["content"])["tool_name"]
            == "review_itinerary"
        ]
        assert len(reviews) == 1 and reviews[0].attempts == 1
        assert "quality-private-reasoning" not in response.text
        assert "candidate_validation" not in response.text


@pytest.mark.parametrize("claim_conflict", [False, True])
async def test_save_review_final_publishes_one_draft_even_when_findings_fail(
    tmp_path, claim_conflict
):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)

        def inspect_draft(messages):
            context = planning(messages)
            assert context["current_itinerary"]["id"] == first["id"]
            draft = context["pending_itinerary"]
            assert draft["document"]["items"][1]["title"] == "第二天下午室内候选"
            arguments = review_arguments(draft)
            if claim_conflict:
                arguments["claims"][0]["travelers"] = 99
            return tool_reply("review_itinerary", arguments)

        model.responses = [
            save_reply(
                {
                    "base_version_id": first["id"],
                    "patch": {"items": [{"id": "day2", "title": "第二天下午室内候选"}]},
                    "change_reason": "只修改第二天下午",
                }
            ),
            inspect_draft,
            final("局部修改已交付；保留未知费用和复核项"),
        ]
        await send(client, cid, "第二天下午改室内，其他安排保留", "edit-review")
        latest = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert latest["revision"] == 2
        assert latest["document"]["items"][1]["title"] == "第二天下午室内候选"
        result = (await results_for(service, cid, "review_itinerary"))[-1]
        assert result["status"] == "ok"
        assert result["data"]["checked_version_id"] == latest["id"]
        assert result["data"]["claims"][0]["status"] == ("fail" if claim_conflict else "pass")
        assert result["data"]["status"] == ("fail" if claim_conflict else "unknown")
        rows = await versions_for(service, cid)
        assert len(rows) == 2 and all(row.status == "published" for row in rows)


@pytest.mark.parametrize("target", ["foreign", "old_published", "other_run_draft"])
async def test_review_rejects_versions_outside_current_or_own_draft(tmp_path, target):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        qid = None
        if target == "foreign":
            foreign = (await client.post("/conversations", json={})).json()["id"]
            selected = await setup_plan(client, model, foreign)
        else:
            model.responses = [
                save_reply(
                    {
                        "base_version_id": first["id"],
                        "patch": {"items": [{"id": "day2", "title": "新候选"}]},
                        "change_reason": "新版本候选",
                    }
                ),
                question("是否继续这个候选？") if target == "other_run_draft" else final(),
            ]
            await send(client, cid, "考虑新候选", "make-candidate")
            if target == "old_published":
                selected = first
            else:
                rows = await versions_for(service, cid)
                selected = {"id": next(row.id for row in rows if row.status == "draft")}
                qid = (await client.get(f"/conversations/{cid}")).json()["question_id"]
        previous_public = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        before_count = len(await versions_for(service, cid))
        model.responses = [
            tool_reply("review_itinerary", {"base_version_id": selected["id"]}),
            final("无法读取该版本，当前方案保持"),
        ]
        await send(client, cid, "只检查这份版本", "reject-version", qid)
        result = (await results_for(service, cid, "review_itinerary"))[-1]
        assert result["status"] == "error"
        assert result["error"]["code"] == "unavailable_review_version"
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"] == previous_public
        assert len(await versions_for(service, cid)) == before_count


async def test_saved_draft_with_trusted_release_can_be_reviewed_without_new_authorization(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)

        def lock(messages):
            reference = {
                "message_id": planning(messages)["user_message_references"][-1]["message_id"],
                "quote": "返程G123已订，锁定",
            }
            return save_reply(
                {
                    "base_version_id": first["id"],
                    "patch": {
                        "items": [
                            {
                                "id": "return",
                                "protection": {
                                    "state": "user_reported_booked",
                                    "user_reference": reference,
                                },
                            }
                        ]
                    },
                    "change_reason": "按用户原话记录已订锁定",
                }
            )

        model.responses = [lock, final("返程锁定保留")]
        await send(client, cid, "返程G123已订，锁定", "lock")
        locked = (await client.get(f"/conversations/{cid}")).json()["itinerary"]

        def release(messages):
            reference = {
                "message_id": planning(messages)["user_message_references"][-1]["message_id"],
                "quote": "我明确解除G123锁定，可以改返程",
            }
            return save_reply(
                {
                    "base_version_id": locked["id"],
                    "patch": {"items": [{"id": "return", "title": "候选G456", "protection": None}]},
                    "release_protections": [
                        {"section": "items", "id": "return", "user_reference": reference}
                    ],
                    "change_reason": "按用户新原话解除指定返程锁定",
                }
            )

        model.responses = [
            release,
            lambda messages: tool_reply(
                "review_itinerary", review_arguments(planning(messages)["pending_itinerary"])
            ),
            final("按已给出的解除授权修改返程候选，未核验新预订"),
        ]
        await send(client, cid, "我明确解除G123锁定，可以改返程", "release-review")
        review = (await results_for(service, cid, "review_itinerary"))[-1]
        assert review["status"] == "ok"
        assert not any(
            finding["code"] == "protected_item_changed" for finding in review["data"]["references"]
        )
        latest = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        assert latest["revision"] == 3
        assert latest["document"]["items"][-1]["title"] == "候选G456"
        assert latest["document"]["items"][-1]["protection"] is None
        assert review["data"]["checked_version_id"] == latest["id"]


@pytest.mark.parametrize("forgery", ["user", "source", "source_import"])
async def test_candidate_review_does_not_upgrade_fake_attribution(tmp_path, forgery):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]

        def forged_candidate(messages):
            data = document()
            message_id = planning(messages)["user_message_references"][-1]["message_id"]
            data["requirements"] = [
                {
                    "id": "budget",
                    "statement": "两人总预算2000元",
                    "origin": "user",
                    "user_reference": {
                        "message_id": message_id,
                        "quote": "从未说过这个授权" if forgery == "user" else "两人总预算2000元",
                    },
                }
            ]
            arguments = {
                "document": data,
                "requirement_assessments": [
                    {
                        "requirement_id": "budget",
                        "assessment": "satisfied",
                        "explanation": "模型声称满足不是证明",
                        "cost_ids": ["hotel-cost"],
                        "source_ids": ["estimate"],
                    }
                ],
                "claims": [
                    {
                        "id": "price",
                        "kind": "cost_amount",
                        "cost_id": "hotel-cost",
                        "amount": {"amount": "600", "currency": "CNY"},
                        "basis": "verified_quote",
                    },
                    {
                        "id": "statement",
                        "kind": "source_statement",
                        "statement": "酒店价格已经核实且必定可售",
                        "source_ids": ["estimate"],
                    },
                ],
            }
            if forgery != "user":
                data["source_catalog"]["estimate"] = {
                    "provider": "fabricated-supplier",
                    "url": "https://example.test/fake-price",
                    "retrieved_at": "2026-10-09T12:00:00Z",
                }
                data["evidence"]["estimate"] = {
                    "origin": "tool",
                    "kind": "quote",
                    "travel_dates": ["2026-10-12", "2026-10-13"],
                    "query_conditions": {"check_in": "2026-10-12"},
                }
                data["costs"][0].update(
                    kind="query_quote", tax_basis="included", fee_basis="included"
                )
                if forgery == "source_import":
                    arguments["available_source_ids"] = ["source_fabricated"]
            return tool_reply("review_itinerary", arguments)

        model.responses = [forged_candidate, final("仅做检查，未知与未证实来源保持未知")]
        await send(client, cid, "两人总预算2000元，检查候选", "fake-attribution")
        result = (await results_for(service, cid, "review_itinerary"))[-1]
        if forgery == "source":
            assert result["status"] == "ok"
            assert result["data"]["requirements"][0]["status"] == "unknown"
            assert all(claim["status"] == "unknown" for claim in result["data"]["claims"])
            assert result["data"]["candidate_validation"]["evidence"]["estimate"]["state"] == (
                "needs_review"
            )
        else:
            assert result["status"] == "error"
            assert result["error"]["code"] == (
                "invalid_user_reference" if forgery == "user" else "unknown_available_source"
            )
        assert await versions_for(service, cid) == []
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"] is None
        assert source_bank(await service.facts.history(cid)) == {}


async def test_ask_user_mixed_with_review_executes_neither_tool(tmp_path):
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        mixed = tool_reply("review_itinerary", review_arguments(first))
        mixed.message["tool_calls"].extend(question("补充偏好", "ask-mixed").message["tool_calls"])
        model.responses = [mixed, final("已收到工具协议反馈，当前方案保持")]
        await send(client, cid, "检查方案", "mixed-review")
        for name in ("review_itinerary", "ask_user"):
            result = (await results_for(service, cid, name))[-1]
            assert result["status"] == "error"
            assert result["error"]["code"] == "ask_user_must_be_alone"
        public = (await client.get(f"/conversations/{cid}")).json()
        assert public["status"] == "completed" and public["question_id"] is None
        assert public["itinerary"] == first
        assert len(await versions_for(service, cid)) == 1


@pytest.mark.parametrize("protected_section", ["items", "lodging"])
async def test_comparison_binds_current_protection_even_if_model_omits_or_rewrites_it(
    tmp_path, monkeypatch, protected_section
):
    install_comparison_registry(monkeypatch)
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        protected_id = "return" if protected_section == "items" else "hotel"

        def lock(messages):
            reference = {
                "message_id": planning(messages)["user_message_references"][-1]["message_id"],
                "quote": "这个已订安排锁定保留",
            }
            return save_reply(
                {
                    "base_version_id": first["id"],
                    "patch": {
                        protected_section: [
                            {
                                "id": protected_id,
                                "protection": {
                                    "state": "user_reported_booked",
                                    "user_reference": reference,
                                },
                            }
                        ]
                    },
                    "change_reason": "记录用户自报已订保护",
                }
            )

        model.responses = [lock, final("保护已记录")]
        await send(client, cid, "这个已订安排锁定保留", "lock-comparison")
        locked = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
        arguments = comparison_arguments(locked["document"])
        changed = next(
            entry
            for entry in arguments["options"][1]["itinerary"][protected_section]
            if entry["id"] == protected_id
        )
        changed["title"] = "模型尝试替换已订项目"
        # The item case omits the common protection; the lodging case tries to
        # replace it. Both must bind the server's current accepted arrangement.
        if protected_section == "lodging":
            arguments["basis"]["protected_lodging"] = [deepcopy(changed)]
        model.responses = [
            tool_reply("compare_itinerary_options", arguments),
            final("保留用户已订安排，另一候选存在保护冲突"),
        ]
        await send(client, cid, "比较这两个候选，保持已订项目", "compare-locked")
        result = (await results_for(service, cid, "compare_itinerary_options"))[-1]
        assert result["status"] == "ok"
        options = {option["id"]: option for option in result["data"]["options"]}
        assert any(
            check["category"] == "protection"
            and check["status"] == "pass"
            and check["ids"] == [protected_id]
            for check in options["preserved"]["checks"]
        )
        assert options["changed"]["status"] == "conflict"
        assert any(
            check["category"] == "protection" and check["status"] == "fail"
            for check in options["changed"]["checks"]
        )
        assert (await client.get(f"/conversations/{cid}")).json()["itinerary"] == locked
        assert len(await versions_for(service, cid)) == 2
        assert source_bank(await service.facts.history(cid)) == {}
        assert planning(model.requests[-1])["available_sources"] == {}


@pytest.mark.parametrize("malformed", ["basis_type", "protected_missing_id", "version_id_type"])
async def test_invalid_quality_arguments_are_tool_errors_and_model_can_continue(
    tmp_path, monkeypatch, malformed
):
    install_comparison_registry(monkeypatch)
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        if malformed == "version_id_type":
            name, arguments = "review_itinerary", {"base_version_id": {"invalid": "type"}}
        else:
            name, arguments = "compare_itinerary_options", comparison_arguments(first["document"])
            if malformed == "basis_type":
                arguments["basis"] = ["invalid"]
            else:
                arguments["basis"]["protected_items"] = [{"title": "缺ID和其他字段"}]
        model.responses = [tool_reply(name, arguments), final("参数错误已明确返回，继续正常交付")]
        await send(client, cid, "只比较或复核，不修改", "bad-quality-arguments")
        result = (await results_for(service, cid, name))[-1]
        assert result["status"] == "error"
        assert result["error"]["code"] == "invalid_itinerary"
        public = (await client.get(f"/conversations/{cid}")).json()
        assert public["status"] == "completed"
        assert public["itinerary"] == first
        supplied_results = [
            decode_tool_result(message["content"])
            for message in model.requests[-1]
            if message.get("role") == "tool"
        ]
        assert supplied_results[-1]["tool_name"] == name
        assert supplied_results[-1]["error"]["code"] == "invalid_itinerary"


@pytest.mark.parametrize("name", ["compare_itinerary_options", "review_itinerary", "unknown"])
def test_model_authored_tool_source_echo_does_not_become_observed_evidence(name):
    source = {
        "provider": "model-supplied-source",
        "url": "https://example.test/claimed-official",
        "retrieved_at": "2026-10-09T12:00:00Z",
    }
    reply = tool_reply(name, {"source": source})
    call = reply.message["tool_calls"][0]
    history = [reply.message, _tool_result(call, data={"sources": [source]})]
    assert source_bank(history) == {}
    assert available_sources(source_bank(history)) == {}


@pytest.mark.parametrize("name", ["compare_itinerary_options", "review_itinerary"])
async def test_quality_json_error_keeps_model_running_without_save_only_repair_advice(
    tmp_path, monkeypatch, name
):
    install_comparison_registry(monkeypatch)
    model = ReplayModel()
    async with api_client(tmp_path, model) as (client, service):
        cid = (await client.post("/conversations", json={})).json()["id"]
        first = await setup_plan(client, model, cid)
        malformed = tool_reply(name, {})
        malformed.message["tool_calls"][0]["function"]["arguments"] = '{"document":'
        model.responses = [malformed, final("已收到语法错误位置，当前方案保持")]
        await send(client, cid, "只检查，不修改", "bad-quality-json")
        result = (await results_for(service, cid, name))[-1]
        assert result["status"] == "error"
        assert result["error"]["code"] == "invalid_itinerary_json"
        assert result["data"]["json_error"]
        assert "retry_from_call_id" not in result["error"]["message"]
        assert "argument_edits" not in result["error"]["message"]
        supplied_results = [
            decode_tool_result(message["content"])
            for message in model.requests[-1]
            if message.get("role") == "tool"
        ]
        assert supplied_results[-1]["error"]["code"] == "invalid_itinerary_json"
        public = (await client.get(f"/conversations/{cid}")).json()
        assert public["status"] == "completed"
        assert public["itinerary"] == first
        assert len(await versions_for(service, cid)) == 1


@pytest.mark.parametrize("reported_name", ["search_web", "compare_itinerary_options"])
def test_supplier_origin_must_match_result_tool_name_before_importing_source(reported_name):
    source = {"provider": "test-search", "retrieved_at": "2026-10-09T12:00:00Z"}
    reply = tool_reply("search_web", {"query": "公开公告"})
    call = reply.message["tool_calls"][0]
    raw = _tool_result(call, data={"sources": [source]})
    result = json.loads(raw["content"])
    result["tool_name"] = reported_name
    raw["content"] = json.dumps(result)
    bank = source_bank([reply.message, raw])
    assert bool(bank) == (reported_name == "search_web")

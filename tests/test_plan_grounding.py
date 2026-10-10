"""Explicit claims never turn model assessments or a source ID into proof."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from travel_agent.itineraries import source_key
from travel_agent.itinerary_schema import ItineraryDocument
from travel_agent.plan_grounding import ReviewItineraryInput, review_itinerary

USERS = {"u1": {"content": "两人预算1000元，不坐飞机，酒店已订请保留。", "seq": 1}}


def plan():
    return {
        "title": "周末出行",
        "planning_window": {
            "start": "2026-10-12T08:00:00+08:00",
            "end": "2026-10-13T22:00:00+08:00",
        },
        "party": {"travelers": 2, "rooms": 1},
        "items": [
            {
                "id": "trip",
                "title": "高铁出行",
                "kind": "transport",
                "start": "2026-10-12T10:00:00+08:00",
                "end": "2026-10-12T12:00:00+08:00",
            }
        ],
        "source_catalog": {
            "s1": {
                "provider": "rail",
                "url": "https://example.test/rail",
                "retrieved_at": "2026-10-09T12:00:00Z",
            }
        },
        "evidence": {
            "s1": {
                "origin": "tool",
                "kind": "quote",
                "travel_dates": ["2026-10-12"],
                "travelers": 2,
                "cost_ids": ["fare"],
                "query_conditions": {"departure_date": "2026-10-12"},
            }
        },
        "costs": [
            {
                "id": "fare",
                "title": "车费",
                "kind": "query_quote",
                "total": {"amount": "200", "currency": "CNY"},
                "source_ids": ["s1"],
                "item_ids": ["trip"],
                "tax_basis": "included",
                "fee_basis": "included",
                "basis": {"travelers": 2, "calculation": "每人100元×2人"},
            }
        ],
        "required_cost_ids": ["fare"],
        "budget_scope": "limited",
        "budget": {"amount": "1000", "currency": "CNY"},
    }


def requirement(identifier="no-flight", quote="不坐飞机"):
    return {
        "id": identifier,
        "statement": quote,
        "origin": "user",
        "user_reference": {"message_id": "u1", "quote": quote},
    }


def assessment(identifier="no-flight", **fields):
    return {
        "requirement_id": identifier,
        "assessment": "satisfied",
        "explanation": "模型认为选择高铁满足用户要求",
        "item_ids": ["trip"],
        **fields,
    }


def budget_claim():
    return {
        "id": "budget",
        "kind": "within_budget",
        "budget": {"amount": "1000", "currency": "CNY"},
    }


def cost_claim(**fields):
    return {
        "id": "cost",
        "kind": "cost_amount",
        "cost_id": "fare",
        "amount": {"amount": "200", "currency": "CNY"},
        **fields,
    }


def review(data=None, *, assessments=None, claims=None, reports=None, proof=None, previous=None):
    document = ItineraryDocument.model_validate(data or plan())
    raw_proof = {
        "arguments": {"departure_date": "2026-10-12", "travelers": {"adults": 2}},
        "prices": [
            {
                "money": {"amount": "100", "currency": "CNY"},
                "kind": "query_quote",
                "unit": "per_person",
                "priced_persons": 1,
                "tax_basis": "included",
                "fee_basis": "included",
                "valid_until": None,
            }
        ],
    }
    bank = {source_key(document.source_catalog["s1"]): [raw_proof if proof is None else proof]}
    request = ReviewItineraryInput.model_validate(
        {
            "document": data or plan(),
            "requirement_assessments": assessments or [],
            "claims": claims or [],
        }
    )
    return review_itinerary(
        document,
        request.requirement_assessments,
        request.claims,
        users=USERS,
        source_reports=reports or {"s1": {"state": "observed", "reasons": []}},
        bank=bank,
        previous_document=previous,
    )


def test_active_requirement_omission_and_raw_message_coverage():
    data = plan()
    data["requirements"] = [requirement(), requirement("budget", "两人预算1000元")]
    result = review(data, assessments=[assessment()])
    assert result.status == "fail"
    assert result.unassessed_requirement_ids == ["budget"]
    assert result.raw_user_message_ids_without_requirement_reference == []
    assert result.requirements[0].assessment_state == "claimed"
    assert result.requirements[0].status == "unknown"
    assert result.requirements[1].code == "requirement_unassessed"


def test_fake_satisfaction_is_not_semantic_proof():
    data = plan()
    data["items"][0]["title"] = "飞机出行"
    data["requirements"] = [requirement()]
    result = review(data, assessments=[assessment()])
    assert result.requirements[0].status == "unknown"
    assert result.requirements[0].code == "semantic_satisfaction_unverified"


@pytest.mark.parametrize("field", ["item_ids", "cost_ids", "lodging_ids", "source_ids"])
def test_invalid_coverage_targets_fail(field):
    data = plan()
    data["requirements"] = [requirement()]
    result = review(data, assessments=[assessment(**{field: ["fabricated"]})])
    assert result.requirements[0].code == "invalid_requirement_targets"
    assert result.requirements[0].status == "fail"


def test_raw_user_quote_mismatch_is_reported():
    data = plan()
    data["requirements"] = [requirement(quote="三人预算500元")]
    result = review(data, assessments=[assessment()])
    assert result.status == "fail"
    assert result.references[0].code == "raw_user_quote_mismatch"
    assert result.raw_user_message_ids_without_requirement_reference == ["u1"]


def test_no_extracted_requirements_does_not_mean_full_coverage():
    result = review(claims=[cost_claim()])
    assert result.claims[0].status == "pass"
    assert result.status == "unknown"
    assert result.raw_user_message_ids_without_requirement_reference == ["u1"]
    assert "complete coverage" in result.scope


def test_previously_locked_plan_cannot_be_silently_changed():
    data = plan()
    data["items"][0]["protection"] = {
        "state": "locked",
        "user_reference": {"message_id": "u1", "quote": "请保留"},
    }
    previous = ItineraryDocument.model_validate(deepcopy(data))
    data["items"][0]["start"] = "2026-10-12T11:00:00+08:00"
    result = review(data, previous=previous)
    assert result.references[-1].code == "protected_item_changed"
    assert result.status == "fail"


def test_preserved_protection_and_previous_requirement_omission():
    previous_data = plan()
    previous_data["requirements"] = [requirement()]
    result = review(previous=ItineraryDocument.model_validate(previous_data))
    assert result.requirements[0].code == "previous_requirement_missing"


@pytest.mark.parametrize(
    "money",
    [
        {"amount": "201", "currency": "CNY"},
        {"amount": "200", "currency": "USD"},
    ],
)
def test_cost_amount_and_currency_mismatch(money):
    result = review(claims=[cost_claim(amount=money)])
    assert result.claims[0].status == "fail"
    assert result.claims[0].code == "cost_amount_mismatch"


def test_cost_party_scope_mismatch_and_correct_planned_amount():
    result = review(claims=[cost_claim(travelers=1)])
    assert result.claims[0].code == "cost_basis_mismatch"
    assert result.claims[0].status == "fail"
    assert review(claims=[cost_claim(travelers=2)]).claims[0].status == "pass"


def test_included_tax_claim_distinguishes_unknown_from_known_exclusion():
    data = plan()
    data["costs"][0]["tax_basis"] = "unknown"
    assert review(data, claims=[cost_claim(taxes_fees="included")]).claims[0].status == "unknown"
    data["costs"][0]["tax_basis"] = "excluded"
    assert review(data, claims=[cost_claim(taxes_fees="included")]).claims[0].status == "fail"


def test_source_id_and_success_never_prove_arbitrary_statement():
    result = review(
        claims=[
            {
                "id": "text",
                "kind": "source_statement",
                "statement": "全程安全且必定有票",
                "source_ids": ["s1"],
            }
        ]
    )
    assert result.claims[0].status == "unknown"
    assert result.claims[0].code == "source_semantics_unverified"
    wrong = review(
        claims=[
            {
                "id": "text",
                "kind": "source_statement",
                "statement": "有票",
                "source_ids": ["fake"],
            }
        ]
    )
    assert wrong.claims[0].status == "fail"


def test_verified_quote_checks_real_tool_price_and_source_time():
    result = review(claims=[cost_claim(basis="verified_quote", taxes_fees="included")])
    assert result.claims[0].status == "pass"
    assert "不证明所选班次/酒店/房型身份绑定" in result.claims[0].feedback[0]
    assert "selected train/flight/hotel or room-type identity" in result.scope
    stale = review(
        claims=[cost_claim(basis="verified_quote")],
        reports={
            "s1": {"state": "needs_review", "reasons": ["来源有效期已过"]},
        },
    )
    assert stale.claims[0].status == "unknown"
    assert "来源有效期已过" in stale.claims[0].feedback
    fake = plan()
    fake["costs"][0]["total"]["amount"] = "250"
    result = review(
        fake,
        claims=[
            cost_claim(
                basis="verified_quote",
                amount={"amount": "250", "currency": "CNY"},
            )
        ],
    )
    assert result.claims[0].status == "unknown"
    assert any("真实工具报价" in feedback for feedback in result.claims[0].feedback)


def test_budget_pass_is_only_the_complete_declared_quote_snapshot():
    result = review(claims=[budget_claim()])
    assert result.claims[0].status == "pass"
    assert result.claims[0].code == "within_declared_budget_snapshot"
    assert "不保证费用范围" in result.claims[0].feedback[0]
    assert "与报价的身份绑定" in result.claims[0].feedback[0]


def test_empty_cost_scope_cannot_prove_zero_cost_budget():
    data = plan()
    data["costs"] = []
    data["evidence"]["s1"]["cost_ids"] = []
    data["required_cost_ids"] = []
    result = review(data, claims=[budget_claim()])
    assert result.claims[0].status == "unknown"


@pytest.mark.parametrize(
    "case",
    [
        "missing_scope",
        "missing_cost",
        "unknown_cost",
        "tax_unknown",
        "fee_excluded",
        "reference_price",
        "wrong_currency",
        "missing_people",
        "wrong_people",
    ],
)
def test_incomplete_or_unverified_costs_never_prove_within_budget(case):
    data = plan()
    cost = data["costs"][0]
    if case == "missing_scope":
        data.pop("required_cost_ids")
    elif case == "missing_cost":
        data["required_cost_ids"].append("meal")
    elif case == "unknown_cost":
        cost.update(kind="unknown", total=None)
    elif case == "tax_unknown":
        cost["tax_basis"] = "unknown"
    elif case == "fee_excluded":
        cost["fee_basis"] = "excluded"
    elif case == "reference_price":
        cost["kind"] = "reference"
    elif case == "wrong_currency":
        cost["total"]["currency"] = "USD"
    elif case == "missing_people":
        cost.pop("basis")
    elif case == "wrong_people":
        cost["basis"]["travelers"] = 1
    result = review(data, claims=[budget_claim()])
    assert result.claims[0].status == "unknown"
    assert result.claims[0].code == "budget_coverage_unverified"


def test_expired_original_quote_and_missing_tool_history_cannot_pass_budget():
    proof = {
        "arguments": {"travelers": {"adults": 2}},
        "prices": [
            {
                "money": {"amount": "100", "currency": "CNY"},
                "kind": "query_quote",
                "unit": "per_person",
                "priced_persons": 1,
                "tax_basis": "included",
                "fee_basis": "included",
                "valid_until": "2000-01-01T00:00:00Z",
            }
        ],
    }
    assert review(claims=[budget_claim()], proof=proof).claims[0].status == "unknown"
    request = ReviewItineraryInput.model_validate({"document": plan(), "claims": [budget_claim()]})
    result = review_itinerary(
        request.document,
        [],
        request.claims,
        users=USERS,
        source_reports={"s1": {"state": "observed", "reasons": []}},
    )
    assert result.claims[0].status == "unknown"


def test_known_quoted_costs_above_budget_and_wrong_budget_fail():
    data = plan()
    data["budget"]["amount"] = "100"
    result = review(
        data,
        claims=[
            {
                "id": "cap",
                "kind": "within_budget",
                "budget": {"amount": "100", "currency": "CNY"},
            }
        ],
    )
    assert result.claims[0].code == "known_quotes_exceed_budget"
    assert result.claims[0].status == "fail"
    assert review(data, claims=[budget_claim()]).claims[0].code == "budget_mismatch"


def test_atomic_item_times_compare_instants_and_party_values():
    result = review(
        claims=[
            {
                "id": "start",
                "kind": "item_start",
                "item_id": "trip",
                "time": "2026-10-12T02:00:00Z",
            },
            {"id": "end", "kind": "item_end", "item_id": "trip", "time": "2026-10-12T05:00:00Z"},
            {"id": "party", "kind": "party_size", "travelers": 3},
        ]
    )
    assert [claim.status for claim in result.claims] == ["pass", "fail", "fail"]
    assert result.claims[1].code == "item_time_mismatch"
    assert result.claims[2].code == "party_size_mismatch"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"base_version_id": "v1", "document": plan()},
        {"base_version_id": "v1", "claims": [cost_claim(), cost_claim()]},
        {"base_version_id": "v1", "requirement_assessments": [assessment(), assessment()]},
    ],
)
def test_review_input_rejects_ambiguous_targets_and_duplicate_ids(fields):
    with pytest.raises(ValidationError):
        ReviewItineraryInput.model_validate(fields)

"""Comparison must preserve unknown facts, hard constraints, and common cost scope."""

import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from travel_tools.plan_comparison import compare_itinerary_options
from travel_tools.schemas.plan_comparison import CompareItineraryOptionsInput

SOURCE = {"provider": "supplied-test-evidence", "retrieved_at": "2026-10-09T01:00:00Z"}


@pytest.fixture
def comparison_request():
    window = {"start": "2026-10-10T08:00:00+08:00", "end": "2026-10-10T20:00:00+08:00"}
    plan = {
        "planning_window": deepcopy(window),
        "items": [
            {
                "id": "visit",
                "title": "Visit",
                "kind": "activity",
                "start": "2026-10-10T09:00:00+08:00",
                "end": "2026-10-10T10:00:00+08:00",
                "start_place_id": "park",
                "end_place_id": "park",
                "opening_hours_required": False,
            }
        ],
        "fixed_commitments_complete": True,
        "required_lodging_nights": [],
        "required_cost_ids": ["admission"],
        "costs": [
            {
                "id": "admission",
                "title": "Admission",
                "kind": "query_quote",
                "total": {"amount": "100.10", "currency": "CNY"},
                "tax_basis": "included",
                "fee_basis": "included",
                "sources": [SOURCE],
                "basis": {"travelers": 2, "calculation": "Two travelers total"},
            }
        ],
        "budget_scope": "limited",
        "budget": {"amount": "300.00", "currency": "CNY"},
    }
    other = deepcopy(plan)
    other["costs"][0]["total"]["amount"] = "200.20"
    return {
        "basis": {
            "planning_window": deepcopy(window),
            "travelers": 2,
            "budget_scope": "limited",
            "budget": {"amount": "300.00", "currency": "CNY"},
            "required_cost_ids": ["admission"],
            "required_lodging_nights": [],
            "fixed_commitments_complete": True,
        },
        "options": [
            {"id": "cheap", "label": "Cheap", "itinerary": plan},
            {"id": "expensive", "label": "Expensive", "itinerary": other},
        ],
    }


def compare(data):
    return asyncio.run(compare_itinerary_options(CompareItineraryOptionsInput.model_validate(data)))


def first_plan(comparison_request):
    return comparison_request["options"][0]["itinerary"]


def test_complete_common_basis_derives_dominance_and_preserves_sources(comparison_request):
    result = compare(comparison_request)
    assert [option.status for option in result.options] == ["feasible", "feasible"]
    assert result.pareto_option_ids == ["cheap"]
    assert result.options[1].dominated_by == ["cheap"]
    assert result.options[0].metrics.exact_total_cost.amount == Decimal("100.10")
    assert result.tradeoffs[0].relation == "dominates"
    assert result.tradeoffs[0].differences[0].unit == "CNY"
    assert len(result.sources) == 1
    assert "market optimum" in result.scope


def test_equal_options_both_remain_on_frontier(comparison_request):
    comparison_request["options"][1]["itinerary"] = deepcopy(first_plan(comparison_request))
    result = compare(comparison_request)
    assert result.pareto_option_ids == ["cheap", "expensive"]
    assert result.tradeoffs[0].relation == "equivalent"


def test_cost_and_activity_tradeoff_is_not_weighted_model_score(comparison_request):
    comparison_request["options"][1]["itinerary"]["items"][0]["end"] = "2026-10-10T11:00:00+08:00"
    result = compare(comparison_request)
    assert result.pareto_option_ids == ["cheap", "expensive"]
    assert result.tradeoffs[0].relation == "tradeoff"
    assert [(diff.objective, diff.preferred) for diff in result.tradeoffs[0].differences] == [
        ("total_cost", "left"),
        ("travel_minutes", "equal"),
        ("activity_minutes", "right"),
    ]


@pytest.mark.parametrize("change", ["amount", "tax", "fee", "reference", "basis", "travelers"])
def test_missing_or_reference_facts_never_win_by_being_zero(comparison_request, change):
    cost = first_plan(comparison_request)["costs"][0]
    if change == "amount":
        cost.update(total=None, kind="unknown")
    elif change in {"tax", "fee"}:
        cost[f"{change}_basis"] = "unknown"
    elif change == "reference":
        cost["kind"] = "reference"
    elif change == "basis":
        cost.pop("basis")
    else:
        cost["basis"].pop("travelers")
    result = compare(comparison_request)
    assert result.options[0].status == "needs_review"
    assert result.options[0].metrics.exact_total_cost is None
    assert result.pareto_option_ids == ["expensive"]
    assert result.tradeoffs[0].relation == "incomparable"
    if change == "reference":
        assert result.options[0].metrics.reference_cost_totals[0].amount == Decimal("100.10")
        assert result.options[0].metrics.quoted_cost_totals == []
    if change == "amount":
        assert result.options[0].metrics.unknown_cost_ids == ["admission"]
        assert result.options[0].metrics.quoted_cost_totals == []


def test_wrong_party_is_known_conflict_and_not_ranked(comparison_request):
    first_plan(comparison_request)["costs"][0]["basis"]["travelers"] = 1
    result = compare(comparison_request)
    assert result.options[0].status == "conflict"
    assert result.tradeoffs[0].relation == "excluded"
    assert result.pareto_option_ids == ["expensive"]


@pytest.mark.parametrize("field", ["required_cost_ids", "required_lodging_nights", "budget_scope"])
def test_candidate_self_declared_completeness_does_not_fill_common_unknown_scope(
    comparison_request, field
):
    if field == "budget_scope":
        comparison_request["basis"].update(budget_scope="unknown", budget=None)
    else:
        comparison_request["basis"].pop(field)
    result = compare(comparison_request)
    assert all(option.status == "needs_review" for option in result.options)
    assert result.pareto_option_ids == []


def test_missing_common_cost_category_is_unknown_even_if_candidate_hides_it(comparison_request):
    comparison_request["basis"]["required_cost_ids"].append("return-transport")
    result = compare(comparison_request)
    assert all(option.status == "needs_review" for option in result.options)
    assert all(option.metrics.exact_total_cost is None for option in result.options)


def test_extra_cost_lines_are_counted_and_cannot_be_hidden_by_scope(comparison_request):
    plan = first_plan(comparison_request)
    extra = deepcopy(plan["costs"][0])
    extra.update(id="extra", title="Extra")
    extra["total"]["amount"] = "250"
    plan["costs"].append(extra)
    result = compare(comparison_request)
    assert result.options[0].status == "conflict"
    assert result.options[0].metrics.quoted_cost_totals[0].amount == Decimal("350.10")
    assert any(
        check.status == "fail" and check.category == "budget"
        for check in result.options[0].validation.checks
    )


def test_budget_applies_to_entire_party_and_candidate_cannot_remove_cap(comparison_request):
    comparison_request["basis"]["budget"]["amount"] = "150"
    comparison_request["options"][1]["itinerary"].update(budget_scope="unlimited", budget=None)
    result = compare(comparison_request)
    assert result.options[1].status == "conflict"
    assert result.options[0].status == "feasible"


def test_reference_prices_stay_unknown_even_without_budget_limit(comparison_request):
    comparison_request["basis"].update(budget_scope="unlimited", budget=None)
    first_plan(comparison_request)["costs"][0]["kind"] = "reference"
    result = compare(comparison_request)
    assert result.options[0].status == "needs_review"
    assert result.options[0].metrics.exact_total_cost is None


def test_distinct_currencies_form_separate_unranked_frontiers(comparison_request):
    comparison_request["basis"].update(budget_scope="unlimited", budget=None)
    first_plan(comparison_request)["costs"][0]["total"].update(currency="USD", amount="1")
    result = compare(comparison_request)
    assert all(option.status == "feasible" for option in result.options)
    assert result.tradeoffs[0].relation == "incomparable"
    assert len(result.pareto_groups) == 2
    assert result.pareto_option_ids == ["cheap", "expensive"]
    assert {group.metric_units["total_cost"] for group in result.pareto_groups} == {"USD", "CNY"}


def test_mixed_currencies_in_one_option_are_not_summed(comparison_request):
    comparison_request["basis"].update(budget_scope="unlimited", budget=None)
    plan = first_plan(comparison_request)
    extra = deepcopy(plan["costs"][0])
    extra.update(id="extra", title="Extra")
    extra["total"].update(amount="1", currency="USD")
    plan["costs"].append(extra)
    result = compare(comparison_request)
    assert result.options[0].status == "needs_review"
    assert result.options[0].metrics.exact_total_cost is None
    assert [
        (money.currency, money.amount) for money in result.options[0].metrics.quoted_cost_totals
    ] == [
        ("CNY", Decimal("100.10")),
        ("USD", Decimal("1")),
    ]


def test_shared_time_window_overrides_candidate_window(comparison_request):
    plan = first_plan(comparison_request)
    plan["planning_window"]["start"] = "2026-10-09T08:00:00+08:00"
    plan["items"][0].update(start="2026-10-09T09:00:00+08:00", end="2026-10-09T10:00:00+08:00")
    # The basis has its own copy, so the candidate cannot widen it.
    comparison_request["basis"]["planning_window"]["start"] = "2026-10-10T08:00:00+08:00"
    result = compare(comparison_request)
    assert result.options[0].status == "conflict"
    assert result.options[1].status == "feasible"


def test_shared_fixed_commitment_cannot_be_omitted_or_rewritten(comparison_request):
    commitment = {
        "id": "meeting",
        "title": "Meeting",
        "place_id": "office",
        "start": "2026-10-10T11:00:00+08:00",
        "end": "2026-10-10T12:00:00+08:00",
    }
    comparison_request["basis"]["fixed_commitments"] = [commitment]
    result = compare(comparison_request)
    assert all(option.status == "conflict" for option in result.options)
    first_plan(comparison_request)["fixed_commitments"] = [
        {**commitment, "end": "2026-10-10T11:30:00+08:00"}
    ]
    assert any(
        check.category == "scope" and check.status == "fail"
        for check in compare(comparison_request).options[0].checks
    )


@pytest.mark.parametrize("change", ["missing", "time", "place", "protection"])
def test_shared_protected_item_is_preserved_exactly(comparison_request, change):
    protected = first_plan(comparison_request)["items"][0]
    protected["protection"] = {
        "state": "user_reported_booked",
        "user_reference": {"message_id": "user-1", "quote": "这个活动已经订了"},
    }
    comparison_request["basis"]["protected_items"] = [deepcopy(protected)]
    second = comparison_request["options"][1]["itinerary"]["items"][0]
    second.update(deepcopy(protected))
    if change == "missing":
        comparison_request["options"][1]["itinerary"]["items"] = []
    elif change == "time":
        second["start"] = "2026-10-10T09:30:00+08:00"
    elif change == "place":
        second["start_place_id"] = "elsewhere"
    else:
        second.pop("protection")
    result = compare(comparison_request)
    assert result.options[0].status == "feasible"
    assert result.options[1].status == "conflict"


def add_lodging(comparison_request):
    comparison_request["basis"].update(
        rooms=2,
        required_lodging_nights=[
            {"night": "2026-10-10", "destination_id": "beijing"},
        ],
    )
    comparison_request["basis"]["required_cost_ids"].append("hotel")
    for option in comparison_request["options"]:
        plan = option["itinerary"]
        plan["lodging"] = [
            {
                "id": "stay",
                "check_in": "2026-10-10",
                "check_out": "2026-10-11",
                "destination_id": "beijing",
                "hotel_id": "hotel-1",
            }
        ]
        hotel_cost = deepcopy(plan["costs"][0])
        hotel_cost.update(id="hotel", title="Hotel", lodging_ids=["stay"])
        hotel_cost["total"]["amount"] = "50"
        hotel_cost["basis"].update(rooms=2, nights=1)
        plan["costs"].append(hotel_cost)


@pytest.mark.parametrize("change", ["correct", "rooms", "nights", "missing", "date", "no_link"])
def test_lodging_cost_dates_room_count_and_nights_use_actual_stays(comparison_request, change):
    add_lodging(comparison_request)
    plan = first_plan(comparison_request)
    cost = plan["costs"][1]
    if change in {"rooms", "nights"}:
        cost["basis"][change] = 3
    elif change == "missing":
        cost["basis"].pop("rooms")
    elif change == "date":
        plan["lodging"][0].update(check_in="2026-10-11", check_out="2026-10-12")
    elif change == "no_link":
        cost.pop("lodging_ids")
    result = compare(comparison_request)
    expected = (
        "feasible"
        if change == "correct"
        else ("needs_review" if change in {"missing", "no_link"} else "conflict")
    )
    assert result.options[0].status == expected
    assert result.options[1].status == "feasible"


def test_shared_protected_lodging_cannot_be_replaced(comparison_request):
    add_lodging(comparison_request)
    plan = first_plan(comparison_request)
    plan["lodging"][0]["protection"] = {
        "state": "locked",
        "user_reference": {"message_id": "user-1", "quote": "酒店不要换"},
    }
    comparison_request["basis"]["protected_lodging"] = [deepcopy(plan["lodging"][0])]
    result = compare(comparison_request)
    assert result.options[0].status == "feasible"
    assert result.options[1].status == "conflict"


def test_transport_duration_and_transfer_are_counted_once_without_buffers(comparison_request):
    for option in comparison_request["options"]:
        plan = option["itinerary"]
        plan["items"].insert(
            0,
            {
                "id": "train",
                "title": "Train",
                "kind": "transport",
                "start": "2026-10-10T08:00:00+08:00",
                "end": "2026-10-10T08:30:00+08:00",
                "start_place_id": "station-a",
                "end_place_id": "station-b",
                "opening_hours_required": False,
                "travel_duration": {
                    "minutes": 30,
                    "basis": "queried",
                    "explanation": "Queried train",
                    "sources": [SOURCE],
                },
            },
        )
        plan["transfers"] = [
            {
                "from_item_id": "train",
                "to_item_id": "visit",
                "minimum_minutes": 10,
                "sources": [SOURCE],
                "buffers_complete": True,
                "buffers": [
                    {
                        "kind": "admission",
                        "minutes": 5,
                        "basis": "queried",
                        "explanation": "Admission",
                        "sources": [SOURCE],
                    }
                ],
            }
        ]
    result = compare(comparison_request)
    assert result.options[0].metrics.travel_minutes == 40
    assert result.options[0].metrics.scheduled_transport_minutes == 30
    assert result.options[0].status == "feasible"


@pytest.mark.parametrize("basis", ["missing", "estimate", "unknown"])
def test_unverified_transport_block_duration_is_not_derived_as_actual_travel(
    comparison_request, basis
):
    item = first_plan(comparison_request)["items"][0]
    item["kind"] = "transport"
    if basis != "missing":
        item["travel_duration"] = {
            "minutes": 30 if basis == "estimate" else None,
            "basis": basis,
            "explanation": "Unverified duration",
        }
    result = compare(comparison_request)
    assert result.options[0].status == "needs_review"
    assert result.options[0].metrics.travel_minutes is None
    assert result.options[0].metrics.scheduled_transport_minutes == 60
    assert result.options[0].metrics.reference_travel_minutes == (
        30 if basis == "estimate" else None
    )


def test_unknown_place_transition_is_not_zero(comparison_request):
    plan = first_plan(comparison_request)
    another = deepcopy(plan["items"][0])
    another.update(id="second", start="2026-10-10T11:00:00+08:00", end="2026-10-10T12:00:00+08:00")
    another.pop("start_place_id")
    plan["items"].append(another)
    result = compare(comparison_request)
    assert result.options[0].metrics.travel_minutes is None
    assert result.options[0].status == "needs_review"


def test_dangling_cost_targets_are_known_conflicts(comparison_request):
    first_plan(comparison_request)["costs"][0]["item_ids"] = ["nonexistent"]
    assert compare(comparison_request).options[0].status == "conflict"


def test_no_hidden_mutation_of_original_candidates(comparison_request):
    parsed = CompareItineraryOptionsInput.model_validate(comparison_request)
    before = parsed.model_dump_json()
    asyncio.run(compare_itinerary_options(parsed))
    assert parsed.model_dump_json() == before


def test_actual_zero_quoted_amount_remains_zero(comparison_request):
    first_plan(comparison_request)["costs"][0]["total"]["amount"] = "0.00"
    result = compare(comparison_request)
    assert result.options[0].metrics.exact_total_cost.amount == 0
    assert result.options[0].status == "feasible"


def test_three_options_derive_all_dominators_without_dropping_tradeoff(comparison_request):
    middle = deepcopy(comparison_request["options"][0])
    middle.update(id="middle", label="Middle")
    middle["itinerary"]["costs"][0]["total"]["amount"] = "150"
    comparison_request["options"].append(middle)
    result = compare(comparison_request)
    assert result.pareto_option_ids == ["cheap"]
    assert result.options[1].dominated_by == ["cheap", "middle"]
    assert result.options[2].dominated_by == ["cheap"]
    assert len(result.tradeoffs) == 3


def test_shared_source_ids_are_resolved_and_propagated(comparison_request):
    for option in comparison_request["options"]:
        plan = option["itinerary"]
        plan["source_catalog"] = {"quote-1": SOURCE}
        plan["costs"][0].pop("sources")
        plan["costs"][0]["source_ids"] = ["quote-1"]
    result = compare(comparison_request)
    assert result.pareto_option_ids == ["cheap"]
    assert len(result.sources) == 1


def test_duplicate_lodging_targets_cannot_manufacture_correct_nights(comparison_request):
    add_lodging(comparison_request)
    cost = first_plan(comparison_request)["costs"][1]
    cost["lodging_ids"] = ["stay", "stay"]
    cost["basis"]["nights"] = 2
    result = compare(comparison_request)
    assert result.options[0].status == "conflict"
    assert result.options[0].metrics.exact_total_cost is None


@pytest.mark.parametrize("objectives", [["activity_minutes"], ["total_cost", "travel_minutes"]])
def test_empty_schedule_does_not_establish_zero_actual_travel(comparison_request, objectives):
    first_plan(comparison_request)["items"] = []
    comparison_request["objectives"] = objectives
    result = compare(comparison_request)
    assert result.options[0].status == "needs_review"
    assert result.options[0].metrics.travel_minutes is None
    assert not result.options[0].pareto_eligible
    assert "cheap" not in result.pareto_option_ids
    assert any(
        check.category == "scope"
        and check.status == "unknown"
        and "empty schedule" in check.message
        for check in result.options[0].checks
    )


@pytest.mark.parametrize("buffers_complete", ["omitted", None, False, True])
def test_transfer_buffer_scope_must_be_explicitly_complete(comparison_request, buffers_complete):
    plan = first_plan(comparison_request)
    another = deepcopy(plan["items"][0])
    another.update(
        id="second",
        start="2026-10-10T11:00:00+08:00",
        end="2026-10-10T12:00:00+08:00",
        start_place_id="another-park",
        end_place_id="another-park",
    )
    plan["items"].append(another)
    transfer = {
        "from_item_id": "visit",
        "to_item_id": "second",
        "minimum_minutes": 10,
        "sources": [SOURCE],
    }
    if buffers_complete != "omitted":
        transfer["buffers_complete"] = buffers_complete
    plan["transfers"] = [transfer]
    # Check even without selecting travel as a comparison objective.
    comparison_request["objectives"] = ["activity_minutes"]
    result = compare(comparison_request)
    option = result.options[0]
    assert option.metrics.travel_minutes == 10
    if buffers_complete is True:
        assert option.status == "feasible"
        assert option.pareto_eligible
        assert not any(
            check.category == "travel" and check.status == "unknown" for check in option.checks
        )
    else:
        assert option.status == "needs_review"
        assert not option.pareto_eligible
        assert "cheap" not in result.pareto_option_ids
        assert any(
            check.category == "travel"
            and check.status == "unknown"
            and check.ids == ["visit", "second"]
            for check in option.checks
        )


@pytest.mark.parametrize(
    "change", ["duplicate_option", "score", "duplicate_objective", "budget", "protection"]
)
def test_invalid_or_model_scored_inputs_are_rejected(comparison_request, change):
    if change == "duplicate_option":
        comparison_request["options"][1]["id"] = "cheap"
    elif change == "score":
        comparison_request["options"][0]["score"] = 99
    elif change == "duplicate_objective":
        comparison_request["objectives"] = ["total_cost", "total_cost"]
    elif change == "budget":
        comparison_request["basis"]["budget_scope"] = "unlimited"
    else:
        comparison_request["basis"]["protected_items"] = [
            deepcopy(first_plan(comparison_request)["items"][0])
        ]
    with pytest.raises(ValidationError):
        CompareItineraryOptionsInput.model_validate(comparison_request)

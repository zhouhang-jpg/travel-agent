import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from travel_tools.itinerary import validate_itinerary
from travel_tools.schemas.itinerary import ValidateItineraryInput

SOURCE = {"provider": "test-supplied-evidence", "retrieved_at": "2026-10-09T01:00:00Z"}


@pytest.fixture
def plan():
    return {
        "planning_window": {
            "start": "2026-10-10T08:00:00+08:00",
            "end": "2026-10-10T20:00:00+08:00",
        },
        "items": [
            {
                "id": "museum",
                "title": "Museum",
                "start": "2026-10-10T09:00:00+08:00",
                "end": "2026-10-10T10:00:00+08:00",
                "start_place_id": "museum",
                "end_place_id": "museum",
                "opening_hours_required": True,
            },
            {
                "id": "meeting",
                "title": "Meeting",
                "start": "2026-10-10T11:00:00+08:00",
                "end": "2026-10-10T12:00:00+08:00",
                "start_place_id": "office",
                "end_place_id": "office",
                "opening_hours_required": False,
                "commitment_ids": ["fixed-meeting"],
            },
        ],
        "transfers": [
            {
                "from_item_id": "museum",
                "to_item_id": "meeting",
                "minimum_minutes": 30,
                "sources": [SOURCE],
            }
        ],
        "opening_hours": [
            {
                "item_id": "museum",
                "state": "open_windows",
                "sources": [SOURCE],
                "windows": [
                    {"start": "2026-10-10T09:00:00+08:00", "end": "2026-10-10T17:00:00+08:00"}
                ],
            }
        ],
        "fixed_commitments": [
            {
                "id": "fixed-meeting",
                "title": "Work",
                "place_id": "office",
                "start": "2026-10-10T11:00:00+08:00",
                "end": "2026-10-10T12:00:00+08:00",
            }
        ],
        "fixed_commitments_complete": True,
        "required_lodging_nights": [],
        "required_cost_ids": ["admission"],
        "costs": [
            {
                "id": "admission",
                "title": "Admission",
                "total": {"amount": "100.00", "currency": "CNY"},
                "kind": "query_quote",
                "tax_basis": "included",
                "fee_basis": "included",
                "sources": [SOURCE],
            }
        ],
        "budget_scope": "limited",
        "budget": {"amount": "200.00", "currency": "CNY"},
    }


def check(data):
    return asyncio.run(validate_itinerary(ValidateItineraryInput.model_validate(data)))


def test_shared_source_references_preserve_validation_and_resolve_once(plan):
    expected = check(plan)
    compact = deepcopy(plan)
    compact["source_catalog"] = {"evidence": SOURCE}
    for name in ("transfers", "opening_hours", "costs"):
        for entry in compact[name]:
            entry.pop("sources")
            entry["source_ids"] = ["evidence"]
    parsed = ValidateItineraryInput.model_validate(compact)
    assert asyncio.run(validate_itinerary(parsed)) == expected
    assert len(parsed.costs[0].sources) == 1
    revalidated = ValidateItineraryInput.model_validate(parsed.model_dump())
    assert len(revalidated.costs[0].sources) == 1


@pytest.mark.parametrize("name", ["transfers", "opening_hours", "costs"])
def test_unknown_shared_source_is_rejected_instead_of_becoming_evidence(plan, name):
    plan[name][0].pop("sources")
    plan[name][0]["source_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="source_catalog"):
        ValidateItineraryInput.model_validate(plan)


def statuses(result, category):
    return [entry.status for entry in result.checks if entry.category == category]


def test_complete_evidence_passes_and_preserves_source(plan):
    result = check(plan)
    assert result.status == "valid"
    assert result.known_cost_totals[0].amount == 100
    assert len(result.sources) == 1


def test_sparse_request_is_unknown(plan):
    result = check({"planning_window": plan["planning_window"]})
    assert result.status == "unknown"
    assert "pass" not in {entry.status for entry in result.checks}


@pytest.mark.parametrize(
    "field",
    [
        "transfers",
        "opening_hours",
        "required_cost_ids",
        "required_lodging_nights",
        "fixed_commitments_complete",
    ],
)
def test_omitted_evidence_does_not_pass(plan, field):
    del plan[field]
    assert check(plan).status == "unknown"


def test_timezone_offsets_compare_instants(plan):
    plan["items"][0]["start"] = "2026-10-10T01:00:00Z"
    plan["items"][0]["end"] = "2026-10-10T02:00:00Z"
    assert check(plan).status == "valid"


@pytest.mark.parametrize("change", ["naive", "reversed", "equal"])
def test_invalid_time_intervals_rejected(plan, change):
    if change == "naive":
        plan["items"][0]["start"] = "2026-10-10T09:00:00"
    else:
        plan["items"][0]["end"] = (
            "2026-10-10T08:00:00+08:00" if change == "reversed" else plan["items"][0]["start"]
        )
    with pytest.raises(ValidationError):
        ValidateItineraryInput.model_validate(plan)


def test_nonadjacent_nested_overlap_detected(plan):
    plan["items"][0]["end"] = "2026-10-10T14:00:00+08:00"
    plan["items"].append(
        {
            "id": "third",
            "title": "Third",
            "start": "2026-10-10T13:00:00+08:00",
            "end": "2026-10-10T13:30:00+08:00",
            "opening_hours_required": False,
        }
    )
    result = check(plan)
    overlaps = [entry for entry in result.checks if entry.category == "overlaps"]
    assert result.status == "invalid"
    assert {tuple(entry.item_ids) for entry in overlaps} == {
        ("museum", "meeting"),
        ("museum", "third"),
    }


def test_touching_items_do_not_overlap_but_need_transfer(plan):
    plan["items"][0]["end"] = plan["items"][1]["start"]
    result = check(plan)
    assert statuses(result, "overlaps") == ["pass"]
    assert statuses(result, "transfers") == ["fail"]


def test_explicit_same_location_allows_zero_transfer(plan):
    plan["transfers"] = []
    plan["items"][0]["end_place_id"] = "office"
    assert statuses(check(plan), "transfers") == ["pass"]


def test_contradictory_transfer_evidence_unknown(plan):
    other = deepcopy(plan["transfers"][0])
    other["minimum_minutes"] = 90
    plan["transfers"].append(other)
    assert statuses(check(plan), "transfers") == ["unknown"]


def test_short_transfer_fails(plan):
    plan["transfers"][0]["minimum_minutes"] = 61
    assert statuses(check(plan), "transfers") == ["fail"]


def test_closed_place_fails(plan):
    plan["opening_hours"][0].update(state="closed", windows=[])
    assert "fail" in statuses(check(plan), "opening_hours")


def test_contradictory_opening_evidence_unknown(plan):
    plan["opening_hours"].append({"item_id": "museum", "state": "closed", "sources": [SOURCE]})
    assert "unknown" in statuses(check(plan), "opening_hours")


def test_opening_windows_merge_only_without_a_gap(plan):
    plan["opening_hours"][0]["windows"] = [
        {"start": "2026-10-10T09:00:00+08:00", "end": "2026-10-10T09:30:00+08:00"},
        {"start": "2026-10-10T09:30:00+08:00", "end": "2026-10-10T10:00:00+08:00"},
    ]
    assert "fail" not in statuses(check(plan), "opening_hours")
    plan["opening_hours"][0]["windows"][1]["start"] = "2026-10-10T09:31:00+08:00"
    assert "fail" in statuses(check(plan), "opening_hours")


@pytest.mark.parametrize(
    "change,expected",
    [("missing", "fail"), ("wrong_place", "fail"), ("unknown_place", "unknown"), ("short", "fail")],
)
def test_fixed_commitment_checks_time_and_location(plan, change, expected):
    if change == "missing":
        plan["items"][1]["commitment_ids"] = []
    elif change == "wrong_place":
        plan["items"][1].update(start_place_id="wrong", end_place_id="wrong")
    elif change == "unknown_place":
        plan["items"][1].update(start_place_id=None, end_place_id=None)
    else:
        plan["items"][1]["end"] = "2026-10-10T11:30:00+08:00"
    assert statuses(check(plan), "fixed_commitments") == [expected]


@pytest.mark.parametrize(
    "destination,expected", [("shanghai", "pass"), ("beijing", "fail"), (None, "unknown")]
)
def test_lodging_date_and_destination(plan, destination, expected):
    plan["required_lodging_nights"] = [{"night": "2026-10-10", "destination_id": "shanghai"}]
    plan["lodging"] = [
        {
            "id": "hotel",
            "check_in": "2026-10-10",
            "check_out": "2026-10-11",
            "destination_id": destination,
        }
    ]
    assert statuses(check(plan), "lodging") == [expected]


def test_checkout_night_is_not_covered(plan):
    plan["required_lodging_nights"] = [{"night": "2026-10-11"}]
    plan["lodging"] = [{"id": "hotel", "check_in": "2026-10-10", "check_out": "2026-10-11"}]
    assert statuses(check(plan), "lodging") == ["fail"]


def test_multiple_lodgings_same_night_fail(plan):
    plan["required_lodging_nights"] = [{"night": "2026-10-10"}]
    plan["lodging"] = [
        {"id": stay_id, "check_in": "2026-10-10", "check_out": "2026-10-11"}
        for stay_id in ("one", "two")
    ]
    assert statuses(check(plan), "lodging") == ["fail"]


@pytest.mark.parametrize("change", ["missing", "unknown", "reference", "tax", "fee", "currency"])
def test_incomplete_costs_cannot_pass_budget(plan, change):
    if change == "missing":
        plan["required_cost_ids"].append("unpriced-transfer")
    elif change == "unknown":
        plan["costs"][0].update(total=None, kind="unknown")
    elif change == "reference":
        plan["costs"][0]["kind"] = "reference"
    elif change in ("tax", "fee"):
        plan["costs"][0][f"{change}_basis"] = "unknown"
    else:
        plan["costs"][0]["total"]["currency"] = "USD"
    assert statuses(check(plan), "budget") == ["unknown"]


def test_known_subtotal_exceeding_cap_fails_even_with_missing_costs(plan):
    plan["budget"]["amount"] = "50"
    plan["required_cost_ids"].append("unknown-extra")
    assert statuses(check(plan), "budget") == ["fail"]


def test_reference_price_above_budget_is_explicit_estimate_unknown(plan):
    plan["budget"]["amount"] = "50"
    plan["costs"][0]["kind"] = "reference"
    result = check(plan)
    assert statuses(result, "budget") == ["unknown"]
    assert "above the cap" in next(
        entry.message for entry in result.checks if entry.category == "budget"
    )


def test_decimal_totals_and_zero_are_preserved(plan):
    plan["costs"][0]["total"]["amount"] = "0.10"
    for cost_id, amount in (("second", "0.20"), ("free", "0.00")):
        cost = deepcopy(plan["costs"][0])
        cost.update(id=cost_id, title=cost_id)
        cost["total"]["amount"] = amount
        plan["costs"].append(cost)
    result = check(plan)
    assert str(result.known_cost_totals[0].amount) == "0.30"


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "dangling",
        "unsourced",
        "unknown_amount",
        "budget_mismatch",
        "negative_duration",
        "infinite_duration",
    ],
)
def test_inconsistent_schema_evidence_rejected(plan, change):
    if change == "duplicate":
        plan["items"][1]["id"] = "museum"
    elif change == "dangling":
        plan["transfers"][0]["to_item_id"] = "missing"
    elif change == "unsourced":
        plan["transfers"][0]["sources"] = []
    elif change == "unknown_amount":
        plan["costs"][0]["kind"] = "unknown"
    elif change == "budget_mismatch":
        plan["budget_scope"] = "unlimited"
    else:
        plan["transfers"][0]["minimum_minutes"] = (
            -1 if change == "negative_duration" else float("inf")
        )
    with pytest.raises(ValidationError):
        ValidateItineraryInput.model_validate(plan)


def test_outside_planning_window_fails(plan):
    plan["planning_window"]["start"] = "2026-10-10T10:00:00+08:00"
    assert statuses(check(plan), "time_bounds") == ["fail"]


@pytest.mark.parametrize("blank", ["", " ", "\t\n", "\u3000"])
def test_blank_places_cannot_manufacture_same_place_transfer(plan, blank):
    plan["transfers"] = []
    plan["items"][0]["end_place_id"] = blank
    plan["items"][1]["start_place_id"] = blank
    with pytest.raises(ValidationError):
        ValidateItineraryInput.model_validate(plan)


@pytest.mark.parametrize("blank", ["", " \t "])
def test_blank_fixed_places_cannot_satisfy_fixed_commitment(plan, blank):
    plan["fixed_commitments"][0]["place_id"] = blank
    plan["items"][1].update(start_place_id=blank, end_place_id=blank)
    with pytest.raises(ValidationError):
        ValidateItineraryInput.model_validate(plan)


@pytest.mark.parametrize("field", ["destination_id", "hotel_id"])
@pytest.mark.parametrize("blank", ["", " \t "])
def test_blank_lodging_identifiers_cannot_prove_coverage(plan, field, blank):
    plan["required_lodging_nights"] = [{"night": "2026-10-10", "destination_id": "shanghai"}]
    plan["lodging"] = [
        {
            "id": "hotel",
            "check_in": "2026-10-10",
            "check_out": "2026-10-11",
            "destination_id": "shanghai",
            field: blank,
        }
    ]
    if field == "destination_id":
        plan["required_lodging_nights"][0]["destination_id"] = blank
    with pytest.raises(ValidationError):
        ValidateItineraryInput.model_validate(plan)


@pytest.mark.parametrize("field", ["item", "transfer", "opening", "commitment", "cost"])
def test_blank_identifiers_and_references_are_rejected(plan, field):
    if field == "item":
        plan["items"][0]["id"] = " "
    elif field == "transfer":
        plan["transfers"][0]["from_item_id"] = " "
    elif field == "opening":
        plan["opening_hours"][0]["item_id"] = " "
    elif field == "commitment":
        plan["items"][1]["commitment_ids"] = [" "]
    else:
        plan["required_cost_ids"] = [" "]
    with pytest.raises(ValidationError):
        ValidateItineraryInput.model_validate(plan)


def test_identifier_whitespace_is_trimmed_before_matching_and_uniqueness(plan):
    plan["items"][0]["id"] = " museum "
    plan["items"][1]["start_place_id"] = " office "
    plan["items"][1]["commitment_ids"] = [" fixed-meeting "]
    plan["required_cost_ids"] = [" admission "]
    assert check(plan).status == "valid"
    plan["items"][1]["id"] = "museum"
    with pytest.raises(ValidationError):
        ValidateItineraryInput.model_validate(plan)


def test_unknown_transfer_place_ids_stay_unknown(plan):
    plan["transfers"] = []
    plan["items"][0]["end_place_id"] = None
    plan["items"][1]["start_place_id"] = None
    assert statuses(check(plan), "transfers") == ["unknown"]

from copy import deepcopy

import pytest
from pydantic import ValidationError

from travel_tools.schemas.quotes import (
    FlightOffer,
    InventoryEvidence,
    PriceEvidence,
    SearchCoachesInput,
    SearchFlightsInput,
    SearchHotelsInput,
    SearchTrainsInput,
)


@pytest.fixture
def transport_request():
    return {
        "origin": {"query": "上海"},
        "destination": {"query": "北京"},
        "departure_date": "2026-10-20",
        "travelers": {"adults": 2, "children_ages": [6]},
    }


@pytest.mark.parametrize("schema", [SearchFlightsInput, SearchTrainsInput, SearchCoachesInput])
def test_transport_request_records_party_and_date(transport_request, schema):
    request = schema.model_validate(transport_request)
    assert request.travelers.persons == 3
    assert request.departure_date.isoformat() == "2026-10-20"
    assert request.preferred_currency is None


def test_hotel_request_has_dates_rooms_and_guests():
    request = SearchHotelsInput(
        destination={"query": "杭州"},
        check_in="2026-10-20",
        check_out="2026-10-22",
        rooms=2,
        travelers={"adults": 4},
    )
    assert request.rooms == 2
    assert request.travelers.persons == 4


@pytest.mark.parametrize("check_out", ["2026-10-19", "2026-10-20"])
def test_hotel_invalid_interval_rejected(check_out):
    with pytest.raises(ValidationError):
        SearchHotelsInput(
            destination={"query": "杭州"},
            check_in="2026-10-20",
            check_out=check_out,
            rooms=1,
            travelers={"adults": 1},
        )


def test_price_unknowns_preserved():
    price = PriceEvidence(kind="unknown")
    assert price.money is None
    assert price.priced_persons is None
    assert price.priced_rooms is None
    assert price.priced_nights is None
    assert price.tax_basis == price.fee_basis == "unknown"
    assert InventoryEvidence().status == "unknown"


def test_reference_price_never_implies_availability():
    price = PriceEvidence(kind="reference", money={"amount": "99.99", "currency": "CNY"})
    assert price.kind == "reference"
    assert price.unit == "unknown"
    assert price.priced_persons is None


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "query_quote"},
        {"kind": "unknown", "money": {"amount": "1", "currency": "CNY"}},
        {"kind": "query_quote", "money": {"amount": "-1", "currency": "CNY"}},
        {"kind": "query_quote", "money": {"amount": "NaN", "currency": "CNY"}},
        {"kind": "query_quote", "money": {"amount": "1", "currency": "cny"}},
        {
            "kind": "query_quote",
            "money": {"amount": "1", "currency": "CNY"},
            "taxes": {"amount": "1", "currency": "USD"},
        },
    ],
)
def test_invalid_or_contradictory_price_rejected(payload):
    with pytest.raises(ValidationError):
        PriceEvidence.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "available", "remaining": 0},
        {"status": "unavailable", "remaining": 1},
        {"status": "unknown", "remaining": 10},
        {"status": "request_only", "remaining": 2},
    ],
)
def test_inventory_evidence_not_conflated(payload):
    with pytest.raises(ValidationError):
        InventoryEvidence.model_validate(payload)


@pytest.fixture
def offer():
    return {
        "supplier": "contract-fixture-only",
        "queried_at": "2026-10-09T01:00:00Z",
        "sources": [{"provider": "contract-fixture-only", "retrieved_at": "2026-10-09T01:00:00Z"}],
        "price": {
            "kind": "query_quote",
            "money": {"amount": "1200", "currency": "CNY"},
            "unit": "whole_party",
            "priced_persons": 2,
            "tax_basis": "included",
            "fee_basis": "included",
        },
        "inventory": {"status": "unknown"},
        "origin": {"query": "上海"},
        "destination": {"query": "北京"},
        "departure_at": "2026-10-20T09:00:00+08:00",
        "arrival_at": "2026-10-20T11:00:00+08:00",
    }


def test_flight_contract_preserves_exact_quote_scope_and_unknown_inventory(offer):
    parsed = FlightOffer.model_validate(offer)
    assert parsed.price.priced_persons == 2
    assert parsed.inventory.status == "unknown"
    assert parsed.operator is None
    assert parsed.stops is None


@pytest.mark.parametrize("change", ["naive", "reversed", "unsourced", "expired"])
def test_supplier_offer_requires_time_and_source_integrity(offer, change):
    if change == "naive":
        offer["queried_at"] = "2026-10-09T01:00:00"
    elif change == "reversed":
        offer["arrival_at"] = offer["departure_at"]
    elif change == "unsourced":
        offer["sources"] = []
    else:
        offer["price"]["valid_until"] = "2026-10-08T01:00:00Z"
    with pytest.raises(ValidationError):
        FlightOffer.model_validate(offer)


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "2"])
def test_passenger_counts_are_positive_integers(transport_request, count):
    payload = deepcopy(transport_request)
    payload["travelers"]["adults"] = count
    with pytest.raises(ValidationError):
        SearchFlightsInput.model_validate(payload)

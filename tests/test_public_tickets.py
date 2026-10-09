from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from travel_agent.tool_encoding import decode_tool_result, encode_tool_result
from travel_tools.common import ToolFailure
from travel_tools.providers.bus365 import Bus365Adapter, coach_category
from travel_tools.providers.ceair import CeairAdapter, airport_codes
from travel_tools.providers.rail12306 import Rail12306Adapter, parse_seat
from travel_tools.providers.ticket_data import display_price, inventory, validate_rendered_rows
from travel_tools.providers.ticket_routing import FlightRouter, TrainRouter
from travel_tools.schemas.quotes import (
    QueryCoverage,
    SearchCoachesInput,
    SearchFlightsInput,
    SearchLocation,
    SearchTrainsInput,
    SearchTrainsOutput,
)


def request(schema, **changes):
    return schema.model_validate(
        {
            "origin": {"query": "上海虹桥"},
            "destination": {"query": "杭州东"},
            "departure_date": (
                datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=2)
            ).isoformat(),
            "travelers": {"adults": 1},
            **changes,
        }
    )


class StubRuntime:
    def __init__(self, **data):
        self.data = {
            "retrieved_at": "2026-10-10T01:00:00+08:00",
            "cache_hit": False,
            "elapsed_seconds": 2.1,
            "url": "https://example.com/query",
            **data,
        }

    async def query(self, key, callback):
        return deepcopy(self.data)


@pytest.mark.parametrize(
    "text,status,remaining",
    [
        ("有", "available", None),
        ("2", "available", 2),
        ("无", "unavailable", None),
        ("0", "unavailable", 0),
        ("候补", "waitlist", None),
        ("--", "not_offered", None),
        ("尚未开售", "not_on_sale", None),
        ("暂停网售", "sales_suspended", None),
        ("预订", "unknown", None),
        ("", "unknown", None),
    ],
)
def test_inventory_states_never_invent_counts_or_availability(text, status, remaining):
    value = inventory(text)
    assert value.status == status and value.remaining == remaining


def test_display_price_does_not_lose_exact_amount_when_currency_unknown():
    raw = display_price("￥53.00")
    assert raw.money is None and raw.kind == "unknown"
    assert raw.display.amount == 53 and raw.display.currency_text == "￥"
    explicit = display_price("¥ 1,000", currency="CNY", tax_basis="included")
    assert explicit.money.amount == 1000 and explicit.tax_basis == "included"
    assert explicit.fee_basis == "unknown" and explicit.priced_persons is None
    hidden = display_price("3*")
    assert hidden.display.masked and hidden.display.amount is None and hidden.money is None


def bus_row(**changes):
    return {
        "time": "07:30",
        "origin": "兰州客运中心",
        "destination": "平凉",
        "vehicle": "大型高一级座",
        "duration": "",
        "remaining": "42",
        "price": "￥117",
        "sale": "预订",
        **changes,
    }


@pytest.mark.parametrize(
    "vehicle,category",
    [
        ("大型高一级座", "ordinary_coach"),
        ("中型客车", "ordinary_coach"),
        ("轿车或商务车", "private_car"),
        ("机场巴士", "airport_shuttle"),
        ("高铁接驳专线", "rail_shuttle"),
        ("", "unknown"),
    ],
)
def test_coach_category_uses_product_evidence(vehicle, category):
    assert coach_category(vehicle) == category


async def test_coaches_filter_actual_destination_and_product_not_station_keywords():
    runtime = StubRuntime(
        rows=[
            bus_row(),
            bus_row(destination="六盘山镇"),
            bus_row(vehicle="商务车"),
            bus_row(origin="兰州机场客运站"),
            bus_row(vehicle=""),
            bus_row(vehicle="机场巴士"),
        ],
        page_origin="兰州市",
        adult_basis=True,
        more_pages=True,
    )
    result = await Bus365Adapter(runtime).search(
        request(
            SearchCoachesInput,
            origin={"query": "兰州"},
            destination={"query": "平凉"},
            max_results=50,
        )
    )
    assert len(result.offers) == 3
    assert result.offers[1].origin.query == "兰州机场客运站"
    assert result.offers[2].product_category == "unknown"
    assert all(offer.destination.query == "平凉" for offer in result.offers)
    assert result.coverage.more_pages and not result.complete
    assert result.offers[0].inventory.remaining == 42
    assert result.offers[0].price.priced_persons == 1


async def test_jingchuan_actual_origin_survives_city_heading_and_query_limit():
    runtime = StubRuntime(
        rows=[bus_row(origin="泾川汽车站", destination="西安") for _ in range(5)],
        page_origin="平凉市",
        adult_basis=True,
        more_pages=False,
    )
    result = await Bus365Adapter(runtime).search(
        request(
            SearchCoachesInput,
            origin={"query": "泾川"},
            destination={"query": "西安"},
            max_results=2,
        )
    )
    assert len(result.offers) == 2 and result.coverage.matched_count == 5
    assert result.offers[0].origin.query == "泾川汽车站" and not result.complete


def rail_row(origin="上海虹桥", destination="杭州东", number="D931"):
    return {
        "train": number,
        "summary": [origin, destination, "08:00", "09:00", "01:00"],
        "cells": [{"text": "train summary", "label": None}]
        + [
            {"text": "2", "label": f"{number}次列车，商务座票价673元，余票2"},
            {"text": "--", "label": None},
            {"text": "有", "label": f"{number}次列车，一等座票价313元，余票有"},
            {"text": "候补", "label": f"{number}次列车，二等座票价53元，余票候补"},
        ]
        + [{"text": "--", "label": None}] * 7,
    }


async def test_rail_exact_endpoints_all_seats_and_waitlist_are_preserved():
    runtime = StubRuntime(rows=[rail_row(), rail_row(origin="上海南"), rail_row(number="G123")])
    async with httpx.AsyncClient() as client:
        adapter = Rail12306Adapter(runtime, client)
        adapter._stations = {"上海虹桥": "AOH", "杭州东": "HGH"}
        result = await adapter.search(
            request(SearchTrainsInput, seat_class="二等座", train_numbers=["D931"])
        )
    assert len(result.offers) == 1 and result.coverage.scanned_count == 3
    offer = result.offers[0]
    assert offer.inventory.status == "waitlist" and offer.inventory.remaining is None
    assert offer.price.money is None and offer.price.display.amount == 53
    assert offer.seats[0].price.display.amount == 673 and offer.seats[0].inventory.remaining == 2
    assert (
        offer.seats[2].inventory.status == "available"
        and offer.seats[2].inventory.remaining is None
    )
    assert offer.arrival_at > offer.departure_at and offer.direct
    envelope = {"status": "ok", "data": result.model_dump(mode="json")}
    assert decode_tool_result(encode_tool_result(envelope)) == envelope


async def test_rail_city_scope_keeps_actual_stations_and_no_ticket_is_not_all_market():
    runtime = StubRuntime(rows=[rail_row(origin="上海南")])
    async with httpx.AsyncClient() as client:
        adapter = Rail12306Adapter(runtime, client)
        adapter._stations = {"上海虹桥": "AOH", "杭州东": "HGH"}
        result = await adapter.search(request(SearchTrainsInput, station_scope="city"))
    assert result.offers[0].origin.query == "上海南" and not result.complete


def test_missing_seat_prices_remain_missing_without_zero_fare():
    seat = parse_seat({"text": "有", "label": None}, "二等座")
    assert seat.price.display.amount is None and seat.inventory.status == "available"


def test_missing_core_fields_are_parser_error_not_empty_ticket_market():
    with pytest.raises(ToolFailure) as error:
        validate_rendered_rows([{"origin": "兰州", "destination": ""}], ("origin", "destination"))
    assert error.value.code == "provider_invalid_results"
    assert validate_rendered_rows([], ("origin", "destination")) == 0
    assert (
        validate_rendered_rows(
            [
                {"origin": "兰州", "destination": "平凉"},
                {"origin": "兰州", "destination": ""},
            ],
            ("origin", "destination"),
        )
        == 1
    )


async def test_ceair_prices_keep_tax_view_terminals_codeshare_and_unknown_inventory():
    rows = [
        {
            "flight": "MU1234",
            "origin": "虹桥 T2",
            "destination": "首都 T3",
            "departure_time": "23:00",
            "arrival_time": "01:00 +1 天",
            "departure_terminal": "T2",
            "arrival_terminal": "T3",
            "duration": "2小时",
            "routing": "直达",
            "shared": "共享",
            "prices": ["¥ 1,000", "— —"],
        }
    ]
    adapter = CeairAdapter(StubRuntime(rows=rows, tax_basis="included"))
    result = await adapter.search(
        request(
            SearchFlightsInput,
            origin={"query": "上海"},
            destination={"query": "北京"},
            provider="ceair",
        )
    )
    offer = result.offers[0]
    assert offer.price.money.amount == 1000 and offer.price.tax_basis == "included"
    assert offer.inventory.status == "unknown" and offer.operating_carrier is None
    assert offer.codeshare is True and offer.marketing_carrier == "MU"
    assert offer.segments[0].departure_terminal == "T2"
    assert offer.arrival_at.date() > offer.departure_at.date()
    narrowed = await adapter.search(
        request(
            SearchFlightsInput,
            origin={"query": "上海", "provider_location_id": "PVG"},
            destination={"query": "北京"},
            provider="ceair",
        )
    )
    assert narrowed.offers == [] and narrowed.coverage.query_status == "filtered_empty"


@pytest.mark.parametrize("price", ["¥ 6xx", "¥ ***", "— —", ""])
async def test_ceair_keeps_flight_metadata_when_price_is_masked_or_missing(price):
    rows = [
        {
            "flight": "MU1234",
            "origin": "虹桥 T2",
            "destination": "首都 T3",
            "departure_time": "08:00",
            "arrival_time": "10:00",
            "departure_terminal": "T2",
            "arrival_terminal": "T3",
            "duration": "2小时",
            "routing": "直达",
            "shared": "",
            "prices": [price],
        }
    ]
    result = await CeairAdapter(StubRuntime(rows=rows, tax_basis="unknown")).search(
        request(
            SearchFlightsInput,
            origin={"query": "上海"},
            destination={"query": "北京"},
            provider="ceair",
        )
    )
    assert len(result.offers) == 1 and result.offers[0].price.money is None
    assert result.offers[0].service_number == "MU1234"
    assert result.offers[0].inventory.status == "unknown"
    assert result.offers[0].price.display.text == price


def test_airport_resolver_accepts_verified_codes_and_rejects_unknown_names():
    assert airport_codes(SearchLocation(query="上海")) == "SHA,PVG"
    assert airport_codes(SearchLocation(query="airport", provider_location_id="CAN")) == "CAN"
    with pytest.raises(ToolFailure):
        airport_codes(SearchLocation(query="不存在的机场"))


class SourceAdapter:
    def __init__(self, error=None):
        self.calls, self.error = 0, error

    async def search(self, value):
        self.calls += 1
        if self.error:
            raise ToolFailure(self.error, "safe source failure")
        return SearchTrainsOutput(
            queried_at="2026-10-10T01:00:00+08:00",
            offers=[],
            complete=False,
            coverage=QueryCoverage(
                provider="flyai",
                query_status="no_results",
                scanned_count=0,
                matched_count=0,
                returned_count=0,
                scope="limited test candidate source",
                data_time="2026-10-10T01:00:00+08:00",
                elapsed_seconds=1,
            ),
        )


@pytest.mark.parametrize(
    "code", ["outside_sale_window", "past_departure_date", "unsupported_parameters"]
)
async def test_rail_date_or_user_errors_never_silently_fall_back(code):
    primary, fallback = SourceAdapter(code), SourceAdapter()
    with pytest.raises(ToolFailure, match="safe source failure"):
        await TrainRouter({"12306": primary, "flyai": fallback}, "12306", "flyai").search(
            request(SearchTrainsInput)
        )
    assert fallback.calls == 0


async def test_explicit_rail_source_never_falls_back_when_blocked():
    primary, fallback = SourceAdapter("provider_access_blocked"), SourceAdapter()
    with pytest.raises(ToolFailure):
        await TrainRouter({"12306": primary, "flyai": fallback}, "12306", "flyai").search(
            request(SearchTrainsInput, provider="12306")
        )
    assert fallback.calls == 0


async def test_configured_rail_fallback_discloses_failure_and_preserves_coverage():
    primary, fallback = SourceAdapter("provider_access_blocked"), SourceAdapter()
    result = await TrainRouter({"12306": primary, "flyai": fallback}, "12306", "flyai").search(
        request(SearchTrainsInput)
    )
    assert primary.calls == fallback.calls == 1
    assert result.coverage.fallback_from == "12306"
    assert result.coverage.fallback_reason == "provider_access_blocked"
    assert result.coverage.query_status == "no_results" and not result.complete
    assert "not been officially verified" in result.warnings[0]


async def test_empty_official_results_do_not_trigger_other_supplier():
    primary, fallback = SourceAdapter(), SourceAdapter()
    await TrainRouter({"12306": primary, "flyai": fallback}, "12306", "flyai").search(
        request(SearchTrainsInput)
    )
    assert fallback.calls == 0


async def test_flight_source_unavailable_is_an_error_not_an_empty_market():
    with pytest.raises(ToolFailure, match="not configured"):
        await FlightRouter({}).search(request(SearchFlightsInput, provider="ceair"))

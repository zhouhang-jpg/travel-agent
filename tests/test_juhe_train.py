"""Offline contract tests based on Juhe product 817 examples, never live tickets."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from travel_tools.common import ToolFailure
from travel_tools.providers import juhe_train
from travel_tools.providers.juhe_train import JUHE_TRAIN_URL, JuheTrainAdapter
from travel_tools.schemas.quotes import SearchTrainsInput

KEY = "offline-juhe-key-not-a-credential"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    # UTC is still October 8; local railway date is already October 9.
    monkeypatch.setattr(juhe_train, "utc_now", lambda: datetime(2026, 10, 8, 17, tzinfo=UTC))


def query(**overrides):
    args = {
        "origin": {"query": "北京"},
        "destination": {"query": "苏州"},
        "departure_date": "2026-10-09",
        "travelers": {"adults": 1},
    }
    return SearchTrainsInput(**(args | overrides))


def payload():
    # Adapted from https://www.juhe.cn/docs/api/id/817 response example.
    return {
        "error_code": 0,
        "reason": "success.",
        "result": [
            {
                "train_no": "G25",
                "departure_station": "北京南",
                "arrival_station": "苏州北",
                "departure_station_code": "VNP",
                "arrival_station_code": "OHH",
                "departure_time": "18:04",
                "arrival_time": "22:32",
                "duration": "04:28",
                "enable_booking": "Y",
                "prices": [
                    {"seat_name": "商务座", "seat_type_code": "9", "price": 2194, "num": "无"},
                    {"seat_name": "二等座", "seat_type_code": "O", "price": 627, "num": "1"},
                ],
            }
        ],
    }


async def run(body, request=None):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        return await JuheTrainAdapter(KEY, client).search(request or query())


async def test_documented_endpoint_does_not_invent_city_station_and_fare_scope():
    def handler(request):
        assert request.url.copy_with(query=None) == httpx.URL(JUHE_TRAIN_URL)
        assert dict(request.url.params) == {
            "key": KEY,
            "search_type": "1",
            "departure_station": "北京",
            "arrival_station": "苏州",
            "date": "2026-10-09",
            "enable_booking": "2",
        }
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JuheTrainAdapter(KEY, client).search(query())
    offer = result.offers[1]
    assert offer.origin.query == "北京南"
    assert offer.origin.provider_location_id == "juhe_train:VNP"
    assert offer.price.money.amount == Decimal("627")
    assert offer.price.money.currency == "CNY"
    assert offer.price.kind == "query_quote"
    assert offer.price.unit == "per_person" and offer.price.priced_persons == 1
    assert offer.price.tax_basis == offer.price.fee_basis == "unknown"
    assert offer.price.taxes is None and offer.price.fees is None
    assert offer.price.valid_until is None
    assert offer.inventory.remaining == 1
    assert result.offers[0].inventory.status == "unavailable"
    assert offer.arrival_at.isoformat() == "2026-10-09T22:32:00+08:00"
    assert KEY not in result.model_dump_json()


async def test_scoped_station_codes_and_local_seat_filter():
    def handler(request):
        assert request.url.params["search_type"] == "2"
        assert request.url.params["departure_station"] == "VNP"
        assert request.url.params["arrival_station"] == "OHH"
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JuheTrainAdapter(KEY, client).search(
            query(
                origin={"query": "北京南", "provider_location_id": "juhe_train:VNP"},
                destination={"query": "苏州北", "provider_location_id": "juhe_train:OHH"},
                seat_class="二等座",
                train_types=["high_speed"],
            )
        )
    assert len(result.offers) == 1 and result.offers[0].seat_or_cabin == "二等座"


async def test_cross_day_arrival_uses_duration_not_clock_order():
    body = payload()
    body["result"][0].update(departure_time="23:30", arrival_time="02:15", duration="26:45")
    result = await run(body)
    assert result.offers[0].arrival_at.isoformat() == "2026-10-11T02:15:00+08:00"


async def test_missing_duration_does_not_guess_arrival_day():
    body = payload()
    body["result"][0]["duration"] = None
    result = await run(body)
    assert result.offers[0].arrival_at is None
    assert not result.complete


async def test_missing_fare_details_not_complete_after_seat_filter():
    body = payload()
    body["result"][0]["prices"] = None
    result = await run(body, query(seat_class="二等座"))
    assert result.offers == [] and not result.complete


async def test_returned_station_codes_must_match_code_query():
    with pytest.raises(ToolFailure) as exc:
        await run(
            payload(),
            query(
                origin={"query": "北京南", "provider_location_id": "juhe_train:VNP"},
                destination={"query": "上海虹桥", "provider_location_id": "juhe_train:AOH"},
            ),
        )
    assert exc.value.code == "provider_invalid_response"


@pytest.mark.parametrize(
    "value, expected, remaining",
    [
        ("有", "available", None),
        ("无", "unavailable", 0),
        ("0", "unavailable", 0),
        ("9", "available", 9),
        (None, "unknown", None),
        ("--", "unknown", None),
        ("候补", "unknown", None),
        (True, "unknown", None),
    ],
)
async def test_inventory_does_not_follow_fare_presence(value, expected, remaining):
    body = payload()
    body["result"][0]["prices"][0]["num"] = value
    result = await run(body)
    assert result.offers[0].inventory.status == expected
    assert result.offers[0].inventory.remaining == remaining


@pytest.mark.parametrize("price", [None, "", "--", 0, "0.00"])
async def test_missing_or_zero_price_does_not_imply_free_ticket(price):
    body = payload()
    body["result"][0]["prices"][0]["price"] = price
    result = await run(body)
    assert result.offers[0].price.money is None
    assert result.offers[0].price.kind == "unknown"
    assert not result.complete


@pytest.mark.parametrize(
    "changes",
    [
        {"departure_date": "2026-10-08"},
        {"departure_date": "2026-10-24"},
        {"travelers": {"adults": 2}},
        {"travelers": {"adults": 1, "children_ages": [6]}},
        {"preferred_currency": "USD"},
        {"origin": {"query": "北京南", "provider_location_id": "amap:VNP"}},
        {"origin": {"query": "北京南", "provider_location_id": "juhe_train:VNP"}},
    ],
)
async def test_unsupported_request_rejected_before_network(changes):
    def handler(_):
        pytest.fail("Unsupported request must not reach supplier")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolFailure):
            await JuheTrainAdapter(KEY, client).search(query(**changes))


async def test_inclusive_local_15_day_window():
    assert (await run(payload(), query(departure_date="2026-10-23"))).offers


@pytest.mark.parametrize(
    "code, expected",
    [
        (10001, "provider_authentication"),
        (10002, "provider_authentication"),
        (10011, "provider_rate_limited"),
        (10020, "provider_rejected"),
        (281701, "provider_rejected"),
        (None, "provider_invalid_response"),
        (False, "provider_invalid_response"),
    ],
)
async def test_business_error_is_not_empty_success_or_secret_echo(code, expected):
    with pytest.raises(ToolFailure) as exc:
        await run({"error_code": code, "reason": KEY, "result": []})
    assert exc.value.code == expected
    assert KEY not in str(exc.value)


@pytest.mark.parametrize("mutation", ["arrival", "price", "rows", "station", "clock"])
async def test_invalid_evidence_rejected(mutation):
    body = deepcopy(payload())
    row = body["result"][0]
    if mutation == "arrival":
        row["arrival_time"] = "22:33"
    elif mutation == "price":
        row["prices"][0]["price"] = "NaN"
    elif mutation == "rows":
        body["result"] = None
    elif mutation == "station":
        row["departure_station"] = None
    else:
        row["departure_time"] = "25:01"
    with pytest.raises(ToolFailure) as exc:
        await run(body)
    assert exc.value.code == "provider_invalid_response"


async def test_explicit_empty_and_truncation():
    empty = await run({"error_code": 0, "result": []})
    assert empty.offers == []
    limited = await run(payload(), query(max_results=1))
    assert len(limited.offers) == 1 and not limited.complete


async def test_blank_key_and_http_error_are_safe():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text=KEY))
    ) as client:
        with pytest.raises(ToolFailure) as blank:
            JuheTrainAdapter(" ", client)
        assert blank.value.code == "provider_unconfigured"
        with pytest.raises(ToolFailure) as error:
            await JuheTrainAdapter(KEY, client).search(query())
        assert error.value.retryable and KEY not in str(error.value)

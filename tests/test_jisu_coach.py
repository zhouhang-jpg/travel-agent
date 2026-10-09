"""Offline reference-fare tests based on Jisu's public city2c contract."""

from decimal import Decimal

import httpx
import pytest

from travel_tools.common import ToolFailure
from travel_tools.providers.jisu_coach import JISU_COACH_URL, JisuCoachAdapter
from travel_tools.schemas.quotes import SearchCoachesInput

KEY = "offline-jisu-key-not-a-credential"


def query(**overrides):
    args = {
        "origin": {"query": "杭州"},
        "destination": {"query": "上海"},
        "departure_date": "2026-10-20",
        "travelers": {"adults": 1},
    }
    return SearchCoachesInput(**(args | overrides))


def payload():
    # Public https://www.jisuapi.com/api/bus/ sample, not current ticket data.
    return {
        "status": 0,
        "msg": "ok",
        "result": [
            {
                "startcity": "杭州",
                "endcity": "上海",
                "startstation": "客运中心站",
                "endstation": "上海南站",
                "starttime": "07:10",
                "price": "68",
                "bustype": "大高3",
                "distance": "181",
            }
        ],
    }


async def run(body, request=None):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        return await JisuCoachAdapter(KEY, client).search(request or query())


async def test_undated_reference_is_never_promoted_to_dated_inventory():
    def handler(request):
        assert request.url.copy_with(query=None) == httpx.URL(JISU_COACH_URL)
        assert dict(request.url.params) == {"appkey": KEY, "start": "杭州", "end": "上海"}
        data = payload()
        data["result"][0]["costTime"] = "约230公里"
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JisuCoachAdapter(KEY, client).search(query())
    offer = result.offers[0]
    assert not result.complete
    assert offer.departure_at is None and offer.arrival_at is None
    assert offer.inventory.status == "unknown" and offer.inventory.remaining is None
    assert offer.origin.query == "客运中心站"
    assert offer.price.money.amount == Decimal("68")
    assert offer.price.kind == "reference" and offer.price.money.currency == "CNY"
    assert offer.price.unit == "unknown" and offer.price.priced_persons is None
    assert offer.price.tax_basis == offer.price.fee_basis == "unknown"
    assert "07:10" in offer.price.conditions
    assert "departure_date" in " ".join(result.warnings)
    assert "2026-10-20" not in result.model_dump_json()
    assert KEY not in result.model_dump_json()


@pytest.mark.parametrize("price", [None, "", "--", 0, "0.0"])
async def test_missing_and_zero_reference_price_stays_unknown(price):
    body = payload()
    body["result"][0]["price"] = price
    result = await run(body)
    assert result.offers[0].price.kind == "unknown"
    assert result.offers[0].price.money is None


@pytest.mark.parametrize("clock", [None, "24:90", "每30分钟", "06:00-18:00"])
async def test_unspecified_or_rolling_clock_not_coerced_to_timestamp(clock):
    body = payload()
    body["result"][0]["starttime"] = clock
    result = await run(body)
    assert result.offers[0].departure_at is None
    assert any("clock" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "changes",
    [
        {"travelers": {"adults": 2}},
        {"travelers": {"adults": 1, "children_ages": [5]}},
        {"preferred_currency": "USD"},
        {"origin": {"query": "杭州", "provider_location_id": "amap:123"}},
    ],
)
async def test_unsupported_scope_does_not_call_supplier(changes):
    def handler(_):
        pytest.fail("Unsupported request must not reach supplier")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolFailure):
            await JisuCoachAdapter(KEY, client).search(query(**changes))


@pytest.mark.parametrize("code", [101, 103, 104, 107, 203, 999])
async def test_upstream_errors_are_failures_not_empty_results(code):
    with pytest.raises(ToolFailure) as exc:
        await run({"status": code, "msg": KEY, "result": []})
    assert KEY not in str(exc.value)
    assert exc.value.retryable is (code == 107)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": 0, "result": None},
        {"status": False, "result": []},
        {"status": 0, "result": [None]},
        {"status": 0, "result": [{}]},
    ],
)
async def test_invalid_response_rejected(body):
    with pytest.raises(ToolFailure) as exc:
        await run(body)
    assert exc.value.code == "provider_invalid_response"


@pytest.mark.parametrize("price", ["NaN", -1, True, []])
async def test_malformed_fares_rejected(price):
    body = payload()
    body["result"][0]["price"] = price
    with pytest.raises(ToolFailure):
        await run(body)


async def test_station_filter_empty_and_limit_stay_partial():
    result = await run(payload(), query(departure_station="另一客运站"))
    assert result.offers == [] and not result.complete
    body = payload()
    body["result"] *= 3
    result = await run(body, query(max_results=1))
    assert len(result.offers) == 1 and not result.complete
    assert any("truncated" in warning for warning in result.warnings)


async def test_key_and_http_failure_are_safe():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text=KEY))
    ) as client:
        with pytest.raises(ToolFailure) as blank:
            JisuCoachAdapter("", client)
        assert blank.value.code == "provider_unconfigured"
        with pytest.raises(ToolFailure) as exc:
            await JisuCoachAdapter(KEY, client).search(query())
        assert exc.value.code == "provider_authentication" and KEY not in str(exc.value)

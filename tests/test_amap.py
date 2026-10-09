"""Offline Amap contract fixtures. These do not prove external API/account access."""

from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from travel_tools.common import ToolFailure
from travel_tools.providers import amap
from travel_tools.providers.amap import AmapAdapter
from travel_tools.schemas.places import (
    GetPlaceDetailsInput,
    GetRoutesInput,
    SearchPlacesInput,
)

POINT = {"longitude": 116.4, "latitude": 39.9, "crs": "gcj02"}
SECRET = "test-placeholder-not-real-key"


def success(**fields):
    return {"status": "1", "info": "OK", "infocode": "10000", **fields}


def route_request(mode="walking", **kwargs):
    return GetRoutesInput(origin=POINT, destination=POINT, mode=mode, **kwargs)


@pytest.mark.asyncio
async def test_text_search_maps_business_evidence_without_hotel_quote():
    def respond(request):
        assert request.url.path == "/v5/place/text"
        assert request.url.params["keywords"] == "北京大学"
        assert request.url.params["region"] == "北京市"
        assert request.url.params["city_limit"] == "true"
        assert request.url.params["show_fields"] == "business,navi"
        return httpx.Response(
            200,
            json=success(
                count="1",
                pois=[
                    {
                        "id": "B000A7BM4H",
                        "name": "北京大学",
                        "location": "116.31,39.99",
                        "address": "海淀区",
                        "citycode": "010",
                        "adcode": "110108",
                        "business": {
                            "opentime_week": "周一至周五 09:00-17:00",
                            "cost": "20.50",
                            "rating": "4.5",
                            "tel": "010-00000000",
                        },
                        "navi": {"entr_location": "116.32,39.98"},
                    }
                ],
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await AmapAdapter(SECRET, client).search_places(
            SearchPlacesInput(keywords="北京大学", region="北京市", city_limit=True)
        )
    assert result.returned_count == 1
    assert result.total_count is None
    assert result.more_results == "unknown"
    poi = result.places[0]
    assert poi.location.crs == "gcj02"
    assert poi.entrance.longitude == 116.32
    assert poi.opening_hours_week == "周一至周五 09:00-17:00"
    assert poi.average_spend.amount == Decimal("20.50")
    assert poi.average_spend.price_status == "reference"
    assert poi.average_spend.inventory_status == "unknown"
    assert poi.average_spend.tax_inclusion == "unknown"
    assert poi.average_spend.room_count is None
    assert result.sources[0].retrieved_at.tzinfo is not None
    assert result.sources[0].data_time is None
    assert SECRET not in result.model_dump_json()


@pytest.mark.asyncio
async def test_around_query_preserves_zero_radius_and_null_fields():
    def respond(request):
        assert request.url.path == "/v5/place/around"
        assert request.url.params["location"] == "116.400000,39.900000"
        assert request.url.params["radius"] == "0"
        assert request.url.params["types"] == "050000|070000"
        return httpx.Response(
            200,
            json=success(
                pois=[
                    {
                        "id": "B123",
                        "name": "测试地点",
                        "location": [],
                        "address": [],
                        "business": {"cost": [], "rating": "", "tel": None},
                        "navi": [],
                    }
                ]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await AmapAdapter(SECRET, client).search_places(
            SearchPlacesInput(
                center=POINT,
                radius_m=0,
                type_codes=["050000", "070000"],
            )
        )
    poi = result.places[0]
    assert poi.location is poi.address is poi.phone is poi.average_spend is poi.rating is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pois,found", [([], False), ([{"id": "B123", "name": "公园"}], True)])
async def test_detail_can_distinguish_not_found(pois, found):
    def respond(request):
        assert request.url.path == "/v5/place/detail"
        assert request.url.params["id"] == "B123"
        return httpx.Response(200, json=success(pois=pois))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await AmapAdapter(SECRET, client).get_place_details(
            GetPlaceDetailsInput(place_id="B123")
        )
    assert result.found is found
    assert (result.place is not None) is found


@pytest.mark.asyncio
async def test_wrong_detail_id_fails_instead_of_substituting_a_place():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=success(pois=[{"id": "WRONG", "name": "别处"}]))
        )
    ) as client:
        with pytest.raises(ToolFailure, match="invalid response"):
            await AmapAdapter(SECRET, client).get_place_details(
                GetPlaceDetailsInput(place_id="B123")
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["walking", "driving", "bicycling"])
async def test_routes_use_v5_cost_duration_and_explicit_estimates(mode):
    def respond(request):
        assert request.url.path == f"/v5/direction/{mode}"
        assert request.url.params["show_fields"] == "cost"
        return httpx.Response(
            200,
            json=success(
                route={
                    "taxi_cost": "15",
                    "paths": [
                        {
                            "distance": "1000",
                            "cost": {"duration": "120", "tolls": "0"},
                            "restriction": "0",
                            "steps": [
                                {
                                    "instruction": "向前行驶",
                                    "road_name": "中山路",
                                    "step_distance": "1000",
                                    "cost": {"duration": "120"},
                                }
                            ],
                        }
                    ],
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await AmapAdapter(SECRET, client).get_routes(route_request(mode))
    assert result.routes[0].duration_s == 120
    assert result.routes[0].steps[0].road_or_line == "中山路"
    assert result.routes[0].reference_costs[0].amount == 0
    assert result.routes[0].reference_costs[0].price_status == "reference"
    assert result.evidence_kind == "map_route_estimate"
    assert bool(result.reference_costs) is (mode == "driving")


@pytest.mark.asyncio
async def test_transit_date_converts_to_china_time_and_keeps_first_parallel_bus():
    def respond(request):
        assert request.url.path == "/v5/direction/transit/integrated"
        assert request.url.params["city1"] == "010"
        assert request.url.params["city2"] == "010"
        assert request.url.params["date"] == "2026-10-10"
        assert request.url.params["time"] == "08-30"
        return httpx.Response(
            200,
            json=success(
                route={
                    "cost": {"taxi_fee": "30"},
                    "transits": [
                        {
                            "distance": "5000",
                            "cost": {"duration": "900"},
                            "segments": [
                                {
                                    "walking": {
                                        "steps": [{"instruction": "步行到车站", "distance": "100"}]
                                    },
                                    "bus": {
                                        "buslines": [
                                            {
                                                "name": "1路",
                                                "distance": "4900",
                                                "duration": "600",
                                                "departure_stop": {"name": "甲站"},
                                                "arrival_stop": {"name": "乙站"},
                                            },
                                            {"name": "2路"},
                                        ]
                                    },
                                    "railway": [],
                                }
                            ],
                        }
                    ],
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await AmapAdapter(SECRET, client).get_routes(
            route_request(
                "transit",
                origin_city_code="010",
                destination_city_code="010",
                departure_time="2026-10-09T19:30:00-05:00",
            )
        )
    assert result.routes[0].duration_s == 900
    assert len(result.routes[0].steps) == 2
    assert result.routes[0].steps[1].road_or_line == "1路"
    assert result.routes[0].steps[1].departure_stop == "甲站"
    assert result.routes[0].steps[0].duration_s is None
    assert result.reference_costs[0].kind == "taxi_estimate"


@pytest.mark.asyncio
async def test_route_missing_duration_is_unknown_not_zero():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=success(route={"paths": [{"distance": [], "cost": [], "steps": []}]})
            )
        )
    ) as client:
        result = await AmapAdapter(SECRET, client).get_routes(route_request())
    assert result.routes[0].duration_s is result.routes[0].distance_m is None
    assert result.routes[0].reference_costs == []


@pytest.mark.parametrize(
    "path_cost,flat_duration,expected",
    [
        (None, "1024", 1024),
        ({"duration": "120"}, "1024", 120),
        ({"duration": "0"}, "1024", 0),
        ({"duration": []}, "1024", 1024),
        (None, None, None),
    ],
)
async def test_bicycling_total_duration_accepts_live_v5_shape(path_cost, flat_duration, expected):
    # Shape was observed live; values and geometry here are an offline fixture.
    path = {
        "distance": "3000",
        "duration": flat_duration,
        "steps": [{"step_distance": "100", "cost": {"duration": "33"}}],
    }
    if path_cost is not None:
        path["cost"] = path_cost
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=success(route={"paths": [path]}))
        )
    ) as client:
        result = await AmapAdapter(SECRET, client).get_routes(route_request("bicycling"))
    assert result.routes[0].duration_s == expected
    assert result.routes[0].steps[0].duration_s == 33


@pytest.mark.parametrize("duration", ["-1", "invalid", "NaN"])
async def test_bicycling_invalid_flat_duration_is_rejected(duration):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=success(route={"paths": [{"duration": duration, "steps": []}]})
            )
        )
    ) as client:
        with pytest.raises(ToolFailure) as error:
            await AmapAdapter(SECRET, client).get_routes(route_request("bicycling"))
    assert error.value.code == "provider_invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "infocode,expected",
    [
        ("10001", "provider_auth"),
        ("10002", "provider_auth"),
        ("10003", "provider_rate_limited"),
        ("10004", "provider_rate_limited"),
        ("10016", "provider_error"),
        ("20000", "provider_error"),
    ],
)
async def test_provider_failure_is_safe_and_not_success_empty(infocode, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"status": "0", "infocode": infocode, "info": SECRET}
            )
        )
    ) as client:
        with pytest.raises(ToolFailure) as error:
            await AmapAdapter(SECRET, client).search_places(SearchPlacesInput(keywords="公园"))
    assert error.value.code == expected
    assert SECRET not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        success(),
        success(pois=None),
        success(pois={}),
        success(pois=["bad"]),
        success(pois=[{"id": "B123"}]),
        success(pois=[{"id": "B123", "name": "公园", "location": "999,1"}]),
        success(pois=[{"id": "B123", "name": "公园", "business": {"rating": "nan"}}]),
        success(pois=[{"id": "B123", "name": "公园", "business": {"cost": "Infinity"}}]),
    ],
)
async def test_malformed_payloads_fail(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ToolFailure) as error:
            await AmapAdapter(SECRET, client).search_places(SearchPlacesInput(keywords="公园"))
    assert error.value.code == "provider_invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (302, "provider_http_error"),
        (401, "provider_auth"),
        (429, "provider_rate_limited"),
        (503, "provider_http_error"),
    ],
)
async def test_http_failure_and_redirect_are_not_followed(status, expected):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(ToolFailure) as error:
            await AmapAdapter(SECRET, client).search_places(SearchPlacesInput(keywords="公园"))
    assert error.value.code == expected
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_response_size_limit(monkeypatch):
    monkeypatch.setattr(amap, "MAX_RESPONSE_BYTES", 16)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 17))
    ) as client:
        with pytest.raises(ToolFailure) as error:
            await AmapAdapter(SECRET, client).search_places(SearchPlacesInput(keywords="公园"))
    assert error.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_timeout_does_not_leak_request_key():
    def respond(request):
        raise httpx.ReadTimeout(str(request.url), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ToolFailure) as error:
            await AmapAdapter(SECRET, client).search_places(SearchPlacesInput(keywords="公园"))
    assert error.value.code == "provider_timeout"
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"keywords": " "},
        {"keywords": "x", "unexpected": True},
        {"keywords": "x", "page_size": 26},
        {"keywords": "x", "page": 9, "page_size": 25},
        {"keywords": "x", "radius_m": 500},
        {"keywords": "x", "city_limit": True},
        {"center": {**POINT, "crs": "wgs84"}},
        {"type_codes": ["hotel"]},
    ],
)
def test_search_input_rejects_unimplementable_or_ambiguous_requests(fields):
    with pytest.raises(ValidationError):
        SearchPlacesInput(**fields)


@pytest.mark.parametrize(
    "mode,fields",
    [
        ("transit", {}),
        ("transit", {"origin_city_code": "北京市", "destination_city_code": "010"}),
        ("driving", {"departure_time": "2026-10-09T12:00:00+08:00"}),
        (
            "transit",
            {
                "origin_city_code": "010",
                "destination_city_code": "010",
                "departure_time": "2026-10-09T12:00:00",
            },
        ),
    ],
)
def test_route_input_requires_correct_mode_and_timezone(mode, fields):
    with pytest.raises(ValidationError):
        route_request(mode, **fields)

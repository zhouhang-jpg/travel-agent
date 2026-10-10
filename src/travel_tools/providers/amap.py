"""Read-only Amap V5 adapter; see docs/providers/amap.md for verified API references."""

import asyncio
import json
import math
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import ValidationError

from travel_tools.common import Source, ToolFailure
from travel_tools.scheduling import supplier_slot
from travel_tools.schemas.places import (
    AmapCoordinates,
    GetPlaceDetailsInput,
    GetPlaceDetailsOutput,
    GetRoutesInput,
    GetRoutesOutput,
    Place,
    ReferenceCost,
    RouteOption,
    RouteStep,
    SearchPlacesInput,
    SearchPlacesOutput,
)

BASE_URL = "https://restapi.amap.com"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_S = 15.0
CHINA_TIME = timezone(timedelta(hours=8))


def _invalid() -> ToolFailure:
    return ToolFailure("provider_invalid_response", "Amap returned an invalid response")


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == []


def _text(value: Any) -> str | None:
    if _missing(value):
        return None
    if not isinstance(value, str):
        raise _invalid()
    return value.strip() or None


def _number(value: Any) -> float | None:
    if _missing(value):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _invalid()
    try:
        result = float(value)
    except ValueError:
        raise _invalid() from None
    if not math.isfinite(result) or result < 0:
        raise _invalid()
    return result


def _object(value: Any, *, optional: bool = False) -> dict[str, Any]:
    if optional and _missing(value):
        return {}
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _objects(value: Any, *, optional: bool = False) -> list[dict[str, Any]]:
    if optional and value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _invalid()
    return value


def _coords(value: Any) -> AmapCoordinates | None:
    value = _text(value)
    if value is None:
        return None
    try:
        longitude, latitude = value.split(",")
        return AmapCoordinates(longitude=longitude, latitude=latitude, crs="gcj02")
    except (ValueError, ValidationError):
        raise _invalid() from None


def _coord_param(value: AmapCoordinates) -> str:
    return f"{value.longitude:.6f},{value.latitude:.6f}"


def _cost(value: Any, kind: str, basis: str) -> ReferenceCost | None:
    if _missing(value):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _invalid()
    try:
        amount = Decimal(str(value))
        return ReferenceCost(kind=kind, amount=amount, pricing_basis=basis)
    except (InvalidOperation, ValidationError):
        raise _invalid() from None


def _place(value: dict[str, Any]) -> Place:
    business = _object(value.get("business"), optional=True)
    navi = _object(value.get("navi"), optional=True)
    try:
        return Place(
            place_id=_text(value.get("id")),
            name=_text(value.get("name")),
            location=_coords(value.get("location")),
            entrance=_coords(navi.get("entr_location")),
            address=_text(value.get("address")),
            category=_text(value.get("type")),
            type_code=_text(value.get("typecode")),
            province=_text(value.get("pname")),
            city=_text(value.get("cityname")),
            district=_text(value.get("adname")),
            city_code=_text(value.get("citycode")),
            adcode=_text(value.get("adcode")),
            phone=_text(business.get("tel")),
            opening_hours_today=_text(business.get("opentime_today")),
            opening_hours_week=_text(business.get("opentime_week")),
            rating=_number(business.get("rating")),
            average_spend=_cost(business.get("cost"), "average_spend", "per_person"),
            distance_from_center_m=_number(value.get("distance")),
        )
    except ValidationError:
        raise _invalid() from None


def _step(value: dict[str, Any], mode: str, *, legacy: bool = False) -> RouteStep:
    cost = _object(value.get("cost"), optional=True)
    return RouteStep(
        mode=mode,
        instruction=_text(value.get("instruction")),
        road_or_line=_text(value.get("road" if legacy else "road_name")),
        distance_m=_number(value.get("distance" if legacy else "step_distance")),
        duration_s=_number(value.get("duration") if legacy else cost.get("duration")),
    )


def _transit_steps(segments: Any) -> list[RouteStep]:
    steps = []
    for segment in _objects(segments):
        walking = _object(segment.get("walking"), optional=True)
        steps.extend(
            _step(step, "walking", legacy=True)
            for step in _objects(walking.get("steps"), optional=True)
        )
        bus = _object(segment.get("bus"), optional=True)
        # buslines in one segment are alternatives, not sequential legs. Keep the
        # first documented route option rather than adding all alternatives as legs.
        lines = _objects(bus.get("buslines"), optional=True)
        vehicles = [("bus", lines[0])] if lines else []
        railway = _object(segment.get("railway"), optional=True)
        taxi = _object(segment.get("taxi"), optional=True)
        if railway:
            vehicles.append(("railway", railway))
        if taxi:
            vehicles.append(("taxi", taxi))
        for mode, vehicle in vehicles:
            start = _object(vehicle.get("departure_stop"), optional=True)
            end = _object(vehicle.get("arrival_stop"), optional=True)
            duration_key = {"bus": "duration", "railway": "time", "taxi": "drivetime"}[mode]
            steps.append(
                RouteStep(
                    mode=mode,
                    road_or_line=_text(vehicle.get("name")),
                    departure_stop=_text(start.get("name")) or _text(vehicle.get("startname")),
                    arrival_stop=_text(end.get("name")) or _text(vehicle.get("endname")),
                    distance_m=_number(vehicle.get("distance")),
                    duration_s=_number(vehicle.get(duration_key)),
                )
            )
    return steps


class AmapAdapter:
    def __init__(self, api_key: str, client: httpx.AsyncClient):
        self._api_key = api_key
        self._client = client

    async def _get(self, path: str, params: dict[str, Any]) -> tuple[dict[str, Any], Source]:
        if not self._api_key.strip():
            raise ToolFailure("not_configured", "Amap API key is not configured")
        request_params = {**params, "key": self._api_key, "output": "json"}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S), supplier_slot("amap"):
                async with self._client.stream(
                    "GET",
                    BASE_URL + path,
                    params=request_params,
                    timeout=REQUEST_TIMEOUT_S,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (401, 403):
                        raise ToolFailure("provider_auth", "Amap rejected API access")
                    if response.status_code == 429:
                        raise ToolFailure("provider_rate_limited", "Amap rate limit reached", True)
                    if response.status_code != 200:
                        raise ToolFailure(
                            "provider_http_error",
                            "Amap HTTP request failed",
                            response.status_code >= 500,
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ToolFailure("response_too_large", "Amap response exceeded limit")
                        body.extend(chunk)
        except (TimeoutError, httpx.TimeoutException):
            raise ToolFailure("provider_timeout", "Amap request timed out", True) from None
        except httpx.HTTPError:
            raise ToolFailure("provider_network_error", "Amap connection failed", True) from None
        try:
            data = _object(json.loads(body))
        except (ValueError, UnicodeDecodeError):
            raise _invalid() from None
        if data.get("status") == "0":
            code = data.get("infocode")
            if code in {"10003", "10004", "10010", "10014", "10019"}:
                raise ToolFailure("provider_rate_limited", "Amap quota or rate limit reached", True)
            if code in {
                "10001",
                "10002",
                "10005",
                "10006",
                "10007",
                "10008",
                "10009",
                "10012",
                "10013",
            }:
                raise ToolFailure("provider_auth", "Amap key, permissions or access rules rejected")
            raise ToolFailure(
                "provider_error", "Amap rejected the request", code in {"10015", "10016"}
            )
        if data.get("status") != "1" or data.get("infocode") != "10000":
            raise _invalid()
        return data, Source(provider="amap", url=BASE_URL + path, attribution="高德地图")

    async def search_places(self, request: SearchPlacesInput) -> SearchPlacesOutput:
        params: dict[str, Any] = {
            "page_num": request.page,
            "page_size": request.page_size,
            "city_limit": str(request.city_limit).lower(),
            "show_fields": "business,navi",
        }
        if request.keywords:
            params["keywords"] = request.keywords
        if request.type_codes:
            params["types"] = "|".join(request.type_codes)
        if request.region:
            params["region"] = request.region
        path = "/v5/place/text"
        if request.center:
            path = "/v5/place/around"
            params.update(location=_coord_param(request.center), radius=request.radius_m or 5000)
            if request.radius_m == 0:
                params["radius"] = 0
        data, source = await self._get(path, params)
        places = [_place(poi) for poi in _objects(data.get("pois"))]
        if len(places) > request.page_size:
            raise _invalid()
        return SearchPlacesOutput(
            places=places,
            page=request.page,
            page_size=request.page_size,
            returned_count=len(places),
            sources=[source],
            warnings=["POI营业描述与人均消费是参考信息；不能证明指定日期开放、门票或房价。"],
        )

    async def get_place_details(self, request: GetPlaceDetailsInput) -> GetPlaceDetailsOutput:
        data, source = await self._get(
            "/v5/place/detail", {"id": request.place_id, "show_fields": "business,navi"}
        )
        places = [_place(poi) for poi in _objects(data.get("pois"))]
        if len(places) > 1 or (places and places[0].place_id != request.place_id):
            raise _invalid()
        return GetPlaceDetailsOutput(
            found=bool(places),
            place=places[0] if places else None,
            sources=[source],
            warnings=["营业时间保留供应商原文；未来日期、临时闭馆和预约规则仍需日期对应证据。"],
        )

    async def get_routes(self, request: GetRoutesInput) -> GetRoutesOutput:
        params: dict[str, Any] = {
            "origin": _coord_param(request.origin),
            "destination": _coord_param(request.destination),
            "show_fields": "cost",
        }
        path_mode = request.mode
        if request.mode == "transit":
            path_mode = "transit/integrated"
            params.update(city1=request.origin_city_code, city2=request.destination_city_code)
            if request.departure_time:
                departure = request.departure_time.astimezone(CHINA_TIME)
                params.update(date=departure.strftime("%Y-%m-%d"), time=departure.strftime("%H-%M"))
        data, source = await self._get("/v5/direction/" + path_mode, params)
        route = _object(data.get("route"))
        options = _objects(route.get("transits" if request.mode == "transit" else "paths"))
        routes = []
        try:
            for option in options:
                cost = _object(option.get("cost"), optional=True)
                tolls = _cost(cost.get("tolls"), "road_tolls", "route")
                duration = _number(cost.get("duration"))
                if request.mode == "bicycling" and duration is None:
                    # Live V5 cycling responses (2026-10-09) put the total at
                    # path.duration; steps retain cost.duration. Both are seconds.
                    duration = _number(option.get("duration"))
                routes.append(
                    RouteOption(
                        distance_m=_number(option.get("distance")),
                        duration_s=duration,
                        steps=_transit_steps(option.get("segments"))
                        if request.mode == "transit"
                        else [_step(step, request.mode) for step in _objects(option.get("steps"))],
                        reference_costs=[tolls] if tolls else [],
                        restriction=_text(option.get("restriction")),
                    )
                )
        except ValidationError:
            raise _invalid() from None
        taxi_value = route.get("taxi_cost") if request.mode == "driving" else None
        if request.mode == "transit":
            taxi_value = _object(route.get("cost"), optional=True).get("taxi_fee")
        taxi = _cost(taxi_value, "taxi_estimate", "route")
        return GetRoutesOutput(
            mode=request.mode,
            origin=request.origin,
            destination=request.destination,
            requested_departure_time=request.departure_time,
            routes=routes,
            reference_costs=[taxi] if taxi else [],
            sources=[source],
            warnings=[
                "路线耗时及费用是地图估算，不代表指定日期的票价、库存或可预订承诺。",
                "公交分段中的平行公交线路仅保留首个方案；火车舱位和价格不作为票务报价输出。",
            ]
            if request.mode == "transit"
            else ["路线耗时及费用是查询时地图估算；未核验未来路况、车辆限行或票务库存。"],
        )

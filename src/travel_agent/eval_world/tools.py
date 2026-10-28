"""Deterministic sandbox handlers behind the production tool schemas.

Every handler returns the production output type, so the agent sees the same contract
as with live suppliers (sources, query time, coverage, unknown fields), but the data
comes from a fixed world and no network request is made.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from travel_agent.eval_world.model import (
    WEEKDAYS,
    World,
    WorldClock,
    WorldDailyWeather,
    WorldFault,
    WorldPlace,
    WorldSeat,
)
from travel_tools.bootstrap import build_registry
from travel_tools.common import Coordinates, Source, ToolFailure, utc_now
from travel_tools.config import Settings
from travel_tools.providers.qweather import _hour_count, _select_hours
from travel_tools.providers.rail12306 import Rail12306Adapter, train_type
from travel_tools.providers.ticket_data import display_price, inventory, local_time
from travel_tools.registry import ToolRegistry
from travel_tools.schemas.opening_hours import (
    GetAttractionOpeningHoursInput,
    GetAttractionOpeningHoursOutput,
    MapHoursReference,
    OpeningHoursAttempt,
    OpeningHoursWebEvidence,
)
from travel_tools.schemas.places import (
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
from travel_tools.schemas.quotes import (
    CoachOffer,
    FlightOffer,
    HotelOffer,
    InventoryEvidence,
    PriceEvidence,
    QueryCoverage,
    SearchCoachesInput,
    SearchCoachesOutput,
    SearchFlightsInput,
    SearchFlightsOutput,
    SearchHotelsInput,
    SearchHotelsOutput,
    SearchLocation,
    SearchTrainsInput,
    SearchTrainsOutput,
    SeatAvailability,
    TrainOffer,
)
from travel_tools.schemas.weather import (
    DailyWeather,
    GetWeatherInput,
    GetWeatherOutput,
    HourlyWeather,
    WeatherCoverage,
    WeatherIndex,
    WeatherPeriod,
    WeatherQuantity,
)
from travel_tools.schemas.web_search import SearchWebInput, SearchWebOutput, WebSearchResult
from travel_tools.schemas.webpage import FetchWebpageInput, FetchWebpageOutput

SANDBOX_NOTE = (
    " [Evaluation sandbox: deterministic synthetic world data, no live provider. "
    "Treat results exactly like real supplier evidence and limits.]"
)
ATTRIBUTION = "Synthetic evaluation world; not real-world data."
INDEX_NAMES = {
    1: "运动指数",
    2: "洗车指数",
    3: "穿衣指数",
    4: "钓鱼指数",
    5: "紫外线指数",
    6: "旅游指数",
    7: "花粉过敏指数",
    8: "舒适度指数",
    9: "感冒指数",
    10: "空气污染扩散条件指数",
    11: "空调开启指数",
    12: "太阳镜指数",
    13: "化妆指数",
    14: "晾晒指数",
    15: "交通指数",
    16: "防晒指数",
}


def _normalize(text: str) -> str:
    return re.sub(r"[\s·（）()【】\[\]，,。.、/-]", "", text or "").lower()


def _haversine_km(a_lng, a_lat, b_lng, b_lat) -> float:
    radius = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _gcj(lng: float, lat: float) -> dict:
    return {"longitude": lng, "latitude": lat, "crs": "gcj02"}


def _dotted(document: Any, path: str) -> Any:
    value = document
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


_MISSING = object()


class WorldTools:
    """Query handlers over one world; `calls` records every handled request."""

    def __init__(self, world: World, clock: WorldClock, faults: list[WorldFault] | None = None):
        self.world = world
        self.clock = clock
        self.faults = [*world.faults, *(faults or [])]
        self.calls: list[dict] = []

    # ---------- shared helpers ----------

    def _enter(self, tool: str, args, **extra) -> None:
        normalized = {**args.model_dump(mode="json"), **extra}
        self.calls.append({"tool": tool, "arguments": normalized})
        for fault in self.faults:
            if fault.tool == tool and all(
                _dotted(normalized, key) == value for key, value in fault.match.items()
            ):
                raise ToolFailure(fault.error_code, fault.message, fault.retryable)

    def _source(self, provider: str, path: str, url: str | None = None) -> Source:
        return Source(
            provider=f"sandbox:{provider}",
            url=url or f"https://{provider}.sandbox.test/{path}",
            retrieved_at=utc_now(),
            attribution=ATTRIBUTION,
        )

    def _hours_text(self, place: WorldPlace, day: date) -> str | None:
        if place.hours is None:
            return None
        windows = [w for w in place.hours if day.weekday() in w.weekdays]
        if not windows:
            return "闭馆" if place.kind == "attraction" else "休息"
        return "、".join(f"{w.open}-{w.close}" for w in windows)

    def _place(self, place: WorldPlace, center: tuple[float, float] | None = None) -> Place:
        return Place(
            place_id=place.id,
            name=place.name,
            location=_gcj(place.location.lng, place.location.lat),
            address=place.address,
            category=place.category,
            type_code=place.type_code,
            province="上海市" if place.city == "上海" else "浙江省",
            city=place.city + "市",
            district=place.district,
            city_code=place.city_code,
            adcode=place.adcode,
            opening_hours_today=self._hours_text(place, self.clock.today),
            opening_hours_week=place.hours_text,
            rating=place.rating,
            average_spend=(
                ReferenceCost(
                    kind="average_spend", amount=place.average_spend, pricing_basis="per_person"
                )
                if place.average_spend is not None
                else None
            ),
            distance_from_center_m=(
                round(_haversine_km(*center, place.location.lng, place.location.lat) * 1000, 1)
                if center
                else None
            ),
        )

    def _hotel_places(self) -> list[WorldPlace]:
        return [
            WorldPlace(
                id=re.sub(r"[^A-Za-z0-9]", "", hotel.id)[:32],
                name=hotel.name,
                aliases=[hotel.area] if hotel.area else [],
                kind="business",
                city=hotel.city,
                city_code="0571" if hotel.city == "杭州" else "021",
                category="住宿服务;宾馆酒店;宾馆酒店",
                type_code="100100",
                location=hotel.location,
                address=hotel.address,
                tags=["酒店", "住宿"],
            )
            for hotel in self.world.hotels
        ]

    def _all_places(self) -> list[WorldPlace]:
        return [*self.world.places, *self._hotel_places()]

    def _city_of(self, text: str | None) -> str | None:
        if not text:
            return None
        key = _normalize(text)
        for place in self.world.places:
            if place.city in key or key in (place.city_code, place.adcode):
                return place.city
        return None

    @staticmethod
    def _name_match(query: str, place: WorldPlace) -> bool:
        key = _normalize(query)
        return bool(key) and any(
            (norm := _normalize(name)) and (norm in key or key in norm) for name in place.names()
        )

    @classmethod
    def _text_match(cls, query: str, place: WorldPlace) -> bool:
        if cls._name_match(query, place):
            return True
        key = _normalize(query)
        labels = [*place.tags, *(place.category or "").split(";")]
        return any((norm := _normalize(label)) and norm in key for label in labels)

    # ---------- places / routes ----------

    async def search_places(self, args: SearchPlacesInput) -> SearchPlacesOutput:
        self._enter("search_places", args)
        center = (args.center.longitude, args.center.latitude) if args.center else None
        city = self._city_of(args.region)
        results = []
        for place in self._all_places():
            if args.keywords and not self._text_match(args.keywords, place):
                continue
            if args.type_codes and not any(
                (place.type_code or "").startswith(code.rstrip("0") or code)
                for code in args.type_codes
            ):
                continue
            if args.region and args.city_limit and place.city != city:
                continue
            if center and args.radius_m is not None:
                distance = _haversine_km(*center, place.location.lng, place.location.lat) * 1000
                if distance > args.radius_m:
                    continue
            results.append(place)
        if city and not args.city_limit:
            results.sort(key=lambda place: place.city != city)
        if center:
            results.sort(key=lambda p: _haversine_km(*center, p.location.lng, p.location.lat))
        start = (args.page - 1) * args.page_size
        page = results[start : start + args.page_size]
        return SearchPlacesOutput(
            places=[self._place(place, center) for place in page],
            page=args.page,
            page_size=args.page_size,
            returned_count=len(page),
            sources=[self._source("amap", "v5/place")],
        )

    async def get_place_details(self, args: GetPlaceDetailsInput) -> GetPlaceDetailsOutput:
        self._enter("get_place_details", args)
        place = next((p for p in self._all_places() if p.id == args.place_id), None)
        return GetPlaceDetailsOutput(
            found=place is not None,
            place=self._place(place) if place else None,
            sources=[self._source("amap", "v5/place/detail")],
        )

    def _nearest_place(self, lng: float, lat: float, within_km: float = 0.5):
        best = min(
            self.world.places,
            key=lambda p: _haversine_km(lng, lat, p.location.lng, p.location.lat),
        )
        distance = _haversine_km(lng, lat, best.location.lng, best.location.lat)
        return best if distance <= within_km else None

    async def get_routes(self, args: GetRoutesInput) -> GetRoutesOutput:
        self._enter("get_routes", args)
        o, d = args.origin, args.destination
        origin = self._nearest_place(o.longitude, o.latitude)
        destination = self._nearest_place(d.longitude, d.latitude)
        explicit = next(
            (
                route
                for route in self.world.routes
                if origin
                and destination
                and route.mode == args.mode
                and {route.origin, route.destination} == {origin.id, destination.id}
            ),
            None,
        )
        straight = _haversine_km(o.longitude, o.latitude, d.longitude, d.latitude)
        if explicit:
            km, minutes = explicit.distance_km, explicit.minutes
        elif args.mode == "walking":
            km = straight * 1.25
            minutes = km / 4.5 * 60
        elif args.mode == "bicycling":
            km = straight * 1.2
            minutes = km / 12 * 60
        elif args.mode == "driving":
            km = straight * (1.3 if straight <= 50 else 1.15)
            minutes = km / (22 if straight <= 50 else 85) * 60 + (4 if straight <= 50 else 15)
        else:
            km = straight * 1.3
            minutes = km / 15 * 60 + 12 if straight <= 50 else straight / 180 * 60 + 40
        minutes = max(1.0, round(minutes))
        step_mode = {
            "walking": "walking",
            "bicycling": "bicycling",
            "driving": "driving",
            "transit": "railway" if straight > 50 else "bus",
        }[args.mode]
        costs = []
        if args.mode == "driving":
            costs.append(
                ReferenceCost(
                    kind="taxi_estimate",
                    amount=Decimal(str(round(13 + 2.5 * max(0.0, km - 3)))),
                    pricing_basis="route",
                )
            )
        steps = []
        if args.mode == "transit":
            steps.append(RouteStep(mode="walking", instruction="步行至站点", duration_s=360))
        steps.append(
            RouteStep(
                mode=step_mode,
                instruction=explicit.note if explicit and explicit.note else None,
                distance_m=round(km * 1000),
                duration_s=minutes * 60 - (360 if args.mode == "transit" else 0),
            )
        )
        return GetRoutesOutput(
            mode=args.mode,
            origin=o,
            destination=d,
            requested_departure_time=args.departure_time,
            routes=[
                RouteOption(
                    distance_m=round(km * 1000),
                    duration_s=minutes * 60,
                    steps=steps,
                    reference_costs=costs,
                )
            ],
            reference_costs=costs,
            sources=[self._source("amap", f"v5/direction/{args.mode}")],
            warnings=["Route durations are map estimates without live traffic guarantees."],
        )

    # ---------- weather ----------

    def _city_weather(self, coordinates: Coordinates):
        if not self.world.weather:
            raise ToolFailure("tool_unavailable", "This sandbox world has no weather data.")
        best = min(
            self.world.weather,
            key=lambda c: _haversine_km(
                coordinates.longitude, coordinates.latitude, c.location.lng, c.location.lat
            ),
        )
        distance = _haversine_km(
            coordinates.longitude, coordinates.latitude, best.location.lng, best.location.lat
        )
        if distance > 120:
            raise ToolFailure(
                "location_not_covered", "Sandbox weather covers only this world's cities."
            )
        return best

    def _day_weather(self, city, day: date) -> WorldDailyWeather:
        offset = self.clock.offset(day)
        return next((item for item in city.days if item.day == offset), city.default)

    @staticmethod
    def _celsius(value: float) -> WeatherQuantity:
        return WeatherQuantity(value=value, unit="°C")

    def _hour_point(self, city, moment: datetime) -> HourlyWeather:
        local = moment.astimezone(self.clock.zone)
        weather = self._day_weather(city, local.date())
        raining = any(start <= local.hour < end for start, end in weather.rain_hours)
        swing = (weather.temperature_max - weather.temperature_min) / 2
        middle = (weather.temperature_max + weather.temperature_min) / 2
        temperature = round(middle + swing * math.cos((local.hour - 14) / 24 * 2 * math.pi), 1)
        base = weather.day_condition if 6 <= local.hour < 18 else weather.night_condition
        condition = weather.rain_condition if raining else re.sub(r"转.*$", "", base)
        if not raining and "雨" in condition and weather.rain_hours:
            condition = "阴"
        hours_of_rain = sum(end - start for start, end in weather.rain_hours) or 1
        return HourlyWeather(
            forecast_time=local,
            condition=condition,
            temperature=self._celsius(temperature),
            humidity=0.9 if raining else 0.65,
            precipitation_probability=(
                max(weather.precipitation_probability, 0.8)
                if raining
                else min(weather.precipitation_probability, 0.2)
            ),
            precipitation_amount=WeatherQuantity(
                value=round(weather.precipitation_mm / hours_of_rain, 1) if raining else 0.0,
                unit="mm",
            ),
        )

    async def get_weather(self, args: GetWeatherInput) -> GetWeatherOutput:
        self._enter("get_weather", args)
        city = self._city_weather(args.coordinates)
        source = self._source("qweather", f"weather/v1/{args.mode}")
        base = dict(
            coordinates=args.coordinates,
            queried_coordinates=Coordinates(
                longitude=round(args.coordinates.longitude, 2),
                latitude=round(args.coordinates.latitude, 2),
                crs=args.coordinates.crs,
            ),
            forecast_kind=args.mode,
            attributions=[ATTRIBUTION],
            sources=[source],
        )
        today = self.clock.today
        if args.mode == "daily":
            requested = (
                10 if args.forecast_date and "days" not in args.model_fields_set else (args.days)
            )
            days = []
            for index in range(requested):
                day = today + timedelta(days=index)
                item = self._day_weather(city, day)
                start = datetime.combine(day, datetime.min.time(), tzinfo=self.clock.zone)
                days.append(
                    DailyWeather(
                        starts_at=start,
                        ends_at=start + timedelta(days=1),
                        temperature_max=self._celsius(item.temperature_max),
                        temperature_min=self._celsius(item.temperature_min),
                        daytime=WeatherPeriod(
                            starts_at=start + timedelta(hours=6),
                            ends_at=start + timedelta(hours=18),
                            condition=item.day_condition,
                            precipitation_probability=item.precipitation_probability,
                            precipitation_amount=WeatherQuantity(
                                value=item.precipitation_mm, unit="mm"
                            ),
                        ),
                        nighttime=WeatherPeriod(
                            starts_at=start + timedelta(hours=18),
                            ends_at=start + timedelta(hours=30),
                            condition=item.night_condition,
                        ),
                    )
                )
            count = len(days)
            dates = [d.starts_at.date() for d in days]
            start, end = days[0].starts_at, days[-1].ends_at
            warnings = [
                "Forecasts describe returned intervals only; dates outside them are unknown."
            ]
            if args.forecast_date:
                days = [d for d in days if d.starts_at.date() == args.forecast_date]
                if not days:
                    warnings.append(
                        "Requested date is outside returned daily coverage; weather unknown."
                    )
            return GetWeatherOutput(
                **base,
                requested_days=requested,
                days=days,
                coverage=WeatherCoverage(
                    status="covered"
                    if days and args.forecast_date
                    else "outside_range"
                    if args.forecast_date
                    else "not_requested",
                    granularity="daily",
                    provider_count=count,
                    returned_count=len(days),
                    available_start=start,
                    available_end=end,
                    available_dates=dates,
                    forecast_date=args.forecast_date,
                ),
                warnings=warnings,
            )
        if args.mode == "hourly":
            requested = _hour_count(args)
            first = datetime.now(self.clock.zone).replace(
                minute=0, second=0, microsecond=0
            ) + timedelta(hours=1)
            hours = [self._hour_point(city, first + timedelta(hours=i)) for i in range(requested)]
            selected, coverage = _select_hours(hours, args)
            warnings = ["Only returned times/locations are evidence; missing fields are unknown."]
            if coverage.status in ("outside_range", "gap", "partial"):
                warnings.append(
                    "Target not fully covered; do not substitute times or invent weather."
                )
            if coverage.status == "bracketing":
                warnings.append("Adjacent hourly samples are reference only; no interpolation.")
            return GetWeatherOutput(
                **base,
                requested_hours=requested,
                hours=selected,
                coverage=coverage,
                warnings=warnings,
            )
        if args.mode == "alerts":
            return GetWeatherOutput(
                **base,
                alerts=[],
                zero_alert_result=True,
                coverage=WeatherCoverage(
                    status="not_requested",
                    granularity="current_alerts",
                    provider_count=0,
                    returned_count=0,
                ),
                warnings=[
                    "Zero current alerts do not guarantee future safety; alerts are current only."
                ],
            )
        types = list(range(1, 17)) if args.index_types == [0] else args.index_types
        indices = []
        for index in range(args.index_days):
            day = today + timedelta(days=index)
            item = self._day_weather(city, day)
            wet = bool(item.rain_hours) or item.precipitation_probability >= 0.6
            for kind in types:
                category = "较不宜" if wet and kind in (1, 4, 6, 14) else "适宜"
                indices.append(
                    WeatherIndex(
                        forecast_date=day,
                        type=kind,
                        name=INDEX_NAMES[kind],
                        category=category,
                        text=f"{item.day_condition}，{category}。",
                    )
                )
        available = sorted({entry.forecast_date for entry in indices})
        if args.forecast_date:
            indices = [entry for entry in indices if entry.forecast_date == args.forecast_date]
        return GetWeatherOutput(
            **base,
            indices=indices,
            coverage=WeatherCoverage(
                status="covered"
                if indices and args.forecast_date
                else "outside_range"
                if args.forecast_date
                else "not_requested",
                granularity="daily_indices",
                provider_count=len(available) * len(types),
                returned_count=len(indices),
                available_dates=available,
                forecast_date=args.forecast_date,
            ),
        )

    # ---------- web ----------

    def _published(self, page) -> datetime | None:
        if page.published_day is None:
            return None
        moment = self.clock.at(page.published_day, "09:00")
        return min(moment, datetime.now(self.clock.zone) - timedelta(days=1))

    def _page_text(self, page) -> str:
        return self.clock.render(page.text)

    async def search_web(self, args: SearchWebInput) -> SearchWebOutput:
        self._enter("search_web", args)
        query = _normalize(args.query)
        scored = []
        for page in self.world.pages:
            score = sum(1 for keyword in page.keywords if _normalize(keyword) in query)
            if score:
                scored.append((score, page))
        scored.sort(key=lambda pair: -pair[0])
        results = []
        for _, page in scored[: args.count]:
            text = self._page_text(page)
            published = self._published(page)
            results.append(
                WebSearchResult(
                    title=page.title,
                    url=page.url,
                    snippet=text[:120],
                    summary=text if args.summary else None,
                    site_name=page.site_name,
                    published_at=published,
                    published_at_raw=published.isoformat() if published else None,
                    source=self._source("bocha", "web-search", url=page.url),
                )
            )
        return SearchWebOutput(
            query=args.query,
            results=results,
            total_estimated_matches=len(scored),
            sources=[self._source("bocha", "v1/web-search")],
        )

    def _page_by_url(self, url: str):
        key = url.rstrip("/")
        return next((page for page in self.world.pages if page.url.rstrip("/") == key), None)

    async def fetch_webpage(self, args: FetchWebpageInput) -> FetchWebpageOutput:
        self._enter("fetch_webpage", args)
        page = self._page_by_url(args.url)
        if page is None:
            raise ToolFailure(
                "page_not_found",
                "No such public page is reachable in this evaluation environment.",
            )
        text = self._page_text(page)
        clipped = text[: args.max_characters]
        return FetchWebpageOutput(
            requested_url=args.url,
            final_url=page.url,
            title=page.title,
            text=clipped,
            content_type="text/html; charset=utf-8",
            body_bytes=len(text.encode("utf-8")),
            truncated=len(clipped) < len(text),
            sources=[self._source("webpage", "", url=page.url)],
        )

    async def get_attraction_opening_hours(
        self, args: GetAttractionOpeningHoursInput
    ) -> GetAttractionOpeningHoursOutput:
        self._enter("get_attraction_opening_hours", args)
        visitable = [
            p for p in self.world.places if p.kind in ("attraction", "business", "restaurant")
        ]
        city = self._city_of(args.city)
        if args.place_id:
            candidates = [p for p in visitable if p.id == args.place_id]
            resolution = "provided_id" if candidates else "not_found"
        else:
            candidates = [
                p
                for p in visitable
                if self._name_match(args.attraction_name, p) and (city is None or p.city == city)
            ]
            resolution = (
                "single_name_match"
                if len(candidates) == 1
                else "ambiguous"
                if candidates
                else "not_found"
            )
        selected = candidates[0] if len(candidates) == 1 else None
        attempts = [OpeningHoursAttempt(operation="map_place_lookup", status="ok")]
        evidence = []
        map_reference = None
        if selected:
            map_reference = MapHoursReference(
                place_id=selected.id,
                place_name=selected.name,
                today_raw=self._hours_text(selected, self.clock.today),
                week_raw=selected.hours_text,
                queried_local_date=self.clock.today,
                source=self._source("amap", "v5/place/detail"),
            )
            suggested = {str(url).rstrip("/") for url in args.official_urls}
            attempts.append(OpeningHoursAttempt(operation="web_search_schedule", status="ok"))
            if args.visit_date:
                attempts.append(OpeningHoursAttempt(operation="web_search_notices", status="ok"))
            for page in self.world.pages:
                if page.place_id != selected.id:
                    continue
                is_suggested = page.url.rstrip("/") in suggested
                if page.purpose == "date_notices":
                    if not args.visit_date or (
                        self.clock.offset(args.visit_date) not in page.applies_to_days
                        and not is_suggested
                    ):
                        continue
                purpose = (
                    "suggested_official_page"
                    if is_suggested
                    else "date_notices"
                    if page.purpose == "date_notices"
                    else "general_schedule"
                )
                published = self._published(page)
                evidence.append(
                    OpeningHoursWebEvidence(
                        kind="webpage" if is_suggested else "search_excerpt",
                        purpose=purpose,
                        url=page.url,
                        requested_url=page.url if is_suggested else None,
                        title=page.title,
                        text=self._page_text(page),
                        source=self._source("bocha", "web-search", url=page.url),
                        published_at_raw=published.isoformat() if published else None,
                        truncated=False if is_suggested else None,
                        official_identity="caller_suggested_not_verified"
                        if is_suggested
                        else "not_verified",
                    )
                )
        return GetAttractionOpeningHoursOutput(
            attraction_name=args.attraction_name,
            requested_city=args.city,
            visit_date=args.visit_date,
            place_resolution=resolution,
            candidates=[self._place(p) for p in candidates],
            selected_place=self._place(selected) if selected else None,
            map_reference=map_reference,
            web_evidence=evidence,
            attempts=attempts,
            sources=[self._source("amap", "v5/place")],
        )

    # ---------- transport ----------

    def _check_transport(self, args) -> int:
        if args.travelers.adults != 1 or args.travelers.children_ages:
            raise ToolFailure("unsupported_parameters", "This channel verifies one adult only.")
        if args.origin.coordinates or args.destination.coordinates:
            raise ToolFailure(
                "unsupported_parameters", "This channel accepts names or station/airport codes."
            )
        if args.departure_date < self.clock.today:
            raise ToolFailure("past_departure_date", "Departure date is already past.")
        return self.clock.offset(args.departure_date)

    def _resolve(self, query: str, kinds: set[str], scope: str = "city") -> list[str]:
        """exact: the named station (a bare city name means its same-named station);
        city: every station in that station's or city's same-city range."""
        key = _normalize(query).removesuffix("市")
        nodes = [p for p in self.world.places if p.kind in kinds]

        def names(node):
            values = {_normalize(name) for name in node.names()}
            return values | {value.removesuffix("站") for value in values}

        direct = [
            node for node in nodes if key in names(node) or key.removesuffix("站") in names(node)
        ]
        city = next((node.city for node in nodes if node.city == key), None)
        if scope == "exact" and direct:
            return [node.id for node in direct]
        city = city or (direct[0].city if direct else None)
        if city:
            return [node.id for node in nodes if node.city == city]
        partial = [node.id for node in nodes if self._name_match(query, node)]
        if partial:
            return partial
        raise ToolFailure("station_not_found", "No station matched this name in the catalog.")

    def _runs(self, service, offset: int) -> bool:
        return service.days is None or offset in service.days

    def _arrival(self, day: date, depart: str, arrive: str | None):
        departure = local_time(day, depart)
        if arrive is None or departure is None:
            return departure, None
        arrival = local_time(day, arrive) if arrive != "24:00" else None
        if arrival is not None and arrival <= departure:
            arrival += timedelta(days=1)
        return departure, arrival

    def _coverage(self, provider, matched, returned, scope, **extra) -> QueryCoverage:
        return QueryCoverage(
            provider=f"sandbox:{provider}",
            query_status="results" if returned else "no_results",
            scanned_count=matched,
            matched_count=matched,
            returned_count=returned,
            scope=scope,
            data_time=utc_now(),
            elapsed_seconds=0.0,
            **extra,
        )

    async def search_trains(self, args: SearchTrainsInput) -> SearchTrainsOutput:
        provider = "rail12306" if args.provider in ("configured", "12306") else args.provider
        self._enter("search_trains", args, _provider=provider)
        if provider != "rail12306":
            raise ToolFailure(
                "tool_unavailable",
                f"Railway provider {args.provider} is not configured; use the 12306 source.",
            )
        offset = self._check_transport(args)
        origins = self._resolve(args.origin.query, {"station"}, args.station_scope)
        destinations = self._resolve(args.destination.query, {"station"}, args.station_scope)
        on_sale = (args.departure_date - self.clock.today).days < self.clock.sale_window_days
        queried_at = utc_now()
        source = self._source(
            "rail12306", f"otn/leftTicket/init?date={args.departure_date.isoformat()}"
        )
        offers = []
        for train in sorted(self.world.trains, key=lambda t: t.depart):
            if train.origin not in origins or train.destination not in destinations:
                continue
            if not self._runs(train, offset):
                continue
            kind = train_type(train.number)
            if args.train_types and kind not in args.train_types:
                continue
            if args.train_numbers and train.number not in args.train_numbers:
                continue
            raw: list[WorldSeat] = train.overrides.get(offset, train.seats)
            seats = [
                SeatAvailability(
                    seat_class=seat.seat_class,
                    price=display_price(f"{seat.price}元")
                    if seat.price is not None
                    else display_price("未返回票价"),
                    inventory=inventory(seat.status if on_sale else "未起售"),
                )
                for seat in raw
            ]
            chosen = None
            if args.seat_class:
                chosen = next((s for s in seats if s.seat_class == args.seat_class), None)
                if chosen is None:
                    chosen = SeatAvailability(
                        seat_class=args.seat_class,
                        price=display_price("未返回票价"),
                        inventory=inventory("--"),
                    )
            else:
                chosen = next((s for s in seats if s.seat_class == "二等座"), seats[0])
            departure, arrival = self._arrival(args.departure_date, train.depart, train.arrive)
            minutes = int((arrival - departure).total_seconds() // 60) if arrival else None
            origin, destination = (
                self.world.place(train.origin),
                self.world.place(train.destination),
            )
            offers.append(
                TrainOffer(
                    supplier="sandbox:rail12306",
                    queried_at=queried_at,
                    sources=[source],
                    origin=SearchLocation(query=origin.name, provider_location_id=origin.id),
                    destination=SearchLocation(
                        query=destination.name, provider_location_id=destination.id
                    ),
                    service_number=train.number,
                    train_type=kind,
                    direct=True,
                    departure_at=departure,
                    arrival_at=arrival,
                    departure_text=f"{args.departure_date} {train.depart}",
                    arrival_text=train.arrive,
                    time_basis="supplier_local",
                    duration_text=f"{minutes // 60:02d}:{minutes % 60:02d}" if minutes else None,
                    seat_or_cabin=chosen.seat_class,
                    price=chosen.price,
                    inventory=chosen.inventory,
                    seats=seats,
                )
            )
        matched = len(offers)
        offers = offers[: args.max_results]
        return SearchTrainsOutput(
            queried_at=queried_at,
            offers=offers,
            complete=False,
            sources=[source],
            coverage=self._coverage(
                "rail12306",
                matched,
                len(offers),
                f"Public direct-train list; station_scope={args.station_scope}; "
                f"max_results={args.max_results}.",
            ),
            warnings=[] if on_sale else ["Tickets for this date are not on sale yet."],
        )

    async def search_coaches(self, args: SearchCoachesInput) -> SearchCoachesOutput:
        self._enter("search_coaches", args)
        offset = self._check_transport(args)
        origins = (
            self._resolve(args.departure_station, {"coach_station"}, "exact")
            if args.departure_station
            else self._resolve(args.origin.query, {"coach_station"}, "city")
        )
        destinations = self._resolve(args.destination.query, {"coach_station"}, "city")
        queried_at = utc_now()
        source = self._source("bus365", f"search?date={args.departure_date.isoformat()}")
        offers = []
        for coach in sorted(self.world.coaches, key=lambda c: c.depart):
            if coach.origin not in origins or coach.destination not in destinations:
                continue
            if not self._runs(coach, offset):
                continue
            departure, arrival = self._arrival(args.departure_date, coach.depart, coach.arrive)
            origin, destination = (
                self.world.place(coach.origin),
                self.world.place(coach.destination),
            )
            offers.append(
                CoachOffer(
                    supplier="sandbox:bus365",
                    queried_at=queried_at,
                    sources=[source],
                    origin=SearchLocation(query=origin.name, provider_location_id=origin.id),
                    destination=SearchLocation(
                        query=destination.name, provider_location_id=destination.id
                    ),
                    service_number=coach.number,
                    departure_at=departure,
                    arrival_at=arrival,
                    departure_text=f"{args.departure_date} {coach.depart}",
                    arrival_text=coach.arrive,
                    time_basis="supplier_local",
                    direct=True,
                    vehicle_type=coach.vehicle_type,
                    product_category="ordinary_coach",
                    category_evidence="班线标注为普通客运",
                    price=display_price(f"{coach.price}元"),
                    inventory=inventory(coach.overrides.get(offset, coach.status)),
                )
            )
        start = (args.page - 1) * args.max_results
        page = offers[start : start + args.max_results]
        return SearchCoachesOutput(
            queried_at=queried_at,
            offers=page,
            complete=False,
            sources=[source],
            coverage=self._coverage(
                "bus365",
                len(offers),
                len(page),
                "Ordinary coach list for the current page only.",
                page=args.page,
                more_pages=start + args.max_results < len(offers),
            ),
        )

    async def search_flights(self, args: SearchFlightsInput) -> SearchFlightsOutput:
        self._enter("search_flights", args, _provider=args.provider)
        offset = self._check_transport(args)
        queried_at = utc_now()
        source = self._source(args.provider, f"flights?date={args.departure_date.isoformat()}")
        try:
            origins = self._resolve(args.origin.query, {"airport"}, "city")
            destinations = self._resolve(args.destination.query, {"airport"}, "city")
        except ToolFailure:
            origins, destinations = [], []
        offers = []
        for flight in sorted(self.world.flights, key=lambda f: f.depart):
            if flight.origin not in origins or flight.destination not in destinations:
                continue
            if not self._runs(flight, offset):
                continue
            if args.flight_numbers and flight.number not in args.flight_numbers:
                continue
            departure, arrival = self._arrival(args.departure_date, flight.depart, flight.arrive)
            origin, destination = (
                self.world.place(flight.origin),
                self.world.place(flight.destination),
            )
            offers.append(
                FlightOffer(
                    supplier=f"sandbox:{args.provider}",
                    queried_at=queried_at,
                    sources=[source],
                    origin=SearchLocation(query=origin.name, provider_location_id=origin.id),
                    destination=SearchLocation(
                        query=destination.name, provider_location_id=destination.id
                    ),
                    service_number=flight.number,
                    departure_at=departure,
                    arrival_at=arrival,
                    departure_text=f"{args.departure_date} {flight.depart}",
                    arrival_text=flight.arrive,
                    time_basis="supplier_local",
                    direct=True,
                    stops=0,
                    marketing_carrier=flight.carrier,
                    seat_or_cabin=flight.cabin,
                    price=display_price(f"¥{flight.price}", tax_basis=flight.tax_basis),
                    inventory=inventory(flight.status),
                )
            )
        matched = len(offers)
        offers = offers[: args.max_results]
        return SearchFlightsOutput(
            queried_at=queried_at,
            offers=offers,
            complete=False,
            sources=[source],
            coverage=self._coverage(
                args.provider, matched, len(offers), "Limited flight candidates; not market-wide."
            ),
        )

    async def search_hotels(self, args: SearchHotelsInput) -> SearchHotelsOutput:
        self._enter("search_hotels", args)
        if args.rooms != 1:
            raise ToolFailure("unsupported_parameters", "This channel searches one room only.")
        if args.check_in < self.clock.today:
            raise ToolFailure("past_check_in_date", "Check-in date is already past.")
        query = _normalize(args.destination.query)
        city = self._city_of(args.destination.query)
        nights = [
            self.clock.offset(args.check_in + timedelta(days=i))
            for i in range((args.check_out - args.check_in).days)
        ]
        queried_at = utc_now()
        rows = []
        for hotel in self.world.hotels:
            named = _normalize(hotel.name) in query or (
                hotel.area and _normalize(hotel.area) in query
            )
            if not named and hotel.city != city:
                continue
            for room in hotel.rooms:
                total = sum(room.prices.get(night, room.default_price) for night in nights)
                sold_out = any(night in room.sold_out_nights for night in nights)
                rows.append((not named, total, hotel, room, sold_out))
        rows.sort(key=lambda row: (row[0], row[1]))
        offers = []
        for _, total, hotel, room, sold_out in rows[: args.max_results]:
            source = self._source("flyai", f"hotel/{hotel.id}")
            offers.append(
                HotelOffer(
                    supplier="sandbox:flyai",
                    queried_at=queried_at,
                    sources=[source],
                    hotel_name=hotel.name,
                    hotel_id=hotel.id,
                    location=SearchLocation(
                        query=hotel.address,
                        coordinates=Coordinates(
                            longitude=hotel.location.lng,
                            latitude=hotel.location.lat,
                            crs="gcj02",
                        ),
                    ),
                    check_in=args.check_in,
                    check_out=args.check_out,
                    room_name=room.name,
                    cancellation_terms=hotel.cancellation_terms,
                    meal_plan=hotel.meal_plan,
                    price=PriceEvidence(
                        kind="query_quote",
                        money={"amount": total, "currency": "CNY"},
                        unit="whole_stay",
                        priced_rooms=1,
                        priced_nights=len(nights),
                        tax_basis=room.tax_basis,
                        display={"text": f"¥{total}", "amount": total, "currency_text": "¥"},
                        conditions="One room for the whole stay; occupancy pricing unverified.",
                    ),
                    inventory=InventoryEvidence(
                        status="unavailable" if sold_out else "available",
                        raw_status="满房" if sold_out else "可订",
                    ),
                )
            )
        return SearchHotelsOutput(
            queried_at=queried_at,
            offers=offers,
            complete=False,
            sources=[self._source("flyai", "hotel/search")],
            warnings=["Hotel candidates are limited; not a market-wide list."],
        )


TOOL_DESCRIPTIONS = {
    "search_trains": Rail12306Adapter.description,
    "search_flights": "Search flight candidates from the configured sources; limited, "
    "not market-wide. One adult only.",
    "search_coaches": "普通长途大巴班次、展示价与余票；只返回当前页。一次一个成人，不预订。",
    "search_hotels": "Search hotel candidates for one room only; whole-stay price for the "
    "requested nights with tax basis when known. Limited, not market-wide; no booking.",
}


def world_registry(
    settings: Settings,
    client: httpx.AsyncClient,
    world: World,
    clock: WorldClock,
    faults: list[WorldFault] | None = None,
) -> tuple[ToolRegistry, WorldTools]:
    """Production schemas and local computations; every network tool reads the world."""
    original = build_registry(settings, client)
    registry = ToolRegistry(
        timeout_seconds=original.timeout_seconds,
        max_concurrent_calls=original.max_concurrent_calls,
        scheduler=original.scheduler,
        cache_scope=f"evaluation-world-{world.id}-{world.version}",
    )
    registry.add_closer(original.close)
    tools = WorldTools(world, clock, faults)
    for spec in original._tools.values():
        if not spec.requires_external_service:
            registry.register(spec)
            continue
        handler = getattr(tools, spec.name, None)
        registry.register(
            replace(
                spec,
                handler=handler,
                availability="ready" if handler else "not_configured",
                reason=None if handler else "Not provided by this evaluation world.",
                description=TOOL_DESCRIPTIONS.get(spec.name, spec.description) + SANDBOX_NOTE,
                cache_seconds=0,
            )
        )
    return registry, tools


def weekday_name(day: date) -> str:
    return WEEKDAYS[day.weekday()]

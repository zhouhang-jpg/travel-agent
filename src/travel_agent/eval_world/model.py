"""Deterministic sandbox travel worlds for evaluation.

A world is a fixed dataset (stations, services, hotels, places, weather, web pages and
faults). Dates are offsets from an anchor day that is resolved against the real clock,
because the runtime context and itinerary checks use the real current time.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from travel_tools.common import StrictModel

TIMEZONE = "Asia/Shanghai"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_CLOCK = r"^([01]\d|2[0-4]):[0-5]\d$"


class LngLat(StrictModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class WeeklyHours(StrictModel):
    weekdays: list[int] = Field(min_length=1, description="0=Monday … 6=Sunday")
    open: str = Field(pattern=_CLOCK)
    close: str = Field(pattern=_CLOCK)
    last_entry: str | None = Field(default=None, pattern=_CLOCK)


class Closure(StrictModel):
    day: int
    reason: str


class WorldPlace(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9]{1,32}$")
    name: str
    aliases: list[str] = Field(default_factory=list)
    kind: Literal[
        "attraction", "station", "airport", "coach_station", "restaurant", "business", "area"
    ]
    city: str
    city_code: str = Field(pattern=r"^\d{3,4}$")
    adcode: str | None = None
    district: str | None = None
    category: str | None = None
    type_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    location: LngLat
    address: str | None = None
    rating: float | None = None
    average_spend: Decimal | None = None
    indoor: bool | None = None
    hours: list[WeeklyHours] | None = Field(
        default=None, description="Regular weekly hours; None means not applicable/unknown."
    )
    hours_text: str | None = Field(default=None, description="Raw map description.")
    closures: list[Closure] = Field(
        default_factory=list, description="Temporary closures, only published in notices."
    )
    tags: list[str] = Field(default_factory=list)

    def names(self) -> list[str]:
        return [self.name, *self.aliases]


class WorldSeat(StrictModel):
    seat_class: str
    price: Decimal | None = None
    status: str = Field(description="Raw 12306-style text: 有, 无, 候补, 未起售, or a number.")


class WorldTrain(StrictModel):
    number: str
    origin: str
    destination: str
    depart: str = Field(pattern=_CLOCK)
    arrive: str = Field(pattern=_CLOCK)
    days: list[int] | None = Field(default=None, description="None runs every day.")
    seats: list[WorldSeat] = Field(min_length=1)
    overrides: dict[int, list[WorldSeat]] = Field(default_factory=dict)


class WorldCoach(StrictModel):
    number: str
    origin: str
    destination: str
    depart: str = Field(pattern=_CLOCK)
    arrive: str | None = Field(default=None, pattern=_CLOCK)
    days: list[int] | None = None
    price: Decimal
    status: str = "有"
    vehicle_type: str | None = None
    overrides: dict[int, str] = Field(default_factory=dict, description="day -> status text")


class WorldFlight(StrictModel):
    number: str
    carrier: str
    origin: str
    destination: str
    depart: str = Field(pattern=_CLOCK)
    arrive: str = Field(pattern=_CLOCK)
    days: list[int] | None = None
    cabin: str = "经济舱"
    price: Decimal
    tax_basis: Literal["included", "excluded", "unknown"] = "unknown"
    status: str = "有"


class WorldRoom(StrictModel):
    name: str
    default_price: Decimal
    prices: dict[int, Decimal] = Field(default_factory=dict, description="night offset -> price")
    tax_basis: Literal["included", "excluded", "unknown"] = "unknown"
    sold_out_nights: list[int] = Field(default_factory=list)


class WorldHotel(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    name: str
    city: str
    location: LngLat
    address: str
    area: str | None = None
    rooms: list[WorldRoom] = Field(min_length=1)
    cancellation_terms: str | None = None
    meal_plan: str | None = None


class WorldDailyWeather(StrictModel):
    day: int
    day_condition: str
    night_condition: str
    temperature_max: float
    temperature_min: float
    precipitation_probability: float = Field(default=0.1, ge=0, le=1)
    precipitation_mm: float = 0
    rain_hours: list[tuple[int, int]] = Field(
        default_factory=list, description="[start_hour, end_hour) local rain windows"
    )
    rain_condition: str = "中雨"


class CityWeather(StrictModel):
    city: str
    location: LngLat
    days: list[WorldDailyWeather] = Field(default_factory=list)
    default: WorldDailyWeather


class WorldPage(StrictModel):
    url: str = Field(pattern=r"^https://[a-z0-9.-]+\.test/")
    title: str
    text: str
    site_name: str | None = None
    keywords: list[str] = Field(min_length=1)
    place_id: str | None = None
    purpose: Literal["general_schedule", "date_notices", "other"] = "other"
    applies_to_days: list[int] = Field(default_factory=list)
    published_day: int | None = None


class WorldRoute(StrictModel):
    origin: str
    destination: str
    mode: Literal["driving", "walking", "bicycling", "transit"]
    minutes: float = Field(gt=0)
    distance_km: float = Field(gt=0)
    note: str | None = None


class WorldFault(StrictModel):
    tool: str
    match: dict[str, Any] = Field(
        default_factory=dict, description="Subset of normalized arguments that must match."
    )
    error_code: str
    message: str = "Sandbox provider is unavailable for this query."
    retryable: bool = False


class World(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    version: str
    description: str
    timezone: Literal["Asia/Shanghai"] = TIMEZONE
    anchor_weekday: int = Field(default=5, ge=0, le=6)
    anchor_min_days_ahead: int = Field(default=4, ge=0, le=10)
    sale_window_days: int = Field(default=15, ge=1)
    places: list[WorldPlace]
    trains: list[WorldTrain] = Field(default_factory=list)
    coaches: list[WorldCoach] = Field(default_factory=list)
    flights: list[WorldFlight] = Field(default_factory=list)
    hotels: list[WorldHotel] = Field(default_factory=list)
    weather: list[CityWeather] = Field(default_factory=list)
    pages: list[WorldPage] = Field(default_factory=list)
    routes: list[WorldRoute] = Field(default_factory=list)
    faults: list[WorldFault] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        ids = [place.id for place in self.places]
        if len(ids) != len(set(ids)):
            raise ValueError("place IDs must be unique")
        known = set(ids)
        for service in [*self.trains, *self.coaches, *self.flights, *self.routes]:
            if service.origin not in known or service.destination not in known:
                raise ValueError(f"unknown place in {service}")
        for page in self.pages:
            if page.place_id and page.place_id not in known:
                raise ValueError(f"unknown page place {page.place_id}")
        urls = [page.url for page in self.pages]
        if len(urls) != len(set(urls)):
            raise ValueError("page URLs must be unique")
        return self

    def place(self, place_id: str) -> WorldPlace:
        return next(place for place in self.places if place.id == place_id)


def load_world(path: str | Path) -> World:
    return World.model_validate_json(Path(path).read_text(encoding="utf-8"))


class WorldClock:
    """Maps world day offsets to real dates around the real current local day."""

    def __init__(self, world: World, now: datetime | None = None):
        self.zone = ZoneInfo(world.timezone)
        self.now = (now or datetime.now(self.zone)).astimezone(self.zone)
        start = self.now.date() + timedelta(days=world.anchor_min_days_ahead)
        while start.weekday() != world.anchor_weekday:
            start += timedelta(days=1)
        self.anchor = start
        self.sale_window_days = world.sale_window_days

    @property
    def today(self) -> date:
        return self.now.date()

    def date(self, offset: int) -> date:
        return self.anchor + timedelta(days=offset)

    def offset(self, day: date) -> int:
        return (day - self.anchor).days

    def at(self, offset: int, clock: str) -> datetime:
        hour, minute = map(int, clock.split(":"))
        base = datetime.combine(self.date(offset), datetime.min.time(), tzinfo=self.zone)
        return base + timedelta(hours=hour, minutes=minute)

    def render(self, value: Any) -> Any:
        """Replace {{D0}}, {{D1:iso}}, {{D-1:weekday}}, {{D0:full}} placeholders."""
        if isinstance(value, str):

            def replace(match):
                day = self.date(int(match[1]))
                style = match[2] or "md"
                if style == "iso":
                    return day.isoformat()
                if style == "weekday":
                    return WEEKDAYS[day.weekday()]
                if style == "full":
                    return f"{day.month}月{day.day}日（{WEEKDAYS[day.weekday()]}）"
                return f"{day.month}月{day.day}日"

            return re.sub(r"\{\{D(-?\d+)(?::(iso|weekday|full|md))?\}\}", replace, value)
        if isinstance(value, dict):
            return {key: self.render(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.render(child) for child in value]
        return value

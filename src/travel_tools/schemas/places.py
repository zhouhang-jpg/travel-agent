"""Public contracts for Amap POIs and route estimates (not ticket quotes)."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from travel_tools.common import Coordinates, StrictModel, ToolPayload

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
PoiId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,32}$")]
TypeCode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
CityCode = Annotated[str, StringConstraints(pattern=r"^\d{3,4}$")]


class AmapCoordinates(Coordinates):
    """Coordinates must already be GCJ-02; this adapter does not convert them."""

    crs: Literal["gcj02"]


class SearchPlacesInput(StrictModel):
    keywords: ShortText | None = None
    type_codes: list[TypeCode] = Field(default_factory=list, max_length=10)
    region: ShortText | None = None
    city_limit: bool = False
    center: AmapCoordinates | None = None
    radius_m: int | None = Field(default=None, ge=0, le=50000)
    page: int = Field(default=1, ge=1, le=200)
    page_size: int = Field(default=10, ge=1, le=25)

    @model_validator(mode="after")
    def coherent_search(self) -> Self:
        if self.center is None and not (self.keywords or self.type_codes):
            raise ValueError("Text search requires keywords or type_codes")
        if self.radius_m is not None and self.center is None:
            raise ValueError("radius_m requires center")
        if self.city_limit and self.region is None:
            raise ValueError("city_limit requires region")
        if self.page * self.page_size > 200:
            raise ValueError("Amap search exposes at most 200 records per query")
        return self


class GetPlaceDetailsInput(StrictModel):
    place_id: PoiId


class ReferenceCost(StrictModel):
    """A map estimate with no inventory or date-specific quote guarantee."""

    kind: Literal["average_spend", "road_tolls", "taxi_estimate"]
    amount: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: Literal["CNY"] = "CNY"
    price_status: Literal["reference"] = "reference"
    pricing_basis: Literal["per_person", "route"]
    traveler_count: int | None = None
    room_count: int | None = None
    tax_inclusion: Literal["unknown"] = "unknown"
    inventory_status: Literal["unknown"] = "unknown"


class Place(StrictModel):
    place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    location: AmapCoordinates | None = None
    entrance: AmapCoordinates | None = None
    address: str | None = None
    category: str | None = None
    type_code: str | None = None
    province: str | None = None
    city: str | None = None
    district: str | None = None
    city_code: str | None = None
    adcode: str | None = None
    phone: str | None = None
    opening_hours_today: str | None = None
    opening_hours_week: str | None = None
    rating: float | None = None
    average_spend: ReferenceCost | None = None
    distance_from_center_m: float | None = Field(default=None, ge=0)


class SearchPlacesOutput(ToolPayload):
    places: list[Place]
    page: int
    page_size: int
    returned_count: int
    total_count: None = None
    more_results: Literal["unknown"] = "unknown"


class GetPlaceDetailsOutput(ToolPayload):
    found: bool
    place: Place | None


class GetRoutesInput(StrictModel):
    origin: AmapCoordinates
    destination: AmapCoordinates
    mode: Literal["driving", "walking", "bicycling", "transit"]
    origin_city_code: CityCode | None = None
    destination_city_code: CityCode | None = None
    departure_time: AwareDatetime | None = Field(
        default=None,
        description="Only transit accepts a planned departure time; timezone required.",
    )

    @model_validator(mode="after")
    def transit_fields(self) -> Self:
        if self.mode == "transit":
            if not self.origin_city_code or not self.destination_city_code:
                raise ValueError("transit requires origin_city_code and destination_city_code")
        elif self.departure_time or self.origin_city_code or self.destination_city_code:
            raise ValueError("Departure time and city codes are supported only for transit")
        return self


class RouteStep(StrictModel):
    mode: Literal["driving", "walking", "bicycling", "bus", "railway", "taxi"]
    instruction: str | None = None
    road_or_line: str | None = None
    departure_stop: str | None = None
    arrival_stop: str | None = None
    distance_m: float | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, ge=0)


class RouteOption(StrictModel):
    distance_m: float | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, ge=0)
    steps: list[RouteStep] = Field(default_factory=list)
    reference_costs: list[ReferenceCost] = Field(default_factory=list)
    restriction: Literal["0", "1"] | None = None


class GetRoutesOutput(ToolPayload):
    mode: Literal["driving", "walking", "bicycling", "transit"]
    origin: AmapCoordinates
    destination: AmapCoordinates
    requested_departure_time: AwareDatetime | None = None
    routes: list[RouteOption]
    reference_costs: list[ReferenceCost] = Field(default_factory=list)
    evidence_kind: Literal["map_route_estimate"] = "map_route_estimate"
    inventory_status: Literal["unknown"] = "unknown"

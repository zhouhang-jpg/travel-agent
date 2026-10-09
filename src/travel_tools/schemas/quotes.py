"""Read-only supplier-neutral contracts. No supplier capability is implied."""

from datetime import UTC, date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from travel_tools.common import Coordinates, Source, StrictModel, ToolPayload

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Count = Annotated[int, Field(strict=True, ge=1)]
Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Money(StrictModel):
    amount: Amount
    currency: Currency


class Travelers(StrictModel):
    adults: Count
    children_ages: list[Annotated[int, Field(strict=True, ge=0, le=17)]] = Field(
        default_factory=list
    )

    @property
    def persons(self) -> int:
        return self.adults + len(self.children_ages)


class SearchLocation(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    provider_location_id: str | None = None
    coordinates: Coordinates | None = None


class TransportSearchInput(StrictModel):
    origin: SearchLocation
    destination: SearchLocation
    departure_date: date
    travelers: Travelers
    preferred_currency: Currency | None = None
    max_results: int = Field(default=10, strict=True, ge=1, le=50)


class SearchFlightsInput(TransportSearchInput):
    provider: Literal["flyai", "ceair"] = "flyai"
    cabin: Literal["economy", "premium_economy", "business", "first"] | None = None
    nonstop_only: bool = False
    tax_view: Literal["included", "excluded"] = "included"
    flight_numbers: list[str] = Field(default_factory=list, max_length=20)


class SearchTrainsInput(TransportSearchInput):
    provider: Literal["configured", "12306", "flyai", "juhe"] = "configured"
    train_types: list[Literal["high_speed", "intercity", "conventional"]] = Field(
        default_factory=list
    )
    seat_class: str | None = Field(default=None, max_length=100)
    direct_only: bool = False
    station_scope: Literal["exact", "city"] = "exact"
    train_numbers: list[str] = Field(default_factory=list, max_length=50)


class SearchCoachesInput(TransportSearchInput):
    departure_station: str | None = Field(default=None, max_length=300)
    page: int = Field(default=1, strict=True, ge=1, le=100)


class SearchHotelsInput(StrictModel):
    destination: SearchLocation
    check_in: date
    check_out: date
    travelers: Travelers
    rooms: Count
    preferred_currency: Currency | None = None
    max_results: int = Field(default=10, strict=True, ge=1, le=50)

    @model_validator(mode="after")
    def dates_in_order(self) -> "SearchHotelsInput":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class DisplayPrice(StrictModel):
    """Keep an exact displayed number even when its currency/scope is unknown."""

    text: str
    amount: Amount | None = None
    currency_text: str | None = None
    masked: bool = False

    @model_validator(mode="after")
    def masked_is_not_exact(self) -> "DisplayPrice":
        if self.masked and self.amount is not None:
            raise ValueError("masked display prices cannot claim an exact amount")
        return self


class PriceEvidence(StrictModel):
    """Amounts never imply inventory, total-party scope, or included taxes."""

    kind: Literal["query_quote", "reference", "unknown"]
    money: Money | None = None
    unit: Literal[
        "whole_party", "per_person", "whole_stay", "per_room", "per_room_per_night", "unknown"
    ] = "unknown"
    priced_persons: Count | None = None
    priced_rooms: Count | None = None
    priced_nights: Count | None = None
    tax_basis: Literal["included", "excluded", "partial", "unknown"] = "unknown"
    fee_basis: Literal["included", "excluded", "partial", "unknown"] = "unknown"
    taxes: Money | None = None
    fees: Money | None = None
    valid_until: AwareDatetime | None = None
    conditions: str | None = None
    display: DisplayPrice | None = None
    currency_basis: str | None = None

    @model_validator(mode="after")
    def honest_price(self) -> "PriceEvidence":
        if (self.kind == "unknown") != (self.money is None):
            raise ValueError("unknown price must have null money; known prices require money")
        for extra in (self.taxes, self.fees):
            if extra is not None and self.money is not None:
                if extra.currency != self.money.currency:
                    raise ValueError("tax/fee currency must match the quoted price currency")
        return self


class InventoryEvidence(StrictModel):
    status: Literal[
        "available",
        "unavailable",
        "request_only",
        "unknown",
        "waitlist",
        "not_offered",
        "not_on_sale",
        "sales_suspended",
    ] = "unknown"
    remaining: int | None = Field(default=None, strict=True, ge=0)
    raw_status: str | None = None

    @model_validator(mode="after")
    def consistent_inventory(self) -> "InventoryEvidence":
        if self.status == "available" and self.remaining == 0:
            raise ValueError("available inventory cannot have zero remaining")
        if self.status == "unavailable" and self.remaining not in (0, None):
            raise ValueError("unavailable inventory cannot have positive remaining")
        if self.status not in ("available", "unavailable") and self.remaining is not None:
            raise ValueError("unknown/request-only inventory cannot claim a remaining count")
        return self


class SupplierOffer(StrictModel):
    supplier: str = Field(min_length=1)
    offer_id: str | None = None
    queried_at: AwareDatetime
    sources: list[Source] = Field(min_length=1)
    price: PriceEvidence
    inventory: InventoryEvidence

    @model_validator(mode="after")
    def validity_order(self) -> "SupplierOffer":
        if self.price.valid_until is not None and (
            self.price.valid_until.astimezone(UTC) < self.queried_at.astimezone(UTC)
        ):
            raise ValueError("quote expiry cannot precede query time")
        return self


class TransportOffer(SupplierOffer):
    origin: SearchLocation
    destination: SearchLocation
    departure_at: AwareDatetime | None = None
    arrival_at: AwareDatetime | None = None
    operator: str | None = None
    service_number: str | None = None
    seat_or_cabin: str | None = None
    departure_text: str | None = None
    arrival_text: str | None = None
    time_basis: Literal["explicit_offset", "supplier_local", "unspecified"] = "unspecified"
    duration_text: str | None = None
    direct: bool | None = None
    segments: list["TransportSegment"] = Field(default_factory=list)

    @model_validator(mode="after")
    def times_in_order(self) -> "TransportOffer":
        if self.departure_at is not None and self.arrival_at is not None:
            if self.arrival_at.astimezone(UTC) <= self.departure_at.astimezone(UTC):
                raise ValueError("arrival_at must be after departure_at")
        return self


class TransportSegment(StrictModel):
    origin: str
    destination: str
    origin_code: str | None = None
    destination_code: str | None = None
    departure_text: str | None = None
    arrival_text: str | None = None
    departure_terminal: str | None = None
    arrival_terminal: str | None = None
    service_number: str | None = None
    marketing_carrier: str | None = None
    operating_carrier: str | None = None
    codeshare: bool | None = None
    seat_or_cabin: str | None = None
    stop_evidence: str | None = None


class FlightOffer(TransportOffer):
    mode: Literal["flight"] = "flight"
    stops: int | None = Field(default=None, strict=True, ge=0)
    marketing_carrier: str | None = None
    operating_carrier: str | None = None
    codeshare: bool | None = None


class SeatAvailability(StrictModel):
    seat_class: str
    price: PriceEvidence
    inventory: InventoryEvidence


class TrainOffer(TransportOffer):
    mode: Literal["train"] = "train"
    train_type: Literal["high_speed", "intercity", "conventional", "unknown"] = "unknown"
    seats: list[SeatAvailability] = Field(default_factory=list)


class CoachOffer(TransportOffer):
    mode: Literal["coach"] = "coach"
    vehicle_type: str | None = None
    product_category: Literal[
        "ordinary_coach", "airport_shuttle", "rail_shuttle", "private_car", "unknown"
    ] = "unknown"
    category_evidence: str | None = None


class HotelOffer(SupplierOffer):
    hotel_name: str = Field(min_length=1)
    hotel_id: str | None = None
    location: SearchLocation
    check_in: date
    check_out: date
    room_name: str | None = None
    cancellation_terms: str | None = None
    meal_plan: str | None = None

    @model_validator(mode="after")
    def dates_in_order(self) -> "HotelOffer":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class QueryCoverage(StrictModel):
    provider: str
    query_status: Literal["results", "no_results", "filtered_empty"]
    scanned_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    page: int | None = None
    more_pages: bool | None = None
    scope: str
    cache_hit: bool = False
    data_time: AwareDatetime
    elapsed_seconds: float = Field(ge=0)
    fallback_from: str | None = None
    fallback_reason: str | None = None


class SearchFlightsOutput(ToolPayload):
    queried_at: AwareDatetime
    offers: list[FlightOffer]
    complete: bool
    coverage: QueryCoverage | None = None


class SearchTrainsOutput(ToolPayload):
    queried_at: AwareDatetime
    offers: list[TrainOffer]
    complete: bool
    coverage: QueryCoverage | None = None


class SearchCoachesOutput(ToolPayload):
    queried_at: AwareDatetime
    offers: list[CoachOffer]
    complete: bool
    coverage: QueryCoverage | None = None


class SearchHotelsOutput(ToolPayload):
    queried_at: AwareDatetime
    offers: list[HotelOffer]
    complete: bool

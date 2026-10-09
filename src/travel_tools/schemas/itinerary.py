"""Evidence-driven consistency checks; these schemas do not plan an itinerary."""

from datetime import UTC, date
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from travel_tools.common import Source, StrictModel, ToolPayload
from travel_tools.schemas.quotes import Money

CheckStatus = Literal["pass", "fail", "unknown", "not_applicable"]
Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class TimeInterval(StrictModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> "TimeInterval":
        if self.end.astimezone(UTC) <= self.start.astimezone(UTC):
            raise ValueError("end must be after start")
        return self


class ScheduledItem(TimeInterval):
    id: Identifier
    title: str = Field(min_length=1)
    start_place_id: Identifier | None = None
    end_place_id: Identifier | None = None
    opening_hours_required: bool | None = None
    commitment_ids: list[Identifier] = Field(default_factory=list)


class TransferEvidence(StrictModel):
    from_item_id: Identifier
    to_item_id: Identifier
    minimum_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    sources: list[Source] = Field(default_factory=list)

    @model_validator(mode="after")
    def sourced_duration(self) -> "TransferEvidence":
        if self.minimum_minutes is not None and not self.sources:
            raise ValueError("a known transfer duration requires a source")
        return self


class OpeningEvidence(StrictModel):
    item_id: Identifier
    state: Literal["open_windows", "closed", "unknown"]
    windows: list[TimeInterval] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_windows(self) -> "OpeningEvidence":
        if self.state == "open_windows" and not self.windows:
            raise ValueError("open_windows requires at least one interval")
        if self.state != "open_windows" and self.windows:
            raise ValueError("only open_windows may supply opening intervals")
        if self.state != "unknown" and not self.sources:
            raise ValueError("known opening restrictions require a source")
        return self


class FixedCommitment(TimeInterval):
    id: Identifier
    title: str = Field(min_length=1)
    place_id: Identifier | None = None


class LodgingNight(StrictModel):
    night: date
    destination_id: Identifier | None = None


class LodgingStay(StrictModel):
    id: Identifier
    check_in: date
    check_out: date
    destination_id: Identifier | None = None
    hotel_id: Identifier | None = None

    @model_validator(mode="after")
    def ordered(self) -> "LodgingStay":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class PlannedCost(StrictModel):
    id: Identifier
    title: str = Field(min_length=1)
    total: Money | None = None
    kind: Literal["query_quote", "reference", "unknown"] = "unknown"
    tax_basis: Literal["included", "excluded", "partial", "unknown"] = "unknown"
    fee_basis: Literal["included", "excluded", "partial", "unknown"] = "unknown"
    sources: list[Source] = Field(default_factory=list)

    @model_validator(mode="after")
    def honest_cost(self) -> "PlannedCost":
        if (self.kind == "unknown") != (self.total is None):
            raise ValueError("unknown costs require null total; known costs require total")
        if self.total is not None and not self.sources:
            raise ValueError("known costs require a source")
        return self


class ValidateItineraryInput(StrictModel):
    planning_window: TimeInterval
    items: list[ScheduledItem] = Field(default_factory=list, max_length=500)
    transfers: list[TransferEvidence] = Field(default_factory=list, max_length=1000)
    opening_hours: list[OpeningEvidence] = Field(default_factory=list, max_length=1000)
    fixed_commitments: list[FixedCommitment] = Field(default_factory=list, max_length=500)
    fixed_commitments_complete: bool = False
    required_lodging_nights: list[LodgingNight] | None = Field(default=None, max_length=366)
    lodging: list[LodgingStay] = Field(default_factory=list, max_length=366)
    required_cost_ids: list[Identifier] | None = Field(default=None, max_length=1000)
    costs: list[PlannedCost] = Field(default_factory=list, max_length=1000)
    budget_scope: Literal["limited", "unlimited", "unknown"] = "unknown"
    budget: Money | None = None

    @model_validator(mode="after")
    def consistent_references(self) -> "ValidateItineraryInput":
        for name in ("items", "fixed_commitments", "lodging", "costs"):
            ids = [entry.id for entry in getattr(self, name)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} IDs must be unique")
        if self.required_cost_ids is not None:
            if len(self.required_cost_ids) != len(set(self.required_cost_ids)):
                raise ValueError("required_cost_ids must be unique")
        if self.required_lodging_nights is not None:
            nights = [entry.night for entry in self.required_lodging_nights]
            if len(nights) != len(set(nights)):
                raise ValueError("required lodging dates must be unique")
        item_ids = {item.id for item in self.items}
        commitment_ids = {item.id for item in self.fixed_commitments}
        for transfer in self.transfers:
            if transfer.from_item_id not in item_ids or transfer.to_item_id not in item_ids:
                raise ValueError("transfers must reference existing items")
            if transfer.from_item_id == transfer.to_item_id:
                raise ValueError("a transfer must connect different items")
        for opening in self.opening_hours:
            if opening.item_id not in item_ids:
                raise ValueError("opening evidence must reference an existing item")
        for item in self.items:
            if set(item.commitment_ids) - commitment_ids:
                raise ValueError("items must reference existing fixed commitments")
        if (self.budget_scope == "limited") != (self.budget is not None):
            raise ValueError("exactly limited budget scope requires a budget amount")
        return self


class ItineraryCheck(StrictModel):
    category: Literal[
        "time_bounds",
        "overlaps",
        "transfers",
        "opening_hours",
        "fixed_commitments",
        "lodging",
        "cost_coverage",
        "budget",
    ]
    status: CheckStatus
    message: str
    item_ids: list[Identifier] = Field(default_factory=list)


class ValidateItineraryOutput(ToolPayload):
    status: Literal["valid", "invalid", "unknown"]
    checks: list[ItineraryCheck]
    known_cost_totals: list[Money]
    scope: str = "Consistency of supplied evidence; not booking, availability, or safety assurance."

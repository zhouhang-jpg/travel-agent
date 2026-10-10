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


class UserReference(StrictModel):
    message_id: Identifier
    quote: str = Field(min_length=1, description="Exact quote from a raw user message.")


class Protection(StrictModel):
    state: Literal["locked", "user_reported_booked"]
    user_reference: UserReference


class BufferAllowance(StrictModel):
    kind: Literal["station_entry", "security", "connection", "admission", "meal", "rest", "other"]
    minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    basis: Literal["queried", "estimate", "unknown"] = Field(
        default="unknown",
        description=(
            "queried requires sources; estimate needs explanation; unknown requires null minutes."
        ),
    )
    explanation: str = Field(min_length=1)
    sources: list[Source] = Field(default_factory=list)
    source_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def honest_buffer(self):
        if self.basis == "unknown" and self.minutes is not None:
            raise ValueError("unknown buffers must have null minutes")
        if self.basis != "unknown" and self.minutes is None:
            raise ValueError("known buffers require minutes")
        if self.basis == "queried" and not (self.sources or self.source_ids):
            raise ValueError("queried buffers require sources")
        return self


class TravelDurationEvidence(BufferAllowance):
    kind: Literal["other"] = "other"


class ScheduledItem(TimeInterval):
    id: Identifier
    title: str = Field(min_length=1)
    start_place_id: Identifier | None = None
    end_place_id: Identifier | None = None
    opening_hours_required: bool | None = None
    commitment_ids: list[Identifier] = Field(
        default_factory=list,
        description="IDs of fixed commitments this item fully covers, from start to end. "
        "An attraction's opening window is not a fixed commitment.",
    )
    kind: Literal["activity", "transport", "work", "meal", "rest"] = "activity"
    description: str | None = None
    protection: Protection | None = None
    travel_duration: TravelDurationEvidence | None = Field(
        default=None,
        description="Total travel time for a transport block, queried/estimated/unknown.",
    )


class TransferEvidence(StrictModel):
    from_item_id: Identifier
    to_item_id: Identifier
    minimum_minutes: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        description="Travel minutes in the gap AFTER from_item.end and BEFORE to_item.start. "
        "The gap must cover minimum_minutes plus all buffer minutes. "
        "Travel already inside a transport item belongs in its travel_duration; "
        "do not count that journey again in this gap.",
    )
    sources: list[Source] = Field(default_factory=list)
    source_ids: list[Identifier] = Field(default_factory=list)
    duration_basis: Literal["queried", "estimate", "unknown"] = Field(
        default="queried",
        description=(
            "Queried duration requires sources. Estimates may instead use duration_explanation."
        ),
    )
    duration_explanation: str | None = None
    buffers: list[BufferAllowance] = Field(default_factory=list)
    buffers_complete: bool | None = None

    @model_validator(mode="after")
    def sourced_duration(self) -> "TransferEvidence":
        if self.minimum_minutes is not None and not (self.sources or self.source_ids):
            if self.duration_basis != "estimate" or not self.duration_explanation:
                raise ValueError(
                    "queried duration requires sources; estimate requires duration_explanation"
                )
        return self


class OpeningEvidence(StrictModel):
    item_id: Identifier
    state: Literal["open_windows", "closed", "unknown"]
    windows: list[TimeInterval] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    source_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_windows(self) -> "OpeningEvidence":
        if self.state == "open_windows" and not self.windows:
            raise ValueError("open_windows requires at least one interval")
        if self.state != "open_windows" and self.windows:
            raise ValueError("only open_windows may supply opening intervals")
        if self.state != "unknown" and not (self.sources or self.source_ids):
            raise ValueError("known opening restrictions require a source")
        return self


class FixedCommitment(TimeInterval):
    """Time that must be fully reserved, linked by an item's commitment_ids.

    Use opening_hours for an attraction's opening window, not fixed_commitments.
    """

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
    title: str | None = None
    protection: Protection | None = None

    @model_validator(mode="after")
    def ordered(self) -> "LodgingStay":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class CostBasis(StrictModel):
    travelers: int | None = Field(default=None, ge=1)
    rooms: int | None = Field(default=None, ge=1)
    nights: int | None = Field(default=None, ge=1)
    calculation: str = Field(
        min_length=1, description="Total for the entire applicable party/stay."
    )


class PlannedCost(StrictModel):
    id: Identifier
    title: str = Field(min_length=1)
    total: Money | None = Field(
        default=None,
        description="Total for all applicable travelers/rooms/nights. "
        "null requires kind=unknown; a numeric total requires query_quote/reference "
        "and at least one source or source_id. Unverified personal estimates may be "
        "explained in basis.calculation with kind=unknown and total=null.",
    )
    kind: Literal["query_quote", "reference", "unknown"] = Field(
        default="unknown",
        description="unknown requires total=null. query_quote/reference require a numeric "
        "total AND a source/source_id; a reference range without one total remains unknown. "
        "Do not invent a supplier source for a personal estimate.",
    )
    tax_basis: Literal["included", "excluded", "partial", "unknown"] = "unknown"
    fee_basis: Literal["included", "excluded", "partial", "unknown"] = "unknown"
    sources: list[Source] = Field(default_factory=list)
    source_ids: list[Identifier] = Field(default_factory=list)
    basis: CostBasis | None = None
    item_ids: list[Identifier] = Field(default_factory=list)
    lodging_ids: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def honest_cost(self) -> "PlannedCost":
        if (self.kind == "unknown") != (self.total is None):
            raise ValueError("unknown costs require null total; known costs require total")
        if self.total is not None and not (self.sources or self.source_ids):
            raise ValueError("known costs require a source")
        return self


class ValidateItineraryInput(StrictModel):
    planning_window: TimeInterval
    source_catalog: dict[Identifier, Source] = Field(
        default_factory=dict,
        description=(
            "Shared evidence sources, keyed by ID. "
            "Evidence may use source_ids instead of repeating sources."
        ),
    )
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
        for entry in [
            *self.transfers,
            *self.opening_hours,
            *self.costs,
            *(buffer for transfer in self.transfers for buffer in transfer.buffers),
            *(item.travel_duration for item in self.items if item.travel_duration is not None),
        ]:
            if any(source_id not in self.source_catalog for source_id in entry.source_ids):
                raise ValueError("source_ids must reference existing source_catalog entries")
            if entry.source_ids:
                entry.sources = [
                    *entry.sources,
                    *(self.source_catalog[source_id] for source_id in entry.source_ids),
                ]
                # Resolve once so subsequent model validation/assignment is idempotent.
                entry.source_ids = []
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

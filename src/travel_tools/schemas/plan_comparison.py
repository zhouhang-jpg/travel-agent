"""Compare supplied complete itineraries using common constraints, without model scores."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from travel_tools.common import StrictModel, ToolPayload
from travel_tools.schemas.itinerary import (
    CheckStatus,
    FixedCommitment,
    Identifier,
    LodgingNight,
    LodgingStay,
    ScheduledItem,
    TimeInterval,
    ValidateItineraryInput,
    ValidateItineraryOutput,
)
from travel_tools.schemas.quotes import Money

ComparisonObjective = Literal["total_cost", "travel_minutes", "activity_minutes"]


class ComparisonBasis(StrictModel):
    planning_window: TimeInterval
    travelers: int = Field(strict=True, ge=1)
    rooms: int | None = Field(default=None, strict=True, ge=1)
    budget_scope: Literal["limited", "unlimited", "unknown"]
    budget: Money | None = Field(
        default=None, description="Budget for the entire party and declared cost scope."
    )
    required_cost_ids: list[Identifier] | None = Field(
        default=None,
        description="Complete common cost categories, using the SAME semantic IDs in every "
        "option. null means scope unknown; [] explicitly means no required charges. "
        "All additional supplied cost lines are also counted, never discarded.",
    )
    required_lodging_nights: list[LodgingNight] | None = None
    fixed_commitments: list[FixedCommitment] = Field(default_factory=list)
    fixed_commitments_complete: bool = False
    protected_items: list[ScheduledItem] = Field(
        default_factory=list,
        description="Previously user-locked/booked items that every option must preserve exactly. "
        "This tool cannot release protections or authenticate user references.",
    )
    protected_lodging: list[LodgingStay] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_basis(self):
        if (self.budget_scope == "limited") != (self.budget is not None):
            raise ValueError("exactly limited budget scope requires a budget")
        for name in ("fixed_commitments", "protected_items", "protected_lodging"):
            entries = getattr(self, name)
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError(f"{name} IDs must be unique")
        if self.required_cost_ids is not None and len(set(self.required_cost_ids)) != len(
            self.required_cost_ids
        ):
            raise ValueError("required_cost_ids must be unique")
        if self.required_lodging_nights is not None:
            nights = [entry.night for entry in self.required_lodging_nights]
            if len(set(nights)) != len(nights):
                raise ValueError("required lodging dates must be unique")
        if any(
            entry.protection is None for entry in [*self.protected_items, *self.protected_lodging]
        ):
            raise ValueError("protected entries must carry user protection")
        return self


class ItineraryOption(StrictModel):
    id: Identifier
    label: str = Field(min_length=1)
    itinerary: ValidateItineraryInput


class CompareItineraryOptionsInput(StrictModel):
    basis: ComparisonBasis
    options: list[ItineraryOption] = Field(min_length=2, max_length=50)
    objectives: list[ComparisonObjective] = Field(
        default_factory=lambda: ["total_cost", "travel_minutes", "activity_minutes"],
        min_length=1,
        description="Minimize exact complete total_cost/travel_minutes; maximize scheduled "
        "activity_minutes. No subjective/model-written score or weighted sum is accepted.",
    )

    @model_validator(mode="after")
    def unique_ids_and_objectives(self):
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("option IDs must be unique")
        if len(set(self.objectives)) != len(self.objectives):
            raise ValueError("objectives must be unique")
        return self


class ComparisonCheck(StrictModel):
    category: Literal["party_basis", "lodging_basis", "protection", "scope", "travel", "price"]
    status: CheckStatus
    message: str
    ids: list[Identifier] = Field(default_factory=list)


class ItineraryMetrics(StrictModel):
    quoted_cost_totals: list[Money]
    reference_cost_totals: list[Money]
    unknown_cost_ids: list[Identifier]
    incomplete_charge_ids: list[Identifier]
    cost_scope_complete: bool
    exact_total_cost: Money | None = Field(
        default=None,
        description="Only complete, quoted, tax/fee-inclusive, correctly scoped single-currency "
        "costs. null is unknown, never zero. Explicit no-cost scope may yield zero in the "
        "budget currency only.",
    )
    travel_minutes: float | None = Field(
        default=None,
        ge=0,
        description="All actual transport and adjacent transfer durations "
        "are queried. Excludes buffers and does not double-count transport blocks.",
    )
    reference_travel_minutes: float | None = Field(default=None, ge=0)
    scheduled_transport_minutes: float = Field(ge=0)
    activity_minutes: float = Field(
        ge=0, description="Sum of scheduled kind=activity intervals, not a satisfaction score."
    )


class ComparedOption(StrictModel):
    id: Identifier
    label: str
    status: Literal["feasible", "conflict", "needs_review"]
    validation: ValidateItineraryOutput
    checks: list[ComparisonCheck]
    metrics: ItineraryMetrics
    pareto_eligible: bool = False
    dominated_by: list[Identifier] = Field(default_factory=list)


class ObjectiveDifference(StrictModel):
    objective: ComparisonObjective
    left_value: Decimal
    right_value: Decimal
    unit: str
    preferred: Literal["left", "right", "equal"]


class OptionTradeoff(StrictModel):
    left_id: Identifier
    right_id: Identifier
    relation: Literal[
        "dominates", "dominated", "equivalent", "tradeoff", "incomparable", "excluded"
    ]
    differences: list[ObjectiveDifference] = Field(default_factory=list)
    reason: str


class ParetoGroup(StrictModel):
    metric_units: dict[ComparisonObjective, str]
    option_ids: list[Identifier]
    pareto_option_ids: list[Identifier]


class CompareItineraryOptionsOutput(ToolPayload):
    options: list[ComparedOption]
    objectives: list[ComparisonObjective]
    pareto_option_ids: list[Identifier]
    pareto_groups: list[ParetoGroup]
    tradeoffs: list[OptionTradeoff]
    scope: str = (
        "Only the supplied alternatives and supplied evidence under the common basis are "
        "compared. Feasible means no known or unknown consistency checks, not verified booking, "
        "inventory, safety, or market optimum. Different currency groups are not ranked. "
        "Common requirements and cost completeness are caller declarations; omitted natural "
        "language requirements and provenance authenticity are not established by this tool."
    )

"""Opening-hours evidence, with no implicit date or official-source confirmation."""

from datetime import date
from typing import Literal

from pydantic import Field, HttpUrl, field_validator

from travel_tools.common import Source, StrictModel, ToolPayload
from travel_tools.schemas.places import Place, PoiId, ShortText


class GetAttractionOpeningHoursInput(StrictModel):
    attraction_name: ShortText
    city: ShortText | None = None
    place_id: PoiId | None = Field(
        default=None, description="Amap POI ID, if already resolved; check the returned identity."
    )
    visit_date: date | None = Field(
        default=None, description="Planned local visit date; omit if not yet decided."
    )
    official_urls: list[HttpUrl] = Field(
        default_factory=list,
        max_length=3,
        description="Suggested operator/official notice URLs, if known. Identity is not assumed.",
    )

    @field_validator("official_urls")
    @classmethod
    def no_url_credentials(cls, values):
        if any(value.username is not None or value.password is not None for value in values):
            raise ValueError("Source URLs must not contain credentials")
        return values


class OpeningHoursAttempt(StrictModel):
    operation: str
    status: Literal["ok", "error", "not_configured"]
    error_code: str | None = None


class MapHoursReference(StrictModel):
    place_id: str
    place_name: str
    today_raw: str | None = None
    week_raw: str | None = None
    queried_local_date: date
    source: Source
    date_confirmation: Literal["unconfirmed"] = "unconfirmed"


class OpeningHoursWebEvidence(StrictModel):
    kind: Literal["search_excerpt", "webpage"]
    purpose: Literal["general_schedule", "date_notices", "suggested_official_page"]
    url: str
    requested_url: str | None = None
    title: str | None = None
    text: str
    source: Source
    published_at_raw: str | None = None
    truncated: bool | None = None
    official_identity: Literal["caller_suggested_not_verified", "not_verified"] = "not_verified"
    content_trust: Literal["untrusted_external_data"] = "untrusted_external_data"


class GetAttractionOpeningHoursOutput(ToolPayload):
    attraction_name: str
    requested_city: str | None
    visit_date: date | None
    place_resolution: Literal[
        "provided_id", "single_name_match", "ambiguous", "not_found", "unavailable"
    ]
    candidates: list[Place] = Field(default_factory=list)
    selected_place: Place | None = None
    map_reference: MapHoursReference | None = None
    web_evidence: list[OpeningHoursWebEvidence] = Field(default_factory=list)
    attempts: list[OpeningHoursAttempt] = Field(default_factory=list)
    date_specific_status: Literal["unconfirmed"] = "unconfirmed"
    source_conflicts: Literal["not_evaluated"] = "not_evaluated"
    interpretation_required: Literal[True] = True
    review_topics: list[str] = Field(
        default_factory=lambda: [
            "exact_attraction_or_branch_identity",
            "regular_opening_intervals_and_weekly_closures",
            "last_ticket_sale_and_last_entry",
            "holiday_or_temporary_changes_and_applicable_dates",
            "official_source_identity_recency_and_conflicts",
        ]
    )

"""Search results are retrieved evidence, never booking/price guarantees."""

from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, field_validator

from travel_tools.common import Source, StrictModel, ToolPayload


class SearchWebInput(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    freshness: Literal["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"] = "noLimit"
    count: int = Field(default=10, ge=1, le=50, strict=True)
    summary: bool = True

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must contain non-whitespace text")
        return value


class WebSearchResult(StrictModel):
    title: str | None = None
    url: HttpUrl
    snippet: str | None = None
    summary: str | None = None
    site_name: str | None = None
    published_at: AwareDatetime | None = None
    published_at_raw: str | None = None
    last_crawled_raw: str | None = None
    source: Source

    @field_validator("url")
    @classmethod
    def forbid_url_credentials(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("Search URLs must not contain credentials")
        return value


class SearchWebOutput(ToolPayload):
    query: str
    results: list[WebSearchResult] = Field(max_length=50)
    total_estimated_matches: int | None = Field(default=None, ge=0)
    content_is_untrusted: Literal[True] = True

"""Shared evidence, coordinate and failure contracts."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Source(StrictModel):
    provider: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    url: str | None = None
    retrieved_at: AwareDatetime = Field(default_factory=utc_now)
    data_time: AwareDatetime | None = None
    attribution: str | None = None


class Coordinates(StrictModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    crs: Literal["wgs84", "gcj02", "bd09"]


class ToolPayload(StrictModel):
    sources: list[Source] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ToolFailure(Exception):
    """Only safe, curated messages belong here; never raw HTTP exception text."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

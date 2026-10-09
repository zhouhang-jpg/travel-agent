from typing import Literal

from pydantic import Field

from travel_tools.common import StrictModel, ToolPayload


class FetchWebpageInput(StrictModel):
    url: str = Field(min_length=8, max_length=4096)
    max_characters: int = Field(default=30000, ge=100, le=50000)


class FetchWebpageOutput(ToolPayload):
    requested_url: str
    final_url: str
    title: str | None = None
    title_truncated: bool = False
    text: str
    content_type: str
    body_bytes: int
    truncated: bool
    content_trust: Literal["untrusted_external_data"] = "untrusted_external_data"

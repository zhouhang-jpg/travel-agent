"""User-facing activity projections; raw messages and results stay private."""

import re
from urllib.parse import urlsplit

from pydantic import ValidationError

from travel_agent.tool_arguments import ArgumentJSONError, parse_arguments
from travel_tools.schemas.opening_hours import GetAttractionOpeningHoursInput
from travel_tools.schemas.places import GetPlaceDetailsInput, GetRoutesInput, SearchPlacesInput
from travel_tools.schemas.quotes import (
    SearchCoachesInput,
    SearchFlightsInput,
    SearchHotelsInput,
    SearchTrainsInput,
)
from travel_tools.schemas.weather import GetWeatherInput
from travel_tools.schemas.web_search import SearchWebInput
from travel_tools.schemas.webpage import FetchWebpageInput

ACTIVITY_TYPES = frozenset(
    {"commentary", "tool_started", "tool_progress", "tool_finished", "model_progress"}
)

_SCHEMAS = {
    "search_flights": SearchFlightsInput,
    "search_trains": SearchTrainsInput,
    "search_coaches": SearchCoachesInput,
    "search_hotels": SearchHotelsInput,
    "search_places": SearchPlacesInput,
    "get_place_details": GetPlaceDetailsInput,
    "get_routes": GetRoutesInput,
    "get_weather": GetWeatherInput,
    "get_attraction_opening_hours": GetAttractionOpeningHoursInput,
    "search_web": SearchWebInput,
    "fetch_webpage": FetchWebpageInput,
}

_SENSITIVE = re.compile(
    r"(?:api[ _-]?key|authorization|bearer|password|secret|token|sk-[a-z0-9]|"
    r"密钥|密码|凭据|https?://|[a-z0-9_-]{32,})",
    re.IGNORECASE,
)


def _text(value):
    """Project brief request labels, never arbitrary JSON or credential-looking text."""
    if not isinstance(value, str) or _SENSITIVE.search(value):
        return ""
    value = " ".join(value.split())
    return value[:80] + ("…" if len(value) > 80 else "")


def tool_summary(call):
    """Describe only validated, explicitly selected request fields; infer no results.

    Unknown tools, malformed arguments and fields outside this projection have no
    detail. This does not rewrite the original arguments or model/tool history.
    """
    name = call["function"]["name"]
    schema = _SCHEMAS.get(name)
    if schema is None:
        return ""
    try:
        arguments = parse_arguments(call["function"]["arguments"], call["id"])
        arguments = dict(arguments)
        refresh = arguments.pop("force_refresh", False)
        if not isinstance(refresh, bool):
            return ""
        query = schema.model_validate(arguments)
    except (ArgumentJSONError, ValidationError, ValueError, TypeError):
        return ""

    if name in {"search_flights", "search_trains", "search_coaches"}:
        route = " → ".join(
            part for part in (_text(query.origin.query), _text(query.destination.query)) if part
        )
        return " · ".join(part for part in (route, query.departure_date.isoformat()) if part)
    if name == "search_hotels":
        return " · ".join(
            part
            for part in (
                _text(query.destination.query),
                f"{query.check_in.isoformat()} 至 {query.check_out.isoformat()}",
                f"{query.rooms} 间房",
            )
            if part
        )
    if name == "search_places":
        return " · ".join(part for part in (_text(query.region), _text(query.keywords)) if part)
    if name == "get_place_details":
        return "地点详情"
    if name == "get_routes":
        mode = {"driving": "驾车", "walking": "步行", "bicycling": "骑行", "transit": "公共交通"}
        return mode[query.mode]
    if name == "get_weather":
        label = {
            "daily": "每日预报",
            "hourly": "逐小时预报",
            "alerts": "当前预警",
            "indices": "生活指数",
        }
        selected = query.forecast_date or query.target_time or query.start_time
        return label[query.mode] + (f" · {selected.isoformat()}" if selected else "")
    if name == "get_attraction_opening_hours":
        return " · ".join(
            part
            for part in (
                _text(query.city),
                _text(query.attraction_name),
                query.visit_date.isoformat() if query.visit_date else "",
            )
            if part
        )
    if name == "search_web":
        return _text(query.query)
    if name == "fetch_webpage":
        try:
            url = urlsplit(query.url)
            # Path, query, fragment and user-info can contain secrets. Show host only.
            return _text(url.hostname) if url.scheme in {"http", "https"} else ""
        except ValueError:
            return ""
    return ""

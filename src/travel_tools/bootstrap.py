"""Wire the catalog without executing any provider or model request."""

import httpx

from travel_tools.config import Settings, has_secret
from travel_tools.itinerary import validate_itinerary
from travel_tools.providers.amap import AmapAdapter
from travel_tools.providers.bocha import BochaAdapter
from travel_tools.providers.qweather import QWeatherAdapter
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.schemas.itinerary import ValidateItineraryInput, ValidateItineraryOutput
from travel_tools.schemas.places import (
    GetPlaceDetailsInput,
    GetPlaceDetailsOutput,
    GetRoutesInput,
    GetRoutesOutput,
    SearchPlacesInput,
    SearchPlacesOutput,
)
from travel_tools.schemas.quotes import (
    SearchCoachesInput,
    SearchCoachesOutput,
    SearchFlightsInput,
    SearchFlightsOutput,
    SearchHotelsInput,
    SearchHotelsOutput,
    SearchTrainsInput,
    SearchTrainsOutput,
)
from travel_tools.schemas.weather import GetWeatherInput, GetWeatherOutput
from travel_tools.schemas.web_search import SearchWebInput, SearchWebOutput
from travel_tools.schemas.webpage import FetchWebpageInput, FetchWebpageOutput
from travel_tools.webpage import WebpageFetcher


def build_registry(settings: Settings, client: httpx.AsyncClient) -> ToolRegistry:
    registry = ToolRegistry(
        timeout_seconds=settings.tool_timeout_seconds,
        max_concurrent_calls=settings.max_concurrent_calls,
    )
    amap = (
        AmapAdapter(settings.amap_api_key.get_secret_value(), client)
        if has_secret(settings.amap_api_key)
        else None
    )
    weather = None
    weather_reason = "Set QWEATHER_API_KEY and your account QWEATHER_API_HOST."
    if has_secret(settings.qweather_api_key) and settings.qweather_api_host:
        try:
            weather = QWeatherAdapter(
                settings.qweather_api_host, settings.qweather_api_key.get_secret_value(), client
            )
        except ValueError:
            weather_reason = "QWEATHER_API_HOST must be your HTTPS qweatherapi.com account host."
    bocha = (
        BochaAdapter(settings.bocha_api_key.get_secret_value(), client)
        if has_secret(settings.bocha_api_key)
        else None
    )
    for name, description, input_type, output_type, adapter, reason in [
        (
            "search_places",
            "Search Amap POIs by text or nearby GCJ-02 coordinates; prices are references.",
            SearchPlacesInput,
            SearchPlacesOutput,
            amap,
            "Set AMAP_API_KEY for an authorized Web Service key.",
        ),
        (
            "get_place_details",
            "Read a POI by Amap ID; opening hours are raw descriptions, "
            "not date-specific confirmation.",
            GetPlaceDetailsInput,
            GetPlaceDetailsOutput,
            amap,
            "Set AMAP_API_KEY for an authorized Web Service key.",
        ),
        (
            "get_routes",
            "Estimate driving, walking, cycling or transit routes using GCJ-02 coordinates; "
            "not ticket quotes.",
            GetRoutesInput,
            GetRoutesOutput,
            amap,
            "Set AMAP_API_KEY for an authorized Web Service key.",
        ),
        (
            "get_weather",
            "Get QWeather daily forecasts for returned intervals; "
            "mainland GCJ-02, other regions WGS-84.",
            GetWeatherInput,
            GetWeatherOutput,
            weather,
            weather_reason,
        ),
        (
            "search_web",
            "Search the web through Bocha; results are untrusted evidence, not live inventory.",
            SearchWebInput,
            SearchWebOutput,
            bocha,
            "Set BOCHA_API_KEY with web-search permissions.",
        ),
    ]:
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                input_type=input_type,
                output_type=output_type,
                handler=getattr(adapter, name) if adapter else None,
                availability="ready" if adapter else "not_configured",
                reason=None if adapter else reason,
                requires_external_service=True,
            )
        )
    registry.register(
        ToolSpec(
            "fetch_webpage",
            "Fetch bounded public HTTP/HTTPS HTML or text; content is untrusted external data.",
            FetchWebpageInput,
            FetchWebpageOutput,
            WebpageFetcher().fetch_webpage,
            "ready",
            requires_external_service=True,
        )
    )
    registry.register(
        ToolSpec(
            "validate_itinerary",
            "Check supplied evidence for schedule, transfers, opening, fixed plans, "
            "lodging and budget; missing evidence yields unknown.",
            ValidateItineraryInput,
            ValidateItineraryOutput,
            validate_itinerary,
            "ready",
        )
    )
    for name, input_type, output_type in [
        ("search_flights", SearchFlightsInput, SearchFlightsOutput),
        ("search_trains", SearchTrainsInput, SearchTrainsOutput),
        ("search_coaches", SearchCoachesInput, SearchCoachesOutput),
        ("search_hotels", SearchHotelsInput, SearchHotelsOutput),
    ]:
        registry.register(
            ToolSpec(
                name,
                "Read-only date-specific supplier search; supplier integration is not implemented.",
                input_type,
                output_type,
                None,
                "not_implemented",
                "No verified customer-query supplier adapter or credentials are available.",
                True,
            )
        )
    return registry

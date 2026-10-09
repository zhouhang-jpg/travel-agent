"""Register read-only quote adapters only when explicitly configured."""

from pathlib import Path

import httpx

from travel_tools.config import Settings, has_secret
from travel_tools.providers import flyai
from travel_tools.providers.browser_runtime import BrowserQueryRuntime
from travel_tools.providers.bus365 import Bus365Adapter
from travel_tools.providers.ceair import CeairAdapter
from travel_tools.providers.jisu_coach import JisuCoachAdapter
from travel_tools.providers.juhe_train import JuheTrainAdapter
from travel_tools.providers.rail12306 import Rail12306Adapter
from travel_tools.providers.ticket_routing import FlightRouter, TrainRouter
from travel_tools.registry import ToolRegistry, ToolSpec
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

_FLYAI_LIMITS = (
    "FlyAI supports one adult, location names only (no IDs or coordinates), and no explicit "
    "quote currency. Results are limited and incomplete; prices may be masked, and taxes, "
    "fees, price scope and inventory are unverified. No booking or full quote is provided."
)


def _flyai_client(settings: Settings, timeout: float) -> tuple[flyai.FlyAIClient | None, str]:
    enabled = has_secret(settings.flyai_api_key) or settings.flyai_enable_demo
    if not enabled:
        return None, "Set FLYAI_API_KEY or explicitly set FLYAI_ENABLE_DEMO=true."
    if not settings.flyai_node_path or not settings.flyai_cli_path:
        return None, "Set FLYAI_NODE_PATH and FLYAI_CLI_PATH to existing local files."
    try:
        paths = (
            Path(settings.flyai_node_path),
            Path(settings.flyai_cli_path),
            Path(flyai.__file__).with_name("flyai_cli_guard.cjs"),
        )
        if not all(path.is_file() for path in paths):
            return None, "FlyAI requires existing Node, CLI and bundled CLI guard files."
        adapter_client = flyai.FlyAIClient(
            settings.flyai_node_path,
            settings.flyai_cli_path,
            settings.flyai_state_directory,
            api_key=(
                settings.flyai_api_key.get_secret_value().strip()
                if has_secret(settings.flyai_api_key)
                else None
            ),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None, "FlyAI local paths or execution settings are invalid."
    return adapter_client, ""


def register_quote_tools(
    registry: ToolRegistry, settings: Settings, client: httpx.AsyncClient
) -> None:
    """Wire adapters without installing software, executing the CLI or sending requests."""
    flyai_client, flyai_reason = _flyai_client(settings, registry.timeout_seconds)
    flight_adapters = {"flyai": flyai.FlyAIFlightAdapter(flyai_client)} if flyai_client else {}
    hotels = flyai.FlyAIHotelAdapter(flyai_client) if flyai_client else None
    train_adapters = {"flyai": flyai.FlyAITrainAdapter(flyai_client)} if flyai_client else {}
    runtime = None
    if getattr(settings, "browser_queries_enabled", False):
        runtime = BrowserQueryRuntime(
            timeout=min(
                getattr(settings, "browser_query_timeout_seconds", 18),
                registry.timeout_seconds * 0.8,
            ),
            cache_seconds=getattr(settings, "browser_query_cache_seconds", 0),
        )
        registry.add_closer(runtime.close)
        train_adapters["12306"] = Rail12306Adapter(runtime, client)
        flight_adapters["ceair"] = CeairAdapter(runtime)
    flights = FlightRouter(flight_adapters) if flight_adapters else None
    if flights and "flyai" in flight_adapters:
        flights.description += " " + _FLYAI_LIMITS
    train_reason = "Configure the selected FlyAI railway provider: " + flyai_reason
    if settings.train_search_provider == "juhe":
        train_reason = "Set JUHE_TRAIN_API_KEY for the selected Juhe railway provider."
    if has_secret(settings.juhe_train_api_key):
        train_adapters["juhe"] = JuheTrainAdapter(
            settings.juhe_train_api_key.get_secret_value(), client
        )
    primary = settings.train_search_provider
    fallback = getattr(settings, "train_fallback_provider", "none")
    trains = (
        TrainRouter(train_adapters, primary, fallback)
        if primary in train_adapters or fallback in train_adapters
        else None
    )
    if trains and primary == "juhe":
        trains.description += " " + JuheTrainAdapter.description
    if primary == "12306":
        train_reason = "Enable BROWSER_QUERIES_ENABLED and install Playwright Chromium for 12306."
    jisu = (
        JisuCoachAdapter(settings.jisu_coach_api_key.get_secret_value(), client)
        if has_secret(settings.jisu_coach_api_key)
        else None
    )
    coach_provider = getattr(settings, "coach_search_provider", "jisu")
    coaches = (
        Bus365Adapter(runtime)
        if coach_provider == "bus365" and runtime
        else jisu
        if coach_provider == "jisu"
        else None
    )

    for name, input_type, output_type, adapter, description, reason in [
        (
            "search_flights",
            SearchFlightsInput,
            SearchFlightsOutput,
            flights,
            "Search flight candidates. " + _FLYAI_LIMITS,
            flyai_reason,
        ),
        (
            "search_trains",
            SearchTrainsInput,
            SearchTrainsOutput,
            trains,
            "Search railway candidates. " + _FLYAI_LIMITS,
            train_reason,
        ),
        (
            "search_coaches",
            SearchCoachesInput,
            SearchCoachesOutput,
            coaches,
            JisuCoachAdapter.description,
            "Enable BROWSER_QUERIES_ENABLED with Playwright Chromium for Bus365; "
            "or explicitly select coach_search_provider=jisu with JISU_COACH_API_KEY.",
        ),
        (
            "search_hotels",
            SearchHotelsInput,
            SearchHotelsOutput,
            hotels,
            "Search hotel candidates for one room only; multiple rooms and verified occupancy "
            "pricing are unsupported. " + _FLYAI_LIMITS,
            flyai_reason,
        ),
    ]:
        registry.register(
            ToolSpec(
                name=name,
                description=getattr(adapter, "description", description),
                input_type=input_type,
                output_type=output_type,
                handler=adapter.search if adapter is not None else None,
                availability="ready" if adapter is not None else "not_configured",
                reason=None if adapter is not None else reason,
                requires_external_service=True,
            )
        )

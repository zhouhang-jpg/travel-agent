"""Wire the catalog without executing any provider or model request."""

from hashlib import sha256

import httpx

from travel_tools.config import Settings, has_secret
from travel_tools.itinerary import validate_itinerary
from travel_tools.opening_hours import OpeningHoursLookup
from travel_tools.providers.amap import AmapAdapter
from travel_tools.providers.bocha import BochaAdapter
from travel_tools.providers.qweather import QWeatherAdapter
from travel_tools.quote_registry import register_quote_tools
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.scheduling import SupplierPolicy, SupplierScheduler
from travel_tools.schemas.itinerary import ValidateItineraryInput, ValidateItineraryOutput
from travel_tools.schemas.opening_hours import (
    GetAttractionOpeningHoursInput,
    GetAttractionOpeningHoursOutput,
)
from travel_tools.schemas.places import (
    GetPlaceDetailsInput,
    GetPlaceDetailsOutput,
    GetRoutesInput,
    GetRoutesOutput,
    SearchPlacesInput,
    SearchPlacesOutput,
)
from travel_tools.schemas.weather import GetWeatherInput, GetWeatherOutput
from travel_tools.schemas.web_search import SearchWebInput, SearchWebOutput
from travel_tools.schemas.webpage import FetchWebpageInput, FetchWebpageOutput
from travel_tools.webpage import WebpageFetcher


def build_registry(settings: Settings, client: httpx.AsyncClient) -> ToolRegistry:
    registry = ToolRegistry(
        timeout_seconds=settings.tool_timeout_seconds,
        max_concurrent_calls=settings.max_concurrent_calls,
        scheduler=SupplierScheduler(
            {
                name: SupplierPolicy(limit.concurrency, limit.requests_per_second)
                for name, limit in settings.supplier_limits.items()
            }
        ),
        cache_scope=sha256(
            (
                repr(settings)
                + repr(
                    {
                        name: value.get_secret_value()
                        for name, value in settings
                        if hasattr(value, "get_secret_value")
                    }
                )
            ).encode()
        ).hexdigest(),
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
            "查询和风天气，mode=daily（默认1–10天，可用forecast_date选日）；"
            "mode=hourly按带UTC偏移的target_time或[start_time,end_time)查询具体时刻/时段。"
            "hours可指定1–240，未指定时按目标自动请求必要小时数。非整点只返回邻近时次参考，"
            "超覆盖或缺小时明确标记，不插值、不用每日预报冒充小时天气。"
            "mode=alerts只查当前官方预警，不预测未来是否安全；mode=indices查1/3天每日指数，"
            "默认运动/穿衣/UV/旅游，可指定index_types，非中国仅1–5。"
            "保留实际时间、单位、缺失、归因和数据边界；大陆GCJ-02，其他WGS-84。按需选模式，不必全部查。",
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
                execution="parallel_read",
                cache_seconds=(
                    settings.cache_weather_seconds
                    if name == "get_weather"
                    else settings.cache_routes_seconds
                    if name == "get_routes"
                    else settings.cache_web_seconds
                    if name == "search_web"
                    else settings.cache_places_seconds
                ),
            )
        )
    registry.register(
        ToolSpec(
            "get_attraction_opening_hours",
            "查询景点或场馆常规营业描述和计划日期相关公告证据；"
            "优先核实官方来源，区分闭馆日、停止售票/入园及临时调整。"
            "返回候选、原文、来源和缺失/失败状态；必须核对场馆身份、日期及来源冲突。"
            "地图描述、日期搜索命中或没找到闭馆公告都不证明当天开放。"
            "已知官方页面可传 official_urls；无搜索/地图配置时仍可读取这些页面。",
            GetAttractionOpeningHoursInput,
            GetAttractionOpeningHoursOutput,
            OpeningHoursLookup(amap, bocha).get_attraction_opening_hours,
            "ready",
            requires_external_service=True,
            execution="parallel_read",
            cache_seconds=settings.cache_web_seconds,
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
            execution="parallel_read",
            cache_seconds=settings.cache_web_seconds,
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
            execution="parallel_read",
        )
    )
    register_quote_tools(registry, settings, client)
    return registry

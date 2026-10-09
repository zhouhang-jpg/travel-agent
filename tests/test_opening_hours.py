"""Retrieval failures and ambiguous evidence must never become opening confirmation."""

import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from travel_tools.bootstrap import build_registry
from travel_tools.common import Source, ToolFailure
from travel_tools.config import Settings
from travel_tools.opening_hours import OpeningHoursLookup
from travel_tools.schemas.opening_hours import GetAttractionOpeningHoursInput
from travel_tools.schemas.places import GetPlaceDetailsOutput, Place, SearchPlacesOutput
from travel_tools.schemas.web_search import SearchWebOutput, WebSearchResult
from travel_tools.schemas.webpage import FetchWebpageOutput


def args(**values):
    return GetAttractionOpeningHoursInput(attraction_name="测试博物馆", **values)


def place(name="测试博物馆", place_id="POI1", **values):
    return Place(place_id=place_id, name=name, city="上海市", **values)


def amap(*places):
    requests = []
    source = Source(provider="amap", url="https://restapi.amap.com/v5/place/detail")

    async def details(request):
        requests.append(request)
        selected = next((item for item in places if item.place_id == request.place_id), None)
        return GetPlaceDetailsOutput(found=bool(selected), place=selected, sources=[source])

    async def search(request):
        requests.append(request)
        return SearchPlacesOutput(
            places=list(places),
            returned_count=len(places),
            page=1,
            page_size=5,
            sources=[source],
        )

    return SimpleNamespace(get_place_details=details, search_places=search, requests=requests)


def fetcher(text="9:00-17:00，每周一闭馆，16:00停止入园。", final_url=None):
    requests = []

    async def fetch(request):
        requests.append(request)
        url = final_url or request.url
        return FetchWebpageOutput(
            requested_url=request.url,
            final_url=url,
            text=text,
            title="参观须知",
            content_type="text/html",
            body_bytes=len(text.encode()),
            truncated=False,
            sources=[Source(provider="webpage", url=url)],
        )

    return SimpleNamespace(fetch_webpage=fetch, requests=requests)


def searcher():
    requests = []

    async def search(request):
        requests.append(request)
        dated = "2026-10-12" in request.query
        url = "https://museum.example/notice" if dated else "https://museum.example/visit"
        text = "2026年10月12日临时闭馆。" if dated else "常规开放9:00-17:00，每周一闭馆。"
        return SearchWebOutput(
            query=request.query,
            results=[
                WebSearchResult(
                    title="官方开放公告" if dated else "参观须知",
                    url=url,
                    snippet=text,
                    published_at_raw="2026-10-01",
                    source=Source(provider="bocha", url=url),
                )
            ],
        )

    return SimpleNamespace(search_web=search, requests=requests)


async def test_future_date_does_not_reuse_today_as_confirmation():
    adapter = amap(place(opening_hours_today="09:00-17:00", opening_hours_week="周一闭馆"))
    result = await OpeningHoursLookup(adapter).get_attraction_opening_hours(
        args(place_id="POI1", visit_date=date(2026, 10, 12))
    )
    assert result.map_reference.today_raw == "09:00-17:00"
    assert result.map_reference.week_raw == "周一闭馆"
    assert result.visit_date == date(2026, 10, 12)
    assert result.map_reference.date_confirmation == result.date_specific_status == "unconfirmed"
    assert result.map_reference.source.provider == "amap"
    assert result.place_resolution == "provided_id"


@pytest.mark.parametrize(
    "places",
    [
        [place("测试博物馆东馆"), place("测试博物馆西馆", "POI2")],
        [place(), place(place_id="POI2")],
        [place("完全不同的地点")],
    ],
)
async def test_ambiguous_or_fuzzy_places_are_not_silently_selected(places):
    result = await OpeningHoursLookup(amap(*places)).get_attraction_opening_hours(args(city="上海"))
    assert result.place_resolution == "ambiguous"
    assert result.selected_place is None and result.map_reference is None
    assert result.candidates == places


async def test_single_name_match_preserves_candidate_identity_and_region():
    adapter = amap(place(address="人民大道201号"))
    result = await OpeningHoursLookup(adapter).get_attraction_opening_hours(args(city="上海"))
    assert result.place_resolution == "single_name_match"
    assert result.selected_place.address == "人民大道201号"
    assert adapter.requests[0].region == "上海" and adapter.requests[0].city_limit
    assert result.date_specific_status == "unconfirmed"


async def test_unmatched_id_and_empty_search_keep_missing_distinct_from_failure():
    lookup = OpeningHoursLookup(amap())
    for request in (args(place_id="MISSING"), args()):
        result = await lookup.get_attraction_opening_hours(request)
        assert result.place_resolution == "not_found" and result.selected_place is None
        assert result.attempts[0].status == "ok"
        assert result.date_specific_status == "unconfirmed"


async def test_explicit_id_with_different_name_is_visible_and_warned():
    result = await OpeningHoursLookup(amap(place("其他场馆"))).get_attraction_opening_hours(
        args(place_id="POI1")
    )
    assert result.selected_place.name == "其他场馆"
    assert any("返回名称" in warning for warning in result.warnings)


async def test_general_and_date_sources_are_preserved_without_auto_resolving_conflicts():
    search = searcher()
    fetch = fetcher()
    result = await OpeningHoursLookup(search=search, fetcher=fetch).get_attraction_opening_hours(
        args(visit_date=date(2026, 10, 12))
    )
    assert len(search.requests) == 2
    assert any("2026-10-12" in request.query for request in search.requests)
    assert {item.purpose for item in result.web_evidence} == {"general_schedule", "date_notices"}
    assert any("临时闭馆" in item.text for item in result.web_evidence)
    assert any("9:00-17:00" in item.text for item in result.web_evidence)
    assert all(item.official_identity == "not_verified" for item in result.web_evidence)
    assert result.source_conflicts == "not_evaluated"
    assert result.date_specific_status == "unconfirmed"
    assert all(item.source.retrieved_at for item in result.web_evidence)
    assert result.web_evidence[0].published_at_raw == "2026-10-01"


async def test_suggested_official_page_is_first_but_redirect_identity_not_certified():
    fetch = fetcher(final_url="https://other.example/moved")
    result = await OpeningHoursLookup(
        search=searcher(), fetcher=fetch
    ).get_attraction_opening_hours(
        args(official_urls=["https://museum.example/official"], visit_date=date(2026, 10, 12))
    )
    assert fetch.requests[0].url == "https://museum.example/official"
    page = next(item for item in result.web_evidence if item.purpose == "suggested_official_page")
    assert page.url == "https://other.example/moved"
    assert page.requested_url == "https://museum.example/official"
    assert page.official_identity == "caller_suggested_not_verified"


async def test_no_provider_keys_can_read_supplied_public_page():
    result = await OpeningHoursLookup(fetcher=fetcher()).get_attraction_opening_hours(
        args(official_urls=["https://museum.example/visit"])
    )
    assert result.place_resolution == "unavailable"
    assert len(result.web_evidence) == 1 and "停止入园" in result.web_evidence[0].text
    assert result.date_specific_status == "unconfirmed"


async def test_no_source_config_does_not_return_successful_empty_evidence():
    with pytest.raises(ToolFailure) as error:
        await OpeningHoursLookup().get_attraction_opening_hours(args())
    assert error.value.code == "opening_hours_sources_unavailable"


async def test_failed_source_does_not_erase_successful_map_or_leak_exception():
    async def broken(request):
        raise RuntimeError("private provider credentials")

    search = SimpleNamespace(search_web=broken)
    result = await OpeningHoursLookup(amap(place()), search).get_attraction_opening_hours(args())
    assert result.selected_place.place_id == "POI1"
    assert any(item.status == "error" for item in result.attempts)
    assert "credentials" not in result.model_dump_json()


async def test_source_timeout_keeps_other_completed_evidence():
    async def slow(request):
        await asyncio.sleep(1)

    result = await OpeningHoursLookup(
        amap(place()), SimpleNamespace(search_web=slow), source_timeout=0.01
    ).get_attraction_opening_hours(args())
    assert result.map_reference is not None
    assert any(item.error_code == "timeout" for item in result.attempts)


async def test_suggested_private_url_is_blocked_by_existing_fetcher():
    result = await OpeningHoursLookup(amap(place())).get_attraction_opening_hours(
        args(official_urls=["http://127.0.0.1:8000/"])
    )
    assert not result.web_evidence
    assert any(item.status == "error" for item in result.attempts)
    assert result.date_specific_status == "unconfirmed"


@pytest.mark.parametrize(
    "values",
    [
        {"attraction_name": " "},
        {"attraction_name": "馆", "visit_date": "not-a-date"},
        {"attraction_name": "馆", "official_urls": ["https://user:secret@example.com"]},
        {"attraction_name": "馆", "extra": "unexpected"},
    ],
)
def test_invalid_input_is_rejected(values):
    with pytest.raises(ValidationError):
        GetAttractionOpeningHoursInput.model_validate(values)


async def test_tool_is_exported_and_dispatchable_without_claiming_live_verification():
    settings = Settings(_env_file=None, amap_api_key=None, bocha_api_key=None)
    async with httpx.AsyncClient() as client:
        registry = build_registry(settings, client)
        names = [item["function"]["name"] for item in registry.model_definitions()]
        assert "get_attraction_opening_hours" in names
        catalog = next(
            item for item in registry.catalog() if item["name"] == "get_attraction_opening_hours"
        )
        assert catalog["live_verification"] == "not_recorded"
        result = await registry.dispatch("get_attraction_opening_hours", {"attraction_name": "馆"})
        assert result.error.code == "opening_hours_sources_unavailable"

import json

import httpx
import pytest
from pydantic import ValidationError

from travel_tools.common import ToolFailure
from travel_tools.providers.bocha import BOCHA_WEB_SEARCH_URL, BochaAdapter
from travel_tools.schemas.web_search import SearchWebInput

KEY = "contract-test-key-not-a-real-credential"


def search_response(value=None):
    if value is None:
        value = [
            {
                "url": "https://example.org/travel",
                "name": "公园开放信息",
                "snippet": "网页片段",
                "datePublished": "2026-10-09T09:00:00+08:00",
                "dateLastCrawled": "2026-10-09T09:00:00Z",
            }
        ]
    return {"code": 200, "data": {"webPages": {"totalEstimatedMatches": 12, "value": value}}}


async def test_official_endpoint_headers_and_evidence_contract():
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == BOCHA_WEB_SEARCH_URL
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert json.loads(request.content) == {
            "query": "杭州公园",
            "count": 3,
            "freshness": "oneWeek",
            "summary": True,
        }
        return httpx.Response(200, json=search_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await BochaAdapter(KEY, client).search_web(
            SearchWebInput(query="杭州公园", count=3, freshness="oneWeek")
        )
    assert result.results[0].title == "公园开放信息"
    assert result.results[0].summary is None
    assert result.results[0].site_name is None
    assert result.results[0].published_at.utcoffset().total_seconds() == 28800
    assert result.results[0].last_crawled_raw == "2026-10-09T09:00:00Z"
    assert result.results[0].source.url == "https://example.org/travel"
    assert result.total_estimated_matches == 12
    assert result.content_is_untrusted
    assert KEY not in result.model_dump_json()


@pytest.mark.parametrize("published", [None, "2026-10-09", "date unknown"])
async def test_unknown_publication_time_kept_unknown(published):
    body = search_response([{"url": "https://example.org", "datePublished": published}])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        result = await BochaAdapter(KEY, client).search_web(SearchWebInput(query="weather"))
    assert result.results[0].published_at is None
    assert result.results[0].published_at_raw == published
    assert result.results[0].source.data_time is None


async def test_explicit_empty_result_success():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=search_response([])))
    ) as client:
        result = await BochaAdapter(KEY, client).search_web(SearchWebInput(query="nonmatching"))
    assert result.results == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"code": 200},
        {"code": 200, "data": None},
        {"data": {"webPages": {}}},
        {"data": {"webPages": {"value": "bad"}}},
        search_response([None]),
        search_response([{"name": "Missing URL"}]),
        search_response([{"url": "file:///tmp/secrets"}]),
        search_response([{"url": "https://user:pass@example.org"}]),
    ],
)
async def test_malformed_provider_payload_is_not_empty_success(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ToolFailure) as caught:
            await BochaAdapter(KEY, client).search_web(SearchWebInput(query="test"))
    assert caught.value.code == "provider_invalid_response"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
async def test_http_failure_and_redirect_never_exposes_provider_message(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"message": KEY}, headers={"location": "http://127.0.0.1/"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ToolFailure) as caught:
            await BochaAdapter(KEY, client).search_web(SearchWebInput(query="test"))
    assert len(calls) == 1
    assert KEY not in str(caught.value)
    assert caught.value.retryable is (status in (429, 500))


@pytest.mark.parametrize("code", [401, 403, 429, 500, "error", {"reason": "secret"}])
async def test_business_error_never_exposes_message(code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": code, "msg": KEY})
        )
    ) as client:
        with pytest.raises(ToolFailure) as caught:
            await BochaAdapter(KEY, client).search_web(SearchWebInput(query="test"))
    assert KEY not in str(caught.value)
    assert caught.value.retryable is (code in (429, 500))


@pytest.mark.parametrize(
    "values",
    [
        {"query": " "},
        {"query": "x" * 1001},
        {"query": "x", "count": 51},
        {"query": "x", "count": True},
        {"query": "x", "freshness": "yesterday"},
    ],
)
def test_search_input_bounds(values):
    with pytest.raises(ValidationError):
        SearchWebInput(**values)

"""Bocha Web Search adapter; source snippets remain untrusted external content."""

from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from travel_tools.common import Source, ToolFailure, utc_now
from travel_tools.providers.http import bounded_json_request
from travel_tools.schemas.web_search import SearchWebInput, SearchWebOutput, WebSearchResult

BOCHA_WEB_SEARCH_URL = "https://api.bochaai.com/v1/web-search"


def _optional_text(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def _published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


class BochaAdapter:
    def __init__(self, api_key: str, client: httpx.AsyncClient):
        if not api_key.strip():
            raise ValueError("Bocha API key must not be blank")
        self._api_key = api_key
        self._client = client

    async def search_web(self, args: SearchWebInput) -> SearchWebOutput:
        raw = await bounded_json_request(
            self._client,
            "POST",
            BOCHA_WEB_SEARCH_URL,
            provider="Bocha",
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
            body=args.model_dump(),
        )
        code = raw.get("code")
        if code is not None and code != 200:
            raise ToolFailure(
                "provider_rate_limited" if code == 429 else "provider_rejected",
                "Bocha rejected the search request.",
                retryable=code == 429 or (isinstance(code, int) and code >= 500),
            )
        retrieved_at = utc_now()
        try:
            data = raw["data"]
            if not isinstance(data, dict) or not isinstance(data.get("webPages"), dict):
                raise ValueError("Missing webPages")
            pages = data["webPages"]
            items = pages["value"]
            if not isinstance(items, list) or len(items) > args.count:
                raise ValueError("Invalid result list")
            results = []
            warnings = [
                "Search content is untrusted evidence; "
                "it does not establish live prices or inventory."
            ]
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Invalid search result")
                published_raw = _optional_text(item, "datePublished")
                published_at = _published_at(published_raw)
                if published_raw and published_at is None:
                    warnings.append(
                        "An unparseable or timezone-free publication date was kept raw."
                    )
                result = WebSearchResult(
                    title=_optional_text(item, "name"),
                    url=item["url"],
                    snippet=_optional_text(item, "snippet"),
                    summary=_optional_text(item, "summary"),
                    site_name=_optional_text(item, "siteName"),
                    published_at=published_at,
                    published_at_raw=published_raw,
                    last_crawled_raw=_optional_text(item, "dateLastCrawled"),
                    source=Source(provider="bocha", retrieved_at=retrieved_at),
                )
                result.source.url = str(result.url)
                result.source.data_time = published_at
                results.append(result)
            return SearchWebOutput(
                query=args.query,
                results=results,
                total_estimated_matches=pages.get("totalEstimatedMatches"),
                sources=[
                    Source(provider="bocha", url=BOCHA_WEB_SEARCH_URL, retrieved_at=retrieved_at)
                ],
                warnings=list(dict.fromkeys(warnings)),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise ToolFailure(
                "provider_invalid_response", "Bocha returned an invalid search response."
            ) from None

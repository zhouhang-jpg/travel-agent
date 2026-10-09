"""Retrieve map descriptions and notice evidence without inventing opening rules.

The caller/model interprets source text, official identity, date applicability and
conflicts. Querying for a date never turns a search hit into date confirmation.
"""

import asyncio
import re
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from travel_tools.common import Source, ToolFailure
from travel_tools.schemas.opening_hours import (
    GetAttractionOpeningHoursInput,
    GetAttractionOpeningHoursOutput,
    MapHoursReference,
    OpeningHoursAttempt,
    OpeningHoursWebEvidence,
)
from travel_tools.schemas.places import GetPlaceDetailsInput, SearchPlacesInput
from travel_tools.schemas.web_search import SearchWebInput
from travel_tools.schemas.webpage import FetchWebpageInput
from travel_tools.webpage import WebpageFetcher


def _normalized_name(value: str) -> str:
    return re.sub(r"[\s()（）]", "", value)


def _official_hint(item: OpeningHoursWebEvidence) -> bool:
    # A ranking hint only: even a government page may describe another branch,
    # and a title claiming to be official is not proof of official identity.
    host = (urlsplit(item.url).hostname or "").lower()
    return host.endswith(".gov.cn") or any(
        word in (item.title or "") for word in ("官网", "官方", "开放公告", "参观须知")
    )


class OpeningHoursLookup:
    def __init__(self, amap=None, search=None, fetcher=None, *, source_timeout: float = 8):
        self.amap = amap
        self.search = search
        self.fetcher = fetcher or WebpageFetcher()
        self.source_timeout = source_timeout

    async def _attempt(self, operation, request):
        try:
            async with asyncio.timeout(self.source_timeout):
                result = await request()
            return result, OpeningHoursAttempt(operation=operation, status="ok")
        except ToolFailure as exc:
            code = exc.code
        except TimeoutError:
            code = "timeout"
        except Exception:
            code = "source_failed"
        return None, OpeningHoursAttempt(operation=operation, status="error", error_code=code)

    async def _map(self, args):
        if self.amap is None:
            return (
                "unavailable",
                [],
                None,
                [],
                [OpeningHoursAttempt(operation="amap", status="not_configured")],
            )
        if args.place_id:
            data, attempt = await self._attempt(
                "amap_details",
                lambda: self.amap.get_place_details(GetPlaceDetailsInput(place_id=args.place_id)),
            )
            if data is None:
                return "unavailable", [], None, [], [attempt]
            return (
                "provided_id" if data.found else "not_found",
                [data.place] if data.place else [],
                data.place,
                data.sources,
                [attempt],
            )
        data, attempt = await self._attempt(
            "amap_search",
            lambda: self.amap.search_places(
                SearchPlacesInput(
                    keywords=args.attraction_name,
                    region=args.city,
                    city_limit=bool(args.city),
                    page_size=5,
                )
            ),
        )
        if data is None:
            return "unavailable", [], None, [], [attempt]
        # Never pick the first fuzzy result, or silently merge different branches.
        matches = [
            place
            for place in data.places
            if _normalized_name(place.name) == _normalized_name(args.attraction_name)
        ]
        selected = matches[0] if len(matches) == 1 else None
        state = "single_name_match" if selected else "ambiguous" if data.places else "not_found"
        return state, data.places, selected, data.sources, [attempt]

    async def _search(self, args, purpose):
        if self.search is None:
            return [], [], OpeningHoursAttempt(operation=purpose, status="not_configured")
        identity = " ".join(value for value in (args.city, args.attraction_name) if value)
        query = identity + " 官网 开放时间 每周闭馆 停止售票 停止入园"
        if purpose == "date_notices":
            query = (
                f"{identity} {args.visit_date.isoformat()} "
                f"{args.visit_date.year}年{args.visit_date.month}月{args.visit_date.day}日 "
                "官方公告 开放调整 临时闭馆 节假日"
            )
        if args.official_urls:
            hosts = list(dict.fromkeys(url.host for url in args.official_urls))
            query += " (" + " OR ".join("site:" + host for host in hosts) + ")"
        data, attempt = await self._attempt(
            purpose, lambda: self.search.search_web(SearchWebInput(query=query, count=4))
        )
        if data is None:
            return [], [], attempt
        evidence = [
            OpeningHoursWebEvidence(
                kind="search_excerpt",
                purpose=purpose,
                url=str(item.url),
                title=item.title,
                text="\n".join(value for value in (item.snippet, item.summary) if value),
                source=item.source,
                published_at_raw=item.published_at_raw,
            )
            for item in data.results
        ]
        return evidence, data.sources, attempt

    async def get_attraction_opening_hours(self, args: GetAttractionOpeningHoursInput):
        tasks = [self._map(args), self._search(args, "general_schedule")]
        if args.visit_date:
            tasks.append(self._search(args, "date_notices"))
        results = await asyncio.gather(*tasks)
        state, candidates, selected, sources, attempts = results[0]
        sources = list(sources)
        attempts = list(attempts)
        evidence = []
        for found, found_sources, attempt in results[1:]:
            evidence.extend(found)
            sources.extend(found_sources)
            attempts.append(attempt)
        pages = []
        seen = set()
        # Caller-suggested official pages come first, followed by one result from
        # each search purpose, preferring official hints without certifying them.
        suggested = [
            (str(url), "suggested_official_page", "caller_suggested_not_verified")
            for url in args.official_urls
        ]
        ranked = []
        remaining = []
        for purpose in ("date_notices", "general_schedule"):
            found = [item for item in evidence if item.purpose == purpose]
            suggested_hosts = {url.host for url in args.official_urls}
            found.sort(
                key=lambda item: (
                    urlsplit(item.url).hostname in suggested_hosts,
                    _official_hint(item),
                ),
                reverse=True,
            )
            ranked.extend((item.url, item.purpose, "not_verified") for item in found[:1])
            remaining.extend((item.url, item.purpose, "not_verified") for item in found[1:])
        for url, purpose, identity in [*suggested, *ranked, *remaining]:
            if url in seen:
                continue
            seen.add(url)
            pages.append((url, purpose, identity))
            if len(pages) == 3:
                break

        async def fetch(url, purpose, identity):
            data, attempt = await self._attempt(
                "fetch_" + purpose,
                lambda: self.fetcher.fetch_webpage(FetchWebpageInput(url=url)),
            )
            if data is None:
                return None, [], attempt
            source = (
                data.sources[0] if data.sources else Source(provider="webpage", url=data.final_url)
            )
            return (
                OpeningHoursWebEvidence(
                    kind="webpage",
                    purpose=purpose,
                    url=data.final_url,
                    requested_url=url,
                    title=data.title,
                    text=data.text,
                    source=source,
                    truncated=data.truncated,
                    official_identity=identity,
                ),
                data.sources,
                attempt,
            )

        for item, found_sources, attempt in await asyncio.gather(*(fetch(*page) for page in pages)):
            if item:
                evidence.append(item)
            sources.extend(found_sources)
            attempts.append(attempt)
        if not any(attempt.status == "ok" for attempt in attempts):
            raise ToolFailure(
                "opening_hours_sources_unavailable",
                "No opening-hours source could be queried; check configuration or source access.",
                retryable=any(attempt.status == "error" for attempt in attempts),
            )
        warnings = [
            "优先核实景区或场馆官方页面身份及适用日期；官方字样或调用方提供的URL不构成验证。",
            "地图today/week原文仅为参考，不证明计划日期开放；查询时间不等于公告发布时间。",
            "日期定向搜索结果不等于当天确认，未找到当天公告不代表开放。",
            "需结合原文判断常规时段、闭馆日、停止售票/入园、节假日及临时调整，并报告冲突。",
        ]
        if state == "ambiguous":
            warnings.append("地点未消歧，候选可能属于不同场馆；不得拼接成同一景点的开放规则。")
        if any(attempt.status == "error" for attempt in attempts):
            warnings.append("部分来源查询失败，已保留其他证据；失败与缺失不构成开放确认。")
        if selected and _normalized_name(selected.name) != _normalized_name(args.attraction_name):
            warnings.append("指定POI的返回名称与请求不同，请核对实际场馆及地址。")
        reference = None
        if selected and sources:
            map_source = next((source for source in sources if source.provider == "amap"), None)
            if map_source:
                reference = MapHoursReference(
                    place_id=selected.place_id,
                    place_name=selected.name,
                    today_raw=selected.opening_hours_today,
                    week_raw=selected.opening_hours_week,
                    queried_local_date=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                    source=map_source,
                )
        return GetAttractionOpeningHoursOutput(
            attraction_name=args.attraction_name,
            requested_city=args.city,
            visit_date=args.visit_date,
            place_resolution=state,
            candidates=candidates,
            selected_place=selected,
            map_reference=reference,
            web_evidence=evidence,
            attempts=attempts,
            sources=[*sources, *(item.source for item in evidence)],
            warnings=warnings,
        )

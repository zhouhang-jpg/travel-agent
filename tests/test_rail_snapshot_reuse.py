"""Different selections reuse complete rail observations without renewing timestamps."""

import asyncio
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from test_public_tickets import StubRuntime, rail_row, request

from travel_tools.providers.rail12306 import Rail12306Adapter
from travel_tools.query_reuse import snapshot_policy
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.schemas.quotes import SearchTrainsInput, SearchTrainsOutput


class CountRuntime(StubRuntime):
    def __init__(self, **data):
        super().__init__(**data)
        self.count = 0

    async def query(self, key, callback):
        self.count += 1
        await asyncio.sleep(0)
        return await super().query(key, callback)


async def test_changed_limit_and_train_filter_reuse_full_list_and_refresh_bypasses():
    runtime = CountRuntime(rows=[rail_row(number=f"G{i}") for i in range(1, 49)])
    async with httpx.AsyncClient() as client:
        adapter = Rail12306Adapter(runtime, client)
        adapter._stations = {"上海虹桥": "AOH", "杭州东": "HGH"}
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="search_trains",
                description="test",
                input_type=SearchTrainsInput,
                output_type=SearchTrainsOutput,
                handler=adapter.search,
                availability="ready",
                execution="parallel_read",
                requires_external_service=True,
                cache_seconds=20,
            )
        )
        args = request(SearchTrainsInput, max_results=30).model_dump(mode="json")
        first, expanded = await asyncio.gather(
            registry.dispatch("search_trains", args),
            registry.dispatch("search_trains", {**args, "max_results": 50}),
        )
        selected = await registry.dispatch("search_trains", {**args, "train_numbers": ["G48"]})
        assert runtime.count == 1
        assert len(first.data["offers"]) == 30 and len(expanded.data["offers"]) == 48
        assert selected.data["offers"][0]["service_number"] == "G48"
        assert expanded.reuse == selected.reuse == "cache"
        assert expanded.data["queried_at"] == first.data["queried_at"]
        assert expanded.data["coverage"]["data_time"] == first.data["coverage"]["data_time"]
        await registry.dispatch("search_trains", {**args, "force_refresh": True})
        assert runtime.count == 2
        await registry.close()


async def test_same_day_upcoming_trains_are_selected_before_departed_trains():
    now = datetime(2026, 10, 10, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    morning, evening = rail_row(number="G1"), rail_row(number="G2")
    evening["summary"][2:4] = ["19:00", "20:00"]
    runtime = CountRuntime(rows=[morning, evening])
    async with httpx.AsyncClient() as client:
        adapter = Rail12306Adapter(runtime, client)
        adapter._stations = {"上海虹桥": "AOH", "杭州东": "HGH"}
        with patch("travel_tools.providers.rail12306.datetime") as clock:
            clock.now.return_value = now
            clock.fromisoformat.side_effect = datetime.fromisoformat
            result = await adapter.search(
                request(SearchTrainsInput, departure_date=now.date(), max_results=1)
            )
        assert result.offers[0].service_number == "G2"
        assert result.coverage.matched_count == 2


async def test_snapshot_expiration_and_disabled_cache_do_not_reuse():
    runtime = CountRuntime(rows=[rail_row()])
    async with httpx.AsyncClient() as client:
        adapter = Rail12306Adapter(runtime, client)
        adapter._stations = {"上海虹桥": "AOH", "杭州东": "HGH"}
        token = snapshot_policy.set(("test", 20))
        try:
            with patch("travel_tools.query_reuse.time.monotonic", return_value=0):
                await adapter.search(request(SearchTrainsInput))
            with patch("travel_tools.query_reuse.time.monotonic", return_value=21):
                await adapter.search(request(SearchTrainsInput, max_results=50))
            assert runtime.count == 2
        finally:
            snapshot_policy.reset(token)
        await adapter.search(request(SearchTrainsInput))
        await adapter.search(request(SearchTrainsInput))
        assert runtime.count == 4

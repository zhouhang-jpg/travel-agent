"""Synthetic mapping/DOM tests; live evidence is recorded separately."""

from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from travel_tools.bootstrap import build_registry
from travel_tools.common import ToolFailure
from travel_tools.config import Settings
from travel_tools.providers.browser_runtime import BrowserQueryRuntime
from travel_tools.providers.csair import CsairAdapter
from travel_tools.schemas.quotes import SearchFlightsInput

DAY = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=7)


def request(**changes):
    return SearchFlightsInput.model_validate(
        {
            "provider": "csair",
            "origin": {"query": "广州"},
            "destination": {"query": "上海"},
            "departure_date": str(DAY),
            "travelers": {"adults": 1},
            **changes,
        }
    )


def row(**changes):
    return {
        "flight": "CZ3533\n空客350",
        "origin": "广州白云国际机场 T2",
        "destination": "上海虹桥国际机场 T2",
        "origin_code": "CAN",
        "destination_code": "SHA",
        "departure": f"{DAY:%Y%m%d} 07:00",
        "arrival": f"{DAY:%Y%m%d} 09:20",
        "duration": "2h20m",
        "routing": "",
        "cabins": [{"index": "1", "text": "¥1450"}, {"index": "3", "text": "¥690"}],
        **changes,
    }


class Runtime:
    def __init__(self, rows):
        self.rows, self.key = rows, None

    async def query(self, key, callback):
        self.key = key
        return {
            "rows": deepcopy(self.rows),
            "retrieved_at": "2026-10-10T01:00:00+08:00",
            "url": "https://b2c.csair.com/query",
            "cache_hit": False,
            "elapsed_seconds": 1,
            "first_results_seconds": 0.5,
        }


async def test_price_cabin_unknown_fields_and_airport_scope():
    runtime = Runtime(
        [
            row(),
            row(flight="CZ8211", destination="上海浦东国际机场 T2", destination_code="PVG"),
            row(destination_code="PEK"),
        ]
    )
    output = await CsairAdapter(runtime).search(request())
    assert len(output.offers) == 2 and output.coverage.scanned_count == 3
    first = output.offers[0]
    assert first.price.display.amount == 690 and first.price.money is None
    assert first.price.tax_basis == first.price.fee_basis == "unknown"
    assert first.price.priced_persons == 1 and first.inventory.status == "unknown"
    assert first.direct is None and first.operating_carrier is None and first.codeshare is None
    assert first.segments[0].departure_terminal == "T2"
    exact = await CsairAdapter(runtime).search(request(destination={"query": "SHA"}))
    assert len(exact.offers) == 1


async def test_masked_price_is_not_partial_numeric_price_and_ticket_few_not_count():
    runtime = Runtime(
        [row(cabins=[{"index": "3", "text": "¥6** 票少"}, {"index": "1", "text": "¥1450"}])]
    )
    economy = (await CsairAdapter(runtime).search(request())).offers[0]
    assert economy.price.display.amount is None and economy.price.display.masked
    assert economy.inventory.remaining is None and economy.inventory.status == "unknown"
    business = (await CsairAdapter(runtime).search(request(cabin="business"))).offers[0]
    assert business.price.display.amount == 1450 and business.seat_or_cabin == "公务舱"


async def test_wrong_date_or_airport_is_omitted_and_next_day_is_explicit():
    runtime = Runtime(
        [
            row(),
            row(departure=f"{DAY - timedelta(days=1):%Y%m%d} 07:00"),
            row(destination="上海浦东国际机场 T2", destination_code="SHA"),
            row(
                flight="CZ3586",
                departure=f"{DAY:%Y%m%d} 22:00",
                arrival=f"{DAY + timedelta(days=1):%Y%m%d} 00:20",
            ),
        ]
    )
    output = await CsairAdapter(runtime).search(request())
    assert len(output.offers) == 2 and output.coverage.matched_count == 2
    assert output.offers[-1].arrival_at.date() == DAY + timedelta(days=1)
    assert not (await CsairAdapter(runtime).search(request(flight_numbers=["CZ0001"]))).offers


async def test_directness_requires_evidence_and_invalid_rows_fail():
    runtime = Runtime(
        [row(), row(flight="CZ3531", routing="直飞"), row(flight="CZ3537", routing="经停")]
    )
    output = await CsairAdapter(runtime).search(request(nonstop_only=True))
    assert [o.service_number for o in output.offers] == ["CZ3531"]
    with pytest.raises(ToolFailure, match="fields missing"):
        await CsairAdapter(Runtime([row(origin="")])).search(request())
    shared = (
        await CsairAdapter(Runtime([row(flight="CZ3533 共享\n实际承运厦门航空")])).search(request())
    ).offers[0]
    assert shared.codeshare is True and shared.operating_carrier == "厦门航空"


@pytest.mark.parametrize(
    "change",
    [{"travelers": {"adults": 2}}, {"preferred_currency": "USD"}, {"origin": {"query": "LHR"}}],
)
async def test_unsupported_conditions_never_start_browser(change):
    runtime = Runtime([])
    with pytest.raises(ToolFailure):
        await CsairAdapter(runtime).search(request(**change))
    assert runtime.key is None


async def test_source_capability_export_without_query():
    async with httpx.AsyncClient() as client:
        registry = build_registry(Settings(_env_file=None, browser_queries_enabled=True), client)
        try:
            definition = next(
                d["function"]
                for d in registry.model_definitions()
                if d["function"]["name"] == "search_flights"
            )
            assert "csair" in definition["parameters"]["properties"]["provider"]["enum"]
            assert "csair" in definition["description"]
        finally:
            await registry.close()


async def test_local_synthetic_dom_excludes_calendar_and_hidden_templates():
    runtime = BrowserQueryRuntime(timeout=4)
    html = f'''<meta charset="utf-8"><p>旅客人数：成人 x 1儿童 x 0婴儿 x 0</p><p>低价日历 ¥1</p>
      <input name="single-formCalender" value="{DAY}">
      <div class="zls-flight-cell" data-dep="CAN" data-arr="SHA">
        <div class="zls-flgno-info">CZ3533</div>
        <div class="zls-flgtime-dep" data-value="{DAY:%Y%m%d} 07:00">07:00
          <div class="zls-flplace">广州白云国际机场 T2</div></div>
        <div class="zls-flgtime-arr" data-value="{DAY:%Y%m%d} 09:20">09:20
          <div class="zls-flplace">上海虹桥国际机场 T2</div></div>
        <div class="zls-cabin-cell" data-cabin="1">¥1450</div>
        <div class="zls-cabin-cell" data-cabin="3">¥690<span hidden>¥0 剩余0张</span></div>
      </div><div class="zls-flight-cell" style="display:none">CZ0000 ¥1 剩余0张</div>'''

    async def query(page):
        await page.route(
            "https://b2c.csair.com/**",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        return await CsairAdapter(runtime)._fetch(page, request(), "https://b2c.csair.com/probe")

    try:
        try:
            result = await runtime.query("csair:synthetic-dom", query)
        except ToolFailure as exc:
            if exc.code == "provider_unavailable":
                pytest.skip("Install Chromium for the optional local DOM test.")
            raise
        assert len(result["rows"]) == 1
        assert result["rows"][0]["cabins"][1]["text"] == "¥690"
    finally:
        await runtime.close()

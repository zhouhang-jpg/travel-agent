"""Visible China Southern domestic fare lists, in independent browser contexts."""

import re
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from travel_tools.common import Source, ToolFailure
from travel_tools.providers.browser_runtime import BrowserQueryRuntime, check_access, navigate
from travel_tools.providers.ceair import AIRPORT_ALIASES, DOMESTIC_CODES, airport_codes
from travel_tools.providers.ticket_data import (
    coverage,
    display_price,
    validate_party,
    validate_rendered_rows,
)
from travel_tools.schemas.quotes import (
    FlightOffer,
    InventoryEvidence,
    SearchFlightsInput,
    SearchFlightsOutput,
)

CABINS = {
    "first": (0, "头等舱"),
    "business": (1, "公务舱"),
    "premium_economy": (2, "明珠经济舱"),
    "economy": (3, "经济舱"),
}
BASE_URL = "https://b2c.csair.com/B2C40/newTrips/static/main/page/booking/index.html"


def page_time(value):
    try:
        return datetime.strptime(value, "%Y%m%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except (ValueError, TypeError):
        return None


def visible_airport_matches(text, code):
    names = {mapped for name, mapped in AIRPORT_ALIASES.items() if name in text}
    return not names or code in names


def terminal(text):
    match = re.search(r"\bT\d+\b", text)
    return match[0] if match else None


class CsairAdapter:
    description = (
        "南航官网国内单程列表：provider=csair，单成人；城市或IATA机场集合，上海城市可覆盖SHA/PVG。"
        "未指定cabin时查经济舱，也可选头等/公务/明珠经济舱。仅读取实际可见舱等对应价格，"
        "税费/ISO币种、最终库存、实际承运/共享和直飞关系缺失保持未知，tax_view无法在此列表切换。"
        "缺失直飞证据不会通过nonstop_only筛选。列表/部分来源无结果不能推论全市场无航班。"
    )

    def __init__(self, runtime: BrowserQueryRuntime):
        self.runtime = runtime

    async def _fetch(self, page, request, url):
        start = time.monotonic()
        await navigate(page, url)
        while True:
            await check_access(page)
            if await page.locator(".zls-flight-cell:visible").evaluate_all("""es => es.some(e =>
              /[A-Z0-9]{2}\\s?\\d{3,4}/.test(e.querySelector('.zls-flgno-info')?.innerText || '') &&
              e.querySelector('.zls-flgtime-dep')?.getAttribute('data-value') &&
              e.querySelector('.zls-flgtime-arr')?.getAttribute('data-value'))"""):
                break
            await page.wait_for_timeout(200)
        first_results_seconds = time.monotonic() - start
        # Wait for the initial list to settle; this is not exhaustive pagination.
        previous = -1
        for _ in range(4):
            await page.wait_for_timeout(200)
            count = await page.locator(".zls-flight-cell:visible").count()
            if count == previous:
                break
            previous = count
        await check_access(page)
        if await page.locator('input[name="single-formCalender"]').input_value() != str(
            request.departure_date
        ):
            raise ToolFailure(
                "provider_date_mismatch", "Airline page did not confirm the requested date."
            )
        body = await page.locator("body").inner_text()
        if not re.search(r"成人\s*x\s*1\s*儿童\s*x\s*0\s*婴儿\s*x\s*0", body):
            raise ToolFailure(
                "provider_party_mismatch", "Airline page did not confirm one adult only."
            )
        rows = await page.locator(".zls-flight-cell:visible").evaluate_all("""es => es.map(e => {
            const visible = x => x && x.getBoundingClientRect().height &&
              x.getBoundingClientRect().width;
            const text = selector => { const x=e.querySelector(selector);
              return visible(x) ? x.innerText.trim() : ''; };
            const stamp = selector => { const x=e.querySelector(selector);
              return visible(x) ? x.getAttribute('data-value') || '' : ''; };
            return {flight:text('.zls-flgno-info'),origin:text('.zls-flgtime-dep .zls-flplace'),
              destination:text('.zls-flgtime-arr .zls-flplace'),
              origin_code:e.getAttribute('data-dep'),
              destination_code:e.getAttribute('data-arr'),departure:stamp('.zls-flgtime-dep'),
              arrival:stamp('.zls-flgtime-arr'),duration:text('.zls-flg-time'),
              routing:text('.zls-flg-dir'),
              cabins:Array.from(e.querySelectorAll('.zls-cabin-cell')).map(x => ({
                index:x.getAttribute('data-cabin'),text:visible(x)?x.innerText.trim():''}))};
        })""")
        if not rows:
            raise ToolFailure(
                "provider_missing_results",
                "Rendered airline flights missing; not no-flight evidence.",
            )
        validate_rendered_rows(rows, ("flight", "origin", "destination", "departure", "arrival"))
        return {"rows": rows, "url": page.url, "first_results_seconds": first_results_seconds}

    async def search(self, request: SearchFlightsInput) -> SearchFlightsOutput:
        validate_party(request)
        if request.preferred_currency not in (None, "CNY"):
            raise ToolFailure(
                "unsupported_parameters", "This source has no currency conversion selector."
            )
        origin = airport_codes(request.origin).split(",")
        destination = airport_codes(request.destination).split(",")
        if not set(origin + destination) <= DOMESTIC_CODES:
            raise ToolFailure(
                "unsupported_parameters", "This adapter verifies mainland domestic airports only."
            )
        url = (
            BASE_URL
            + "?"
            + urlencode(
                {
                    "t": "S",
                    "c1": origin[0],
                    "c2": destination[0],
                    "d1": str(request.departure_date),
                    "at": 1,
                    "ct": 0,
                    "it": 0,
                    "b1": "-".join(origin),
                    "b2": "-".join(destination),
                    "orderChannel": "JPSS-YDXC",
                }
            )
        )
        raw = await self.runtime.query(
            "csair:" + request.model_dump_json(), lambda page: self._fetch(page, request, url)
        )
        queried_at = datetime.fromisoformat(raw["retrieved_at"])
        source = Source(
            provider="csair",
            url=raw["url"],
            retrieved_at=queried_at,
            attribution="China Southern public domestic fare list; incomplete market coverage.",
        )
        offers = []
        required = ("flight", "origin", "destination", "departure", "arrival")
        malformed = validate_rendered_rows(raw["rows"], required)
        omitted = 0
        cabin_index, cabin_name = CABINS[request.cabin or "economy"]
        for row in raw["rows"]:
            if not all(row.get(key) for key in required):
                continue
            flight = re.search(r"\b([A-Z0-9]{2})\s?(\d{3,4})\b", row["flight"])
            departure, arrival = page_time(row["departure"]), page_time(row["arrival"])
            if (
                not flight
                or not departure
                or departure.date() != request.departure_date
                or row["origin_code"] not in origin
                or row["destination_code"] not in destination
                or not visible_airport_matches(row["origin"], row["origin_code"])
                or not visible_airport_matches(row["destination"], row["destination_code"])
            ):
                omitted += 1
                continue
            number = "".join(flight.groups())
            if request.flight_numbers and number not in request.flight_numbers:
                continue
            routing = row.get("routing") or ""
            direct = (
                True
                if routing in {"直飞", "直达"}
                else False
                if any(word in routing for word in ("经停", "中转", "换乘"))
                else None
            )
            if request.nonstop_only and direct is not True:
                continue
            price_text = next(
                (c["text"] for c in row.get("cabins", []) if str(c["index"]) == str(cabin_index)),
                "",
            )
            # Only a visible numeric price from this cabin; '票少' is not a seat count.
            price_match = re.search(r"[¥￥]\s*[^\s]+", price_text)
            price = display_price(price_match[0] if price_match else price_text, adult_basis=True)
            price.conditions += (
                " Visible selected cabin only; tax_view and ISO currency not verified."
            )
            price.conditions += " Visible flight label: " + row["flight"]
            shared = True if "共享" in row["flight"] else None
            operator_match = re.search(r"实际承运[：:\s]*([^\n]+)", row["flight"])
            operating = operator_match[1].strip() if operator_match else None
            if arrival and arrival <= departure:
                arrival = None
            offers.append(
                FlightOffer(
                    supplier="csair",
                    queried_at=queried_at,
                    sources=[source],
                    offer_id=f"{request.departure_date}/{number}/{cabin_index}",
                    origin={"query": row["origin"], "provider_location_id": row["origin_code"]},
                    destination={
                        "query": row["destination"],
                        "provider_location_id": row["destination_code"],
                    },
                    service_number=number,
                    marketing_carrier=flight[1],
                    operating_carrier=operating,
                    codeshare=shared,
                    departure_at=departure,
                    arrival_at=arrival,
                    departure_text=row["departure"],
                    arrival_text=row["arrival"],
                    time_basis="supplier_local",
                    duration_text=row.get("duration"),
                    seat_or_cabin=cabin_name,
                    direct=direct,
                    stops=0 if direct is True else None,
                    price=price,
                    inventory=InventoryEvidence(
                        raw_status="票少" if "票少" in price_text else None
                    ),
                    segments=[
                        {
                            "origin": row["origin"],
                            "destination": row["destination"],
                            "origin_code": row["origin_code"],
                            "destination_code": row["destination_code"],
                            "departure_text": row["departure"],
                            "arrival_text": row["arrival"],
                            "departure_terminal": terminal(row["origin"]),
                            "arrival_terminal": terminal(row["destination"]),
                            "service_number": number,
                            "marketing_carrier": flight[1],
                            "operating_carrier": operating,
                            "codeshare": shared,
                            "seat_or_cabin": cabin_name,
                            "stop_evidence": routing or None,
                        }
                    ],
                )
            )
        matched = len(offers)
        offers = offers[: request.max_results]
        return SearchFlightsOutput(
            queried_at=queried_at,
            offers=offers,
            complete=False,
            sources=[source],
            coverage=coverage(
                raw,
                "csair",
                matched,
                len(offers),
                "Initial rendered China Southern domestic list only; "
                "requested airport/date/cabin filters; not full market.",
            ),
            warnings=[
                "Exact visible ¥ amounts retained; ISO currency, taxes/fees "
                "and final inventory are unverified.",
                "tax_view cannot be selected here; one-adult reference display "
                "is not a complete payable quote.",
                "Operating carrier, codeshare, baggage/refund rules "
                "and missing directness remain unknown.",
                f"Read {len(raw['rows'])} flight rows; matched {matched}; returned {len(offers)}; "
                f"malformed {malformed}; mismatched/invalid omitted {omitted}.",
                f"First valid rendered flight after "
                f"{raw.get('first_results_seconds', 'unknown')} seconds; "
                "initial list only, no exhaustive scroll/pagination.",
            ],
        )

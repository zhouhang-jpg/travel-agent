"""12306 public timetable/seat DOM, with public station-name catalog resolution."""

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from travel_tools.common import Source, ToolFailure
from travel_tools.providers.browser_runtime import BrowserQueryRuntime, check_access, navigate
from travel_tools.providers.ticket_data import (
    coverage,
    display_price,
    inventory,
    local_time,
    validate_party,
)
from travel_tools.schemas.quotes import (
    SearchTrainsInput,
    SearchTrainsOutput,
    SeatAvailability,
    TrainOffer,
)

CATALOG_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"


def train_type(number: str) -> str:
    if re.fullmatch(r"[GD]\d+", number):
        return "high_speed"
    if re.fullmatch(r"C\d+", number):
        return "intercity"
    if re.fullmatch(r"[ZTKYSL]?\d+", number):
        return "conventional"
    return "unknown"


def parse_seat(cell: dict, header: str) -> SeatAvailability:
    label = cell.get("label") or ""
    match = re.search(r"次列车[，,](.+?)票价(\d+(?:\.\d+)?)元[，,]余票(.+)$", label)
    if match:
        seat_class, amount, remaining = match.groups()
        price = display_price(amount + "元")
    else:
        seat_class, remaining = header, cell.get("text", "")
        price = display_price("未返回票价")
    return SeatAvailability(seat_class=seat_class, price=price, inventory=inventory(remaining))


class Rail12306Adapter:
    description = (
        "优先查询12306官方指定日期车次、各席别展示价与余票；有/数字、无、候补、未起售分别保留。"
        "station_scope=exact按准确站点筛选，city保留官方同城范围；direct_only排除换乘。"
        "元单位与展示金额保留，未明确ISO币种/税费时不猜。一次一个成人，不预订。"
    )

    def __init__(self, runtime: BrowserQueryRuntime, client: httpx.AsyncClient):
        self.runtime = runtime
        self.client = client
        self._stations = {}

    async def _station(self, location) -> tuple[str, str]:
        if not self._stations:
            try:
                response = await self.client.get(CATALOG_URL, timeout=5, follow_redirects=False)
                if response.status_code in (401, 403, 429, 432):
                    raise ToolFailure(
                        "provider_access_blocked", "Public station catalog access denied."
                    )
                response.raise_for_status()
                for entry in response.text.split("@"):
                    parts = entry.split("|")
                    if len(parts) >= 3 and re.fullmatch(r"[A-Z]{3}", parts[2]):
                        self._stations[parts[1]] = parts[2]
            except httpx.HTTPError:
                raise ToolFailure(
                    "provider_catalog_unavailable", "Public station catalog unavailable.", True
                ) from None
        name = location.query.strip()
        if name not in self._stations and name.endswith("站"):
            name = name[:-1]
        code = self._stations.get(name)
        if not code:
            raise ToolFailure(
                "provider_location_not_found", "Station name is not in the public 12306 catalog."
            )
        if location.provider_location_id and location.provider_location_id != code:
            raise ToolFailure(
                "provider_location_mismatch", "Supplied railway code does not match station name."
            )
        return name, code

    async def _fetch(self, page, url: str, request: SearchTrainsInput) -> dict:
        await navigate(page, url)
        if (
            request.departure_date.isoformat()
            not in await page.locator("input#train_date").input_value()
        ):
            raise ToolFailure("provider_date_mismatch", "12306 page shows a different date.")
        await page.wait_for_function(r"""() => {
            const rows=document.querySelectorAll('#queryLeftTable tr');
            return Array.from(rows).some(e=>/^[GDCZTKYSL]?\d+/.test(e.innerText.trim())) ||
              document.body.innerText.includes('没有查询到符合条件的');
        }""")
        await check_access(page)
        rows = await page.locator("#queryLeftTable tr:visible").evaluate_all(r"""es => es
            .filter(e=>/^[GDCZTKYSL]?\d+/.test(e.innerText.trim())).map(e=>({
              train:e.querySelector('a')?.innerText.trim(),
              summary:Array.from(e.querySelectorAll('strong')).map(x=>x.innerText.trim()),
              text:e.innerText,
              cells:Array.from(e.querySelectorAll('td')).map(x=>({
                text:x.innerText.trim(),label:x.getAttribute('aria-label')}))
            }))""")
        headers = await page.locator("#queryLeftTable").evaluate(r"""e => {
            const table=e.closest('table');
            return Array.from(table?.querySelectorAll('thead th')||[])
              .map(e=>e.innerText.trim().replace(/\s+/g,'/'));
        }""")
        if rows and not any(row.get("train") and len(row["summary"]) >= 5 for row in rows):
            raise ToolFailure("provider_invalid_results", "Rail rows lack identifiers/times.")
        if headers and len(headers) < 15:
            raise ToolFailure("provider_invalid_results", "Rail seat-column layout changed.")
        return {"rows": rows, "headers": headers, "url": page.url}

    async def search(self, request: SearchTrainsInput) -> SearchTrainsOutput:
        validate_party(request)
        if request.preferred_currency is not None:
            raise ToolFailure(
                "unsupported_parameters", "12306 list does not expose an ISO currency selector."
            )
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if request.departure_date > today + timedelta(days=14):
            raise ToolFailure(
                "outside_sale_window", "Date is beyond the current 15-day 12306 sale window."
            )
        origin, destination = (
            await self._station(request.origin),
            await self._station(request.destination),
        )
        url = "https://kyfw.12306.cn/otn/leftTicket/init?" + urlencode(
            {
                "linktypeid": "dc",
                "fs": ",".join(origin),
                "ts": ",".join(destination),
                "date": request.departure_date.isoformat(),
                "flag": "N,N,Y",
            },
            safe=",",
        )
        raw = await self.runtime.query(
            "12306:" + request.model_dump_json(), lambda page: self._fetch(page, url, request)
        )
        queried_at = datetime.fromisoformat(raw["retrieved_at"])
        source = Source(provider="rail12306", url=raw["url"], retrieved_at=queried_at)
        offers, malformed = [], 0
        fallback_headers = [
            "商务座/特等座",
            "优选一等座",
            "一等座",
            "二等座/二等包座",
            "高级软卧",
            "软卧/动卧/一等卧",
            "硬卧/二等卧",
            "软座",
            "硬座",
            "无座",
            "其他",
        ]
        seat_headers = raw.get("headers", [])[4:15] or fallback_headers
        for row in raw["rows"]:
            if len(row["summary"]) < 5 or not row.get("train"):
                malformed += 1
                continue
            if request.train_numbers and row["train"] not in request.train_numbers:
                continue
            from_name, to_name, dep, arr, duration = row["summary"][:5]
            if request.station_scope == "exact" and (
                from_name != origin[0] or to_name != destination[0]
            ):
                continue
            kind = train_type(row["train"])
            if request.train_types and kind not in request.train_types:
                continue
            seats = [parse_seat(cell, seat_headers[i]) for i, cell in enumerate(row["cells"][1:12])]
            if not seats:
                malformed += 1
                continue
            if request.seat_class:
                chosen = next(
                    (
                        s
                        for s in seats
                        if request.seat_class == s.seat_class
                        or request.seat_class in s.seat_class.split("/")
                    ),
                    None,
                )
                if chosen is None:
                    chosen = SeatAvailability(
                        seat_class=request.seat_class,
                        price=display_price("未返回票价"),
                        inventory=inventory("--"),
                    )
            else:
                chosen = next((s for s in seats if s.seat_class == "二等座"), None)
                if chosen is None:
                    chosen = next(
                        (s for s in seats if s.inventory.status != "not_offered"), seats[0]
                    )
            departure = local_time(request.departure_date, dep)
            arrival = None
            if departure and re.fullmatch(r"\d{2,3}:\d{2}", duration):
                hours, minutes = map(int, duration.split(":"))
                if hours > 0 or minutes > 0:
                    arrival = departure + timedelta(hours=hours, minutes=minutes)
                    if arrival.strftime("%H:%M") != arr:
                        arrival = None
            offers.append(
                TrainOffer(
                    supplier="rail12306",
                    queried_at=queried_at,
                    sources=[source],
                    origin={
                        "query": from_name,
                        "provider_location_id": self._stations.get(from_name),
                    },
                    destination={
                        "query": to_name,
                        "provider_location_id": self._stations.get(to_name),
                    },
                    service_number=row["train"],
                    train_type=kind,
                    direct=True,
                    departure_at=departure,
                    arrival_at=arrival,
                    departure_text=f"{request.departure_date} {dep}",
                    arrival_text=arr,
                    time_basis="supplier_local",
                    duration_text=duration,
                    seat_or_cabin=chosen.seat_class,
                    price=chosen.price,
                    inventory=chosen.inventory,
                    seats=seats,
                )
            )
        if malformed and not offers:
            raise ToolFailure(
                "provider_invalid_results",
                "No structurally usable 12306 rows; not no-ticket evidence.",
            )
        matched = len(offers)
        offers = offers[: request.max_results]
        return SearchTrainsOutput(
            queried_at=queried_at,
            offers=offers,
            complete=False,
            sources=[source],
            coverage=coverage(
                raw,
                "rail12306",
                matched,
                len(offers),
                f"Public direct-train list; station_scope={request.station_scope}; "
                f"max_results={request.max_results}.",
            ),
            warnings=[
                "有 means available without a numeric count; 候补 is waitlist.",
                "Price amount and 元 text preserved; "
                "ISO currency, total-party scope and taxes remain unverified.",
                "List observations are not a final fare/inventory or booking guarantee; "
                "intermediate transfers are not searched here.",
                f"Read {len(raw['rows'])} rows; matched {matched}; "
                f"returned {len(offers)}; malformed {malformed}.",
            ],
        )

"""Optional public airline list verification; never claims full market coverage."""

import re
from datetime import datetime, timedelta

from travel_tools.common import Source, ToolFailure
from travel_tools.providers.browser_runtime import BrowserQueryRuntime, check_access, navigate
from travel_tools.providers.ticket_data import (
    coverage,
    display_price,
    local_time,
    validate_party,
    validate_rendered_rows,
)
from travel_tools.schemas.quotes import (
    FlightOffer,
    InventoryEvidence,
    SearchFlightsInput,
    SearchFlightsOutput,
)

# Name aliases help build the public shopping URL. Explicit IATA codes also work;
# unsupported names fail clearly instead of inventing a provider city ID.
CITY_AIRPORTS = {
    "上海": "SHA,PVG",
    "北京": "PEK,PKX",
    "广州": "CAN",
    "深圳": "SZX",
    "成都": "CTU,TFU",
    "重庆": "CKG",
    "杭州": "HGH",
    "南京": "NKG",
    "西安": "XIY",
    "昆明": "KMG",
    "武汉": "WUH",
    "长沙": "CSX",
    "厦门": "XMN",
    "福州": "FOC",
    "青岛": "TAO",
    "济南": "TNA",
    "天津": "TSN",
    "大连": "DLC",
    "沈阳": "SHE",
    "哈尔滨": "HRB",
    "长春": "CGQ",
    "郑州": "CGO",
    "合肥": "HFE",
    "海口": "HAK",
    "三亚": "SYX",
    "贵阳": "KWE",
    "兰州": "LHW",
    "银川": "INC",
    "西宁": "XNN",
    "乌鲁木齐": "URC",
    "南宁": "NNG",
    "南昌": "KHN",
    "太原": "TYN",
    "石家庄": "SJW",
    "呼和浩特": "HET",
    "拉萨": "LXA",
    "桂林": "KWL",
    "丽江": "LJG",
    "大理": "DLU",
    "黄山": "TXN",
}
AIRPORT_ALIASES = {
    "虹桥": "SHA",
    "浦东": "PVG",
    "首都": "PEK",
    "大兴": "PKX",
    "白云": "CAN",
    "宝安": "SZX",
    "双流": "CTU",
    "天府": "TFU",
    "江北": "CKG",
    "萧山": "HGH",
    "禄口": "NKG",
    "咸阳": "XIY",
    "长水": "KMG",
    "天河": "WUH",
    "黄花": "CSX",
    "高崎": "XMN",
    "长乐": "FOC",
    "胶东": "TAO",
    "遥墙": "TNA",
    "滨海": "TSN",
    "周水子": "DLC",
    "桃仙": "SHE",
    "太平": "HRB",
    "龙嘉": "CGQ",
    "新郑": "CGO",
    "新桥": "HFE",
    "美兰": "HAK",
    "凤凰": "SYX",
    "龙洞堡": "KWE",
    "中川": "LHW",
    "河东": "INC",
    "曹家堡": "XNN",
    "地窝堡": "URC",
    "吴圩": "NNG",
    "昌北": "KHN",
    "武宿": "TYN",
    "正定": "SJW",
    "白塔": "HET",
    "贡嘎": "LXA",
    "两江": "KWL",
    "三义": "LJG",
    "大理": "DLU",
    "屯溪": "TXN",
}
DOMESTIC_CODES = {code for codes in CITY_AIRPORTS.values() for code in codes.split(",")}


def airport_codes(location) -> str:
    value = location.provider_location_id or location.query.strip().upper()
    if re.fullmatch(r"[A-Z]{3}(?:,[A-Z]{3})*", value):
        return value
    name = location.query.strip().removesuffix("市")
    if name in CITY_AIRPORTS:
        return CITY_AIRPORTS[name]
    if name in AIRPORT_ALIASES:
        return AIRPORT_ALIASES[name]
    raise ToolFailure(
        "provider_location_not_found",
        "Use verified IATA codes in provider_location_id or a supported city name for Ceair.",
    )


def airport_code(text: str) -> str | None:
    name = re.sub(r"\s*T\d+\s*$", "", text.strip())
    if re.fullmatch(r"[A-Z]{3}", name):
        return name
    return AIRPORT_ALIASES.get(name)


class CeairAdapter:
    description = (
        "可选东航官网航班列表核实：provider=ceair，origin/destination可用城市名或"
        "provider_location_id中的IATA集合（如SHA,PVG）。tax_view选择含税/不含税；flight_numbers可核实候选。"
        "仅经济/超级经济舱、单成人；非全市场覆盖，行李/退改和最终可售库存未核实。"
    )

    def __init__(self, runtime: BrowserQueryRuntime):
        self.runtime = runtime

    async def _fetch(self, page, request: SearchFlightsInput, url: str) -> dict:
        await navigate(page, url)
        consent = page.get_by_text("同意", exact=True)
        if await consent.count() and await consent.first.is_visible():
            await consent.first.click()
        await page.locator(".shopping-item-container").first.wait_for()
        await page.locator(".shopping-item-container .title-flight-no").first.wait_for()
        await check_access(page)
        body = await page.locator("body").inner_text()
        day = request.departure_date
        chinese_date = rf"{day.year}年0?{day.month}月0?{day.day}日"
        if not re.search(chinese_date, body):
            raise ToolFailure(
                "provider_date_mismatch",
                "Airline shopping page did not confirm the requested date.",
            )
        radio_name = "现金-含税" if request.tax_view == "included" else "现金-不含税"
        radio = page.get_by_role("radio", name=radio_name, exact=True)
        if await radio.get_attribute("aria-checked") != "true":
            notice = page.get_by_role("button", name="我知道了", exact=True)
            if await notice.count() and await notice.first.is_visible():
                await notice.first.click()
            await radio.click()
            await page.wait_for_function(
                "name => Array.from(document.querySelectorAll('[role=radio]'))"
                ".some(e=>e.innerText.trim()===name && e.getAttribute('aria-checked')==='true')",
                arg=radio_name,
            )
            # Let the selected view render; no cookies, token extraction or private APIs.
            await page.wait_for_timeout(200)
        basis = (
            request.tax_view if await radio.get_attribute("aria-checked") == "true" else "unknown"
        )
        rows = await page.locator(".shopping-item-container:visible").evaluate_all(
            """es=>es.map(e=>{
            const text=s=>e.querySelector(s)?.innerText.trim()||'';
            return {flight:text('.title-flight-no'),departure_time:text('.flight-info-dep-time'),
              arrival_time:text('.flight-info-arr-time'),origin:text('.flight-info-dep-name'),
              destination:text('.flight-info-arr-name'),duration:text('.flight-info-time .time'),
              departure_terminal:text('.flight-info-dep-name .title-term'),
              arrival_terminal:text('.flight-info-arr-name .title-term'),
              routing:text('.trans-info'),shared:text('.flight-share-container'),
              prices:Array.from(e.querySelectorAll('.cabin-level-item .amt-value'))
                .map(x=>x.innerText.trim())};
        })"""
        )
        if not rows:
            raise ToolFailure(
                "provider_missing_results", "Airline results missing; not evidence of no flights."
            )
        validate_rendered_rows(
            rows, ("flight", "origin", "destination", "departure_time", "arrival_time")
        )
        return {"rows": rows, "url": page.url, "tax_basis": basis}

    async def search(self, request: SearchFlightsInput) -> SearchFlightsOutput:
        validate_party(request)
        if request.preferred_currency not in (None, "CNY") or request.cabin not in (
            None,
            "economy",
            "premium_economy",
        ):
            raise ToolFailure(
                "unsupported_parameters",
                "Ceair list verification supports CNY economy/premium economy only.",
            )
        origin, destination = airport_codes(request.origin), airport_codes(request.destination)
        url = (
            f"https://www.ceair.com/zh/cny/shopping/oneway/{origin}-{destination}/"
            f"{request.departure_date}"
        )
        raw = await self.runtime.query(
            "ceair:" + request.model_dump_json(), lambda page: self._fetch(page, request, url)
        )
        queried_at = datetime.fromisoformat(raw["retrieved_at"])
        source = Source(
            provider="ceair",
            url=raw["url"],
            retrieved_at=queried_at,
            attribution="Airline public list; other carriers possible; incomplete coverage.",
        )
        offers, omitted = [], 0
        required = ("flight", "origin", "destination", "departure_time", "arrival_time")
        malformed = validate_rendered_rows(raw["rows"], required)
        for row in raw["rows"]:
            if not all(row.get(key) for key in required):
                continue
            from_code, to_code = airport_code(row["origin"]), airport_code(row["destination"])
            if (
                not row["flight"]
                or from_code not in origin.split(",")
                or to_code not in destination.split(",")
            ):
                omitted += 1
                continue
            if request.flight_numbers and row["flight"] not in request.flight_numbers:
                continue
            direct = row["routing"] in ("直达", "直飞")
            if request.nonstop_only and not direct:
                continue
            cabin = 1 if request.cabin == "premium_economy" else 0
            # A missing or masked cabin display is not proof that the flight is absent.
            price_text = row["prices"][cabin] if len(row["prices"]) > cabin else ""
            dep = row["departure_time"]
            arr_match = re.search(r"\d{2}:\d{2}", row["arrival_time"])
            arr = arr_match[0] if arr_match else ""
            departure, arrival = None, None
            if from_code in DOMESTIC_CODES and to_code in DOMESTIC_CODES:
                departure = local_time(request.departure_date, dep)
                if departure:
                    arrival = local_time(request.departure_date, arr)
                    explicit_days = re.search(r"\+(\d+)", row["arrival_time"])
                    if arrival and explicit_days:
                        arrival += timedelta(days=int(explicit_days[1]))
                    if arrival and arrival <= departure:
                        arrival = None
            offers.append(
                FlightOffer(
                    supplier="ceair",
                    queried_at=queried_at,
                    sources=[source],
                    origin={"query": row["origin"], "provider_location_id": from_code},
                    destination={"query": row["destination"], "provider_location_id": to_code},
                    departure_at=departure,
                    arrival_at=arrival,
                    departure_text=f"{request.departure_date} {dep}",
                    arrival_text=row["arrival_time"],
                    time_basis="supplier_local",
                    duration_text=row["duration"],
                    direct=direct if row["routing"] else None,
                    stops=0 if direct else None,
                    service_number=row["flight"],
                    seat_or_cabin="超级经济舱" if cabin else "经济舱",
                    codeshare=True if "共享" in row["shared"] else None,
                    marketing_carrier=row["flight"][:2],
                    operating_carrier=None,
                    segments=[
                        {
                            "origin": row["origin"],
                            "destination": row["destination"],
                            "origin_code": from_code,
                            "destination_code": to_code,
                            "departure_terminal": row["departure_terminal"] or None,
                            "arrival_terminal": row["arrival_terminal"] or None,
                            "departure_text": dep,
                            "arrival_text": row["arrival_time"],
                            "service_number": row["flight"],
                            "marketing_carrier": row["flight"][:2],
                            "codeshare": True if "共享" in row["shared"] else None,
                        }
                    ],
                    price=display_price(
                        price_text,
                        currency="CNY",
                        basis="Official website /zh/cny currency configuration and displayed ¥.",
                        tax_basis=raw["tax_basis"],
                    ),
                    inventory=InventoryEvidence(),
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
                "ceair",
                matched,
                len(offers),
                "Rendered airline list only; airport/flight filters applied; "
                "not full market coverage.",
            ),
            warnings=[
                "Displayed fare view preserves included/excluded tax; "
                "fees, baggage, refund/change rules and inventory unverified.",
                "Missing shared label does not prove no codeshare; "
                "marketing carrier is not operating carrier.",
                f"Read {len(raw['rows'])} rows; matched {matched}; returned {len(offers)}; "
                f"unmapped/mismatched endpoints {omitted}.",
                f"Malformed records omitted: {malformed}; missing/masked prices remain unknown.",
            ],
        )

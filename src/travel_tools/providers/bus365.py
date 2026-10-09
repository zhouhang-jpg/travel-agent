"""Read ordinary coach listings through an independent public browser form."""

import re
from datetime import datetime

from travel_tools.common import Source, ToolFailure
from travel_tools.providers.browser_runtime import BrowserQueryRuntime, check_access, navigate
from travel_tools.providers.ticket_data import (
    coverage,
    display_price,
    inventory,
    local_time,
    place_key,
    validate_party,
    validate_rendered_rows,
)
from travel_tools.schemas.quotes import CoachOffer, SearchCoachesInput, SearchCoachesOutput


def coach_category(vehicle: str) -> str:
    # Product evidence, never the occurrence of an airport/rail station in a stop name.
    if any(word in vehicle for word in ("机场专线", "机场巴士", "机场快线")):
        return "airport_shuttle"
    if any(word in vehicle for word in ("高铁接驳", "高铁专线", "高铁快线")):
        return "rail_shuttle"
    if any(word in vehicle for word in ("轿车", "商务车", "拼车", "网约车")):
        return "private_car"
    if any(word in vehicle for word in ("大型", "中型", "客车", "大巴")):
        return "ordinary_coach"
    return "unknown"


class Bus365Adapter:
    description = (
        "查询出行365普通长途大巴的指定日期列表；排除有证据的机场专线、高铁接驳和轿车/商务车。"
        "按实际站点筛选，车型未知保持未知。一次查询一个成人、当前指定页；page可查询后续页。"
        "展示数字价与数字余票分别保留，币种/税费/最终库存无依据时未知，不预订。"
    )

    def __init__(self, runtime: BrowserQueryRuntime):
        self.runtime = runtime

    async def _select_place(self, page, selector: str, name: str) -> str:
        field = page.locator(selector)
        await field.click()
        await field.fill(name)
        await field.press("ArrowDown")
        choices = page.locator(".ui-autocomplete:visible a")
        await choices.first.wait_for()
        labels = await choices.all_text_contents()
        key = place_key(name)
        indexes = [
            i
            for i, label in enumerate(labels)
            if re.match(re.escape(key) + r"(?:市|县|区)?(?:\s|$)", label.strip())
            or label.strip().startswith(key + "市")
            or label.strip().startswith(key + "县")
        ]
        if not indexes:
            indexes = [i for i, label in enumerate(labels) if label.strip().startswith(name)]
        if not indexes:
            raise ToolFailure(
                "provider_location_not_found", "No matching public city/station option."
            )
        await choices.nth(indexes[0]).click()
        return await field.input_value()

    async def _fetch(self, page, request: SearchCoachesInput) -> dict:
        await navigate(page, "https://www.chuxing365.com/schedule")
        await page.wait_for_load_state("load")
        origin = await self._select_place(page, "#departCity", request.origin.query)
        destination = await self._select_place(page, "#reachCity", request.destination.query)
        field = page.locator("#departDate")
        day = request.departure_date
        minimum = await field.get_attribute("mind")
        maximum = await field.get_attribute("maxd")
        if (minimum and day.isoformat() < minimum) or (maximum and day.isoformat() > maximum):
            raise ToolFailure(
                "outside_sale_window", "Requested coach date is outside the displayed sale window."
            )
        if day.isoformat() not in await field.input_value():
            await field.click()
            base = datetime.fromisoformat(minimum).date() if minimum else day
            month = (day.year - base.year) * 12 + day.month - base.month
            cell = page.locator(f'td[name="BUSData_departDate_td"][m="{month}"][tp="1"]')
            await cell.filter(has_text=re.compile(rf"^{day.day}$")).click()
        if day.isoformat() not in await field.input_value():
            raise ToolFailure(
                "provider_date_mismatch", "Coach page did not accept the requested date."
            )
        async with page.expect_navigation(wait_until="load"):
            await page.get_by_text("查询", exact=True).click()
        await check_access(page)
        await page.wait_for_function(
            "day => document.querySelector('#departDate')?.value.includes(day)",
            arg=day.isoformat(),
        )
        if day.isoformat() not in await page.locator("#departDate").input_value():
            raise ToolFailure("provider_date_mismatch", "Coach results belong to a different date.")
        rows = page.locator("#itemContainer > li:visible")
        if request.page > 1:
            link = page.locator(".holder a:visible").filter(
                has_text=re.compile(rf"^{request.page}$")
            )
            if await link.count() != 1:
                raise ToolFailure(
                    "provider_page_unavailable", "Requested coach result page is not available."
                )
            previous = await rows.first.inner_text()
            await link.click()
            await page.wait_for_function(
                "previous => { const e=Array.from(document.querySelectorAll('#itemContainer > li'))"
                ".find(e=>e.getClientRects().length); return e && e.innerText!==previous; }",
                arg=previous,
            )
        extracted = await rows.evaluate_all("""es=>es.map(e=>{
            const top=e.querySelector('.divTop'); const cols=Array.from(top.children);
            const text=s=>top.querySelector(s)?.innerText.trim()||'';
            return {time:text('.Size22'),origin:text('[id^=sf_]'),destination:text('[id^=zd_]'),
              vehicle:text('.busType'),duration:text('.useTime'),
              remaining:cols[4]?.innerText.trim()||'',price:cols[5]?.innerText.trim()||'',
              sale:text('.cz')}; })""")
        body = await page.locator("body").inner_text()
        if not extracted and "未查询出" not in body:
            raise ToolFailure(
                "provider_missing_results",
                "Coach results did not render; not evidence of no service.",
            )
        numbers = await page.locator(".holder a:visible").all_text_contents()
        validate_rendered_rows(extracted, ("time", "origin", "destination"))
        return {
            "rows": extracted,
            "url": page.url,
            "origin": origin,
            "destination": destination,
            "page_origin": await page.locator("#departCity").input_value(),
            "adult_basis": "目前只支持预订成人票" in body,
            "more_pages": any(
                text.strip().isdecimal() and int(text) > request.page for text in numbers
            ),
        }

    async def search(self, request: SearchCoachesInput) -> SearchCoachesOutput:
        validate_party(request)
        if request.origin.provider_location_id or request.destination.provider_location_id:
            raise ToolFailure(
                "unsupported_parameters",
                "Bus365 uses its visible city/station choices, not supplied IDs.",
            )
        if request.preferred_currency is not None:
            raise ToolFailure("unsupported_parameters", "Coach list has no currency selector.")
        raw = await self.runtime.query(
            "bus365:" + request.model_dump_json(), lambda page: self._fetch(page, request)
        )
        queried_at = datetime.fromisoformat(raw["retrieved_at"])
        source = Source(provider="bus365", url=raw["url"], retrieved_at=queried_at)
        malformed = validate_rendered_rows(raw["rows"], ("time", "origin", "destination"))
        offers = []
        omitted = 0
        for row in raw["rows"]:
            if not all(row.get(key) for key in ("time", "origin", "destination")):
                continue
            category = coach_category(row["vehicle"])
            if category not in ("ordinary_coach", "unknown"):
                omitted += 1
                continue
            if not place_key(row["destination"]).startswith(place_key(request.destination.query)):
                omitted += 1
                continue
            if place_key(raw["page_origin"]) != place_key(request.origin.query) and not row[
                "origin"
            ].startswith(place_key(request.origin.query)):
                omitted += 1
                continue
            if request.departure_station and row["origin"] != request.departure_station:
                omitted += 1
                continue
            available = inventory(row["remaining"])
            if "暂停" in row["sale"]:
                available = inventory("暂停网售")
            offers.append(
                CoachOffer(
                    supplier="bus365",
                    queried_at=queried_at,
                    sources=[source],
                    origin={"query": row["origin"]},
                    destination={"query": row["destination"]},
                    departure_at=local_time(request.departure_date, row["time"]),
                    departure_text=f"{request.departure_date} {row['time']}",
                    time_basis="supplier_local",
                    duration_text=row["duration"] or None,
                    vehicle_type=row["vehicle"] or None,
                    product_category=category,
                    category_evidence=row["vehicle"] or None,
                    price=display_price(row["price"], adult_basis=raw["adult_basis"]),
                    inventory=available,
                )
            )
        matched = len(offers)
        offers = offers[: request.max_results]
        return SearchCoachesOutput(
            queried_at=queried_at,
            offers=offers,
            complete=False,
            sources=[source],
            coverage=coverage(
                raw,
                "bus365",
                matched,
                len(offers),
                "Requested rendered page only; not whole-market or final ticketing coverage.",
                page=request.page,
                more_pages=raw["more_pages"],
            ),
            warnings=[
                f"Read page {request.page}; omitted {omitted} endpoint/product mismatches.",
                f"Malformed records omitted: {malformed}; unknown prices are not zero.",
                "Exact displayed numbers are preserved; "
                "￥ alone does not establish ISO currency or total price.",
                "Numeric remaining seats are observed list data, not a booking guarantee. "
                "Unknown categories remain unknown.",
            ],
        )

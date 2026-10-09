"""Evidence mapping shared by public-page ticket adapters."""

import re
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from travel_tools.common import ToolFailure
from travel_tools.schemas.quotes import (
    DisplayPrice,
    InventoryEvidence,
    PriceEvidence,
    QueryCoverage,
)


def validate_party(request) -> None:
    if request.travelers.adults != 1 or request.travelers.children_ages:
        raise ToolFailure("unsupported_parameters", "This channel verifies one adult only.")
    if request.origin.coordinates or request.destination.coordinates:
        raise ToolFailure(
            "unsupported_parameters", "This channel accepts names or station/airport codes."
        )
    if request.departure_date < datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ToolFailure("past_departure_date", "Departure date is already past in Asia/Shanghai.")


def validate_rendered_rows(rows: list[dict], required: tuple[str, ...]) -> int:
    malformed = sum(not all(row.get(key) for key in required) for row in rows)
    if rows and malformed == len(rows):
        raise ToolFailure(
            "provider_invalid_results", "Result fields missing; not no-service evidence."
        )
    return malformed


def numeric_display(text: str, currency_text: str | None = None) -> DisplayPrice:
    normalized = text.replace(",", "").strip()
    match = re.fullmatch(r"(?:[￥¥]|CNY)?\s*(\d+(?:\.\d+)?)(?:元)?", normalized)
    return DisplayPrice(
        text=text,
        amount=Decimal(match[1]) if match else None,
        currency_text=currency_text,
        masked=any(marker in text for marker in ("*", "？", "?"))
        or bool(re.search(r"\d[xX]|[xX]\d|^[xX]+$", text)),
    )


def display_price(
    text: str,
    *,
    currency: str | None = None,
    basis: str | None = None,
    tax_basis: str = "unknown",
    adult_basis: bool = False,
) -> PriceEvidence:
    symbol = next((value for value in ("元", "￥", "¥") if value in text), None)
    display = numeric_display(text, currency or symbol)
    money = (
        {"amount": display.amount, "currency": currency}
        if display.amount is not None and currency
        else None
    )
    return PriceEvidence(
        kind="reference" if money else "unknown",
        money=money,
        display=display,
        currency_basis=basis,
        tax_basis=tax_basis,
        unit="per_person" if adult_basis else "unknown",
        priced_persons=1 if adult_basis else None,
        conditions=(
            "List display only; final purchase price, fees and successful ticketing unverified."
        ),
    )


def inventory(text: str) -> InventoryEvidence:
    text = text.strip()
    status, remaining = "unknown", None
    if text.isdecimal():
        remaining = int(text)
        status = "available" if remaining > 0 else "unavailable"
    elif text == "有":
        status = "available"
    elif text in ("无", "售罄", "售完"):
        status = "unavailable"
    elif text == "候补":
        status = "waitlist"
    elif text in ("--", "—", "不提供"):
        status = "not_offered"
    elif any(marker in text for marker in ("未起售", "尚未开售", "未开售")):
        status = "not_on_sale"
    elif any(marker in text for marker in ("暂停网售", "停止网售", "暂停售票")):
        status = "sales_suspended"
    return InventoryEvidence(status=status, remaining=remaining, raw_status=text or None)


def local_time(day: date, time_text: str) -> datetime | None:
    if not re.fullmatch(r"\d{2}:\d{2}", time_text.strip()):
        return None
    try:
        return datetime.fromisoformat(f"{day.isoformat()}T{time_text.strip()}").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
    except ValueError:
        return None


def coverage(raw: dict, provider: str, matched: int, returned: int, scope: str, **kwargs):
    return QueryCoverage(
        provider=provider,
        query_status="results" if returned else ("filtered_empty" if raw["rows"] else "no_results"),
        scanned_count=len(raw["rows"]),
        matched_count=matched,
        returned_count=returned,
        scope=scope,
        cache_hit=raw["cache_hit"],
        data_time=raw["retrieved_at"],
        elapsed_seconds=raw["elapsed_seconds"],
        **kwargs,
    )


def place_key(text: str) -> str:
    return re.sub(r"[市县区]$", "", text.strip())

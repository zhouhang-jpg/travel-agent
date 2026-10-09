"""Read-only Juhe product 817 adapter, verified against its public documentation.

Requires a separately authorized Juhe key. No booking API is used. Provider
station names are passed through; a city is never expanded to an invented station.
"""

import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from travel_tools.common import Source, ToolFailure, utc_now
from travel_tools.providers.http import bounded_json_request
from travel_tools.schemas.quotes import (
    InventoryEvidence,
    Money,
    PriceEvidence,
    SearchLocation,
    SearchTrainsInput,
    SearchTrainsOutput,
    TrainOffer,
)

JUHE_TRAIN_URL = "https://apis.juhe.cn/fapigw/train/query"
JUHE_TRAIN_DOCS = "https://www.juhe.cn/docs/api/id/817"
# This API only permits current/future dates within 15 days. Mainland China uses
# UTC+08:00 throughout that window; no historical DST conversion is required.
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
DESCRIPTION = (
    "Query Juhe railway schedules, seat fares and inventory within the 15-day "
    "mainland China sales window. Only one standard adult fare in CNY is supported; "
    "station names are passed as given, and city-wide coverage is not guaranteed. "
    "Prices are not locked and do not establish tax or service-fee inclusion."
)


def _text(value: Any, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError("Required text missing")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid text")
    return value.strip()


def _clock(value: Any) -> time | None:
    if value in (None, "", "--"):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Invalid clock")
    return time.fromisoformat(value)


def _duration(value: Any) -> timedelta | None:
    if value in (None, "", "--"):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,3}:[0-5]\d", value):
        raise ValueError("Invalid duration")
    hours, minutes = map(int, value.split(":"))
    if hours * 60 + minutes <= 0:
        raise ValueError("Invalid duration")
    return timedelta(hours=hours, minutes=minutes)


def _price(value: Any) -> Money | None:
    if value in (None, "", "--"):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid price")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid price")
    # The API does not declare zero to mean a free adult ticket.
    return Money(amount=amount, currency="CNY") if amount > 0 else None


def _inventory(value: Any) -> InventoryEvidence:
    if isinstance(value, bool):
        return InventoryEvidence()
    if value == "有":
        return InventoryEvidence(status="available")
    if value == "无":
        return InventoryEvidence(status="unavailable", remaining=0)
    if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"\d+", value)):
        number = int(value)
        if number >= 0:
            return InventoryEvidence(
                status="available" if number else "unavailable", remaining=number
            )
    return InventoryEvidence()


def _station_arg(location: SearchLocation) -> tuple[str, str]:
    if location.provider_location_id:
        match = re.fullmatch(r"juhe_train:([A-Z]{3})", location.provider_location_id)
        if not match:
            raise ToolFailure(
                "unsupported_location_id", "Juhe requires a juhe_train: station code."
            )
        return "2", match.group(1)
    query = location.query.strip()
    if not query:
        raise ToolFailure("invalid_arguments", "A nonblank railway station name is required.")
    return "1", query


class JuheTrainAdapter:
    description = DESCRIPTION

    def __init__(self, api_key: str, client: httpx.AsyncClient):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ToolFailure("provider_unconfigured", "Juhe train API key is not configured.")
        self._api_key = api_key.strip()
        self._client = client

    async def search(self, request: SearchTrainsInput) -> SearchTrainsOutput:
        if request.direct_only or request.station_scope == "city":
            raise ToolFailure(
                "unsupported_parameters",
                "This Juhe adapter cannot verify direct-only or city-wide scope.",
            )
        if request.travelers.adults != 1 or request.travelers.children_ages:
            raise ToolFailure(
                "unsupported_travelers", "Juhe train queries support one standard adult only."
            )
        if request.preferred_currency not in (None, "CNY"):
            raise ToolFailure("unsupported_currency", "Juhe train fares are available in CNY only.")
        today = utc_now().astimezone(SHANGHAI).date()
        if not 0 <= (request.departure_date - today).days < 15:
            raise ToolFailure(
                "unsupported_date",
                "Juhe train date must be today through 14 days ahead in Asia/Shanghai.",
            )
        origin_type, origin = _station_arg(request.origin)
        destination_type, destination = _station_arg(request.destination)
        if origin_type != destination_type:
            raise ToolFailure(
                "unsupported_location_id", "Use either two station names or two Juhe station codes."
            )
        raw = await bounded_json_request(
            self._client,
            "GET",
            JUHE_TRAIN_URL,
            provider="Juhe train",
            headers={"Accept": "application/json"},
            params={
                "key": self._api_key,
                "search_type": origin_type,
                "departure_station": origin,
                "arrival_station": destination,
                "date": request.departure_date.isoformat(),
                "enable_booking": "2",
            },
        )
        code = raw.get("error_code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise ToolFailure("provider_invalid_response", "Juhe train response has no valid code.")
        if code:
            category = (
                "provider_authentication"
                if code in (10001, 10002, 10003, 10004, 10005, 10009)
                else "provider_rate_limited"
                if code in (10011, 10012, 10013)
                else "provider_rejected"
            )
            raise ToolFailure(
                category,
                f"Juhe train rejected the query (code {code}).",
                retryable=code in (10011, 10020),
            )
        queried_at = utc_now()
        source = Source(provider="juhe_train", url=JUHE_TRAIN_DOCS, retrieved_at=queried_at)
        warnings = [
            "Fares are one-way, per standard adult, in CNY; "
            "tax and service-fee inclusion is unknown.",
            "Only the queried stations are covered; names are not expanded to all city stations.",
            "Inventory is observed at query time and is not a reservation or locked price.",
        ]
        offers: list[TrainOffer] = []
        complete = True
        try:
            rows = raw["result"]
            if not isinstance(rows, list):
                raise ValueError("Invalid rows")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid row")
                service = _text(row.get("train_no"), required=True)
                if request.train_numbers and service not in request.train_numbers:
                    continue
                kind = (
                    "high_speed"
                    if service.startswith(("G", "D"))
                    else "intercity"
                    if service.startswith("C")
                    else "conventional"
                    if service.startswith(("Z", "T", "K", "Y", "S")) or service.isdecimal()
                    else "unknown"
                )
                if request.train_types and kind not in request.train_types:
                    continue
                start_name = _text(row.get("departure_station"), required=True)
                end_name = _text(row.get("arrival_station"), required=True)
                start_code = _text(row.get("departure_station_code"))
                end_code = _text(row.get("arrival_station_code"))
                for station_code in (start_code, end_code):
                    if station_code is not None and not re.fullmatch(r"[A-Z]{3}", station_code):
                        raise ValueError("Invalid station code")
                if origin_type == "2" and (start_code != origin or end_code != destination):
                    raise ValueError("Response does not match the requested station codes")
                start_clock = _clock(row.get("departure_time"))
                end_clock = _clock(row.get("arrival_time"))
                duration = _duration(row.get("duration"))
                starts = (
                    datetime.combine(request.departure_date, start_clock, SHANGHAI)
                    if start_clock
                    else None
                )
                ends = starts + duration if starts is not None and duration is not None else None
                if ends is not None and end_clock is not None and ends.time() != end_clock:
                    raise ValueError("Arrival clock disagrees with duration")
                if starts is None or ends is None:
                    warnings.append("A train has incomplete timing; arrival date was not guessed.")
                    complete = False
                prices = row.get("prices")
                if prices is None or prices == []:
                    warnings.append(
                        "A train has no seat-fare details; seat filters cannot verify it."
                    )
                    complete = False
                    prices = [{}]
                if not isinstance(prices, list):
                    raise ValueError("Invalid fares")
                for fare in prices:
                    if not isinstance(fare, dict):
                        raise ValueError("Invalid fare")
                    seat_name = _text(fare.get("seat_name"))
                    seat_code = _text(fare.get("seat_type_code"))
                    if request.seat_class and request.seat_class not in (seat_name, seat_code):
                        continue
                    money = _price(fare.get("price"))
                    inventory = _inventory(fare.get("num"))
                    if money is None or inventory.status == "unknown":
                        warnings.append("Some fare or inventory fields are unknown.")
                        complete = False
                    offers.append(
                        TrainOffer(
                            supplier="juhe_train",
                            queried_at=queried_at,
                            sources=[source],
                            origin=SearchLocation(
                                query=start_name,
                                provider_location_id=f"juhe_train:{start_code}"
                                if start_code
                                else None,
                            ),
                            destination=SearchLocation(
                                query=end_name,
                                provider_location_id=f"juhe_train:{end_code}" if end_code else None,
                            ),
                            departure_at=starts,
                            arrival_at=ends,
                            service_number=service,
                            seat_or_cabin=seat_name or seat_code,
                            train_type=kind,
                            inventory=inventory,
                            price=PriceEvidence(
                                kind="query_quote" if money else "unknown",
                                money=money,
                                unit="per_person",
                                priced_persons=1,
                                conditions=(
                                    "One standard adult, one-way ticket fare; "
                                    "query-time inventory only. "
                                    "Tax and service-fee inclusion is unspecified. "
                                    "Arrival date, when "
                                    "present, is derived from departure plus provider duration."
                                ),
                            ),
                        )
                    )
        except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
            raise ToolFailure(
                "provider_invalid_response", "Juhe train returned invalid schedule or fare data."
            ) from None
        if len(offers) > request.max_results:
            complete = False
            warnings.append("Offers were truncated at max_results; other fares may exist.")
        return SearchTrainsOutput(
            queried_at=queried_at,
            offers=offers[: request.max_results],
            complete=complete,
            sources=[source],
            warnings=list(dict.fromkeys(warnings)),
        )

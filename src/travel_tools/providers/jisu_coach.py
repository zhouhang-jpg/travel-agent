"""Jisu city2c supplies reference coach timetables, not dated bookable inventory."""

import re
from decimal import Decimal, InvalidOperation

import httpx

from travel_tools.common import Source, ToolFailure, utc_now
from travel_tools.providers.http import bounded_json_request
from travel_tools.schemas.quotes import (
    CoachOffer,
    InventoryEvidence,
    Money,
    PriceEvidence,
    SearchCoachesInput,
    SearchCoachesOutput,
    SearchLocation,
)

JISU_COACH_URL = "https://api.jisuapi.com/bus/city2c"
JISU_COACH_DOCS = "https://www.jisuapi.com/api/bus/"
DESCRIPTION = (
    "Look up reference coach schedules and fares between Chinese cities using Jisu city2c. "
    "The source cannot query a travel date or inventory; the requested date is NOT verified. "
    "All prices are references, timestamps and inventory remain unknown, and complete is false."
)


class JisuCoachAdapter:
    description = DESCRIPTION

    def __init__(self, api_key: str, client: httpx.AsyncClient):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ToolFailure("provider_unconfigured", "Jisu coach API key is not configured.")
        self._api_key = api_key.strip()
        self._client = client

    async def search(self, request: SearchCoachesInput) -> SearchCoachesOutput:
        if request.page != 1:
            raise ToolFailure(
                "unsupported_parameters", "Jisu reference channel has no page selector."
            )
        if request.preferred_currency not in (None, "CNY"):
            raise ToolFailure("unsupported_currency", "Jisu coach reference fares are in CNY only.")
        if request.travelers.adults != 1 or request.travelers.children_ages:
            raise ToolFailure(
                "unsupported_travelers",
                "Jisu does not support party or child-specific coach fares.",
            )
        if request.origin.provider_location_id or request.destination.provider_location_id:
            raise ToolFailure(
                "unsupported_location_id", "Jisu coach city2c accepts city names, not provider IDs."
            )
        if not request.origin.query.strip() or not request.destination.query.strip():
            raise ToolFailure(
                "invalid_arguments", "Nonblank origin and destination cities required."
            )
        raw = await bounded_json_request(
            self._client,
            "GET",
            JISU_COACH_URL,
            provider="Jisu coach",
            headers={"Accept": "application/json"},
            params={
                "appkey": self._api_key,
                "start": request.origin.query.strip(),
                "end": request.destination.query.strip(),
            },
        )
        code = raw.get("status")
        if isinstance(code, bool) or not isinstance(code, int):
            raise ToolFailure("provider_invalid_response", "Jisu coach response has no valid code.")
        if code:
            category = (
                "provider_authentication"
                if code in (101, 102, 103)
                else "provider_rate_limited"
                if code in (104, 106)
                else "provider_rejected"
            )
            raise ToolFailure(
                category,
                f"Jisu coach rejected the query (code {code}).",
                retryable=code in (106, 107),
            )
        queried_at = utc_now()
        source = Source(provider="jisu_coach", url=JISU_COACH_DOCS, retrieved_at=queried_at)
        warnings = [
            "REFERENCE ONLY: Jisu city2c cannot query a date. The requested departure_date "
            "is not verified and is not attached to any timetable clock.",
            "No inventory, reservation, live fare or arrival timestamp is available.",
            "Fares are references with unspecified passenger category, taxes and service fees; "
            "they are not a complete party total.",
        ]
        offers: list[CoachOffer] = []
        try:
            rows = raw["result"]
            if not isinstance(rows, list):
                raise ValueError("Invalid rows")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid row")
                start_station, end_station = row.get("startstation"), row.get("endstation")
                if not all(isinstance(v, str) and v.strip() for v in (start_station, end_station)):
                    raise ValueError("Missing actual stations")
                if request.departure_station and start_station != request.departure_station:
                    continue
                value = row.get("price")
                money = None
                if value not in (None, "", "--"):
                    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                        raise ValueError("Invalid price")
                    amount = Decimal(str(value))
                    if not amount.is_finite() or amount < 0:
                        raise ValueError("Invalid price")
                    if amount > 0:
                        money = Money(amount=amount, currency="CNY")
                if money is None:
                    warnings.append("A missing or zero reference fare was kept unknown.")
                clock = row.get("starttime")
                conditions = (
                    "Undated reference only; requested travel date and passenger fare category "
                    "are unsupported. Taxes, fees and inventory are unknown."
                )
                if isinstance(clock, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
                    conditions += f" Undated departure clock: {clock} (Asia/Shanghai)."
                else:
                    warnings.append("Departure clock is unknown or not a single valid time.")
                # distance/costTime are not reliable elapsed-time fields in this source;
                # no duration or dated timestamp is manufactured from them.
                offers.append(
                    CoachOffer(
                        supplier="jisu_coach",
                        queried_at=queried_at,
                        sources=[source],
                        origin=SearchLocation(query=start_station),
                        destination=SearchLocation(query=end_station),
                        inventory=InventoryEvidence(),
                        price=PriceEvidence(
                            kind="reference" if money else "unknown",
                            money=money,
                            unit="unknown",
                            conditions=conditions,
                        ),
                    )
                )
        except (KeyError, ValueError, TypeError, InvalidOperation):
            raise ToolFailure(
                "provider_invalid_response", "Jisu coach returned invalid reference data."
            ) from None
        if len(offers) > request.max_results:
            warnings.append("Reference offers were truncated at max_results.")
        return SearchCoachesOutput(
            queried_at=queried_at,
            offers=offers[: request.max_results],
            complete=False,
            sources=[source],
            warnings=list(dict.fromkeys(warnings)),
        )

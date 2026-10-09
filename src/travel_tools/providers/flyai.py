"""Read-only adapters for the official, locally installed FlyAI CLI.

No package is installed and no booking command is available through this module.
The CLI's experience mode is useful for integration tests, but does not establish
production entitlement, exhaustive results, exact prices, or saleable inventory.
Pin and review the vendor CLI separately. The adjacent preload guard only isolates
local state and handles its immediate successful exit on Windows; it leaves
network requests and nonzero exits unchanged.
"""

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from travel_tools.common import Source, ToolFailure, utc_now
from travel_tools.schemas.quotes import (
    FlightOffer,
    HotelOffer,
    InventoryEvidence,
    Money,
    PriceEvidence,
    SearchFlightsInput,
    SearchFlightsOutput,
    SearchHotelsInput,
    SearchHotelsOutput,
    SearchLocation,
    SearchTrainsInput,
    SearchTrainsOutput,
    TrainOffer,
)

_PROVIDER = "fliggy_flyai"
_DOC_URL = "https://flyai.open.fliggy.com/docs/overview"
_OUTPUT_LIMIT = 4 * 1024 * 1024
_COMMON_WARNINGS = [
    "FlyAI returns limited search candidates; production access is not verified by this adapter.",
    "Price scope, taxes, fees, quote expiry and inventory are unverified; results are incomplete.",
    "Masked prices and prices without an explicit currency are retained as text, not money.",
]


@dataclass(frozen=True)
class CLIResult:
    returncode: int
    stdout: str
    stderr: str = ""


CLIExecutor = Callable[[Sequence[str], Mapping[str, str], float], Awaitable[CLIResult]]


async def _execute_cli(args: Sequence[str], env: Mapping[str, str], duration: float) -> CLIResult:
    """No shell, no implicit installation; kill/reap children on timeout or cancellation."""
    process = await asyncio.create_subprocess_exec(
        *args,
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), duration)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if len(stdout) > _OUTPUT_LIMIT or len(stderr) > _OUTPUT_LIMIT:
        raise ToolFailure("upstream_response_too_large", "FlyAI response exceeds the size limit.")
    try:
        return CLIResult(process.returncode or 0, stdout.decode("utf-8"), stderr.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ToolFailure("upstream_invalid_response", "FlyAI returned invalid UTF-8.") from exc


class FlyAIClient:
    """Explicit local Node and CLI paths; key goes in child environment, never argv.

    ``executor`` is injectable for contract tests. ``state_directory`` must be an
    application-owned directory: the vendor otherwise writes to the user's home.
    An omitted key deliberately selects the vendor's documented experience mode.
    """

    def __init__(
        self,
        node_path: str | Path,
        cli_path: str | Path,
        state_directory: str | Path,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        executor: CLIExecutor | None = None,
    ) -> None:
        if not 0 < timeout <= 120:
            raise ValueError("timeout must be greater than 0 and at most 120 seconds")
        self.node_path = Path(node_path).resolve()
        self.cli_path = Path(cli_path).resolve()
        self.state_directory = Path(state_directory).resolve()
        self.api_key = api_key
        self.timeout = timeout
        self.executor = executor or _execute_cli
        self._check_paths = executor is None

    async def query(self, command: str, arguments: Sequence[str]) -> list[dict[str, Any]]:
        if command not in {"search-flight", "search-train", "search-hotel"}:
            raise ToolFailure("unsupported_operation", "Only read-only FlyAI searches are allowed.")
        guard = Path(__file__).with_name("flyai_cli_guard.cjs")
        if self._check_paths and not all(
            path.is_file() for path in (self.node_path, self.cli_path, guard)
        ):
            raise ToolFailure(
                "provider_not_configured", "Configure existing local Node and CLI files."
            )
        env = dict(os.environ)
        for key in (
            "FLYAI_API_KEY",
            "DEBUG_FLYAI_API_KEY",
            "DEBUG_FLYAI_MCP_URL",
            "FLYAI_SIGN_SECRET",
        ):
            env.pop(key, None)
        if self.api_key:
            env["FLYAI_API_KEY"] = self.api_key
        env["FLYAI_STATE_DIR"] = str(self.state_directory)
        args = [
            str(self.node_path),
            "--require",
            str(guard),
            str(self.cli_path),
            command,
            *arguments,
        ]
        try:
            result = await asyncio.wait_for(self.executor(args, env, self.timeout), self.timeout)
        except TimeoutError as exc:
            raise ToolFailure("upstream_timeout", "FlyAI query timed out.", retryable=True) from exc
        except OSError as exc:
            raise ToolFailure(
                "provider_unavailable", "The local FlyAI CLI could not be executed."
            ) from exc
        if result.returncode != 0:
            # Raw stderr may contain credentials, device identifiers or server messages.
            raise ToolFailure("provider_error", "FlyAI CLI exited unsuccessfully.")
        if len(result.stdout.encode("utf-8")) > _OUTPUT_LIMIT:
            raise ToolFailure(
                "upstream_response_too_large", "FlyAI response exceeds the size limit."
            )
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ToolFailure("upstream_invalid_response", "FlyAI returned invalid JSON.") from exc
        if not isinstance(payload, dict) or type(payload.get("status")) is not int:
            raise ToolFailure("upstream_invalid_response", "FlyAI response has no valid status.")
        if payload["status"] != 0:
            raise ToolFailure("provider_error", "FlyAI rejected the search request.")
        data = payload.get("data")
        items = data.get("itemList") if isinstance(data, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ToolFailure("upstream_invalid_response", "FlyAI returned an invalid result list.")
        return items


def _validate_supported(request: Any) -> None:
    if request.travelers.adults != 1 or request.travelers.children_ages:
        raise ToolFailure(
            "unsupported_parameters", "FlyAI CLI does not support party-size or child-age pricing."
        )
    if request.preferred_currency is not None:
        raise ToolFailure("unsupported_parameters", "FlyAI CLI has no quote-currency selector.")
    locations = [request.destination]
    if hasattr(request, "origin"):
        locations.append(request.origin)
    if any(location.provider_location_id or location.coordinates for location in locations):
        raise ToolFailure(
            "unsupported_parameters",
            "FlyAI CLI accepts location names, not typed IDs or coordinates.",
        )
    if isinstance(request, SearchHotelsInput) and request.rooms != 1:
        raise ToolFailure(
            "unsupported_parameters", "FlyAI CLI does not support multiple-room searches."
        )


def _source(item: dict[str, Any], queried_at: datetime) -> Source:
    candidate = item.get("jumpUrl") or item.get("detailUrl")
    url = _DOC_URL
    if isinstance(candidate, str):
        try:
            parsed = urlsplit(candidate)
            hostname = parsed.hostname or ""
            if (
                parsed.scheme == "https"
                and not parsed.username
                and not parsed.password
                and any(
                    hostname == host or hostname.endswith("." + host)
                    for host in ("fliggy.com", "feizhu.com")
                )
            ):
                url = candidate
        except ValueError:
            pass
    return Source(
        provider=_PROVIDER,
        url=url,
        retrieved_at=queried_at,
        attribution="Official FlyAI CLI search; freshness and production entitlement unverified.",
    )


def _price(item: dict[str, Any], context: dict[str, Any]) -> PriceEvidence:
    raw = next((item[key] for key in ("ticketPrice", "adultPrice", "price") if key in item), None)
    currency = item.get("currency") or item.get("currencyCode")
    text = str(raw).strip() if raw is not None else ""
    # Currency symbols alone (including ¥) are insufficient to establish an ISO currency.
    prefixed = re.fullmatch(r"([A-Z]{3})\s+(\d+(?:\.\d+)?)", text)
    if prefixed and currency is None:
        currency, text = prefixed.groups()
    valid = isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)
    money = None
    if valid and re.fullmatch(r"\d+(?:\.\d+)?", text):
        money = Money(amount=Decimal(text), currency=currency)
    evidence = {
        "raw_price": raw,
        "raw_currency": item.get("currency") or item.get("currencyCode"),
        "limitations": "Display/reference price only; scope, taxes, fees and inventory unknown.",
        **context,
    }
    return PriceEvidence(
        kind="reference" if money else "unknown",
        money=money,
        conditions=json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
    )


def _aware_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Preserve naive supplier values in conditions instead of guessing local time zones.
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    journeys = item.get("journeys")
    if not isinstance(journeys, list) or len(journeys) != 1:
        raise ValueError("expected one outbound journey")
    segments = journeys[0].get("segments") if isinstance(journeys[0], dict) else None
    if (
        not isinstance(segments, list)
        or not segments
        or any(not isinstance(segment, dict) for segment in segments)
    ):
        raise ValueError("missing segments")
    if any(
        not isinstance(segment.get(key), str) or not segment[key].strip()
        for segment in segments
        for key in ("depStationName", "arrStationName", "marketingTransportNo")
    ):
        raise ValueError("missing station/service identifiers")
    return segments


def _transport_fields(
    item: dict[str, Any], segments: list[dict[str, Any]], queried_at: datetime
) -> dict[str, Any]:
    first, last = segments[0], segments[-1]
    evidence_fields = (
        "depCityName",
        "depStationCode",
        "depStationName",
        "depDateTime",
        "arrCityName",
        "arrStationCode",
        "arrStationName",
        "arrDateTime",
        "marketingTransportNo",
        "seatClassName",
        "duration",
        "quantity",
        "stopInfos",
    )
    context = {
        "segments": [{key: segment.get(key) for key in evidence_fields} for segment in segments]
    }
    return {
        "supplier": _PROVIDER,
        "queried_at": queried_at,
        "sources": [_source(item, queried_at)],
        "price": _price(item, context),
        "inventory": InventoryEvidence(),
        "origin": SearchLocation(query=first["depStationName"]),
        "destination": SearchLocation(query=last["arrStationName"]),
        "departure_at": _aware_time(first.get("depDateTime")),
        "arrival_at": _aware_time(last.get("arrDateTime")),
        "service_number": "/".join(segment["marketingTransportNo"] for segment in segments),
        "operator": "/".join(
            dict.fromkeys(
                str(segment["marketingTransportName"])
                for segment in segments
                if segment.get("marketingTransportName")
            )
        )
        or None,
        "seat_or_cabin": "/".join(
            dict.fromkeys(
                str(segment["seatClassName"])
                for segment in segments
                if segment.get("seatClassName")
            )
        )
        or None,
    }


def _transport_arguments(request: Any) -> list[str]:
    return [
        "--origin",
        request.origin.query,
        "--destination",
        request.destination.query,
        "--dep-date",
        request.departure_date.isoformat(),
    ]


def _warnings(max_results: int, *, transport: bool = False) -> list[str]:
    warnings = list(_COMMON_WARNINGS)
    if max_results > 10:
        warnings.append(
            "The official CLI requests at most 10 candidates; no pagination is available."
        )
    if transport:
        warnings.append("Timestamps without UTC offsets remain raw text; no time zone is inferred.")
    return warnings


def _check_malformed(items: list[Any], malformed: int, warnings: list[str]) -> None:
    if malformed and malformed == len(items):
        raise ToolFailure(
            "upstream_invalid_response", "FlyAI returned no structurally valid candidates."
        )
    if malformed:
        warnings.append(f"Skipped {malformed} malformed or mismatched-date candidates.")


class FlyAIFlightAdapter:
    def __init__(self, client: FlyAIClient) -> None:
        self.client = client

    async def search(self, request: SearchFlightsInput) -> SearchFlightsOutput:
        _validate_supported(request)
        arguments = _transport_arguments(request)
        cabin = {
            "economy": "经济舱",
            "premium_economy": "超级经济舱",
            "business": "商务舱",
            "first": "头等舱",
        }.get(request.cabin)
        if cabin:
            arguments.extend(["--seat-class-name", cabin])
        if request.nonstop_only:
            arguments.extend(["--journey-type", "1"])
        items = await self.client.query("search-flight", arguments)
        queried_at = utc_now()
        warnings = _warnings(request.max_results, transport=True)
        offers, malformed, filtered = [], 0, 0
        for item in items:
            try:
                segments = _segments(item)
                if not str(segments[0].get("depDateTime", "")).startswith(
                    request.departure_date.isoformat() + " "
                ) and not str(segments[0].get("depDateTime", "")).startswith(
                    request.departure_date.isoformat() + "T"
                ):
                    raise ValueError("departure date does not match")
                if request.nonstop_only and (len(segments) != 1 or segments[0].get("stopInfos")):
                    filtered += 1
                    continue
                if cabin and any(segment.get("seatClassName") != cabin for segment in segments):
                    filtered += 1
                    continue
                offers.append(
                    FlightOffer(
                        **_transport_fields(item, segments, queried_at),
                        stops=None
                        if any(segment.get("stopInfos") for segment in segments)
                        else len(segments) - 1,
                    )
                )
            except (ValueError, TypeError, ValidationError):
                malformed += 1
        _check_malformed(items, malformed, warnings)
        if filtered:
            warnings.append(
                "Candidates not proving the requested cabin/nonstop condition were omitted."
            )
        return SearchFlightsOutput(
            queried_at=queried_at,
            offers=offers[: request.max_results],
            complete=False,
            sources=[_source({}, queried_at)],
            warnings=warnings,
        )


def _train_type(segments: list[dict[str, Any]]) -> str:
    types = set()
    for segment in segments:
        number = segment["marketingTransportNo"].upper()
        if re.fullmatch(r"[GD]\d+", number):
            types.add("high_speed")
        elif re.fullmatch(r"C\d+", number):
            types.add("intercity")
        elif re.fullmatch(r"[ZTK]?\d+", number):
            types.add("conventional")
        else:
            types.add("unknown")
    return types.pop() if len(types) == 1 else "unknown"


class FlyAITrainAdapter:
    def __init__(self, client: FlyAIClient) -> None:
        self.client = client

    async def search(self, request: SearchTrainsInput) -> SearchTrainsOutput:
        _validate_supported(request)
        arguments = _transport_arguments(request)
        if request.seat_class:
            arguments.extend(["--seat-class-name", request.seat_class])
        items = await self.client.query("search-train", arguments)
        queried_at = utc_now()
        warnings = _warnings(request.max_results, transport=True)
        offers, malformed = [], 0
        for item in items:
            try:
                segments = _segments(item)
                if (
                    str(segments[0].get("depDateTime", ""))[:10]
                    != request.departure_date.isoformat()
                ):
                    raise ValueError("departure date does not match")
                train_type = _train_type(segments)
                if request.train_types and train_type not in request.train_types:
                    continue
                if request.seat_class and any(
                    segment.get("seatClassName") != request.seat_class for segment in segments
                ):
                    continue
                offers.append(
                    TrainOffer(
                        **_transport_fields(item, segments, queried_at),
                        train_type=train_type,
                    )
                )
            except (ValueError, TypeError, ValidationError):
                malformed += 1
        _check_malformed(items, malformed, warnings)
        if request.train_types or request.seat_class:
            warnings.append(
                "Train type/seat filters are verified on the limited returned candidates."
            )
        return SearchTrainsOutput(
            queried_at=queried_at,
            offers=offers[: request.max_results],
            complete=False,
            sources=[_source({}, queried_at)],
            warnings=warnings,
        )


class FlyAIHotelAdapter:
    def __init__(self, client: FlyAIClient) -> None:
        self.client = client

    async def search(self, request: SearchHotelsInput) -> SearchHotelsOutput:
        _validate_supported(request)
        items = await self.client.query(
            "search-hotel",
            [
                "--dest-name",
                request.destination.query,
                "--check-in-date",
                request.check_in.isoformat(),
                "--check-out-date",
                request.check_out.isoformat(),
            ],
        )
        queried_at = utc_now()
        warnings = _warnings(request.max_results)
        warnings.extend(
            [
                "Occupancy, room availability, meals and cancellation are unverified.",
                "Requested dates do not prove a priced whole stay or an available room.",
                "Coordinate system is unspecified; coordinates are retained as raw evidence only.",
            ]
        )
        offers, malformed = [], 0
        for item in items:
            try:
                if not isinstance(item.get("name"), str) or not item["name"].strip():
                    raise ValueError("missing hotel name")
                context = {
                    "requested_check_in": request.check_in.isoformat(),
                    "requested_check_out": request.check_out.isoformat(),
                    "occupancy_verified": False,
                    "raw_location": {
                        key: item.get(key) for key in ("latitude", "longitude", "address")
                    },
                }
                offers.append(
                    HotelOffer(
                        supplier=_PROVIDER,
                        queried_at=queried_at,
                        sources=[_source(item, queried_at)],
                        hotel_name=item["name"],
                        hotel_id=str(item["shId"]) if item.get("shId") is not None else None,
                        location=SearchLocation(query=item.get("address") or item["name"]),
                        check_in=request.check_in,
                        check_out=request.check_out,
                        price=_price(item, context),
                        inventory=InventoryEvidence(),
                    )
                )
            except (ValueError, TypeError, ValidationError):
                malformed += 1
        _check_malformed(items, malformed, warnings)
        return SearchHotelsOutput(
            queried_at=queried_at,
            offers=offers[: request.max_results],
            complete=False,
            sources=[_source({}, queried_at)],
            warnings=warnings,
        )

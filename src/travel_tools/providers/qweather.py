"""QWeather forecasts/alerts and the separately documented v7 indices API."""

import re
from bisect import bisect_left
from datetime import UTC, timedelta
from math import ceil
from typing import Any

import httpx
from pydantic import ValidationError

from travel_tools.common import Coordinates, Source, ToolFailure, utc_now
from travel_tools.providers.http import bounded_json_request
from travel_tools.schemas.weather import (
    DailyWeather,
    GetWeatherInput,
    GetWeatherOutput,
    HourlyWeather,
    WeatherAlert,
    WeatherCoverage,
    WeatherIndex,
    WeatherPeriod,
    WeatherQuantity,
)


def _object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected object")
    return value


def _quantity(value: Any) -> WeatherQuantity | None:
    if value is None:
        return None
    item = _object(value)
    return WeatherQuantity(value=item.get("value"), unit=item.get("unit"))


def _period(value: Any) -> WeatherPeriod | None:
    if value is None:
        return None
    item = _object(value)
    condition = _object(item.get("condition"))
    precipitation = _object(item.get("precipitation"))
    return WeatherPeriod(
        starts_at=item.get("forecastStartTime"),
        ends_at=item.get("forecastEndTime"),
        condition=condition.get("text"),
        condition_code=condition.get("code"),
        temperature_max=_quantity(item.get("temperatureMax")),
        temperature_min=_quantity(item.get("temperatureMin")),
        wind_speed=_quantity(_object(item.get("wind")).get("speed")),
        wind_gust_max=_quantity(item.get("windGustMax")),
        precipitation_amount=_quantity(precipitation.get("amount")),
        precipitation_probability=precipitation.get("probability"),
        precipitation_type=precipitation.get("type"),
        humidity=item.get("humidity"),
    )


class QWeatherAdapter:
    def __init__(self, api_host: str, api_key: str, client: httpx.AsyncClient):
        host = api_host.strip().lower()
        if host.startswith("https://"):
            host = host[8:]
        host = host.removesuffix("/")
        if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.qweatherapi\.com", host):
            raise ValueError("QWEATHER_API_HOST must be the account qweatherapi.com hostname")
        if not api_key.strip():
            raise ValueError("QWeather API key must not be blank")
        self._base_url = f"https://{host}"
        self._api_key = api_key
        self._client = client

    async def get_weather(self, args: GetWeatherInput) -> GetWeatherOutput:
        if args.mode != "daily":
            return await self._get_extended(args)
        query = Coordinates(
            longitude=round(args.coordinates.longitude, 2),
            latitude=round(args.coordinates.latitude, 2),
            crs=args.coordinates.crs,
        )
        path = f"/weather/v1/daily/{query.latitude:.2f}/{query.longitude:.2f}"
        requested_days = (
            10 if args.forecast_date and "days" not in args.model_fields_set else args.days
        )
        raw = await bounded_json_request(
            self._client,
            "GET",
            self._base_url + path,
            provider="QWeather",
            headers={"X-QW-Api-Key": self._api_key, "Accept": "application/json"},
            params={"days": requested_days, "lang": args.language, "localTime": "true"},
        )
        retrieved_at = utc_now()
        try:
            if "error" in raw or "code" in raw:
                raise ValueError("Unexpected error or legacy envelope")
            items = raw["days"]
            if not isinstance(items, list) or not items or len(items) > requested_days:
                raise ValueError("Missing or unexpected forecast list")
            days = []
            for item in items:
                day = _object(item)
                days.append(
                    DailyWeather(
                        starts_at=day.get("forecastStartTime"),
                        ends_at=day.get("forecastEndTime"),
                        temperature_max=_quantity(day.get("temperatureMax")),
                        temperature_min=_quantity(day.get("temperatureMin")),
                        daytime=_period(day.get("daytime")),
                        nighttime=_period(day.get("nighttime")),
                    )
                )
            attributions = _object(raw.get("metadata")).get("attributions", [])
            if not isinstance(attributions, list) or not all(
                isinstance(item, str) for item in attributions
            ):
                raise ValueError("Invalid attributions")
            warnings = [
                "Forecasts describe returned intervals only; "
                "dates outside these intervals are unknown.",
                "Provider forecast issue time is not supplied; retrieved_at is query time only.",
            ]
            if query != args.coordinates:
                warnings.append(
                    "Query coordinates were rounded to the provider's two-decimal precision."
                )
            if len(days) < requested_days:
                warnings.append("Provider returned fewer forecast days than requested.")
            dates = sorted({day.starts_at.date() for day in days})
            count = len(days)
            start, end = min(day.starts_at for day in days), max(day.ends_at for day in days)
            if args.forecast_date:
                days = [day for day in days if day.starts_at.date() == args.forecast_date]
                if not days:
                    warnings.append(
                        "Requested date is outside returned daily coverage; weather unknown."
                    )
            return GetWeatherOutput(
                coordinates=args.coordinates,
                queried_coordinates=query,
                requested_days=requested_days,
                days=days,
                coverage=WeatherCoverage(
                    status="covered"
                    if days and args.forecast_date
                    else "outside_range"
                    if args.forecast_date
                    else "not_requested",
                    granularity="daily",
                    provider_count=count,
                    returned_count=len(days),
                    available_start=start,
                    available_end=end,
                    available_dates=dates,
                    forecast_date=args.forecast_date,
                ),
                attributions=attributions,
                sources=[
                    Source(
                        provider="qweather",
                        url=self._base_url + path,
                        retrieved_at=retrieved_at,
                        attribution="; ".join(attributions) or None,
                    )
                ],
                warnings=warnings,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise ToolFailure(
                "provider_invalid_response", "QWeather returned an invalid daily forecast response."
            ) from None

    async def _get_extended(self, args: GetWeatherInput) -> GetWeatherOutput:
        query = Coordinates(
            longitude=round(args.coordinates.longitude, 2),
            latitude=round(args.coordinates.latitude, 2),
            crs=args.coordinates.crs,
        )
        params = {"lang": args.language, "localTime": "true"}
        requested_hours = None
        if args.mode == "hourly":
            requested_hours = _hour_count(args)
            params["hours"] = requested_hours
            path = f"/weather/v1/hourly/{query.latitude:.2f}/{query.longitude:.2f}"
        elif args.mode == "alerts":
            path = f"/weatheralert/v1/current/{query.latitude:.2f}/{query.longitude:.2f}"
        else:
            path = f"/v7/indices/{args.index_days}d"
            params = {
                "lang": args.language,
                "type": ",".join(map(str, args.index_types)),
                "location": f"{query.longitude:.2f},{query.latitude:.2f}",
            }
        raw = await bounded_json_request(
            self._client,
            "GET",
            self._base_url + path,
            provider="QWeather",
            headers={"X-QW-Api-Key": self._api_key, "Accept": "application/json"},
            params=params,
        )
        retrieved = utc_now()
        try:
            if args.mode == "indices":
                return self._indices(args, query, raw, path, retrieved)
            if "error" in raw or "code" in raw:
                raise ValueError("Unexpected forecast/alert envelope")
            attributions = _strings(_object(raw.get("metadata")).get("attributions", []))
            base = dict(
                coordinates=args.coordinates,
                queried_coordinates=query,
                forecast_kind=args.mode,
                attributions=attributions,
                sources=[
                    Source(
                        provider="qweather",
                        url=self._base_url + path,
                        retrieved_at=retrieved,
                        attribution="; ".join(attributions) or None,
                    )
                ],
            )
            warnings = ["Only returned times/locations are evidence; missing fields are unknown."]
            if query != args.coordinates:
                warnings.append("Coordinates rounded to the provider's two-decimal precision.")
            if args.mode == "hourly":
                points = raw["hours"]
                if not isinstance(points, list) or not points or len(points) > requested_hours:
                    raise ValueError("Invalid hourly list")
                hours = sorted(
                    [_hour(_object(point)) for point in points], key=lambda p: p.forecast_time
                )
                if len({point.forecast_time for point in hours}) != len(hours):
                    raise ValueError("Duplicate/conflicting hourly times")
                selected, coverage = _select_hours(hours, args)
                if len(hours) < requested_hours:
                    warnings.append("Provider returned fewer hours than requested.")
                if coverage.status in ("outside_range", "gap", "partial"):
                    warnings.append(
                        "Target not fully covered; do not substitute times or invent weather."
                    )
                if coverage.status == "bracketing":
                    warnings.append("Adjacent hourly samples are reference only; no interpolation.")
                warnings.append(
                    "forecastTime is an hourly sample, not minute weather; issue time not supplied."
                )
                return GetWeatherOutput(
                    **base,
                    requested_hours=requested_hours,
                    hours=selected,
                    coverage=coverage,
                    warnings=warnings,
                )
            alerts = raw["alerts"]
            zero = _object(raw.get("metadata")).get("zeroResult")
            if (
                not isinstance(alerts, list)
                or (zero is not None and type(zero) is not bool)
                or (not alerts and zero is not True)
                or (alerts and zero is True)
            ):
                raise ValueError("Empty alert response requires explicit zeroResult=true")
            mapped = [_alert(_object(alert)) for alert in alerts]
            warnings.append(
                "Current official alerts only; empty results do not prove future or overall safety."
            )
            warnings.append(
                "Keep cancellation/update, superseded IDs and effective/expiry times; "
                "not all messages are active."
            )
            return GetWeatherOutput(
                **base,
                alerts=mapped,
                zero_alert_result=zero,
                coverage=WeatherCoverage(
                    status="not_requested",
                    granularity="current_alerts",
                    provider_count=len(alerts),
                    returned_count=len(alerts),
                ),
                warnings=warnings,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise ToolFailure(
                "provider_invalid_response", f"QWeather returned invalid {args.mode} data."
            ) from None

    def _indices(self, args, query, raw, path, retrieved):
        code = raw.get("code")
        if code != "200":
            raise ToolFailure(
                "provider_authentication"
                if code in ("401", "402", "403")
                else "provider_rate_limited"
                if code == "429"
                else "provider_rejected",
                "QWeather indices query was rejected; no indices obtained.",
                code == "429",
            )
        records = raw["daily"]
        if not isinstance(records, list) or not records:
            raise ValueError("Invalid indices list")
        reference = _object(raw.get("refer"))
        attributions, licenses = (
            _strings(reference.get("sources", [])),
            _strings(reference.get("license", [])),
        )
        indices = [
            WeatherIndex(
                forecast_date=item.get("date"),
                type=item.get("type"),
                name=item.get("name"),
                level=item.get("level"),
                category=item.get("category"),
                text=item.get("text"),
            )
            for item in map(_object, records)
        ]
        if len({(item.forecast_date, item.type) for item in indices}) != len(indices):
            raise ValueError("Duplicate indices")
        if 0 not in args.index_types and any(item.type not in args.index_types for item in indices):
            raise ValueError("Unexpected index types")
        dates = sorted({item.forecast_date for item in indices})
        if len(dates) > args.index_days:
            raise ValueError("Unexpected index dates")
        count = len(indices)
        if args.forecast_date:
            indices = [item for item in indices if item.forecast_date == args.forecast_date]
        update = raw.get("updateTime")
        output = GetWeatherOutput(
            coordinates=args.coordinates,
            queried_coordinates=query,
            forecast_kind="indices",
            requested_days=args.index_days,
            indices=indices,
            attributions=attributions,
            licenses=licenses,
            provider_update_time=update,
            sources=[
                Source(
                    provider="qweather",
                    url=self._base_url + path,
                    retrieved_at=retrieved,
                    data_time=update,
                    attribution="; ".join(attributions + licenses) or None,
                )
            ],
            coverage=WeatherCoverage(
                status="covered"
                if indices and args.forecast_date
                else "outside_range"
                if args.forecast_date
                else "not_requested",
                granularity="daily_indices",
                provider_count=count,
                returned_count=len(indices),
                available_dates=dates,
                forecast_date=args.forecast_date,
            ),
            warnings=[
                "Daily lifestyle guidance only; not hourly weather or outdoor safety assurance."
            ],
        )
        if args.forecast_date and not indices:
            output.warnings.append("Requested date has no returned index data; no extrapolation.")
        if len(dates) < args.index_days:
            output.warnings.append("Provider returned fewer index days than requested.")
        if 0 not in args.index_types and any(
            not any(item.forecast_date == day and item.type == kind for item in output.indices)
            for day in ({args.forecast_date} if args.forecast_date else set(dates))
            for kind in args.index_types
        ):
            output.warnings.append(
                "Some requested date/index combinations are missing; not inferred."
            )
        if query != args.coordinates:
            output.warnings.append("Coordinates rounded to the provider's two-decimal precision.")
        return output


def _strings(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Invalid attribution/string list")
    return value


def _hour(point: dict) -> HourlyWeather:
    condition, wind, precipitation = (
        _object(point.get(key)) for key in ("condition", "wind", "precipitation")
    )
    direction = _object(wind.get("direction"))
    return HourlyWeather(
        forecast_time=point.get("forecastTime"),
        condition=condition.get("text"),
        condition_code=condition.get("code"),
        temperature=_quantity(point.get("temperature")),
        feels_like=_quantity(point.get("feelsLike")),
        humidity=point.get("humidity"),
        wind_speed=_quantity(wind.get("speed")),
        wind_gust=_quantity(point.get("windGust")),
        wind_direction_degree=direction.get("degree"),
        wind_direction_compass=direction.get("compass"),
        wind_scale=wind.get("scale"),
        precipitation_amount=_quantity(precipitation.get("amount")),
        precipitation_intensity=_quantity(precipitation.get("intensity")),
        precipitation_probability=precipitation.get("probability"),
        precipitation_type=precipitation.get("type"),
        pressure=_quantity(point.get("pressure")),
        visibility=_quantity(point.get("visibility")),
        dew_point=_quantity(point.get("dewPoint")),
        cloud_cover=point.get("cloudCover"),
        uv_index=point.get("uvIndex"),
    )


def _hour_count(args):
    now = utc_now()
    latest = args.target_time or args.end_time
    if latest and latest.astimezone(UTC) < now:
        raise ToolFailure(
            "historical_weather_not_supported", "Hourly mode queries future forecasts, not history."
        )
    if args.hours is not None:
        return args.hours
    if latest:
        needed = max(1, ceil((latest.astimezone(UTC) - now).total_seconds() / 3600) + 1)
        if needed > 241:
            raise ToolFailure(
                "target_outside_supported_horizon",
                "Hourly forecasts support at most 240 returned hours.",
            )
        return min(240, needed)
    return 24


def _select_hours(hours, args):
    times = [hour.forecast_time.astimezone(UTC) for hour in hours]
    status, selected = "not_requested", hours
    if args.target_time:
        target = args.target_time.astimezone(UTC)
        i = bisect_left(times, target)
        if i < len(times) and times[i] == target:
            status, selected = "exact", [hours[i]]
        elif 0 < i < len(times):
            if times[i] - times[i - 1] == timedelta(hours=1):
                status, selected = "bracketing", [hours[i - 1], hours[i]]
            else:
                status, selected = "gap", []
        else:
            status, selected = "outside_range", []
    elif args.start_time:
        start, end = args.start_time.astimezone(UTC), args.end_time.astimezone(UTC)
        selected = [hour for hour, time in zip(hours, times, strict=True) if start <= time < end]
        left, right = bisect_left(times, start), bisect_left(times, end)
        reference = False
        if 0 < left < len(times) and times[left] != start:
            selected.insert(0, hours[left - 1])
            reference = True
        if right < len(times) and times[right] != end and end > times[0]:
            if hours[right] not in selected:
                selected.append(hours[right])
            reference = True
        if not selected:
            status = "outside_range" if end <= times[0] or start > times[-1] else "gap"
        else:
            first, last = (
                selected[0].forecast_time.astimezone(UTC),
                selected[-1].forecast_time.astimezone(UTC),
            )
            complete = (
                first == start
                and last + timedelta(hours=1) == end
                and len(selected) * 3600 == (end - start).total_seconds()
            )
            if complete:
                status = "covered"
            elif (
                reference
                and first <= start
                and (last >= end or last + timedelta(hours=1) == end)
                and all(
                    following.forecast_time.astimezone(UTC) - previous.forecast_time.astimezone(UTC)
                    == timedelta(hours=1)
                    for previous, following in zip(selected, selected[1:], strict=False)
                )
            ):
                status = "bracketing"
            else:
                status = "partial"
    return selected, WeatherCoverage(
        status=status,
        granularity="hourly",
        provider_count=len(hours),
        returned_count=len(selected),
        available_start=hours[0].forecast_time,
        available_end=hours[-1].forecast_time,
        target_time=args.target_time,
        requested_start=args.start_time,
        requested_end=args.end_time,
    )


def _alert(item):
    message, event, color = (
        _object(item.get(key)) for key in ("messageType", "eventType", "color")
    )
    return WeatherAlert(
        id=item.get("id"),
        sender_name=item.get("senderName"),
        issued_time=item.get("issuedTime"),
        message_type=message.get("code"),
        supersedes=_strings(message["supersedes"])
        if message.get("supersedes") is not None
        else None,
        event_name=event.get("name"),
        event_code=event.get("code"),
        urgency=item.get("urgency"),
        severity=item.get("severity"),
        certainty=item.get("certainty"),
        color_code=color.get("code"),
        effective_time=item.get("effectiveTime"),
        onset_time=item.get("onsetTime"),
        expire_time=item.get("expireTime"),
        headline=item.get("headline"),
        description=item.get("description"),
        criteria=item.get("criteria"),
        response_types=_strings(item["responseTypes"])
        if item.get("responseTypes") is not None
        else None,
        instruction=item.get("instruction"),
    )

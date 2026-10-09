"""QWeather daily adapter based on the official weather/v1 documentation."""

import re
from typing import Any

import httpx
from pydantic import ValidationError

from travel_tools.common import Coordinates, Source, ToolFailure, utc_now
from travel_tools.providers.http import bounded_json_request
from travel_tools.schemas.weather import (
    DailyWeather,
    GetWeatherInput,
    GetWeatherOutput,
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
        query = Coordinates(
            longitude=round(args.coordinates.longitude, 2),
            latitude=round(args.coordinates.latitude, 2),
            crs=args.coordinates.crs,
        )
        path = f"/weather/v1/daily/{query.latitude:.2f}/{query.longitude:.2f}"
        raw = await bounded_json_request(
            self._client,
            "GET",
            self._base_url + path,
            provider="QWeather",
            headers={"X-QW-Api-Key": self._api_key, "Accept": "application/json"},
            params={"days": args.days, "lang": args.language, "localTime": "true"},
        )
        retrieved_at = utc_now()
        try:
            if "error" in raw or "code" in raw:
                raise ValueError("Unexpected error or legacy envelope")
            items = raw["days"]
            if not isinstance(items, list) or not items or len(items) > args.days:
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
            if len(days) < args.days:
                warnings.append("Provider returned fewer forecast days than requested.")
            return GetWeatherOutput(
                coordinates=args.coordinates,
                queried_coordinates=query,
                requested_days=args.days,
                days=days,
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

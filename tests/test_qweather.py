import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from travel_tools.common import Coordinates, ToolFailure
from travel_tools.providers.qweather import QWeatherAdapter
from travel_tools.schemas.weather import GetWeatherInput

KEY = "contract-test-key-not-a-real-credential"
HOST = "unit-test.xy.qweatherapi.com"


def weather_input(**overrides):
    return GetWeatherInput(
        coordinates=Coordinates(longitude=116.4123, latitude=39.9245, crs="gcj02"),
        **overrides,
    )


def forecast():
    return {
        "metadata": {"attributions": ["https://developer.qweather.com/attribution.html"]},
        "days": [
            {
                "forecastStartTime": "2026-10-09T00:00:00+08:00",
                "forecastEndTime": "2026-10-10T00:00:00+08:00",
                "temperatureMax": {"value": 24.1, "unit": "°C"},
                "temperatureMin": {"value": 12.5, "unit": "°C"},
                "daytime": {
                    "forecastStartTime": "2026-10-09T07:00:00+08:00",
                    "forecastEndTime": "2026-10-09T19:00:00+08:00",
                    "condition": {"text": "晴", "code": "100"},
                    "precipitation": {"amount": {"value": 0, "unit": "mm"}, "probability": 0},
                },
            }
        ],
    }


async def test_daily_forecast_documented_request_and_null_preservation():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/weather/v1/daily/39.92/116.41"
        assert dict(request.url.params) == {"days": "3", "lang": "zh", "localTime": "true"}
        assert request.headers["X-QW-Api-Key"] == KEY
        assert "key=" not in str(request.url)
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = await QWeatherAdapter(HOST, KEY, client).get_weather(weather_input(days=3))
    assert output.days[0].temperature_max.value == 24.1
    assert output.days[0].temperature_max.unit == "°C"
    assert output.days[0].nighttime is None
    assert output.days[0].daytime.precipitation_probability == 0
    assert output.days[0].daytime.humidity is None
    assert output.queried_coordinates.crs == "gcj02"
    assert output.queried_coordinates.longitude == 116.41
    assert output.sources[0].data_time is None
    assert output.sources[0].retrieved_at.utcoffset().total_seconds() == 0
    assert output.attributions == ["https://developer.qweather.com/attribution.html"]
    assert any("fewer" in warning for warning in output.warnings)
    assert KEY not in output.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"days": []},
        {"days": [None]},
        {"days": "bad"},
        {"error": {"detail": KEY}},
        {"code": "200", "daily": []},
    ],
)
async def test_invalid_json_shapes_are_not_successful_empty_forecasts(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ToolFailure) as caught:
            await QWeatherAdapter(HOST, KEY, client).get_weather(weather_input())
    assert caught.value.code == "provider_invalid_response"
    assert KEY not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("forecastStartTime", "2026-10-09T00:00:00"),
        ("forecastEndTime", "2026-10-08T00:00:00+08:00"),
        ("temperatureMax", {"value": "nan", "unit": "°C"}),
        ("daytime", {"humidity": 1.1}),
    ],
)
async def test_invalid_data_rejected(field, value):
    body = forecast()
    body["days"][0][field] = value
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ToolFailure, match="invalid daily forecast"):
            await QWeatherAdapter(HOST, KEY, client).get_weather(weather_input())


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "provider_authentication", False),
        (403, "provider_authentication", False),
        (429, "provider_rate_limited", True),
        (500, "provider_http_error", True),
        (302, "provider_http_error", False),
    ],
)
async def test_http_errors_and_redirects_are_sanitized(status, code, retryable):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"error": {"detail": KEY}},
            headers={"location": "http://127.0.0.1/private"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ToolFailure) as caught:
            await QWeatherAdapter(HOST, KEY, client).get_weather(weather_input())
    assert len(requests) == 1
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("kind", ["timeout", "network", "json", "size"])
async def test_bounded_transport_failures(kind):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(KEY)
        if kind == "network":
            raise httpx.ConnectError(KEY)
        if kind == "json":
            return httpx.Response(200, content=b"<html>not json</html>")
        return httpx.Response(200, content=json.dumps({"padding": "x" * (2 * 1024 * 1024)}))

    expected = {
        "timeout": "provider_timeout",
        "network": "provider_network_error",
        "json": "provider_invalid_response",
        "size": "provider_response_too_large",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolFailure) as caught:
            await QWeatherAdapter(HOST, KEY, client).get_weather(weather_input())
    assert caught.value.code == expected[kind]
    assert KEY not in str(caught.value)


async def test_total_deadline(monkeypatch):
    monkeypatch.setattr("travel_tools.providers.http.REQUEST_TIMEOUT_SECONDS", 0.01)

    async def handler(request):
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=forecast())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolFailure) as caught:
            await QWeatherAdapter(HOST, KEY, client).get_weather(weather_input())
    assert caught.value.code == "provider_timeout"


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "http://x.qweatherapi.com",
        "x.qweatherapi.com:443",
        "x.qweatherapi.com@evil.com",
        "x.qweatherapi.com/path",
        "api.qweather.com",
        "devapi.qweather.com",
    ],
)
def test_account_host_validation(host):
    with pytest.raises(ValueError):
        QWeatherAdapter(host, KEY, httpx.AsyncClient())


@pytest.mark.parametrize(
    "region,crs", [("mainland_china", "wgs84"), ("other", "gcj02"), ("mainland_china", "bd09")]
)
def test_crs_is_never_silently_relabelled(region, crs):
    with pytest.raises(ValidationError):
        GetWeatherInput(
            coordinates=Coordinates(longitude=116.41, latitude=39.92, crs=crs), region=region
        )


@pytest.mark.parametrize("days", [0, 11, True, 1.5])
def test_forecast_horizon_bounds(days):
    with pytest.raises(ValidationError):
        weather_input(days=days)

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from travel_agent.tool_encoding import decode_tool_result, encode_tool_result
from travel_tools.common import ToolFailure
from travel_tools.providers import qweather
from travel_tools.providers.qweather import QWeatherAdapter
from travel_tools.schemas.weather import GetWeatherInput

HOST, KEY = "weather-test.xy.qweatherapi.com", "offline-weather-key"


def args(**kwargs):
    return GetWeatherInput(
        coordinates={"longitude": 116.4123, "latitude": 39.9245, "crs": "gcj02"}, **kwargs
    )


def hourly():
    start = datetime(2026, 10, 10, 5, tzinfo=UTC)
    return {
        "metadata": {"attributions": ["provider-credit"]},
        "hours": [
            {
                "forecastTime": (start + timedelta(hours=i)).isoformat(),
                "condition": {"text": "晴", "code": "100"},
                "temperature": {"value": 20 + i, "unit": "°C"},
                "precipitation": {"amount": {"value": 0, "unit": "mm"}, "probability": 0},
            }
            for i in range(4)
        ],
    }


@pytest.fixture(autouse=True)
def now(monkeypatch):
    monkeypatch.setattr(qweather, "utc_now", lambda: datetime(2026, 10, 10, 4, tzinfo=UTC))


async def run(data, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as client:
        return await QWeatherAdapter(HOST, KEY, client).get_weather(args(**kwargs))


async def test_hourly_target_offset_auto_horizon_and_only_requested_result():
    def handler(request):
        assert request.url.path == "/weather/v1/hourly/39.92/116.41"
        assert dict(request.url.params) == {"hours": "4", "lang": "zh", "localTime": "true"}
        assert request.headers["X-QW-Api-Key"] == KEY
        return httpx.Response(200, json=hourly())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QWeatherAdapter(HOST, KEY, client).get_weather(
            args(mode="hourly", target_time="2026-10-10T15:00+08:00")
        )
    assert result.coverage.status == "exact" and result.coverage.provider_count == 4
    assert len(result.hours) == 1 and result.hours[0].temperature.value == 22
    assert result.hours[0].precipitation_probability == 0 and result.hours[0].humidity is None
    assert result.hours[0].wind_speed is None and result.requested_hours == 4
    assert result.alerts is None and result.zero_alert_result is None
    assert result.days is None and result.indices is None
    assert KEY not in result.model_dump_json()


@pytest.mark.parametrize(
    "target,status,count",
    [
        ("2026-10-10T14:30+08:00", "bracketing", 2),
        ("2026-10-10T18:00+08:00", "outside_range", 0),
        ("2026-10-10T06:00Z", "exact", 1),
    ],
)
async def test_target_never_silently_uses_nearest_wrong_hour(target, status, count):
    result = await run(hourly(), mode="hourly", hours=24, target_time=target)
    assert result.coverage.status == status and len(result.hours) == count
    if status == "bracketing":
        assert [point.temperature.value for point in result.hours] == [21, 22]
        assert any("no interpolation" in warning for warning in result.warnings)


async def test_missing_hour_is_gap_not_interpolated():
    data = hourly()
    data["hours"].pop(1)
    result = await run(data, mode="hourly", hours=24, target_time="2026-10-10T14:00+08:00")
    assert result.hours == [] and result.coverage.status == "gap"


async def test_cross_midnight_target_uses_absolute_time_not_day_label():
    data = hourly()
    first = datetime.fromisoformat("2026-10-10T23:00+08:00")
    for i, point in enumerate(data["hours"]):
        point["forecastTime"] = (first + timedelta(hours=i)).isoformat()
    result = await run(data, mode="hourly", hours=24, target_time="2026-10-10T16:00Z")
    assert result.coverage.status == "exact" and result.hours[0].forecast_time.day == 11
    assert result.hours[0].temperature.value == 21


@pytest.mark.parametrize(
    "start,end,status,count",
    [
        ("2026-10-10T14:00+08:00", "2026-10-10T16:00+08:00", "covered", 2),
        ("2026-10-10T14:10+08:00", "2026-10-10T14:50+08:00", "bracketing", 2),
        ("2026-10-10T12:00+08:00", "2026-10-10T15:00+08:00", "partial", 2),
        ("2026-10-10T18:00+08:00", "2026-10-10T19:00+08:00", "outside_range", 0),
    ],
)
async def test_ranges_keep_end_exclusive_partial_coverage_and_subhour_reference(
    start, end, status, count
):
    result = await run(hourly(), mode="hourly", hours=24, start_time=start, end_time=end)
    assert result.coverage.status == status and len(result.hours) == count
    original = {"status": "ok", "data": result.model_dump(mode="json")}
    assert decode_tool_result(encode_tool_result(original)) == original


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "hourly", "target_time": "2026-10-10T14:00"},
        {"mode": "daily", "target_time": "2026-10-10T14:00Z"},
        {"mode": "hourly", "start_time": "2026-10-10T14:00Z"},
        {"mode": "hourly", "start_time": "2026-10-10T14:00Z", "end_time": "2026-10-10T13:00Z"},
        {"mode": "alerts", "forecast_date": "2026-10-10"},
        {"mode": "indices", "index_types": [0, 6]},
        {"mode": "indices", "index_types": [6, 6]},
        {"mode": "hourly", "hours": 241},
        {"mode": "alerts", "days": 7},
        {"mode": "hourly", "index_types": [1]},
    ],
)
def test_invalid_or_ambiguous_parameters_are_not_ignored(changes):
    with pytest.raises(ValidationError):
        args(**changes)


@pytest.mark.parametrize(
    "target,code",
    [
        ("2026-10-09T14:00+08:00", "historical_weather_not_supported"),
        ("2026-11-10T14:00+08:00", "target_outside_supported_horizon"),
    ],
)
async def test_unsupported_time_does_not_spend_quota_or_query_current_weather(target, code):
    def forbidden(request):
        pytest.fail("Unsupported target sent to provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ToolFailure) as error:
            await QWeatherAdapter(HOST, KEY, client).get_weather(
                args(mode="hourly", target_time=target)
            )
    assert error.value.code == code


@pytest.mark.parametrize("alteration", ["empty", "duplicate", "naive", "percent", "legacy"])
async def test_bad_hourly_data_never_means_no_rain(alteration):
    data = hourly()
    if alteration == "empty":
        data["hours"] = []
    if alteration == "duplicate":
        data["hours"][1] = deepcopy(data["hours"][0])
    if alteration == "naive":
        data["hours"][0]["forecastTime"] = "2026-10-10T13:00"
    if alteration == "percent":
        data["hours"][0]["precipitation"]["probability"] = 80
    if alteration == "legacy":
        data = {"code": "200", "hourly": []}
    with pytest.raises(ToolFailure) as error:
        await run(data, mode="hourly", hours=24)
    assert error.value.code == "provider_invalid_response"


async def test_empty_current_alerts_require_explicit_success_no_future_safety_claim():
    result = await run(
        {"metadata": {"zeroResult": True, "attributions": ["delay disclaimer"]}, "alerts": []},
        mode="alerts",
    )
    assert result.zero_alert_result is True and result.alerts == []
    assert any("future" in text for text in result.warnings)
    for zero in (None, False, "true"):
        with pytest.raises(ToolFailure):
            await run({"metadata": {"zeroResult": zero}, "alerts": []}, mode="alerts")


async def test_alert_update_cancel_and_unknown_times_are_preserved():
    data = {
        "metadata": {"zeroResult": False, "attributions": ["credit"]},
        "alerts": [
            {
                "id": "updated",
                "senderName": "test bureau",
                "messageType": {"code": "update", "supersedes": ["old"]},
                "eventType": {"name": "大风", "code": "1006"},
                "issuedTime": "2026-10-10T12:00+08:00",
                "effectiveTime": "2026-10-10T13:00+08:00",
                "expireTime": "2026-10-10T18:00+08:00",
                "instruction": "Official protective advice; external data.",
                "severity": "severe",
            },
            {"id": "cancelled", "messageType": {"code": "cancel", "supersedes": ["updated"]}},
        ],
    }
    result = await run(data, mode="alerts")
    assert result.alerts[0].supersedes == ["old"] and result.alerts[0].severity == "severe"
    assert result.alerts[1].message_type == "cancel" and result.alerts[1].expire_time is None
    assert result.alerts[1].response_types is None
    original = {"status": "ok", "data": result.model_dump(mode="json")}
    assert decode_tool_result(encode_tool_result(original)) == original


def indices():
    return {
        "code": "200",
        "updateTime": "2026-10-10T12:00+08:00",
        "refer": {"sources": ["QWeather"], "license": ["license text"]},
        "daily": [
            {
                "date": "2026-10-10",
                "type": "6",
                "name": "旅游指数",
                "category": "适宜",
                "level": "1",
            },
            {
                "date": "2026-10-11",
                "type": "6",
                "name": "旅游指数",
                "category": "不适宜",
                "level": "5",
            },
        ],
    }


async def test_indices_use_documented_legacy_envelope_and_date_not_hour():
    def handler(request):
        assert request.url.path == "/v7/indices/3d"
        assert dict(request.url.params) == {"lang": "zh", "type": "6", "location": "116.41,39.92"}
        return httpx.Response(200, json=indices())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await QWeatherAdapter(HOST, KEY, client).get_weather(
            args(mode="indices", index_types=[6], forecast_date="2026-10-11")
        )
    assert len(result.indices) == 1 and result.indices[0].category == "不适宜"
    assert result.coverage.granularity == "daily_indices" and result.coverage.provider_count == 2
    assert result.sources[0].data_time == result.provider_update_time
    assert result.licenses == ["license text"] and result.indices[0].text is None
    outside = await run(indices(), mode="indices", index_types=[6], forecast_date="2026-10-13")
    assert outside.indices == [] and outside.coverage.status == "outside_range"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "403", "message": KEY},
        {"code": "200", "daily": []},
        {"hours": []},
        {"code": "200", "daily": [{"date": "2026-10-10", "type": "17"}]},
    ],
)
async def test_indices_error_missing_and_bad_type_never_become_advice(body):
    with pytest.raises(ToolFailure) as error:
        await run(body, mode="indices")
    assert KEY not in str(error.value)


async def test_daily_date_selection_keeps_only_real_matching_day():
    from test_qweather import forecast

    selected = await run(forecast(), forecast_date="2026-10-09")
    assert selected.coverage.status == "covered" and len(selected.days) == 1
    outside = await run(forecast(), forecast_date="2026-10-20")
    assert outside.days == [] and outside.coverage.status == "outside_range"

"""Evidence-preserving daily/hourly forecasts, current alerts and daily indices."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from travel_tools.common import Coordinates, StrictModel, ToolPayload


class GetWeatherInput(StrictModel):
    coordinates: Coordinates
    region: Literal["mainland_china", "other"] = "mainland_china"
    days: int = Field(default=7, ge=1, le=10, strict=True, description="Daily mode only.")
    language: Literal["zh", "en"] = "zh"
    mode: Literal["daily", "hourly", "alerts", "indices"] = "daily"
    hours: int | None = Field(
        default=None,
        ge=1,
        le=240,
        strict=True,
        description="Omit for target/range auto horizon; set only to restrict forecast hours.",
    )
    target_time: AwareDatetime | None = Field(
        default=None,
        description="UTC-offset target; adjacent samples are reference, not interpolation.",
    )
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    forecast_date: date | None = Field(
        default=None, description="Daily/indices date only, not an hourly forecast."
    )
    index_days: Literal[1, 3] = 3
    index_types: list[Annotated[int, Field(strict=True, ge=0, le=16)]] = Field(
        default_factory=lambda: [1, 3, 5, 6],
        min_length=1,
        max_length=16,
        description="Indices: 1 exercise, 3 clothing, 5 UV, 6 travel (China); use 0 alone for all.",
    )

    @model_validator(mode="after")
    def validate_coordinate_system(self) -> Self:
        expected = "gcj02" if self.region == "mainland_china" else "wgs84"
        if self.coordinates.crs != expected:
            raise ValueError(f"QWeather requires {expected} coordinates for this region")
        if self.mode != "daily" and "days" in self.model_fields_set:
            raise ValueError("days only applies to daily; use hours or index_days in other modes")
        if self.mode != "indices" and {"index_types", "index_days"} & self.model_fields_set:
            raise ValueError("index_types/index_days require mode=indices")
        timed = (
            self.target_time is not None or self.start_time is not None or self.end_time is not None
        )
        if timed and self.mode != "hourly":
            raise ValueError("target_time/start_time/end_time require mode=hourly")
        if self.target_time is not None and (
            self.start_time is not None or self.end_time is not None
        ):
            raise ValueError("use a target_time or time range, not both")
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("time ranges require both start_time and end_time")
        if self.start_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must follow start_time")
        if self.hours is not None and self.mode != "hourly":
            raise ValueError("hours requires mode=hourly")
        if self.forecast_date is not None and self.mode not in ("daily", "indices"):
            raise ValueError(
                "forecast_date only selects daily forecasts/indices; alerts are current"
            )
        if 0 in self.index_types and self.index_types != [0]:
            raise ValueError("all indices (0) cannot be combined with other types")
        if len(set(self.index_types)) != len(self.index_types):
            raise ValueError("index_types must be unique")
        if (
            self.mode == "indices"
            and self.region == "other"
            and any(t not in (1, 2, 3, 4, 5) for t in self.index_types)
        ):
            raise ValueError("outside China explicitly select supported global index types 1..5")
        return self


class WeatherQuantity(StrictModel):
    """Keep missing values/units unknown; do not guess a missing unit."""

    value: float | None = None
    unit: str | None = None


class WeatherPeriod(StrictModel):
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    condition: str | None = None
    condition_code: str | None = None
    temperature_max: WeatherQuantity | None = None
    temperature_min: WeatherQuantity | None = None
    wind_speed: WeatherQuantity | None = None
    wind_gust_max: WeatherQuantity | None = None
    precipitation_amount: WeatherQuantity | None = None
    precipitation_probability: float | None = Field(default=None, ge=0, le=1)
    precipitation_type: str | None = None
    humidity: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("Weather period end must follow start")
        return self


class DailyWeather(StrictModel):
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    temperature_max: WeatherQuantity | None = None
    temperature_min: WeatherQuantity | None = None
    daytime: WeatherPeriod | None = None
    nighttime: WeatherPeriod | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("Forecast end must follow start")
        return self


class HourlyWeather(StrictModel):
    forecast_time: AwareDatetime
    condition: str | None = None
    condition_code: str | None = None
    temperature: WeatherQuantity | None = None
    feels_like: WeatherQuantity | None = None
    humidity: float | None = Field(default=None, ge=0, le=1)
    wind_speed: WeatherQuantity | None = None
    wind_gust: WeatherQuantity | None = None
    wind_direction_degree: float | None = Field(default=None, ge=0, le=360)
    wind_direction_compass: str | None = None
    wind_scale: int | None = Field(default=None, ge=0)
    precipitation_amount: WeatherQuantity | None = None
    precipitation_intensity: WeatherQuantity | None = None
    precipitation_probability: float | None = Field(default=None, ge=0, le=1)
    precipitation_type: str | None = None
    pressure: WeatherQuantity | None = None
    visibility: WeatherQuantity | None = None
    dew_point: WeatherQuantity | None = None
    cloud_cover: float | None = Field(default=None, ge=0, le=1)
    uv_index: float | None = Field(default=None, ge=0)


class WeatherAlert(StrictModel):
    id: str = Field(min_length=1)
    sender_name: str | None = None
    issued_time: AwareDatetime | None = None
    message_type: str | None = None
    supersedes: list[str] | None = None
    event_name: str | None = None
    event_code: str | None = None
    urgency: str | None = None
    severity: str | None = None
    certainty: str | None = None
    color_code: str | None = None
    effective_time: AwareDatetime | None = None
    onset_time: AwareDatetime | None = None
    expire_time: AwareDatetime | None = None
    headline: str | None = None
    description: str | None = None
    criteria: str | None = None
    response_types: list[str] | None = None
    instruction: str | None = None


class WeatherIndex(StrictModel):
    forecast_date: date
    type: int = Field(ge=1, le=16)
    name: str | None = None
    level: str | None = None
    category: str | None = None
    text: str | None = None


class WeatherCoverage(StrictModel):
    status: Literal[
        "not_requested", "exact", "bracketing", "covered", "partial", "outside_range", "gap"
    ]
    granularity: Literal["daily", "hourly", "current_alerts", "daily_indices"]
    provider_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    available_start: AwareDatetime | None = None
    available_end: AwareDatetime | None = None
    available_dates: list[date] = Field(default_factory=list)
    target_time: AwareDatetime | None = None
    requested_start: AwareDatetime | None = None
    requested_end: AwareDatetime | None = None
    forecast_date: date | None = None


class GetWeatherOutput(ToolPayload):
    coordinates: Coordinates
    queried_coordinates: Coordinates
    forecast_kind: Literal["daily", "hourly", "alerts", "indices"] = "daily"
    requested_days: int | None = Field(default=None, ge=1, le=10)
    requested_hours: int | None = Field(default=None, ge=1, le=240)
    days: list[DailyWeather] | None = Field(default=None, max_length=10)
    hours: list[HourlyWeather] | None = Field(default=None, max_length=240)
    alerts: list[WeatherAlert] | None = None
    indices: list[WeatherIndex] | None = None
    zero_alert_result: bool | None = None
    coverage: WeatherCoverage | None = None
    attributions: list[str] = Field(default_factory=list)
    licenses: list[str] = Field(default_factory=list)
    provider_update_time: AwareDatetime | None = None

"""Provider-neutral, evidence-preserving daily weather contract."""

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from travel_tools.common import Coordinates, StrictModel, ToolPayload


class GetWeatherInput(StrictModel):
    coordinates: Coordinates
    region: Literal["mainland_china", "other"] = "mainland_china"
    days: int = Field(default=7, ge=1, le=10, strict=True)
    language: Literal["zh", "en"] = "zh"

    @model_validator(mode="after")
    def validate_coordinate_system(self) -> Self:
        expected = "gcj02" if self.region == "mainland_china" else "wgs84"
        if self.coordinates.crs != expected:
            raise ValueError(f"QWeather requires {expected} coordinates for this region")
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


class GetWeatherOutput(ToolPayload):
    coordinates: Coordinates
    queried_coordinates: Coordinates
    forecast_kind: Literal["daily"] = "daily"
    requested_days: int = Field(ge=1, le=10)
    days: list[DailyWeather] = Field(max_length=10)
    attributions: list[str] = Field(default_factory=list)

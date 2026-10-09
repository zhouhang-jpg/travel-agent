import pytest
from pydantic import ValidationError

from travel_tools.common import Coordinates, Source


def test_source_provider_cannot_be_whitespace_and_missing_data_time_stays_unknown():
    with pytest.raises(ValidationError):
        Source(provider=" \u3000 ")
    source = Source(provider=" example ")
    assert source.provider == "example"
    assert source.data_time is None and source.retrieved_at.tzinfo is not None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -181])
def test_coordinate_nonfinite_and_out_of_range_rejected(value):
    with pytest.raises(ValidationError):
        Coordinates(longitude=value, latitude=39.9, crs="gcj02")

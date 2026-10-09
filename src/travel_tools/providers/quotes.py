"""Supplier-neutral extension points for read-only search adapters.

Adapters use configured customer-search services or independently queried public
pages, never merchant supply callbacks or desktop browser sessions.
Authentication, provider failures and unavailable capability must raise
ToolFailure rather than returning an empty successful search.
"""

from typing import Protocol, runtime_checkable

from travel_tools.schemas.quotes import (
    SearchCoachesInput,
    SearchCoachesOutput,
    SearchFlightsInput,
    SearchFlightsOutput,
    SearchHotelsInput,
    SearchHotelsOutput,
    SearchTrainsInput,
    SearchTrainsOutput,
)


@runtime_checkable
class FlightSearchAdapter(Protocol):
    async def search(self, request: SearchFlightsInput) -> SearchFlightsOutput: ...


@runtime_checkable
class TrainSearchAdapter(Protocol):
    async def search(self, request: SearchTrainsInput) -> SearchTrainsOutput: ...


@runtime_checkable
class CoachSearchAdapter(Protocol):
    async def search(self, request: SearchCoachesInput) -> SearchCoachesOutput: ...


@runtime_checkable
class HotelSearchAdapter(Protocol):
    async def search(self, request: SearchHotelsInput) -> SearchHotelsOutput: ...

"""Extension points only; no supplier has been implemented or authorized yet.

An adapter must use a documented customer-search API, not a merchant supply
callback. Authentication, provider failures and unavailable capability must raise
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

"""Per-supplier breakers: fail fast, probe once, stay invisible while a route works."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from travel_agent.runtime_context import build_runtime_message, catalog_with_health
from travel_tools.circuit import (
    BROWSER_RUNTIME,
    BreakerPolicy,
    CircuitBreakers,
    CircuitOpen,
    failure_kind,
)
from travel_tools.common import StrictModel, ToolFailure, ToolPayload
from travel_tools.providers.ticket_routing import TrainRouter
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.scheduling import (
    SupplierPolicy,
    SupplierScheduler,
    report_supplier_failure,
    supplier_guard,
    supplier_slot,
)

T0 = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def wall(self):
        return T0 + timedelta(seconds=self.now - 1000)


def breakers(clock=None, **policy):
    clock = clock or Clock()
    return CircuitBreakers(BreakerPolicy(**policy), clock=clock, wall=clock.wall), clock


def timeout():
    return ToolFailure("provider_timeout", "timed out", True)


def test_consecutive_failures_open_and_success_resets():
    b, clock = breakers()
    for _ in range(2):
        b.record_failure("amap", timeout(), b.admit("amap"))
    b.record_success("amap", b.admit("amap"))
    for _ in range(2):
        b.record_failure("amap", timeout(), b.admit("amap"))
    assert b.state("amap") == "closed"
    b.record_failure("amap", timeout(), b.admit("amap"))
    with pytest.raises(CircuitOpen) as opened:
        b.admit("amap")
    assert opened.value.code == "provider_circuit_open" and opened.value.retryable
    assert opened.value.retry_at == T0 + timedelta(seconds=60)
    assert b.state("qweather") == "closed"  # Breakers are per supplier.


@pytest.mark.parametrize(
    "code",
    [
        "provider_location_not_found",
        "invalid_arguments",
        "outside_sale_window",
        "provider_date_mismatch",
        "provider_rejected",
    ],
)
def test_business_results_neither_count_nor_reset(code):
    b, _ = breakers()
    b.record_failure("12306", timeout(), False)
    b.record_failure("12306", timeout(), False)
    b.record_failure("12306", ToolFailure(code, "business"), False)
    b.record_failure("12306", timeout(), False)
    assert b.state("12306") == "open"


def test_raw_transport_errors_and_cancellation_classification():
    assert failure_kind("amap", TimeoutError()) == "failure"
    assert failure_kind("amap", httpx.ConnectError("x")) == "failure"
    assert failure_kind("amap", asyncio.CancelledError()) == "ignore"
    assert failure_kind("amap", ToolFailure("provider_http_error", "404", False)) == "ignore"
    assert failure_kind("amap", ToolFailure("provider_http_error", "503", True)) == "failure"


def test_half_open_admits_one_probe_and_backs_off_with_cap():
    b, clock = breakers(open_seconds=60, max_open_seconds=200)
    for _ in range(3):
        b.record_failure("flyai", timeout(), False)
    clock.now += 61
    probe = b.admit("flyai")
    assert probe is True
    with pytest.raises(CircuitOpen) as second:
        b.admit("flyai")
    assert second.value.retry_at is None  # Probing: no promised time.
    b.record_failure("flyai", timeout(), probe)
    assert b.blocked("flyai")["retry_at"] == clock.wall() + timedelta(seconds=120)
    clock.now += 121
    b.record_failure("flyai", timeout(), b.admit("flyai"))
    assert b.blocked("flyai")["retry_at"] == clock.wall() + timedelta(seconds=200)
    clock.now += 201
    b.record_success("flyai", b.admit("flyai"))
    assert b.state("flyai") == "closed" and b.admit("flyai") is False


def test_cancelled_or_business_probe_releases_the_probe_slot():
    b, clock = breakers()
    for _ in range(3):
        b.record_failure("ceair", timeout(), False)
    clock.now += 61
    b.record_failure("ceair", asyncio.CancelledError(), b.admit("ceair"))
    b.record_failure("ceair", ToolFailure("invalid_arguments", "x"), b.admit("ceair"))
    assert b.admit("ceair") is True


def test_stuck_probe_lease_expires():
    b, clock = breakers(probe_lease_seconds=30)
    for _ in range(3):
        b.record_failure("csair", timeout(), False)
    clock.now += 61
    b.admit("csair")
    clock.now += 31
    assert b.admit("csair") is True


def test_immediate_open_kinds_and_retry_after_hint():
    b, clock = breakers()
    limited = ToolFailure("provider_rate_limited", "429", True)
    limited.retry_after_seconds = 15
    b.record_failure("bocha", limited, False)
    assert b.blocked("bocha")["retry_at"] == T0 + timedelta(seconds=15)

    quota = ToolFailure("provider_rate_limited", "daily quota", True)
    quota.quota_exhausted = True
    b.record_failure("amap", quota, False)
    assert b.blocked("amap")["retry_at"] == T0 + timedelta(seconds=600)

    b.record_failure("12306", ToolFailure("provider_access_blocked", "captcha"), False)
    assert b.blocked("12306")["retry_at"] == T0 + timedelta(seconds=600)

    b.record_failure("qweather", ToolFailure("provider_authentication", "401"), False)
    status = b.blocked("qweather")
    assert status["retry_at"] == T0 + timedelta(seconds=1800) and "凭据" in status["reason"]


def test_disabled_policy_is_a_no_op():
    b, _ = breakers(enabled=False)
    for _ in range(10):
        b.record_failure("amap", timeout(), b.admit("amap"))
    assert b.admit("amap") is False and b.blocked("amap") is None


def test_browser_scope_separates_launch_failures_from_site_failures():
    b, _ = breakers()
    launch = ToolFailure("provider_browser_error", "launch", True)
    launch.breaker_scope = BROWSER_RUNTIME
    for _ in range(3):
        b.record_failure("12306", launch, False)
        b.record_failure(BROWSER_RUNTIME, launch, False)
    assert b.state("12306") == "closed" and b.state(BROWSER_RUNTIME) == "open"

    b2, clock = breakers()
    for _ in range(3):
        b2.record_failure(BROWSER_RUNTIME, launch, False)
    clock.now += 61
    probe = b2.admit(BROWSER_RUNTIME)
    # The site timed out, so the browser itself started: that probe succeeded.
    b2.record_failure(BROWSER_RUNTIME, timeout(), probe)
    assert b2.state(BROWSER_RUNTIME) == "closed"

    configuration = ToolFailure("provider_unavailable", "install chromium")
    configuration.breaker_scope = "configuration"
    assert failure_kind("12306", configuration) == "ignore"


async def test_slot_fails_fast_without_waiting_for_the_supplier_queue():
    b, _ = breakers()
    scheduler = SupplierScheduler({"amap": SupplierPolicy(1, 0)}, b)
    events = []

    async def report(stage, supplier, request_id):
        events.append((stage, supplier))

    from travel_tools.scheduling import current_scheduler, supplier_progress

    token = current_scheduler.set(scheduler)
    progress = supplier_progress.set(report)
    try:
        for _ in range(3):
            with pytest.raises(ToolFailure):
                async with supplier_slot("amap"):
                    raise timeout()
        hold = asyncio.Event()

        async def occupy():
            async with scheduler._gated("amap"):
                await hold.wait()

        holder = asyncio.create_task(occupy())
        await asyncio.sleep(0)
        async with asyncio.timeout(1):
            with pytest.raises(CircuitOpen):
                async with supplier_slot("amap"):
                    pass
        hold.set()
        await holder
        assert ("circuit_open", "amap") in events
        # Body-level rejections found after the slot closed are still recorded.
        report_supplier_failure("bocha", ToolFailure("provider_authentication", "401"))
        assert b.state("bocha") == "open"
        async with supplier_guard("unused"):
            pass
    finally:
        supplier_progress.reset(progress)
        current_scheduler.reset(token)


class Input(StrictModel):
    value: int = 0


class Output(ToolPayload):
    value: int


def registry_with(clock, specs):
    b = CircuitBreakers(BreakerPolicy(), clock=clock, wall=clock.wall)
    registry = ToolRegistry(scheduler=SupplierScheduler({}, b))
    for spec in specs:
        registry.register(spec)
    return registry, b


async def test_dispatch_returns_retry_at_only_for_open_breakers():
    clock = Clock()

    async def handler(_):
        async with supplier_slot("qweather"):
            raise timeout()

    spec = ToolSpec(
        "get_weather", "w", Input, Output, handler, "ready", routes=(("qweather", ("qweather",)),)
    )
    registry, _ = registry_with(clock, [spec])
    plain = await registry.dispatch("get_weather", {})
    assert "retry_at" not in json.loads(plain.model_dump_json())["error"]
    for _ in range(2):
        await registry.dispatch("get_weather", {})
    opened = await registry.dispatch("get_weather", {})
    error = json.loads(opened.model_dump_json())["error"]
    assert error["code"] == "provider_circuit_open" and error["retryable"] is True
    assert error["retry_at"] == "2026-10-28T06:01:00Z"


async def test_train_router_skips_to_fallback_while_primary_is_open():
    clock = Clock()
    b = CircuitBreakers(BreakerPolicy(), clock=clock, wall=clock.wall)
    for _ in range(3):
        b.record_failure("12306", timeout(), False)
    calls = []

    class Primary:
        async def search(self, request):
            calls.append("12306")
            async with supplier_slot("12306"):
                raise AssertionError("an open breaker must not reach the supplier")

    class Coverage:
        fallback_from = fallback_reason = None

    class Result:
        def __init__(self):
            self.warnings, self.coverage = [], Coverage()

        def model_copy(self, deep):
            return self

    class Fallback:
        async def search(self, request):
            calls.append("flyai")
            return Result()

    class Request:
        provider = "configured"

    from travel_tools.scheduling import current_scheduler

    token = current_scheduler.set(SupplierScheduler({}, b))
    try:
        result = await TrainRouter(
            {"12306": Primary(), "flyai": Fallback()}, "12306", "flyai"
        ).search(Request())
    finally:
        current_scheduler.reset(token)
    assert calls == ["12306", "flyai"]
    assert result.coverage.fallback_reason == "provider_circuit_open"


def _spec(name, routes, choice="auto"):
    async def handler(_):
        return Output(value=1)

    return ToolSpec(name, name, Input, Output, handler, "ready", routes=routes, route_choice=choice)


def test_snapshot_shows_only_tools_without_a_working_route():
    clock = Clock()
    registry, b = registry_with(
        clock,
        [
            _spec("search_hotels", (("flyai", ("flyai",)),)),
            _spec(
                "search_trains",
                (("12306", (BROWSER_RUNTIME, "12306")), ("flyai", ("flyai",))),
            ),
            _spec(
                "search_flights",
                (("flyai", ("flyai",)), ("ceair", (BROWSER_RUNTIME, "ceair"))),
                "model",
            ),
            _spec("get_weather", (("qweather", ("qweather",)),)),
        ],
    )
    catalog = registry.catalog()
    definitions = registry.model_definitions()
    shanghai = ZoneInfo("Asia/Shanghai")
    now = clock.wall().astimezone(shanghai)
    assert catalog_with_health(registry, catalog, now) == catalog

    for _ in range(3):
        b.record_failure("flyai", timeout(), False)
    overlaid = {item["name"]: item for item in catalog_with_health(registry, catalog, now)}
    assert overlaid["search_hotels"]["availability"] == "temporarily_unavailable"
    assert overlaid["search_hotels"]["retry_at"] == "2026-10-28T14:01:00+08:00"
    assert overlaid["search_trains"] == next(i for i in catalog if i["name"] == "search_trains")
    assert overlaid["search_flights"]["availability"] == "ready"
    assert "provider=ceair" in overlaid["search_flights"]["reason"]
    assert overlaid["get_weather"]["reason"] is None

    b.record_failure("12306", ToolFailure("provider_access_blocked", "captcha"), False)
    overlaid = {item["name"]: item for item in catalog_with_health(registry, catalog, now)}
    trains = overlaid["search_trains"]
    assert trains["availability"] == "temporarily_unavailable"
    assert trains["retry_at"] == "2026-10-28T14:01:00+08:00"  # Earliest route back.

    # Static catalog and tool definitions are untouched: prompt prefix and manifest stay stable.
    assert registry.catalog() == catalog and registry.model_definitions() == definitions

    first = build_runtime_message(now, "Asia/Shanghai", catalog_with_health(registry, catalog, now))
    history = [first]
    again = build_runtime_message(
        now + timedelta(seconds=5),
        "Asia/Shanghai",
        catalog_with_health(registry, catalog, now),
        history=history,
    )
    assert "tool_availability" not in again["content"]  # Unchanged while still open.
    clock.now += 601
    later = clock.wall().astimezone(shanghai)
    recovered = build_runtime_message(
        later, "Asia/Shanghai", catalog_with_health(registry, catalog, later), history=history
    )
    assert "temporarily_unavailable" not in recovered["content"]
    assert "tool_availability" in recovered["content"]


def test_bootstrap_registers_routes_and_breaker_settings():
    from travel_tools.bootstrap import build_registry
    from travel_tools.config import Settings

    settings = Settings(
        _env_file=None,
        amap_api_key="test-key",
        circuit_failure_threshold=5,
        circuit_open_seconds=30,
    )
    registry = build_registry(settings, httpx.AsyncClient())
    assert registry._tools["search_places"].routes == (("amap", ("amap",)),)
    assert registry.scheduler.breakers.policy.failure_threshold == 5
    assert registry.scheduler.breakers.policy.open_seconds == 30

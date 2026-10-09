import asyncio

import pytest
from pydantic import Field

from travel_tools.common import StrictModel, ToolFailure, ToolPayload
from travel_tools.registry import ToolRegistry, ToolSpec


class Input(StrictModel):
    count: int = Field(ge=1)


class Output(ToolPayload):
    count: int


async def handler(request):
    return Output(count=request.count)


def registry(callback=handler, **kwargs):
    result = ToolRegistry(**kwargs)
    result.register(ToolSpec("test", "test", Input, Output, callback, "ready"))
    result.register(
        ToolSpec("missing", "missing", Input, Output, None, "not_implemented", "No supplier.")
    )
    return result


async def test_definition_filters_and_dispatch():
    tools = registry()
    assert [d["function"]["name"] for d in tools.model_definitions()] == ["test"]
    assert len(tools.catalog()) == 2
    result = await tools.dispatch("test", {"count": 2}, call_id="call-1")
    assert result.status == "ok" and result.data["count"] == 2
    assert result.call_id == "call-1" and result.finished_at >= result.started_at
    assert (await tools.dispatch("missing", {})).error.code == "tool_unavailable"
    assert (await tools.dispatch("unknown", {})).error.code == "unknown_tool"


async def test_validation_does_not_echo_input():
    result = await registry().dispatch("test", {"count": "super-secret", "extra": "secret"})
    assert result.error.code == "invalid_arguments"
    assert "super-secret" not in result.model_dump_json()


async def test_timeout_and_exception_sanitization():
    async def slow(request):
        await asyncio.sleep(1)

    assert (
        await registry(slow, timeout_seconds=0.01).dispatch("test", {"count": 1})
    ).error.code == "timeout"

    async def broken(request):
        raise RuntimeError("SECRET URL WITH API KEY")

    result = await registry(broken).dispatch("test", {"count": 1})
    assert result.error.code == "internal_error" and "SECRET" not in result.model_dump_json()


async def test_controlled_failure_and_output_contract():
    async def controlled(request):
        raise ToolFailure("quota_exceeded", "Provider quota exhausted.", True)

    assert (await registry(controlled).dispatch("test", {"count": 1})).error.retryable

    async def bad_output(request):
        return ToolPayload()

    assert (
        await registry(bad_output).dispatch("test", {"count": 1})
    ).error.code == "internal_error"


async def test_size_bound_and_finite_json():
    assert (
        await registry().dispatch("test", {"count": float("nan")})
    ).error.code == "invalid_arguments"
    assert (
        await registry().dispatch("test", {"count": "x" * (256 * 1024)})
    ).error.code == "arguments_too_large"


async def test_cancellation_propagates():
    async def cancelled(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await registry(cancelled).dispatch("test", {"count": 1})


def test_duplicate_and_inconsistent_registration_rejected():
    tools = registry()
    with pytest.raises(ValueError, match="Duplicate"):
        tools.register(ToolSpec("test", "test", Input, Output, handler, "ready"))
    with pytest.raises(ValueError, match="Only ready"):
        tools.register(ToolSpec("bad", "bad", Input, Output, None, "ready"))


async def test_large_result_fails_explicitly_without_partial_data():
    class LargeOutput(ToolPayload):
        text: str

    async def large(request):
        return LargeOutput(text="x" * (2 * 1024 * 1024))

    tools = ToolRegistry()
    tools.register(ToolSpec("large", "large", Input, LargeOutput, large, "ready"))
    result = await tools.dispatch("large", {"count": 1})
    assert result.error.code == "result_too_large" and result.data is None


async def test_semaphore_queue_is_included_in_deadline():
    active = 0
    maximum = 0

    async def busy(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(1)
        finally:
            active -= 1
        return Output(count=1)

    tools = registry(busy, timeout_seconds=0.02, max_concurrent_calls=1)
    results = await asyncio.gather(*(tools.dispatch("test", {"count": 1}) for _ in range(3)))
    assert maximum == 1
    assert all(result.error.code == "timeout" for result in results)

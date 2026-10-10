"""Deterministic overlap, barriers, independent commits, history and deadlines."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from travel_agent.durable_storage import DurableStore
from travel_agent.graph_runtime import GraphRuntime, failure
from travel_agent.runner import RunLimits
from travel_agent.storage import ConversationStore
from travel_tools.common import Source, StrictModel, ToolFailure, ToolPayload
from travel_tools.registry import ToolRegistry, ToolSpec
from travel_tools.scheduling import SupplierPolicy, SupplierScheduler, supplier_slot


class Input(StrictModel):
    value: int = 0


class Output(ToolPayload):
    value: int


def call(index, name="probe"):
    return {
        "id": str(index),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"value": index}),
        },
    }


async def batch(tmp_path, calls, handler, *, concurrency=4, failpoint=None, deadline=3):
    store = ConversationStore("sqlite+aiosqlite:///" + (tmp_path / "test.db").as_posix())
    await store.initialize()
    facts = DurableStore(store)
    lease = await facts.accept((await store.create("Asia/Shanghai"))["id"], "测试", "r1")
    registry = ToolRegistry(max_concurrent_calls=concurrency, timeout_seconds=deadline)
    registry.register(
        ToolSpec(
            "probe",
            "只读查询",
            Input,
            Output,
            handler,
            "ready",
            execution="parallel_read",
        )
    )
    registry.register(ToolSpec("barrier", "状态边界", Input, Output, handler, "ready"))
    runtime = GraphRuntime(facts, None, registry, RunLimits(), None, failpoint=failpoint)
    key = lease["run_id"] + "/model/0"
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": calls,
        "reasoning_content": "private",
        "vendor_extra": {"full": [1, None]},
    }
    await facts.begin_effect(lease, key, "model", 12)
    await facts.commit_effect(lease, key, {"message": message}, message)
    return store, facts, lease, runtime, key


async def test_parallel_overlap_limit_and_same_name_events(tmp_path):
    active = maximum = 0
    intervals = {}
    release = asyncio.Event()
    saved = asyncio.Event()

    async def failpoint(name):
        if name == "tool_saved_before_checkpoint":
            saved.set()

    async def handler(args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        start = time.monotonic()
        if args.value == 0:
            await release.wait()
        else:
            await asyncio.sleep(0.05)
        intervals[args.value] = (start, time.monotonic())
        active -= 1
        if args.value == 2:
            raise ToolFailure("provider_access_blocked", "停止")
        return Output(value=args.value)

    store, facts, lease, runtime, key = await batch(
        tmp_path,
        [call(i) for i in range(4)],
        handler,
        concurrency=2,
        failpoint=failpoint,
    )
    try:
        task = asyncio.create_task(
            runtime.tools_node(
                {"model_key": key, "step": 0}, SimpleNamespace(context=SimpleNamespace(lease=lease))
            )
        )
        await asyncio.wait_for(saved.wait(), 2)
        fast = await facts.effect(key + "/tool/1")
        assert fast.status == "complete"
        assert (await facts.effect(key + "/tool/0")).status == "started"
        assert len(await facts.history(lease["conversation_id"])) == 2
        release.set()
        await task
        assert maximum == 2
        assert intervals[1][0] < intervals[0][1]
        history = await facts.history(lease["conversation_id"])
        assert [m["tool_call_id"] for m in history[2:]] == ["0", "1", "2", "3"]
        assert [json.loads(m["content"])["status"] for m in history[2:]] == [
            "ok",
            "ok",
            "error",
            "ok",
        ]
        assert history[1]["vendor_extra"] == {"full": [1, None]}
        events = await facts.events(lease["conversation_id"])
        finished = [e for e in events if e["type"] == "tool_finished"]
        assert len({e["call_id"] for e in finished}) == 4
        assert finished[0]["call_id"].endswith("/1")
        from travel_agent.durable_service import DurableService

        snapshot = await DurableService(store, None, runtime.registry, RunLimits(), None).get(
            lease["conversation_id"]
        )
        assert len(snapshot["tool_progress"]) == 4
        assert snapshot["tool_progress"][key + "/tool/2"]["status"] == "error"
        assert snapshot["event_seq"] == max(e["event_seq"] for e in events)
    finally:
        await store.close()


async def test_exclusive_barrier_and_subsequent_dependency(tmp_path):
    completed = []

    async def handler(args):
        if args.value == 2:
            assert set(completed) == {0, 1}
        if args.value == 3:
            assert 2 in completed
        await asyncio.sleep(0.01)
        completed.append(args.value)
        return Output(value=args.value)

    store, facts, lease, runtime, key = await batch(
        tmp_path,
        [call(0), call(1), call(2, "barrier"), call(3)],
        handler,
    )
    try:
        await runtime.tools_node(
            {"model_key": key, "step": 0}, SimpleNamespace(context=SimpleNamespace(lease=lease))
        )
        assert completed[-2:] == [2, 3]
        assert len(await facts.history(lease["conversation_id"])) == 6
    finally:
        await store.close()


async def test_timeout_repair_keeps_already_committed_success_in_order(tmp_path):
    saved = asyncio.Event()

    async def failpoint(name):
        if name == "tool_saved_before_checkpoint":
            saved.set()

    async def handler(args):
        await asyncio.sleep(1 if args.value == 0 else 0.01)
        return Output(value=args.value)

    store, facts, lease, runtime, key = await batch(
        tmp_path, [call(0), call(1)], handler, failpoint=failpoint
    )
    try:
        task = asyncio.create_task(
            runtime.tools_node(
                {"model_key": key, "step": 0}, SimpleNamespace(context=SimpleNamespace(lease=lease))
            )
        )
        await asyncio.wait_for(saved.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await facts.finalize(lease, failure("run_timeout"))
        history = await facts.history(lease["conversation_id"])
        results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
        assert [r["call_id"] for r in results] == ["0", "1"]
        assert [r["status"] for r in results] == ["error", "ok"]
        assert (await facts.effect(key + "/tool/1")).payload["status"] == "ok"
    finally:
        await store.close()


async def test_shared_supplier_gate_covers_nested_fanout_and_frequency():
    from travel_tools.scheduling import current_scheduler

    scheduler = SupplierScheduler({"amap": SupplierPolicy(2, 30)})
    starts = []
    active = maximum = 0

    async def request():
        nonlocal active, maximum
        async with supplier_slot("amap"):
            starts.append(time.monotonic())
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.06)
            active -= 1

    token = current_scheduler.set(scheduler)
    try:
        await asyncio.gather(*(request() for _ in range(8)))
        assert maximum == 2
        assert all(b - a >= 0.03 for a, b in zip(starts, starts[1:], strict=False))
    finally:
        current_scheduler.reset(token)


async def test_cache_normalization_expiry_refresh_error_and_original_time():
    count = 0

    async def handler(args):
        nonlocal count
        count += 1
        if args.value == 9:
            raise ToolFailure("provider_rate_limited", "受限")
        return Output(value=count, sources=[Source(provider="test")])

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "probe",
            "查询",
            Input,
            Output,
            handler,
            "ready",
            requires_external_service=True,
            execution="parallel_read",
            cache_seconds=0.05,
        )
    )
    first = await registry.dispatch("probe", {})
    cached = await registry.dispatch("probe", {"value": 0}, call_id="second")
    assert cached.reuse == "cache" and cached.call_id == "second"
    assert cached.data == first.data and cached.original_started_at == first.started_at
    assert cached.data["sources"][0]["retrieved_at"] == first.data["sources"][0]["retrieved_at"]
    await registry.dispatch("probe", {"value": 1})
    await registry.dispatch("probe", {"force_refresh": True})
    await asyncio.sleep(0.06)
    assert (await registry.dispatch("probe", {})).reuse == "none"
    for _ in range(2):
        assert (await registry.dispatch("probe", {"value": 9})).status == "error"
    assert count == 6


async def test_queued_timeout_does_not_start_second_handler():
    started = []

    async def handler(args):
        started.append(args.value)
        await asyncio.sleep(0.3)
        return Output(value=0)

    registry = ToolRegistry(max_concurrent_calls=1, timeout_seconds=0.05)
    registry.register(ToolSpec("probe", "查询", Input, Output, handler, "ready"))
    results = await asyncio.gather(*(registry.dispatch("probe", {"value": i}) for i in range(2)))
    assert started == [0]
    assert all(r.error.code == "timeout" for r in results)


async def test_120_parallel_calls_and_full_next_model_history(tmp_path):
    async def handler(args):
        return Output(value=args.value)

    store, facts, lease, runtime, key = await batch(
        tmp_path, [call(i) for i in range(120)], handler
    )

    class Model:
        async def complete(self, messages, tools):
            from travel_agent.models import ModelReply

            results = [m for m in messages if m["role"] == "tool"]
            assert [m["tool_call_id"] for m in results] == [str(i) for i in range(120)]
            assert all(
                json.loads(m["content"])["data"]["value"] == i for i, m in enumerate(results)
            )
            assert messages[2]["reasoning_content"] == "private"
            assert messages[2]["vendor_extra"] == {"full": [1, None]}
            return ModelReply({"role": "assistant", "content": "完整结果"}, "stop", {})

    runtime.model = Model()
    try:
        context = SimpleNamespace(context=SimpleNamespace(lease=lease))
        state = await runtime.tools_node({"model_key": key, "step": 0}, context)
        assert (await runtime.model_node(state, context))["route"] == "final"
    finally:
        await store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"rooms": 2},
        {"check_out": "2026-10-19"},
        {"check_in": "2026-10-16"},
        {"travelers": {"adults": 2}},
        {"travelers": {"adults": 1, "children_ages": [8]}},
        {"preferred_currency": "USD"},
        {"destination": {"query": "苏州"}},
    ],
)
async def test_quote_cache_keys_include_all_party_date_currency_conditions(change):
    from travel_tools.schemas.quotes import SearchHotelsInput

    count = 0

    async def handler(args):
        nonlocal count
        count += 1
        return Output(value=count)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "hotel",
            "查询",
            SearchHotelsInput,
            Output,
            handler,
            "ready",
            execution="parallel_read",
            cache_seconds=60,
        )
    )
    args = {
        "destination": {"query": "杭州"},
        "check_in": "2026-10-17",
        "check_out": "2026-10-18",
        "travelers": {"adults": 1},
        "rooms": 1,
    }
    assert (await registry.dispatch("hotel", args)).status == "ok"
    assert (await registry.dispatch("hotel", args)).reuse == "cache"
    assert (await registry.dispatch("hotel", {**args, **change})).reuse == "none"
    assert count == 2


async def test_opening_hours_internal_fanout_uses_shared_production_supplier_limits():
    from travel_tools.bootstrap import build_registry
    from travel_tools.config import Settings, SupplierLimit

    counts = {}
    active = {}
    maximum = {}

    async def response(request):
        name = "amap" if "amap.com" in request.url.host else "bocha"
        counts[name] = counts.get(name, 0) + 1
        active[name] = active.get(name, 0) + 1
        maximum[name] = max(maximum.get(name, 0), active[name])
        await asyncio.sleep(0.02)
        active[name] -= 1
        if name == "amap":
            return httpx.Response(200, json={"status": "1", "count": "0", "pois": []})
        return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": []}}})

    settings = Settings(
        _env_file=None,
        amap_api_key="test",
        bocha_api_key="test",
        supplier_limits={
            "amap": SupplierLimit(concurrency=1, requests_per_second=0),
            "bocha": SupplierLimit(concurrency=1, requests_per_second=0),
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        registry = build_registry(settings, client)
        results = await asyncio.gather(
            *(
                registry.dispatch(
                    "get_attraction_opening_hours",
                    {
                        "attraction_name": name,
                        "city": "北京",
                        "visit_date": "2026-10-17",
                    },
                )
                for name in ("故宫", "天坛")
            )
        )
        await registry.close()
    assert all(r.status == "ok" for r in results)
    assert counts == {"amap": 2, "bocha": 4} and maximum == {"amap": 1, "bocha": 1}


@pytest.mark.parametrize(
    "observations, cacheable",
    [
        ({"query_status": "no_results", "offers": []}, True),
        ({"inventory": {"status": "unknown"}}, True),
        ({"inventory": {"status": "not_on_sale"}}, True),
        ({"inventory": {"status": "waitlist"}}, True),
        ({"attempts": [{"status": "error", "error_code": "provider_rate_limited"}]}, False),
        (
            {"coverage": {"fallback_from": "12306", "fallback_reason": "provider_access_blocked"}},
            False,
        ),
    ],
)
async def test_cache_preserves_empty_unknown_not_on_sale_and_waitlist(observations, cacheable):
    class DetailedOutput(Output):
        observations: dict

    count = 0

    async def handler(args):
        nonlocal count
        count += 1
        return DetailedOutput(value=count, observations=observations)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "probe",
            "查询",
            Input,
            DetailedOutput,
            handler,
            "ready",
            execution="parallel_read",
            cache_seconds=60,
        )
    )
    first = await registry.dispatch("probe", {})
    second = await registry.dispatch("probe", {})
    assert second.data["observations"] == first.data["observations"]
    assert (second.reuse == "cache") == cacheable
    assert count == (1 if cacheable else 2)


async def test_durable_progress_fence_failure_is_never_a_provider_error():
    from travel_agent.durable_storage import StaleOwner

    async def handler(args):
        pytest.fail("No provider I/O after losing the owner fence.")

    async def progress(stage, **detail):
        raise StaleOwner

    registry = ToolRegistry()
    registry.register(ToolSpec("probe", "查询", Input, Output, handler, "ready"))
    with pytest.raises(StaleOwner):
        await registry.dispatch("probe", {}, progress=progress)

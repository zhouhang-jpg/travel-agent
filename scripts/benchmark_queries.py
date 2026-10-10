"""Repeatable graph benchmark; --live separately samples configured suppliers."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

from travel_agent.models import ModelReply
from travel_tools.api import create_app
from travel_tools.bootstrap import build_registry
from travel_tools.common import Source, StrictModel, ToolPayload, utc_now
from travel_tools.config import Settings
from travel_tools.registry import ToolRegistry, ToolSpec


async def offline(directory, concurrency):
    intervals = []
    active = maximum = 0

    class Input(StrictModel):
        value: int

    class Output(ToolPayload):
        value: int

    async def handler(args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        start = time.monotonic()
        await asyncio.sleep(0.15)
        intervals.append({"id": args.value, "start": start, "end": time.monotonic()})
        active -= 1
        return Output(value=args.value, sources=[Source(provider="deterministic")])

    class Model:
        async def complete(self, messages, tools):
            results = [m for m in messages if m["role"] == "tool"]
            if results:
                assert [r["tool_call_id"] for r in results] == [str(i) for i in range(8)]
                assert all(json.loads(r["content"])["status"] == "ok" for r in results)
                return ModelReply({"role": "assistant", "content": "已核对8项结果"}, "stop", {})
            return ModelReply(
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "private",
                    "opaque": {"keep": [1, None]},
                    "tool_calls": [
                        {
                            "id": str(i),
                            "type": "function",
                            "function": {
                                "name": "probe",
                                "arguments": json.dumps({"value": i}),
                            },
                        }
                        for i in range(8)
                    ],
                },
                "tool_calls",
                {},
            )

    registry = ToolRegistry(max_concurrent_calls=concurrency)
    registry.register(
        ToolSpec(
            "probe", "确定性只读查询", Input, Output, handler, "ready", execution="parallel_read"
        )
    )
    config = Settings(
        _env_file=None,
        deepseek_api_key=None,
        database_url="sqlite+aiosqlite:///"
        + (directory / f"c{concurrency}.db").resolve().as_posix(),
    )
    app = create_app(config, registry, model=Model())
    async with app.router.lifespan_context(app):
        service = app.state.agent.durable
        cid = (await service.store.create("Asia/Shanghai"))["id"]
        start = time.monotonic()
        queue = await service.start(cid, "比较同样的8项查询", "benchmark")
        while (await queue.get())["type"] != "done":
            pass
        elapsed = time.monotonic() - start
        assert (await service.get(cid))["status"] == "completed"
        events = await service.facts.events(cid)
        run = await service.facts.run(next(e["run_id"] for e in events))
    overlap = any(
        a["start"] < b["end"] and b["start"] < a["end"]
        for i, a in enumerate(intervals)
        for b in intervals[i + 1 :]
    )
    return {
        "concurrency": concurrency,
        "elapsed_seconds": elapsed,
        "active_seconds": run.active_seconds,
        "maximum_actual_concurrency": maximum,
        "overlap": overlap,
        "external_calls": len(intervals),
        "successes": 8,
        "failures": 0,
        "unknown_results": 0,
        "intervals": intervals,
    }


async def live(directory):
    settings = Settings()
    coordinates = {"longitude": 116.41, "latitude": 39.92, "crs": "gcj02"}
    calls = [
        ("search_places", {"keywords": "故宫", "region": "北京", "page_size": 1}),
        ("get_weather", {"coordinates": coordinates, "days": 1}),
        ("search_web", {"query": "故宫博物院 官方 开放时间", "count": 1}),
    ]
    summary = []
    async with httpx.AsyncClient(trust_env=False) as client:
        for mode in ("serial_fresh", "parallel_fresh", "cache"):
            if mode != "cache":
                registry = build_registry(settings, client)

            async def query(index, name, arguments, registry=registry, mode=mode):
                stages = []

                async def progress(stage, **detail):
                    stages.append({"stage": stage, "at": time.monotonic(), **detail})

                result = await registry.dispatch(
                    name,
                    arguments,
                    call_id=f"{mode}-{index}",
                    refresh=mode != "cache",
                    progress=progress,
                )
                return {
                    "tool": name,
                    "parameters": arguments,
                    "status": result.status,
                    "error": result.error.code if result.error else None,
                    "reuse": result.reuse,
                    "seconds": (result.finished_at - result.started_at).total_seconds(),
                    "sources": (result.data or {}).get("sources", []),
                    "fields": sorted(result.data or {}),
                    "stages": stages,
                }

            start = time.monotonic()
            if mode == "serial_fresh":
                results = [await query(i, *c) for i, c in enumerate(calls)]
            else:
                results = await asyncio.gather(*(query(i, *c) for i, c in enumerate(calls)))
            summary.append(
                {
                    "mode": mode,
                    "elapsed_seconds": time.monotonic() - start,
                    "results": results,
                    "supplier_requests": sum(
                        sum(s["stage"] == "supplier_running" for s in r["stages"]) for r in results
                    ),
                    "failures": sum(r["status"] == "error" for r in results),
                }
            )
            if mode == "serial_fresh":
                await registry.close()
        await registry.close()
    (directory / "live.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return [{k: v for k, v in row.items() if k != "results"} for row in summary]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Consumes configured supplier quota.")
    args = parser.parse_args()
    directory = Path("artifacts") / ("query-benchmark-" + uuid4().hex[:10])
    directory.mkdir(parents=True)
    results = [await offline(directory, c) for c in (1, 4)]
    report = {"observed_at": utc_now().isoformat(), "offline": results}
    if args.live:
        report["live"] = await live(directory)
    (directory / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "directory": str(directory),
                "offline": [{k: v for k, v in r.items() if k != "intervals"} for r in results],
                "live": report.get("live"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

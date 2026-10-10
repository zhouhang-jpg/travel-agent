"""Six-call comparison through real GraphRuntime, isolated SQLite and local data.

The before arm changes only outbound context serialization back to the legacy
layout. Both arms use the same current tools, model, durable runtime and inputs.
All synthetic histories/reasoning remain in ignored artifacts, never the report.
"""

import argparse
import asyncio
import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from scripts.benchmark_context_cache import REQUEST, candidates
from scripts.report_context_cache import report as database_report
from travel_agent.cache_metrics import aggregate_cache_observations, cache_observation
from travel_agent.models import ModelError, OpenAICompatibleModel
from travel_agent.prompts import build_system_message
from travel_agent.runtime_context import RUNTIME_MARKER, runtime_state
from travel_tools.api import create_app
from travel_tools.bootstrap import build_registry
from travel_tools.common import StrictModel, ToolPayload
from travel_tools.config import Settings
from travel_tools.registry import ToolSpec


class FixtureInput(StrictModel):
    pass


class FixtureOutput(ToolPayload):
    source: str
    live_inventory_verified: bool
    candidates: list[dict]


async def compare(args):
    config = Settings(_env_file=args.env_file)
    if (
        config.llm_provider,
        config.deepseek_model,
        config.llm_thinking,
        config.llm_reasoning_effort,
    ) != ("deepseek", "deepseek-flash", True, "high"):
        raise ValueError("Probe requires configured deepseek-flash with thinking/high.")
    if not config.deepseek_api_key:
        raise ValueError("DeepSeek credentials are not configured.")
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "model": config.deepseek_model,
        "thinking": True,
        "reasoning_effort": "high",
        "initial_cache_state": "unknown; common policy/tools may already be cached",
        "delay_between_calls_seconds": args.delay,
        "modes": {},
    }
    async with httpx.AsyncClient() as provider_client:
        base = OpenAICompatibleModel(
            config.deepseek_api_key.get_secret_value(),
            config.deepseek_base_url,
            config.deepseek_model,
            provider_client,
            thinking=True,
            reasoning_effort="high",
            timeout_seconds=config.llm_timeout_seconds,
        )
        for mode in args.order.split(","):
            registry = build_registry(config, provider_client)
            tool_invocations = []

            async def fixture(arguments, invocations=tool_invocations):
                invocations.append("read_synthetic_candidates")
                return FixtureOutput(**candidates())

            registry.register(
                ToolSpec(
                    "read_synthetic_candidates",
                    "读取隔离测试合成候选，不是真实地点或报价。",
                    FixtureInput,
                    FixtureOutput,
                    fixture,
                    "ready",
                )
            )
            catalog = [*registry.catalog(), {"name": "save_itinerary", "availability": "ready"}]
            records, protocols = [], []

            class ObservedModel:
                def __init__(self, mode, catalog, records, protocols):
                    self.previous = None
                    self.mode, self.catalog = mode, catalog
                    self.records, self.protocols = records, protocols

                def __getattr__(self, name):
                    return getattr(base, name)

                async def complete(self, messages, tools):
                    records, protocols = self.records, self.protocols
                    mode, catalog = self.mode, self.catalog
                    if len(records) >= 3:
                        raise ModelError("probe_limit", "Probe made unexpected extra decisions.")
                    if mode == "before":
                        state = runtime_state(messages)
                        system = build_system_message(
                            datetime.now(ZoneInfo("Asia/Shanghai")), "Asia/Shanghai", catalog
                        )
                        system["content"] += (
                            "\n【当前行程与原话引用，仅作本会话索引，完整历史仍保留】\n"
                            + json.dumps(state["planning"], ensure_ascii=False)
                        )
                        messages = [
                            system,
                            *[
                                m
                                for m in messages[1:]
                                if not (
                                    m.get("role") == "system"
                                    and m["content"].startswith(RUNTIME_MARKER)
                                )
                            ],
                        ]
                    start = time.monotonic()
                    reply = await base.complete(messages, tools)
                    records.append(
                        {
                            **cache_observation(reply.usage),
                            "step": len(records),
                            "elapsed_seconds": time.monotonic() - start,
                            "first_request": not records,
                            "retained_previous_request_prefix": (
                                messages[: len(self.previous)] == self.previous
                                if self.previous
                                else None
                            ),
                            "runtime_snapshot_utf8_bytes": len(messages[-1]["content"].encode())
                            if mode == "after"
                            else 0,
                        }
                    )
                    self.previous = deepcopy(messages)
                    calls = reply.message.get("tool_calls") or []
                    protocols.append(
                        {
                            "finish_reason": reply.finish_reason,
                            "reasoning_preserved": "reasoning_content" in reply.message,
                            "tool_names": [c["function"]["name"] for c in calls],
                        }
                    )
                    if len(records) == 1 and (
                        len(calls) != 1
                        or calls[0]["function"]["name"] != "read_synthetic_candidates"
                    ):
                        raise ModelError("probe_protocol", "Probe did not call the fixture tool.")
                    if len(records) > 1 and calls:
                        raise ModelError("probe_protocol", "Probe requested an unexpected tool.")
                    if len(records) < 3:
                        await asyncio.sleep(args.delay)
                    return reply

            db = (output.parent / f"{output.stem}-{mode}-{uuid4().hex[:8]}.db").resolve()
            database_url = "sqlite+aiosqlite:///" + db.as_posix()
            settings = config.model_copy(
                update={"database_url": database_url, "agent_engine": "langgraph"}
            )
            app = create_app(
                settings, registry, model=ObservedModel(mode, catalog, records, protocols)
            )
            async with app.router.lifespan_context(app):
                service = app.state.agent.durable
                cid = (await service.store.create("Asia/Shanghai"))["id"]
                for index, content in enumerate(
                    [
                        REQUEST,
                        (
                            "请用一句话说明费用还缺哪些部分，并报告最新运行状态的当前日期、"
                            "时区及本次之后剩余模型请求次数。"
                        ),
                    ]
                ):
                    queue = await service.start(cid, content, f"probe-{index}")
                    events = []
                    while True:
                        event = await queue.get()
                        events.append(event)
                        if event["type"] == "done":
                            break
                    public = await service.get(cid)
                    if public["status"] != "completed":
                        raise ValueError("Synthetic durable probe did not complete; no retry.")
                    if "reasoning_content" in json.dumps(events):
                        raise ValueError("Private reasoning crossed the event boundary.")
                if len(records) != 3 or tool_invocations != ["read_synthetic_candidates"]:
                    raise ValueError("Synthetic probe deviated from its expected three requests.")
                raw = await service.facts.history(cid)
                latest = runtime_state(raw)
                assert latest["planning"]["run_budget"]["model_requests_remaining_after_this"] == 11
                content = public["transcript"][-1]["content"]
                year, month, day = latest["current_date"].split("-")
                readback = {
                    "date_reported_correctly": (
                        latest["current_date"] in content
                        or f"{year}年{int(month)}月{int(day)}日" in content
                    ),
                    "timezone_reported_correctly": any(
                        value in content
                        for value in ("Asia/Shanghai", "UTC+8", "+08:00", "北京时间")
                    ),
                    "remaining_requests_reported_correctly": "11" in content or "十一" in content,
                }
            result["modes"][mode] = {
                "requests": records,
                "protocol": protocols,
                "aggregate": aggregate_cache_observations(records),
                "subsequent": aggregate_cache_observations(records[1:]),
                "database": await database_report(database_url),
                "runtime_budget_check": True,
                "public_reasoning_hidden": True,
                "model_runtime_readback": readback,
            }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/context-cache/durable-summary.json")
    )
    parser.add_argument("--delay", type=float, default=3)
    parser.add_argument("--order", choices=["before,after", "after,before"], default="before,after")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live for six paid model calls using isolated synthetic databases.")
    asyncio.run(compare(args))

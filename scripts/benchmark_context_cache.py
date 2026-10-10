"""Opt-in, six-call synthetic before/after comparison; never opens user databases.

Real model output (including reasoning) stays in memory. Only numeric usage and
protocol checks are saved. A local fixture tool explicitly supplies synthetic
candidate data, not live quotes. No paid retries or cache warming requests.
"""

import argparse
import asyncio
import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from travel_agent.cache_metrics import aggregate_cache_observations, cache_observation
from travel_agent.itinerary_schema import SAVE_ITINERARY_TOOL
from travel_agent.models import OpenAICompatibleModel
from travel_agent.prompts import ASK_USER_TOOL, build_system_message
from travel_agent.runtime_context import build_policy_message, build_runtime_message
from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings

FIXTURE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_synthetic_candidates",
        "description": "读取隔离测试数据集中的出行候选。全部为合成数据，不是真实地点或报价。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}
REQUEST = (
    "这是隔离测试，不需要真实供应商查询。请先调用read_synthetic_candidates，"
    "再根据返回的合成候选比较交通方式、费用覆盖和时间冲突。"
    "上海出发，2026-10-12半日，单人总预算200元。无需追问、无需保存行程。"
    "答复最多三句，只说明能从这些记录确定的结论，不要编造未知费用。"
)


def candidates():
    return {
        "source": "isolated_synthetic_fixture",
        "live_inventory_verified": False,
        "candidates": [
            {
                "id": f"candidate-{i}",
                "title": f"合成测试公园{i}",
                "transport_mode": "walking" if i % 2 else "transit",
                "travel_minutes_reference": 15 + 5 * i,
                "admission_reference_cny": 10 * i,
                "transport_fare_cny": None,
                "opening_hours_verified_for_date": False,
                "weather_known": False,
                "price_basis": "one adult, admission only; meals and transit unknown",
            }
            for i in range(1, 9)
        ],
    }


async def compare(args):
    config = Settings(_env_file=args.env_file)
    if config.llm_provider != "deepseek" or config.deepseek_model != "deepseek-flash":
        raise ValueError("This probe requires the configured deepseek-flash model.")
    if not config.llm_thinking or config.llm_reasoning_effort != "high":
        raise ValueError("This probe requires the unchanged thinking/high settings.")
    if not config.deepseek_api_key:
        raise ValueError("DeepSeek credentials are not configured.")
    report = {
        "model": config.deepseek_model,
        "thinking": True,
        "reasoning_effort": "high",
        "tool_data": "synthetic",
        "cache_build_wait_seconds": args.delay,
        "initial_cache_state": "unknown; account may already cache common policy/tools",
        "modes": {},
    }
    async with httpx.AsyncClient() as client:
        registry = build_registry(config, client)
        definitions = [
            deepcopy(ASK_USER_TOOL),
            deepcopy(SAVE_ITINERARY_TOOL),
            *registry.model_definitions(),
            deepcopy(FIXTURE_TOOL),
        ]
        catalog = [
            *registry.catalog(),
            {"name": "read_synthetic_candidates", "availability": "ready"},
        ]
        model = OpenAICompatibleModel(
            config.deepseek_api_key.get_secret_value(),
            config.deepseek_base_url,
            config.deepseek_model,
            client,
            thinking=True,
            reasoning_effort="high",
            timeout_seconds=config.llm_timeout_seconds,
        )
        for mode in args.order.split(","):
            history = [{"role": "user", "content": REQUEST}]
            records, previous, protocol = [], None, []
            for step in range(3):
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
                planning = {
                    "run_budget": {
                        "model_requests_used_including_this_request": step + 1,
                        "model_requests_remaining_after_this": 11 - step,
                        "active_seconds_committed": 0,
                        "max_active_seconds": 300,
                    },
                    "current_itinerary": None,
                    "user_message_references": [],
                }
                if mode == "before":
                    system = build_system_message(now, "Asia/Shanghai", catalog)
                    system["content"] += (
                        "\n【当前行程与原话引用，仅作本会话索引，完整历史仍保留】\n"
                        + json.dumps(planning, ensure_ascii=False)
                    )
                else:
                    system = build_policy_message()
                    history.append(
                        build_runtime_message(
                            now, "Asia/Shanghai", catalog, planning, history=history
                        )
                    )
                messages = [system, *deepcopy(history)]
                start = time.monotonic()
                reply = await model.complete(messages, deepcopy(definitions))
                elapsed = time.monotonic() - start
                observation = cache_observation(reply.usage)
                records.append(
                    {
                        **observation,
                        "step": step,
                        "elapsed_seconds": elapsed,
                        "first_request": step == 0,
                        "retained_previous_request_prefix": (
                            messages[: len(previous)] == previous if previous else None
                        ),
                        "runtime_snapshot_utf8_bytes": (
                            len(history[-1]["content"].encode()) if mode == "after" else 0
                        ),
                    }
                )
                previous = deepcopy(messages)
                history.append(deepcopy(reply.message))
                calls = reply.message.get("tool_calls") or []
                protocol.append(
                    {
                        "step": step,
                        "finish_reason": reply.finish_reason,
                        "reasoning_preserved": "reasoning_content" in reply.message,
                        "tool_names": [c["function"]["name"] for c in calls],
                    }
                )
                if step == 0:
                    if (
                        len(calls) != 1
                        or calls[0]["function"]["name"] != "read_synthetic_candidates"
                    ):
                        raise ValueError("Probe did not request its single synthetic fixture tool.")
                    history.append(
                        {
                            "role": "tool",
                            "tool_call_id": calls[0]["id"],
                            "content": json.dumps(candidates(), ensure_ascii=False),
                        }
                    )
                elif calls:
                    raise ValueError("Probe unexpectedly requested another tool; no paid retry.")
                elif step == 1:
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "请用一句话说明费用还缺哪些部分，并报告最新运行快照中的当前日期、"
                                "时区及本次之后剩余模型请求次数。"
                            ),
                        }
                    )
                if step < 2:
                    await asyncio.sleep(args.delay)
            report["modes"][mode] = {
                "requests": records,
                "protocol": protocol,
                "aggregate": aggregate_cache_observations(records),
                "subsequent": aggregate_cache_observations(records[1:]),
            }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/context-cache/summary.json"))
    parser.add_argument("--delay", type=float, default=3)
    parser.add_argument("--order", choices=["before,after", "after,before"], default="before,after")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live for six real paid model requests, without retries.")
    asyncio.run(compare(args))

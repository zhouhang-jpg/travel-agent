"""Compare plain/compact evidence comprehension and usage with opt-in model requests."""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from travel_agent.models import OpenAICompatibleModel
from travel_agent.prompts import ASK_USER_TOOL, build_system_message
from travel_agent.storage import Conversation, ConversationStore
from travel_agent.tool_encoding import decode_tool_result, encode_tool_result
from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--mode", choices=["both", "plain", "compact"], default="both")
    parser.add_argument("--output", type=Path, default=Path("artifacts/tool-encoding-live.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to use one request per selected representation.")
    settings = Settings()
    store = ConversationStore(settings.database_url)
    samples = {}
    try:
        async with store.sessions() as session:
            for history in (await session.execute(select(Conversation.history))).scalars():
                for message in history:
                    if message.get("role") != "tool":
                        continue
                    result = decode_tool_result(message["content"])
                    name = result["tool_name"]
                    if name in {"get_routes", "search_trains"} and result["status"] == "ok":
                        data = result["data"]
                        if data.get("routes") if name == "get_routes" else data.get("offers"):
                            samples[name] = result
    finally:
        await store.engine.dispose()
    if len(samples) != 2:
        raise SystemExit("Need successful saved route and train results for this check.")
    routes = samples["get_routes"]["data"]["routes"]
    offers = samples["search_trains"]["data"]["offers"]
    expected = {
        "route_count": len(routes),
        "first_route_duration_s": routes[0]["duration_s"],
        "first_route_step_count": len(routes[0]["steps"]),
        "train_offer_count": len(offers),
        "first_train_price_kind": offers[0]["price"]["kind"],
        "first_train_source_provider": offers[0]["sources"][0]["provider"],
        "first_train_inventory_status": offers[0]["inventory"]["status"],
    }
    query = (
        "这是工具数据读取验证，请只依据已有结果返回 JSON 对象，不再调用工具，不规划行程。"
        "字段为 route_count（路线条数）、first_route_duration_s（第一条路线总秒数）、"
        "first_route_step_count（第一条路线步骤数）、train_offer_count（火车候选条数）、"
        "first_train_price_kind（第一条火车price.kind）、"
        "first_train_source_provider（第一条火车第一项sources的provider）、"
        "first_train_inventory_status（第一条火车inventory.status）。未知保持null或原unknown。"
        "条数必须是实际返回的数组长度/表格count，不是查询page_size或供应商总数。"
    )
    report = {"cases": []}
    async with httpx.AsyncClient(trust_env=False) as client:
        registry = build_registry(settings, client)
        tools = [
            ASK_USER_TOOL,
            *[
                definition
                for definition in registry.model_definitions()
                if definition["function"]["name"] != "search_coaches"
            ],
        ]
        key, base, name = (
            (settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model)
            if settings.llm_provider == "deepseek"
            else (settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        )
        if key is None:
            raise SystemExit("Model key is not configured.")
        model = OpenAICompatibleModel(
            key.get_secret_value(),
            base,
            name,
            client,
            provider=settings.llm_provider,
            thinking=settings.llm_thinking,
            reasoning_effort=settings.llm_reasoning_effort,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        report["model"] = name
        system = build_system_message(
            datetime.now(ZoneInfo("Asia/Shanghai")), "Asia/Shanghai", registry.catalog()
        )
        modes = ("plain", "compact") if args.mode == "both" else (args.mode,)
        for mode in modes:
            calls = [
                {
                    "id": f"test-{tool}",
                    "type": "function",
                    "function": {"name": tool, "arguments": "{}"},
                }
                for tool in samples
            ]
            messages = [
                system,
                {"role": "user", "content": query},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "",
                    "tool_calls": calls,
                },
            ]
            for tool, result in samples.items():
                content = (
                    encode_tool_result(result)
                    if mode == "compact"
                    else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": f"test-{tool}",
                        "content": content,
                    }
                )
            reply = await model.complete(messages, tools)
            content = (reply.message.get("content") or "").strip()
            if content.startswith("```"):
                content = "\n".join(content.splitlines()[1:-1])
            try:
                received, _ = json.JSONDecoder().raw_decode(content[content.index("{") :])
                correct = received == expected
            except ValueError:
                received = None
                correct = False
            case = {
                "mode": mode,
                "correct": correct,
                "usage": reply.usage,
                "expected": expected,
                "received": received,
                "public_reply": content,
                "finish_reason": reply.finish_reason,
                "selected_tools": [
                    call["function"]["name"] for call in reply.message.get("tool_calls", [])
                ],
            }
            report["cases"].append(case)
            print(json.dumps(case, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all(case["correct"] for case in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

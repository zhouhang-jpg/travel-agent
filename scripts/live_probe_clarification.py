"""Evaluate representative needs; the business case may execute bounded read-only tools.

Reports only public questions/answers and selected tool names, never reasoning.
It evaluates relevance and reuse of known information, not a question count target.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from travel_agent.models import OpenAICompatibleModel
from travel_agent.prompts import ASK_USER_TOOL, build_system_message
from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings

CASES = [
    {
        "name": "missing_constraints",
        "messages": [
            {
                "role": "user",
                "content": "我这周准备去上海玩，预计玩四天，预算5000，帮我规划一下出行。",
            }
        ],
        "review": "是否集中澄清出发地、可执行日期、人数、预算口径及影响规划的偏好。",
    },
    {
        "name": "known_constraints",
        "messages": [
            {"role": "user", "content": "我去上海玩四天，预算5000，请先确认你需要的信息。"},
            {
                "role": "assistant",
                "content": "请补充出发地、具体日期、人数、预算口径，以及兴趣和交通住宿要求。",
                "reasoning_content": "",
            },
            {
                "role": "user",
                "content": "北京出发，2026年10月24日至27日，日期固定。1位成人，无特殊需求。"
                "5000元总预算含往返交通、3晚住宿、餐饮和游玩。美食街巷为主，不去迪士尼，"
                "每天9点后出门、不赶路。高铁优先，酒店每晚300元以内、靠地铁。"
                "没有其他已定安排，去回班次由你选，没有必须到家的时刻。请规划。",
            },
        ],
        "review": "是否复用全会话已知条件并推进查询或规划，避免再次询问已明确字段。",
    },
    {
        "name": "unknown_destination",
        "messages": [
            {
                "role": "user",
                "content": "北京出发，一个人，2026年10月24日至25日两天，1500元含往返和住宿，"
                "想安静散心，但不知道去哪。交通住宿你决定，接受你推荐合适选项。",
            }
        ],
        "review": "是否探索候选/可行性或提出有帮助的选项，不要求用户先确定目的地。",
    },
    {
        "name": "business_free_time",
        "follow_tool_results": True,
        "messages": [
            {
                "role": "user",
                "content": "下周去杭州出差，周三下午3点会议结束，晚上8点从杭州东站返程，"
                "想顺便逛一逛。",
            }
        ],
        "review": "是否澄清会议地点、不可占用时间/转场边界等关键缺失，保留已知返程约束。",
    },
]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/clarification-review.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to use model quotas and limited business-case tool queries.")
    settings = Settings()
    key, base_url, name = (
        (settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model)
        if settings.llm_provider == "deepseek"
        else (settings.llm_api_key, settings.llm_base_url, settings.llm_model)
    )
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        registry = build_registry(settings, client)
        model = OpenAICompatibleModel(
            key.get_secret_value(),
            base_url,
            name,
            client,
            provider=settings.llm_provider,
            thinking=settings.llm_thinking,
            reasoning_effort=settings.llm_reasoning_effort,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        system = build_system_message(
            datetime.now(ZoneInfo("Asia/Shanghai")), "Asia/Shanghai", registry.catalog()
        )
        definitions = [ASK_USER_TOOL] + [
            item
            for item in registry.model_definitions()
            if item["function"]["name"] != "search_coaches"
        ]

        async def probe(case):
            messages = [system, *case["messages"]]
            executed = []
            for step in range(3):
                reply = await model.complete(messages, definitions)
                calls = reply.message.get("tool_calls") or []
                if (
                    not case.get("follow_tool_results")
                    or not calls
                    or any(item["function"]["name"] == "ask_user" for item in calls)
                    or len(calls) > 3
                    or step == 2
                ):
                    break
                messages.append(reply.message)
                for item in calls:
                    name = item["function"]["name"]
                    result = await registry.dispatch(
                        name, json.loads(item["function"]["arguments"]), call_id=item["id"]
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item["id"],
                            "content": result.model_dump_json(),
                        }
                    )
                    executed.append({"tool": name, "status": result.status})
            calls = reply.message.get("tool_calls") or []
            questions = [
                json.loads(item["function"]["arguments"]).get("message")
                for item in calls
                if item["function"]["name"] == "ask_user"
            ]
            return {
                "name": case["name"],
                "review_criteria": case["review"],
                "finish_reason": reply.finish_reason,
                "model_requests": step + 1,
                "executed_tools": executed,
                "selected_tools": [item["function"]["name"] for item in calls],
                "public_question": questions,
                "public_content": reply.message.get("content"),
                "usage": reply.usage,
            }

        # Independent samples; bound concurrent model calls to two.
        semaphore = asyncio.Semaphore(2)

        async def bounded(case):
            async with semaphore:
                return await probe(case)

        results = await asyncio.gather(*(bounded(case) for case in CASES))
    report = {"model": name, "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

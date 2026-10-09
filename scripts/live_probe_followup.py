"""Opt-in real DeepSeek multi-turn follow-up with one targeted railway verification."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from travel_agent.models import OpenAICompatibleModel
from travel_agent.runner import AgentRunner, RunLimits
from travel_agent.tool_encoding import decode_tool_result
from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings


class RecordedModel:
    def __init__(self, model):
        self.model = model
        self.usage = []

    async def complete(self, messages, tools):
        reply = await self.model.complete(messages, tools)
        self.usage.append(reply.usage)
        return reply


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--policy-only", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/followup-live.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "Pass --live to use model/supplier quotas; no user conversations are modified."
        )
    settings = Settings()
    day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=6)).isoformat()
    key, base_url, name = (
        (settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model)
        if settings.llm_provider == "deepseek"
        else (settings.llm_api_key, settings.llm_base_url, settings.llm_model)
    )
    if key is None:
        raise SystemExit("Model key is not configured.")
    report = {
        "model": name,
        "thinking": settings.llm_thinking,
        "reasoning_effort": settings.llm_reasoning_effort,
        "turns": [],
    }
    async with httpx.AsyncClient(trust_env=False) as client:
        registry = build_registry(settings, client)
        model = RecordedModel(
            OpenAICompatibleModel(
                key.get_secret_value(),
                base_url,
                name,
                client,
                provider=settings.llm_provider,
                thinking=settings.llm_thinking,
                reasoning_effort=settings.llm_reasoning_effort,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        )
        runner = AgentRunner(model, registry, RunLimits(max_steps=5, max_run_seconds=150))
        history = []

        async def turn(label, user, *, reset=False):
            nonlocal history
            if reset:
                history = []
            events = []

            async def emit(event):
                events.append(event)

            before = len(model.usage)
            history_count = len(history)
            outcome = await runner.run([*history, {"role": "user", "content": user}], emit)
            history = outcome.history
            record = {
                "case": label,
                "status": outcome.status,
                "error": outcome.error,
                "public_reply": outcome.content,
                "usage": model.usage[before:],
                "tools": [
                    {"name": e["tool_name"], "status": e["status"]}
                    for e in events
                    if e["type"] == "tool_finished"
                ],
                "tool_results": [
                    decode_tool_result(message["content"])
                    for message in outcome.history[history_count:]
                    if message.get("role") == "tool"
                ],
            }
            report["turns"].append(record)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {key: value for key, value in record.items() if key != "tool_results"},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return outcome

        try:
            if args.policy_only:
                await turn(
                    "total_budget_scope",
                    "北京出发，一人，四天三晚，总预算4000包含交通住宿，喜欢自然风光。"
                    "日期目的地交通都由你决定，不想再答问题。这次只说明你如何理解预算范围"
                    "以及哪些费用仍待核实，不查询、不规划具体目的地、不报真实报价。",
                    reset=True,
                )
                evidence_path = Path("artifacts/ticket-tools-live.json")
                if await asyncio.to_thread(evidence_path.is_file):
                    samples = json.loads(
                        await asyncio.to_thread(evidence_path.read_text, encoding="utf-8")
                    )
                    evidence = next(
                        sample for sample in samples if sample["case"] == "rail_waitlist"
                    )
                    history = [
                        {
                            "role": "user",
                            "content": f"{day}上海虹桥到杭州东，D931是首选，不接受候补。",
                        },
                        {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "",
                            "tool_calls": [
                                {
                                    "id": "saved-rail",
                                    "type": "function",
                                    "function": {
                                        "name": "search_trains",
                                        "arguments": json.dumps(evidence["arguments"]),
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "saved-rail",
                            "content": json.dumps(evidence["result"], ensure_ascii=False),
                        },
                    ]
                    await turn(
                        "latest_choice_overrides_previous",
                        "我现在明确改成D935作为首选，也可以接受候补。预算单程200元。"
                        "请按最新选择，只依据已有结果说明能确认什么、不能确认什么，不再查询。",
                    )
                if any(record["status"] == "error" for record in report["turns"]):
                    raise SystemExit(1)
                return
            await turn(
                "initial_question",
                f"{day}从上海虹桥到杭州东，想坐D931二等座。请先集中问我需要确认的条件，再查票。",
            )
            await turn(
                "new_inventory_constraint",
                "1位成人，无儿童。当天必须成行，单程预算200元，时间不限。"
                "D931是首选，不接受候补。先核实这班，有票就给建议；如果只能候补，"
                "先和我讨论换车，不要替我改首选。没有其他安排。",
            )
            await turn(
                "choice_and_delivery",
                "可以换其他直达列车，仍预算200元，其他条件不变。"
                "你决定下一步；这次不用再查，只说明已有证据与还要核实什么。",
            )
            await turn(
                "delegated_no_extra_question",
                "北京出发，一人，四天三晚，总预算4000含交通住宿，"
                "喜欢自然风光，日期目的地交通都由你决定。我不想再答问题。"
                "这次只说明你会如何探索候选及待核实信息，不查票、不报真实报价。",
                reset=True,
            )
            await turn(
                "partial_answer_initial", "想出去玩几天，请先问我需要确认的信息。", reset=True
            )
            await turn(
                "partial_answer_followup",
                "我一个人，喜欢山水，没有其他特殊需求。"
                "其余问题还没回答，请先帮我明确最影响安排的条件。",
            )
        finally:
            await registry.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(turn["status"] == "error" for turn in report["turns"]):
        raise SystemExit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

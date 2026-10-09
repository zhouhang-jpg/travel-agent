"""Three short real conversational turns; explicitly opt in to model/provider usage."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from travel_tools.api import create_app
from travel_tools.config import Settings, has_secret


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use configured billable APIs")
    parser.add_argument("--turn-limit", type=int, choices=[1, 2, 3], default=3)
    parser.add_argument("--output", type=Path, default=Path("artifacts/agent-live-probe.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to explicitly use model and data provider quotas.")
    settings = Settings()
    key = settings.deepseek_api_key if settings.llm_provider == "deepseek" else settings.llm_api_key
    if not has_secret(key):
        print(json.dumps({"status": "not_configured", "provider": settings.llm_provider}))
        raise SystemExit(2)
    settings = settings.model_copy(
        update={
            "database_url": "sqlite+aiosqlite:///artifacts/agent-live-probe.db",
            "agent_max_steps": 6,
            "agent_max_tool_calls": 6,
        }
    )
    app = create_app(settings)
    turns = [
        "我想找个周末出去玩，帮我做个计划。",
        "我从上海出发，1位成人，明天只在上海市区玩一天，预算500元。"
        "只需查询明天上海天气，然后给出简洁的散步建议，不用查询机票、火车或酒店。",
        "预算改成300元，依然一天。只要修改刚才建议，不要重复查询天气。",
    ]
    report = {"provider": settings.llm_provider, "turns": []}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", timeout=240
        ) as client:
            created = await client.post("/conversations", json={"timezone": "Asia/Shanghai"})
            created.raise_for_status()
            conversation_id = created.json()["id"]
            for turn in turns[: args.turn_limit]:
                response = await client.post(
                    f"/conversations/{conversation_id}/messages", json={"content": turn}
                )
                response.raise_for_status()
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                snapshot = (await client.get(f"/conversations/{conversation_id}")).json()
                public = [event for event in events if event["type"] == "message"]
                item = {
                    "status": snapshot["status"],
                    "tool_calls": [
                        event["tool_name"] for event in events if event["type"] == "tool_started"
                    ],
                    "error": snapshot["last_error"],
                    "public_reply": public[-1]["content"] if public else None,
                    "public_transcript_count": len(snapshot["transcript"]),
                    "internal_reasoning_exposed": "reasoning_content" in response.text,
                }
                report["turns"].append(item)
                print(json.dumps(item, ensure_ascii=False))
                if snapshot["status"] == "error":
                    break
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(turn["status"] == "error" for turn in report["turns"]):
        raise SystemExit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

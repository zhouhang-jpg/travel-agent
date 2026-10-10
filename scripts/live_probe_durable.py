"""Opt-in real DeepSeek/tool verification through the durable HTTP application."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select

from travel_agent.durable_storage import Effect, Run
from travel_tools.api import create_app
from travel_tools.config import Settings


async def probe(directory):
    config = Settings(
        agent_engine="langgraph",
        database_url=("sqlite+aiosqlite:///" + (directory / "probe.db").resolve().as_posix()),
    )
    app = create_app(config)
    summary = {
        "engine": "langgraph-v1",
        "model": config.deepseek_model,
        "thinking": config.llm_thinking,
        "reasoning_effort": config.llm_reasoning_effort,
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://probe", timeout=360
        ) as client:
            cid = (await client.post("/conversations", json={})).json()["id"]
            response = await client.post(
                f"/conversations/{cid}/messages",
                json={
                    "request_id": "probe-1",
                    "content": "我想出去玩，还没想好去哪。请先问我出发地和日期，再继续安排。",
                },
            )
            assert response.status_code == 200
            first = (await client.get(f"/conversations/{cid}")).json()
            assert first["status"] == "waiting_user" and first["question_id"]
            response = await client.post(
                f"/conversations/{cid}/messages",
                json={
                    "request_id": "probe-2",
                    "question_id": first["question_id"],
                    "content": (
                        "上海出发，2026年10月12日，半天，单人预算200元。去上海附近，"
                        "优先步行室外休闲活动，不订交通住宿。其他偏好你决定。"
                        "请查上海该日天气再安排，天气不能查到就明确说明。"
                    ),
                },
            )
            assert response.status_code == 200
            public = (await client.get(f"/conversations/{cid}")).json()
            summary["status"] = public["status"]
            summary["question_count"] = sum(m["kind"] == "question" for m in public["transcript"])
            assert public["status"] == "completed", public.get("last_error")
            async with app.state.agent.store.sessions() as session:
                runs = (await session.scalars(select(Run))).all()
                effects = (await session.scalars(select(Effect))).all()
                summary["model_requests"] = sum(r.model_requests for r in runs)
                summary["active_seconds"] = sum(r.active_seconds for r in runs)
                summary["tools"] = [
                    json.loads(e.payload["raw"]["content"])["tool_name"]
                    for e in effects
                    if e.kind == "tool" and e.payload
                ]
                summary["reasoning_preserved"] = all(
                    "reasoning_content" in e.payload["message"]
                    for e in effects
                    if e.kind == "model"
                )
                assert "get_weather" in summary["tools"]
            replay = await client.get(f"/conversations/{cid}/events")
            assert "reasoning_content" not in replay.text
            summary["public_reasoning_hidden"] = True
    await asyncio.to_thread(
        (directory / "summary.json").write_text,
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Consumes configured model/tool quota.")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to authorize real configured provider calls.")
    output = Path("artifacts") / ("durable-live-" + uuid4().hex[:10])
    output.mkdir(parents=True)
    asyncio.run(probe(output))

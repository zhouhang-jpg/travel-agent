"""Opt-in real model/provider evaluation of local edits in an isolated database."""

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


async def probe(directory, resume=False):
    app = create_app(
        Settings(
            agent_engine="langgraph",
            database_url=("sqlite+aiosqlite:///" + (directory / "probe.db").resolve().as_posix()),
        )
    )
    prompts = [
        "两位成人在上海本地玩两天，2026年10月12日09:00至10月13日22:00。"
        "总预算2000元含住宿交通活动餐饮，1间房1晚，酒店先考虑静安寺附近。"
        "节奏轻松，每天1到2个活动，第二天下午先安排室外散步。没有其他偏好，其他你决定。"
        "请给出并保存可后续修改的方案，查不到的费用或开放时间明确未知，不预订。",
        "我补充已定信息：酒店A（静安寺附近，具体酒店名暂不提供）已经订好，"
        "10月12日入住、13日退房，1间房1晚，费用600元已含税费。返程车次也已订，"
        "13日19:00从上海虹桥出发，20:00到苏州园区，编号和票价暂未提供。"
        "酒店和返程两项锁定，都是我提供的信息，尚未让你核验。只更新这些已定信息，其他活动保留。",
        "只把第二天下午改成室内活动，其他安排保持，尤其酒店和返程不动。"
        "这个室内活动你决定。请检查连带转场和预算影响，说明改了什么、为何改、费用变化及待复核项。",
    ]
    summary = {"model": "deepseek-flash", "thinking": True, "reasoning_effort": "high", "turns": []}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://probe", timeout=360
        ) as client:
            cid = (await client.post("/conversations", json={})).json()["id"]
            versions = []
            if resume:
                conversations = (await client.get("/conversations")).json()["items"]
                cid = next(c["id"] for c in conversations if c["id"] != cid)
                saved = (await client.get(f"/conversations/{cid}")).json()["itinerary"]
                assert saved and saved["revision"] == 1
                versions = [saved]
                summary["turns"] = [{"turn": 1, "status": "completed", "revision": 1}]
                summary["resumed_isolated_probe"] = True
            for number, content in enumerate(prompts, 1):
                if resume and number == 1:
                    continue
                response = await client.post(
                    f"/conversations/{cid}/messages",
                    json={
                        "content": content,
                        "request_id": f"edit-probe-{number}"
                        + ("-retry-" + uuid4().hex[:6] if resume else ""),
                    },
                )
                assert response.status_code == 200
                public = (await client.get(f"/conversations/{cid}")).json()
                clarification_count = 0
                while public["status"] == "waiting_user" and clarification_count < 2:
                    clarification_count += 1
                    reply = (
                        "两位成人、2000元总预算、1间房1晚不变。酒店和返程的已订日期时间必须保留。"
                        "如果旧的晚餐或进站转场与19点返程冲突，可必要调整并说明原因；"
                        "第一天活动保留。只在这个范围自行决定；缺失票价保持未知，预订状态未核验。"
                    )
                    response = await client.post(
                        f"/conversations/{cid}/messages",
                        json={
                            "content": reply,
                            "question_id": public["question_id"],
                            "request_id": f"edit-probe-{number}-clarify-" + uuid4().hex[:6],
                        },
                    )
                    assert response.status_code == 200
                    public = (await client.get(f"/conversations/{cid}")).json()
                version = public.get("itinerary")
                summary["turns"].append(
                    {
                        "turn": number,
                        "status": public["status"],
                        "revision": version["revision"] if version else None,
                        "clarification_replies": clarification_count,
                    }
                )
                await asyncio.to_thread(
                    (directory / "summary.json").write_text,
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                assert public["status"] == "completed", (
                    "Probe requires completing a fully specified turn."
                )
                assert version and version["revision"] == number, (
                    "A new delivered plan version is required."
                )
                versions.append(version)
            locked, edited = versions[1], versions[2]
            protected = [
                (name, entry)
                for name in ("items", "lodging")
                for entry in locked["document"][name]
                if entry.get("protection")
            ]
            assert len(protected) >= 2, "Both hotel and return must retain user protection."
            for name, entry in protected:
                assert entry in edited["document"][name], "A protected arrangement changed."
            old_activities = {
                i["id"]: i
                for i in locked["document"]["items"]
                if i["start"].startswith("2026-10-12")
            }
            new_activities = {i["id"]: i for i in edited["document"]["items"]}
            assert all(new_activities.get(key) == value for key, value in old_activities.items())
            assert edited["diff"]["changes"], "The modification must have a visible version diff."
            assert edited["review"]["unknown_cost_ids"], "Missing return price must remain unknown."
            summary.update(
                protected_preserved=len(protected),
                first_day_preserved=True,
                differences=len(edited["diff"]["changes"]),
                unknown_costs=len(edited["review"]["unknown_cost_ids"]),
            )
        async with app.state.agent.store.sessions() as session:
            runs = (await session.scalars(select(Run))).all()
            effects = (await session.scalars(select(Effect))).all()
            summary["model_requests"] = sum(r.model_requests for r in runs)
            summary["active_seconds"] = round(sum(r.active_seconds for r in runs), 2)
            summary["tools"] = [
                json.loads(e.payload["raw"]["content"])["tool_name"]
                for e in effects
                if e.kind == "tool" and e.payload
            ]
        summary["acceptance"] = "passed"
    await asyncio.to_thread(
        (directory / "summary.json").write_text,
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume-probe", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to consume configured model/provider quota.")
    output = args.resume_probe or Path("artifacts") / ("itinerary-edit-live-" + uuid4().hex[:10])
    if args.resume_probe:
        assert output.resolve().parent == Path("artifacts").resolve()
        assert output.name.startswith("itinerary-edit-live-")
    else:
        output.mkdir(parents=True)
    asyncio.run(probe(output, resume=args.resume_probe is not None))

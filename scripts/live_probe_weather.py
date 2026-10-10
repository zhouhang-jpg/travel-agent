"""Opt-in weather HTTP and optional real model verification; no user conversations modified."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from travel_agent.models import OpenAICompatibleModel
from travel_agent.runner import AgentRunner, RunLimits
from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", action="store_true", help="Also make limited real LLM requests")
    parser.add_argument(
        "--model-only", action="store_true", help="Skip HTTP cases, run model cases only"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("artifacts/weather-live.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to use configured weather/model quotas.")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    base = {"coordinates": {"latitude": 39.92, "longitude": 116.41, "crs": "gcj02"}}
    cases = [
        ("exact_hour", {**base, "mode": "hourly", "target_time": target.isoformat()}),
        (
            "subhour_reference",
            {**base, "mode": "hourly", "target_time": (target + timedelta(minutes=30)).isoformat()},
        ),
        (
            "afternoon_range",
            {
                **base,
                "mode": "hourly",
                "start_time": target.isoformat(),
                "end_time": (target + timedelta(hours=3)).isoformat(),
            },
        ),
        (
            "outside_short_query",
            {
                **base,
                "mode": "hourly",
                "hours": 1,
                "target_time": (target + timedelta(days=3)).isoformat(),
            },
        ),
        ("current_alerts", {**base, "mode": "alerts"}),
        ("daily_indices", {**base, "mode": "indices", "index_days": 3}),
        ("daily_date", {**base, "forecast_date": (now.date() + timedelta(days=1)).isoformat()}),
    ]
    report = {"observed_at_utc": datetime.now(UTC).isoformat(), "cases": []}
    async with httpx.AsyncClient(base_url=args.base_url, trust_env=False, timeout=70) as client:
        for name, arguments in [] if args.model_only else cases:
            response = await client.post("/tools/get_weather/call", json={"arguments": arguments})
            response.raise_for_status()
            payload = response.json()
            report["cases"].append({"name": name, "arguments": arguments, "result": payload})
            data = payload.get("data") or {}
            print(
                json.dumps(
                    {
                        "name": name,
                        "status": payload["status"],
                        "error": payload.get("error"),
                        "coverage": data.get("coverage"),
                        "zero_alert_result": data.get("zero_alert_result"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    if args.model or args.model_only:
        settings = Settings()
        key, base_url, model_name = (
            (settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model)
            if settings.llm_provider == "deepseek"
            else (settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        )
        if key is None:
            raise SystemExit("Model key not configured.")
        async with httpx.AsyncClient(trust_env=False) as client:
            registry = build_registry(settings, client)
            model = OpenAICompatibleModel(
                key.get_secret_value(),
                base_url,
                model_name,
                client,
                provider=settings.llm_provider,
                thinking=settings.llm_thinking,
                reasoning_effort=settings.llm_reasoning_effort,
                timeout_seconds=settings.llm_timeout_seconds,
            )
            questions = [
                f"请查北京{target.isoformat()}这个时次的天气。已知GCJ-02坐标116.41,39.92。"
                "只查该时次的逐小时预报，简洁列出温度、降水概率、风及数据边界，不查询其他内容。",
                f"请查北京{(target + timedelta(minutes=30)).isoformat()}的天气参考。"
                "已知GCJ-02坐标116.41,39.92。只查逐小时天气，说明能否精确到这个分钟，不额外查询。",
            ]
            outcomes = []
            try:
                for question in questions:
                    events = []

                    async def emit(event, events=events):
                        events.append(event)

                    result = await AgentRunner(
                        model, registry, RunLimits(max_steps=3, max_run_seconds=150)
                    ).run(
                        [{"role": "user", "content": question}],
                        emit,
                    )
                    record = {
                        "status": result.status,
                        "public_reply": result.content,
                        "tools": [
                            event["tool_name"]
                            for event in events
                            if event["type"] == "tool_started"
                        ],
                        "tool_arguments": [
                            json.loads(call["function"]["arguments"])
                            for message in result.history
                            if message.get("role") == "assistant"
                            for call in message.get("tool_calls", [])
                        ],
                    }
                    outcomes.append(record)
                    print(json.dumps(record, ensure_ascii=False), flush=True)
            finally:
                await registry.close()
            report["model"] = {"name": model_name, "outcomes": outcomes}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(case["result"]["status"] != "ok" for case in report["cases"]):
        raise SystemExit(1)
    if any(item["status"] != "completed" for item in report.get("model", {}).get("outcomes", [])):
        raise SystemExit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

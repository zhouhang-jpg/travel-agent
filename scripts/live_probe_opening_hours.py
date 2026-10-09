"""Explicit, read-only opening-hours probe; saves source evidence in ignored artifacts."""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx

from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--attraction", default="上海博物馆人民广场馆")
    parser.add_argument("--city", default="上海")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 10, 12))
    parser.add_argument("--official-url", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("artifacts/opening-hours-probe.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to use configured map/search quotas and fetch public sources.")
    urls = args.official_url
    if not urls and args.attraction == "上海博物馆人民广场馆":
        urls = ["https://www.shanghaimuseum.cn/mu/frontend/pg/m/service/visit-west"]
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        registry = build_registry(Settings(), client)
        result = await registry.dispatch(
            "get_attraction_opening_hours",
            {
                "attraction_name": args.attraction,
                "city": args.city,
                "visit_date": args.date.isoformat(),
                "official_urls": urls,
            },
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    data = result.data or {}
    print(
        json.dumps(
            {
                "status": result.status,
                "error": result.error.model_dump() if result.error else None,
                "place_resolution": data.get("place_resolution"),
                "date_specific_status": data.get("date_specific_status"),
                "sources": [
                    {key: item[key] for key in ("kind", "purpose", "url", "truncated")}
                    for item in data.get("web_evidence", [])
                ],
                "attempts": data.get("attempts"),
            },
            ensure_ascii=False,
        )
    )
    if result.status != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

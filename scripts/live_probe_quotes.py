"""Explicit single-query supplier probe; only sanitized capability metadata is saved."""

import argparse
import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool",
        required=True,
        choices=["search_flights", "search_trains", "search_coaches", "search_hotels"],
    )
    parser.add_argument("--origin", default="上海")
    parser.add_argument("--destination", default="北京")
    parser.add_argument("--date", help="ISO departure/check-in date; defaults to seven days ahead")
    parser.add_argument(
        "--flyai-demo", action="store_true", help="Explicitly enable experience mode"
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/live-probe-quotes.json"))
    args = parser.parse_args()
    overrides = {}
    if args.flyai_demo:
        overrides = {
            "flyai_api_key": None,
            "flyai_enable_demo": True,
            "flyai_node_path": shutil.which("node"),
            "flyai_cli_path": ".tools/flyai-cli/package/dist/flyai-bundle.cjs",
        }
    settings = Settings(**overrides)
    day = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=7)
    )
    arguments = {
        "destination": {"query": args.destination},
        "travelers": {"adults": 1},
        "max_results": 3,
    }
    if args.tool == "search_hotels":
        arguments.update(
            check_in=day.isoformat(),
            check_out=(day + timedelta(days=2)).isoformat(),
            rooms=1,
        )
    else:
        arguments.update(origin={"query": args.origin}, departure_date=day.isoformat())
    async with httpx.AsyncClient(trust_env=False) as client:
        registry = build_registry(settings, client)
        try:
            result = await registry.dispatch(args.tool, arguments)
        finally:
            await registry.close()
    data = result.data or {}
    offers = data.get("offers", [])
    report = {
        "tool": args.tool,
        "flyai_demo_requested": args.flyai_demo,
        "status": result.status,
        "error_code": result.error.code if result.error else None,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "suppliers": sorted({offer["supplier"] for offer in offers}),
        "returned_count": len(offers),
        "complete": data.get("complete"),
        "offers_with_money": sum(offer["price"]["money"] is not None for offer in offers),
        "offers_with_query_quotes": sum(
            offer["price"]["kind"] == "query_quote" for offer in offers
        ),
        "offers_with_known_inventory": sum(
            offer["inventory"]["status"] != "unknown" for offer in offers
        ),
        "warning_count": len(data.get("warnings", [])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result.status != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""Low-frequency, identical-condition flight-source probes; no personal sessions."""

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from uuid import uuid4

import httpx

from travel_tools.bootstrap import build_registry
from travel_tools.common import utc_now
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Consumes configured supplier quota.")
    parser.add_argument("--origin", default="广州")
    parser.add_argument("--destination", default="上海")
    parser.add_argument("--date", default="2026-10-17")
    parser.add_argument(
        "--providers", nargs="+", choices=["flyai", "csair", "ceair"], default=["flyai", "csair"]
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live for real configured queries.")
    directory = Path("artifacts") / ("flight-sources-live-" + uuid4().hex[:10])
    directory.mkdir(parents=True)
    conditions = {
        "origin": {"query": args.origin},
        "destination": {"query": args.destination},
        "departure_date": args.date,
        "travelers": {"adults": 1},
        "cabin": "economy",
        "nonstop_only": False,
        "max_results": 10,
        "tax_view": "included",
    }
    summary = []
    async with httpx.AsyncClient(trust_env=False) as client:
        registry = build_registry(Settings(), client)
        try:
            for provider in args.providers:
                for refresh in (True, False):
                    requests = 0

                    async def progress(stage, **detail):
                        nonlocal requests
                        requests += stage == "supplier_running"

                    start = time.monotonic()
                    result = await registry.dispatch(
                        "search_flights",
                        {"provider": provider, **conditions},
                        refresh=refresh,
                        progress=progress,
                    )
                    data = result.data or {}
                    offers = data.get("offers", [])
                    first_seconds = None
                    for warning in data.get("warnings", []):
                        found = re.search(
                            r"First valid rendered flight after ([\d.]+) seconds", warning
                        )
                        if found:
                            first_seconds = float(found[1])
                    row = {
                        "provider": provider,
                        "force_refresh": refresh,
                        "reuse": result.reuse,
                        "observed_at": utc_now().isoformat(),
                        "status": result.status,
                        "error": result.error.code if result.error else None,
                        "seconds": time.monotonic() - start,
                        "first_results_seconds": first_seconds,
                        "supplier_queries": requests,
                        "conditions": conditions,
                        "coverage": data.get("coverage"),
                        "source_urls": [s.get("url") for s in data.get("sources", [])],
                        "offers": len(offers),
                        "numeric_display_prices": sum(
                            (o["price"].get("display") or {}).get("amount") is not None
                            for o in offers
                        ),
                        "known_money_quotes": sum(o["price"]["money"] is not None for o in offers),
                        "known_inventory": sum(
                            o["inventory"]["status"] != "unknown" for o in offers
                        ),
                        "known_tax_basis": sum(
                            o["price"]["tax_basis"] != "unknown" for o in offers
                        ),
                        "operating_carriers": sum(
                            o["operating_carrier"] is not None for o in offers
                        ),
                    }
                    summary.append(row)
                    (directory / f"{provider}-{'fresh' if refresh else 'reuse'}.json").write_text(
                        result.model_dump_json(indent=2), encoding="utf-8"
                    )
        finally:
            await registry.close()
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"directory": str(directory), "results": summary}, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())

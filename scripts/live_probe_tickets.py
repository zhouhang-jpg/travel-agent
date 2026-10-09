"""Opt-in real queries through the running HTTP backend, without booking or model calls."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--bus-pages-only", action="store_true")
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/ticket-tools-live.json"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to use supplier quotas and independent browser queries.")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    later = (today + timedelta(days=6)).isoformat()
    base = {"departure_date": later, "travelers": {"adults": 1}, "max_results": 10}
    rail = {
        **base,
        "origin": {"query": "上海虹桥"},
        "destination": {"query": "杭州东"},
        "seat_class": "二等座",
        "direct_only": True,
    }
    air = {
        **base,
        "origin": {"query": "上海"},
        "destination": {"query": "北京"},
        "nonstop_only": True,
        "cabin": "economy",
        "max_results": 3,
    }
    cases = [
        (
            "ordinary_coaches",
            "search_coaches",
            {
                **base,
                "origin": {"query": "长春"},
                "destination": {"query": "梅河口"},
                "departure_date": (today + timedelta(days=1)).isoformat(),
                "max_results": 20,
            },
        ),
        (
            "rail_waitlist",
            "search_trains",
            {**rail, "provider": "12306", "train_numbers": ["D931", "D935"]},
        ),
        (
            "rail_seat_counts",
            "search_trains",
            {
                **rail,
                "provider": "12306",
                "origin": {"query": "北京南"},
                "destination": {"query": "济南西"},
                "train_numbers": ["G547"],
            },
        ),
        ("rail_candidates_direct", "search_trains", {**rail, "provider": "flyai"}),
        ("flight_candidates", "search_flights", air),
        (
            "flight_included_tax",
            "search_flights",
            {
                **air,
                "provider": "ceair",
                "flight_numbers": ["MU5232", "MU5129"],
                "tax_view": "included",
            },
        ),
        (
            "flight_excluded_tax",
            "search_flights",
            {
                **air,
                "provider": "ceair",
                "flight_numbers": ["MU5232", "MU5129"],
                "tax_view": "excluded",
            },
        ),
    ]
    if args.smoke_only:
        cases = [
            case
            for case in cases
            if case[0]
            in {
                "ordinary_coaches",
                "rail_waitlist",
                "flight_included_tax",
            }
        ]
    if args.bus_pages_only:
        cases = [
            (
                f"coach_page_{page}",
                "search_coaches",
                {
                    **base,
                    "origin": {"query": "兰州"},
                    "destination": {"query": "平凉"},
                    "departure_date": (today + timedelta(days=1)).isoformat(),
                    "max_results": 50,
                    "page": page,
                },
            )
            for page in (1, 2)
        ]
    report = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=70, trust_env=False) as client:
        for name, tool, arguments in cases:
            response = await client.post(f"/tools/{tool}/call", json={"arguments": arguments})
            response.raise_for_status()
            result = response.json()
            report.append({"case": name, "arguments": arguments, "result": result})
            data = result.get("data") or {}
            print(
                json.dumps(
                    {
                        "case": name,
                        "status": result["status"],
                        "error": result.get("error"),
                        "offers": len(data.get("offers", [])),
                        "coverage": data.get("coverage"),
                        "facts": [
                            {
                                "service": offer.get("service_number"),
                                "origin": offer["origin"]["query"],
                                "destination": offer["destination"]["query"],
                                "direct": offer.get("direct"),
                                "display_price": offer["price"].get("display"),
                                "tax_basis": offer["price"]["tax_basis"],
                                "inventory": offer["inventory"],
                            }
                            for offer in data.get("offers", [])[:3]
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(item["result"]["status"] != "ok" for item in report):
        raise SystemExit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

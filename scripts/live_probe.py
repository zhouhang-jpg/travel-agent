"""Explicit live checks; default runs only one public-web request.

Reports contain status/shape metadata, never raw supplier payloads or secrets.
Configured supplier calls are opt-in because quotas may be billed.
"""

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from travel_tools.bootstrap import build_registry
from travel_tools.common import utc_now
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--providers", action="store_true", help="Also call configured paid suppliers"
    )
    parser.add_argument(
        "--amap-all-modes",
        action="store_true",
        help="With --providers, also verify nearby search and driving/cycling/transit",
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/live-probe.json"))
    args = parser.parse_args()
    settings = Settings()
    async with httpx.AsyncClient(trust_env=False) as client:
        registry = build_registry(settings, client)
        calls = [("fetch_webpage", {"url": "https://example.com/"})]
        if args.providers:
            calls.extend(
                [
                    ("search_places", {"keywords": "故宫", "region": "北京", "page_size": 1}),
                    (
                        "get_routes",
                        {
                            "origin": {"longitude": 116.397, "latitude": 39.909, "crs": "gcj02"},
                            "destination": {
                                "longitude": 116.407,
                                "latitude": 39.919,
                                "crs": "gcj02",
                            },
                            "mode": "walking",
                        },
                    ),
                    (
                        "get_weather",
                        {
                            "coordinates": {"longitude": 116.41, "latitude": 39.92, "crs": "gcj02"},
                            "days": 1,
                        },
                    ),
                    ("search_web", {"query": "故宫博物院 官方 开放时间", "count": 1}),
                ]
            )
            if args.amap_all_modes:
                origin = {"longitude": 116.397, "latitude": 39.909, "crs": "gcj02"}
                destination = {"longitude": 116.407, "latitude": 39.919, "crs": "gcj02"}
                calls.append(
                    (
                        "search_places",
                        {
                            "center": origin,
                            "keywords": "公园",
                            "radius_m": 2000,
                            "page_size": 1,
                        },
                    )
                )
                for mode in ("driving", "bicycling", "transit"):
                    arguments = {"origin": origin, "destination": destination, "mode": mode}
                    if mode == "transit":
                        arguments.update(origin_city_code="010", destination_city_code="010")
                    calls.append(("get_routes", arguments))
        evidence = []
        for name, arguments in calls:
            result = await registry.dispatch(name, arguments)
            record = {
                "tool": name,
                "status": result.status,
                "started_at": result.started_at.isoformat(),
                "finished_at": result.finished_at.isoformat(),
                "error_code": result.error.code if result.error else None,
                "output_fields": sorted(result.data) if result.data else [],
            }
            if name == "search_places":
                record["mode"] = "nearby" if "center" in arguments else "text"
            if name == "get_routes":
                record["mode"] = arguments["mode"]
            if result.data:
                for field in ("places", "routes", "results", "days"):
                    if field in result.data:
                        record["returned_count"] = len(result.data[field])
                if name == "get_routes":
                    record["routes_with_duration"] = sum(
                        route["duration_s"] is not None for route in result.data["routes"]
                    )
            if name == "fetch_webpage" and result.data:
                record.update(
                    {
                        key: result.data[key]
                        for key in ("final_url", "title", "content_type", "body_bytes", "truncated")
                    }
                )
                record["example_domain_text_present"] = "Example Domain" in result.data["text"]
            evidence.append(record)
            if name == "search_places" and result.data and result.data["places"]:
                detail = await registry.dispatch(
                    "get_place_details", {"place_id": result.data["places"][0]["place_id"]}
                )
                evidence.append(
                    {
                        "tool": "get_place_details",
                        "status": detail.status,
                        "started_at": detail.started_at.isoformat(),
                        "finished_at": detail.finished_at.isoformat(),
                        "error_code": detail.error.code if detail.error else None,
                        "output_fields": sorted(detail.data) if detail.data else [],
                    }
                )
        report = {
            "checked_at": utc_now().isoformat(),
            "provider_probes_requested": args.providers,
            "evidence": evidence,
            "availability": [
                {"tool": x["name"], "availability": x["availability"]} for x in registry.catalog()
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if any(
            item["status"] == "error" and item["error_code"] != "tool_unavailable"
            for item in evidence
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

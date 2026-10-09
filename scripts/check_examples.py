"""Run synthetic local examples through the real dispatcher without credentials."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import httpx

from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def deny_network(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("Example validation must not request any external service.")


async def main() -> int:
    settings = Settings(
        _env_file=None,
        amap_api_key=None,
        qweather_api_key=None,
        qweather_api_host=None,
        bocha_api_key=None,
        tool_timeout_seconds=20,
        max_concurrent_calls=4,
    )
    matched = 0
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(deny_network), trust_env=False
    ) as client:
        registry = build_registry(settings, client)
        for expected in ("valid", "invalid", "unknown"):
            path = EXAMPLES / f"itinerary-{expected}.json"
            arguments = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
            result = await registry.dispatch("validate_itinerary", arguments)
            if result.status == "ok" and result.data is not None:
                actual = result.data["status"]
                counts = dict(
                    sorted(Counter(item["status"] for item in result.data["checks"]).items())
                )
                matched += actual == expected
                summary = {
                    "example": path.name,
                    "expected": expected,
                    "actual": actual,
                    "checks": counts,
                }
            else:
                summary = {
                    "example": path.name,
                    "expected": expected,
                    "actual": "tool_error",
                    "error_code": result.error.code if result.error else "missing_error",
                }
            print(json.dumps(summary, ensure_ascii=False))
    print(f"Examples matched: {matched}/3. Synthetic evidence; no live supplier verification.")
    return 0 if matched == 3 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

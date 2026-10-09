"""Export schemas offline; no credentials or network calls required."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/tool-catalog.json"))
    args = parser.parse_args()
    async with httpx.AsyncClient(trust_env=False) as client:
        registry = build_registry(Settings(), client)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "catalog": registry.catalog(),
                    "model_definitions": registry.model_definitions(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(
        f"Exported {len(registry.catalog())} contracts and "
        f"{len(registry.model_definitions())} available model definitions."
    )


if __name__ == "__main__":
    asyncio.run(main())

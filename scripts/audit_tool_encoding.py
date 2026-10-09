"""Offline lossless size audit of saved tool results; no model/provider requests."""

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from travel_agent.storage import Conversation, ConversationStore
from travel_agent.tool_encoding import decode_tool_result, encode_tool_result
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/tool-encoding-audit.json"))
    args = parser.parse_args()
    store = ConversationStore(Settings().database_url)
    report = []
    try:
        async with store.sessions() as session:
            for identifier, history in await session.execute(
                select(Conversation.id, Conversation.history)
            ):
                sizes = defaultdict(lambda: {"calls": 0, "before": 0, "after": 0})
                for message in history:
                    if message.get("role") != "tool":
                        continue
                    original = decode_tool_result(message["content"])
                    encoded = encode_tool_result(original)
                    if decode_tool_result(encoded) != original:
                        raise RuntimeError("Lossless round-trip failed.")
                    stats = sizes[original["tool_name"]]
                    stats["calls"] += 1
                    stats["before"] += len(
                        json.dumps(original, ensure_ascii=False, separators=(",", ":"))
                    )
                    stats["after"] += len(encoded)
                if sizes:
                    report.append({"conversation_id": identifier, "tools": dict(sizes)})
    finally:
        await store.engine.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

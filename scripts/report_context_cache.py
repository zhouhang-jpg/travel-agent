"""Read-only model prefix-cache diagnostics, with no conversation/model text.

Only usage, observation metadata and effect/run/conversation identifiers are
selected at the SQL boundary. Recovery replays do not create new effects and
are not counted again. Unknown outcomes remain unknown.
"""

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from travel_agent.cache_metrics import aggregate_cache_observations, cache_observation
from travel_agent.durable_storage import Effect, Run
from travel_tools.config import Settings


async def report(database_url: str, conversation_id: str | None = None) -> dict:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise ValueError("Diagnostics require an existing SQLite file.")
        path = await asyncio.to_thread(Path(url.database).resolve)
        url = url.set(
            database="file:" + path.as_posix(),
            query={**url.query, "mode": "ro", "uri": "true"},
        )
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            if engine.dialect.name == "sqlite":
                await connection.execute(text("PRAGMA query_only = ON"))
            elif engine.dialect.name == "postgresql":
                await connection.execute(text("SET TRANSACTION READ ONLY"))
            else:
                raise ValueError("Cache diagnostics support SQLite and PostgreSQL.")
            query = (
                select(
                    Effect.id,
                    Effect.run_id,
                    Run.conversation_id,
                    Effect.status,
                    Effect.attempts,
                    Effect.payload["usage"],
                    Effect.payload["cache_observation"],
                )
                .join(Run, Run.id == Effect.run_id)
                .where(Effect.kind == "model")
            )
            if conversation_id:
                query = query.where(Run.conversation_id == conversation_id)
            rows = (await connection.execute(query.order_by(Effect.id))).all()
        completed, runs, conversations = [], defaultdict(list), defaultdict(list)
        requests, uncertain_attempts = [], 0
        for effect_id, run_id, cid, status, attempts, usage, metadata in rows:
            uncertain_attempts += attempts - (1 if status == "complete" else 0)
            if status != "complete":
                continue
            observation = cache_observation(usage)
            completed.append(observation)
            runs[run_id].append(observation)
            conversations[cid].append(observation)
            safe_metadata = {
                field: metadata.get(field) if isinstance(metadata, dict) else None
                for field in ("context_version", "elapsed_seconds", "runtime_snapshot_utf8_bytes")
            }
            model = metadata.get("model", {}) if isinstance(metadata, dict) else {}
            safe_metadata["model"] = {
                field: model.get(field)
                for field in ("provider", "model", "thinking", "reasoning_effort")
            }
            requests.append(
                {
                    "effect_id": effect_id,
                    "run_id": run_id,
                    "conversation_id": cid,
                    "attempts": attempts,
                    **observation,
                    **safe_metadata,
                }
            )
        return {
            "scope": "committed model effects; supplier query caches excluded",
            "aggregate": aggregate_cache_observations(completed),
            "unknown_outcome_attempts": uncertain_attempts,
            "runs": {key: aggregate_cache_observations(value) for key, value in runs.items()},
            "conversations": {
                key: aggregate_cache_observations(value) for key, value in conversations.items()
            },
            "requests": requests,
        }
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--conversation-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = Settings(_env_file=args.env_file)
    result = asyncio.run(report(config.database_url, args.conversation_id))
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)

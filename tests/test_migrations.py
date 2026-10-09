import asyncio
import json
import sqlite3

from alembic import command
from alembic.config import Config

from travel_agent.runner import RunOutcome, repair_history
from travel_agent.storage import Conversation, ConversationStore


def test_migration_upgrade_persistence_and_downgrade_in_isolated_database(tmp_path, monkeypatch):
    database = tmp_path / "migration-check.db"
    database_url = f"sqlite+aiosqlite:///{database.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001",)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
        assert columns == set(Conversation.__table__.columns.keys())
        assert "ix_conversations_updated_at" in {
            row[1] for row in connection.execute("PRAGMA index_list(conversations)")
        }

    async def verify_persistence():
        store = ConversationStore(database_url)
        created = await store.create("Asia/Shanghai")  # Migration alone created the usable schema.
        lease = await store.acquire(created["id"], "测试需求", repair_history)
        history = [
            *lease["history"],
            {
                "role": "assistant",
                "content": "请补充日期。",
                "reasoning_content": "private-test",
            },
        ]
        await store.append_message(created["id"], lease["run_id"], "请补充日期。", "question")
        await store.finish(
            created["id"],
            lease["run_id"],
            RunOutcome(
                status="waiting_user",
                history=history,
                content="请补充日期。",
            ),
        )
        await store.close()

        reopened = ConversationStore(database_url)
        public = await reopened.get(created["id"])
        assert public["status"] == "waiting_user" and len(public["transcript"]) == 2
        assert "private-test" not in json.dumps(public)
        async with reopened.sessions() as session:
            row = await session.get(Conversation, created["id"])
            assert row.history == history
            assert row.active_run_id is None
        await reopened.close()

    asyncio.run(verify_persistence())
    command.downgrade(config, "base")
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='conversations'"
            ).fetchone()
            is None
        )
    command.upgrade(config, "head")

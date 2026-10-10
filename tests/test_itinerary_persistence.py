"""Version commits survive real process exits on both supported databases."""

import json

import pytest
import test_graph_restarts as restart_fixtures
from alembic import command
from alembic.config import Config
from test_graph_restarts import create, worker

isolated_database = restart_fixtures.isolated_database


@pytest.mark.parametrize("crash", ["tool_saved_before_checkpoint", "final_saved_before_end"])
def test_plan_publish_recovery_is_atomic_and_not_duplicated(isolated_database, tmp_path, crash):
    cid = create(isolated_database)
    worker(tmp_path, isolated_database, cid, scenario="plan", crash=crash)
    public = worker(tmp_path, isolated_database, cid, scenario="plan", mode="recover")
    assert public["status"] == "completed"
    assert public["itinerary"]["revision"] == 1
    assert public["itinerary"]["document"]["title"] == "恢复测试方案"
    assert "query_conditions" not in json.dumps(public)
    assert len([m for m in public["transcript"] if m["kind"] == "answer"]) == 1


def test_plan_migration_reversible_with_old_conversations_intact(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "plan-migration.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///" + database.as_posix())
    config = Config("alembic.ini")
    command.upgrade(config, "0002")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO conversations VALUES "
            "('old', '旧会话', 'idle', 'Asia/Shanghai', '', '', 0, NULL, '[]', '[]', NULL)"
        )
    command.upgrade(config, "head")
    command.downgrade(config, "0002")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT title FROM conversations").fetchone()[0] == "旧会话"
    command.upgrade(config, "head")

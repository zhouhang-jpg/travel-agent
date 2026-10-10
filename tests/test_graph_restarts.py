"""Kill/restart real processes at durable boundaries on SQLite and PostgreSQL."""

import asyncio
import json
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from travel_agent.checkpoints import FencedSaver
from travel_agent.durable_service import DurableService
from travel_agent.durable_storage import DurableStore, Effect, Run, StaleOwner
from travel_agent.models import ModelReply
from travel_agent.runner import RunLimits
from travel_agent.saver_lifecycle import open_saver
from travel_agent.storage import ConversationStore
from travel_tools.registry import ToolRegistry


@pytest.fixture(params=["sqlite", "postgres"])
def isolated_database(request, tmp_path):
    if request.param == "sqlite":
        yield "sqlite+aiosqlite:///" + (tmp_path / "restart.db").as_posix()
        return
    base = os.environ.get("TRAVEL_TEST_POSTGRES_URL")
    if not base:
        pytest.skip("Set TRAVEL_TEST_POSTGRES_URL to an isolated PostgreSQL test server.")
    name = "m1_" + uuid4().hex
    url = make_url(base).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
    try:
        yield url.set(drivername="postgresql+asyncpg", database=name).render_as_string(
            hide_password=False
        )
    finally:
        with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    psycopg.sql.Identifier(name)
                )
            )


def create(database_url):
    async def make():
        store = ConversationStore(database_url)
        await store.initialize()
        cid = (await store.create("Asia/Shanghai"))["id"]
        await store.close()
        return cid

    return asyncio.run(make())


def worker(
    tmp_path, database_url, cid, *, mode="start", crash=None, scenario="question", **options
):
    payload = {
        "database_url": database_url,
        "conversation_id": cid,
        "mode": mode,
        "request_id": options.pop("request_id", "r1"),
        "crash": crash,
        "scenario": scenario,
        "trace": str(tmp_path / "trace.txt"),
        "output": str(tmp_path / "public.json"),
        **options,
    }
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "tests/fixtures/durable_worker.py", str(path)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == (73 if crash else 0), result.stderr[-4000:]
    if not crash:
        return json.loads((tmp_path / "public.json").read_text(encoding="utf-8"))


def trace(tmp_path, name):
    return (tmp_path / "trace.txt").read_text(encoding="utf-8").splitlines().count(name)


@pytest.mark.parametrize("crash", ["question_draft", "waiting_checkpoint", "question_ready"])
def test_waiting_crash_boundaries(isolated_database, tmp_path, crash):
    cid = create(isolated_database)
    worker(tmp_path, isolated_database, cid, crash=crash)
    public = worker(tmp_path, isolated_database, cid, mode="recover")
    assert public["status"] == "waiting_user" and public["question_id"]
    assert len([m for m in public["transcript"] if m["kind"] == "question"]) == 1
    assert trace(tmp_path, "model") == 1


@pytest.mark.parametrize(
    "crash",
    [
        "answer_accepted",
        "resume_advanced",
        "question_draft",
        "waiting_checkpoint",
        "question_ready",
    ],
)
def test_answer_resume_gaps_do_not_reanswer_new_question(isolated_database, tmp_path, crash):
    cid = create(isolated_database)
    first = worker(tmp_path, isolated_database, cid)
    worker(
        tmp_path,
        isolated_database,
        cid,
        request_id="r2",
        content="日期不确定，你决定",
        question_id=first["question_id"],
        crash=crash,
    )
    second = worker(tmp_path, isolated_database, cid, mode="recover")
    assert second["status"] == "waiting_user" and second["question_id"] != first["question_id"]
    final = worker(
        tmp_path,
        isolated_database,
        cid,
        request_id="r3",
        content="目的地也请推荐",
        question_id=second["question_id"],
    )
    assert final["status"] == "completed"
    assert len([m for m in final["transcript"] if m["role"] == "user"]) == 3
    assert trace(tmp_path, "model") == 3


@pytest.mark.parametrize(
    "crash,models,tools",
    [
        ("model_inflight", 3, 2),
        ("model_saved_before_checkpoint", 2, 2),
        ("tool_returned_before_save", 2, 3),
        ("tool_saved_before_checkpoint", 2, 2),
        ("final_saved_before_end", 2, 2),
    ],
)
def test_model_tool_final_recovery(isolated_database, tmp_path, crash, models, tools):
    cid = create(isolated_database)
    worker(tmp_path, isolated_database, cid, scenario="query", crash=crash)
    public = worker(tmp_path, isolated_database, cid, scenario="query", mode="recover")
    assert public["status"] == "completed"
    assert trace(tmp_path, "model") == models and trace(tmp_path, "tool") == tools
    assert len([m for m in public["transcript"] if m["kind"] == "answer"]) == 1

    async def facts():
        store = ConversationStore(isolated_database)
        async with store.sessions() as session:
            run = (await session.scalars(select(Run))).one()
            effects = (await session.scalars(select(Effect))).all()
            assert run.model_requests == models
            assert run.active_seconds >= 0
            if crash == "model_inflight":
                assert run.active_seconds >= 0.5
            if crash == "tool_returned_before_save":
                retries = [e for e in effects if e.attempts > 1]
                assert retries[0].attempt_log[-1]["previous_completion"] == "unknown"
        await store.close()

    asyncio.run(facts())


def test_budget_survives_process_restart(isolated_database, tmp_path):
    cid = create(isolated_database)
    worker(
        tmp_path,
        isolated_database,
        cid,
        scenario="budget",
        steps=2,
        crash="model_saved_before_checkpoint",
    )
    result = worker(tmp_path, isolated_database, cid, scenario="budget", steps=2, mode="recover")
    assert result["status"] == "error" and result["last_error"]["code"] == "step_limit"
    assert trace(tmp_path, "model") == 2


@pytest.mark.parametrize(
    "crash",
    [
        "context_saved_before_model",
        "model_saved_before_checkpoint",
        "tools_history_merged_before_checkpoint",
    ],
)
def test_runtime_snapshot_prefix_and_accounting_survive_process_restart(
    isolated_database, tmp_path, crash
):
    from scripts.report_context_cache import report
    from travel_agent.runtime_context import RUNTIME_MARKER, runtime_state

    cid = create(isolated_database)
    scenario = "parallel" if crash == "tools_history_merged_before_checkpoint" else "query"
    worker(tmp_path, isolated_database, cid, scenario=scenario, crash=crash)

    async def read():
        store = ConversationStore(isolated_database)
        history = await DurableStore(store).history(cid)
        await store.close()
        return history

    before = asyncio.run(read())
    original = json.dumps(before, ensure_ascii=False)
    public = worker(tmp_path, isolated_database, cid, scenario=scenario, mode="recover")
    assert public["status"] == "completed"
    after = asyncio.run(read())
    assert json.dumps(after[: len(before)], ensure_ascii=False) == original
    snapshots = [
        m for m in after if m["role"] == "system" and m["content"].startswith(RUNTIME_MARKER)
    ]
    expected = 3 if crash == "context_saved_before_model" else 2
    assert len(snapshots) == expected
    assert [
        runtime_state(snapshots[:i])["planning"]["run_budget"][
            "model_requests_used_including_this_request"
        ]
        for i in range(1, expected + 1)
    ] == list(range(1, expected + 1))
    assert (
        runtime_state(after)["planning"]["run_budget"]["model_requests_remaining_after_this"]
        == 12 - expected
    )
    assert trace(tmp_path, "model") == 2
    worker(tmp_path, isolated_database, cid, scenario=scenario)
    assert asyncio.run(read()) == after and trace(tmp_path, "model") == 2
    diagnostics = asyncio.run(report(isolated_database))
    assert diagnostics["aggregate"]["completed_requests"] == 2
    assert diagnostics["unknown_outcome_attempts"] == expected - 2
    assert "private-reasoning" not in json.dumps(diagnostics)
    assert "运行状态快照" not in json.dumps(public, ensure_ascii=False)


@pytest.mark.parametrize(
    "crash",
    [
        "parallel_fast_saved_before_slow",
        "tools_effects_saved_before_merge",
        "tools_history_merged_before_checkpoint",
    ],
)
def test_parallel_process_crash_windows(isolated_database, tmp_path, crash):
    cid = create(isolated_database)
    worker(tmp_path, isolated_database, cid, scenario="parallel", crash=crash)

    async def inspect_before():
        store = ConversationStore(isolated_database)
        facts = DurableStore(store)
        async with store.sessions() as session:
            effects = (await session.scalars(select(Effect).where(Effect.kind == "tool"))).all()
            assert next(e for e in effects if e.id.endswith("/1")).status == "complete"
            slow = next(e for e in effects if e.id.endswith("/0"))
            assert slow.status == (
                "started" if crash == "parallel_fast_saved_before_slow" else "complete"
            )
        history = await facts.history(cid)
        assert len([m for m in history if m["role"] == "tool"]) == (
            2 if crash == "tools_history_merged_before_checkpoint" else 0
        )
        await store.close()

    asyncio.run(inspect_before())
    public = worker(tmp_path, isolated_database, cid, scenario="parallel", mode="recover")
    assert public["status"] == "completed"
    assert trace(tmp_path, "model") == 2
    assert trace(tmp_path, "tool:1") == 1
    assert trace(tmp_path, "tool:0") == (2 if crash == "parallel_fast_saved_before_slow" else 1)
    repeated = worker(tmp_path, isolated_database, cid, scenario="parallel")
    assert repeated["status"] == "completed" and trace(tmp_path, "model") == 2


def test_real_postgres_alembic_reversible(isolated_database):
    environment = {**os.environ, "DATABASE_URL": isolated_database}
    for arguments in [("upgrade", "head"), ("downgrade", "0001"), ("upgrade", "head")]:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr[-3000:]

    async def inspect():
        store = ConversationStore(isolated_database)
        cid = (await store.create("Asia/Shanghai"))["id"]
        assert (await store.get(cid))["status"] == "idle"
        await store.close()

    asyncio.run(inspect())


def test_native_checkpoint_fence_on_both_backends(isolated_database):
    async def verify():
        store = ConversationStore(isolated_database)
        await store.initialize()
        facts = DurableStore(store)
        cid = (await store.create("Asia/Shanghai"))["id"]
        old = await facts.accept(cid, "写入围栏", "r1")

        class Model:
            async def complete(self, messages, tools):
                return ModelReply({"role": "assistant", "content": "done"}, "stop", {})

        async with open_saver(store.engine.url) as native:
            runtime = DurableService(store, Model(), ToolRegistry(), RunLimits(), native).runtime
            graph = runtime.graph(old)
            config = {"configurable": {"thread_id": cid + "/langgraph-v1"}}
            await graph.aupdate_state(config, {"step": 0}, as_node="__start__")
            current = (await facts.recover())[0]
            stale = FencedSaver(native, facts, old)
            checkpoint = await stale.aget_tuple(config)
            canonical = await facts.cursor(cid)
            with pytest.raises(StaleOwner):
                await stale.aput(
                    checkpoint.config,
                    checkpoint.checkpoint,
                    checkpoint.metadata,
                    checkpoint.checkpoint["channel_versions"],
                )
            with pytest.raises(StaleOwner):
                await stale.aput_writes(checkpoint.config, [("step", 999)], "old-owner")
            with pytest.raises(StaleOwner):
                await facts.finalize(old, {"status": "completed", "content": "stale"})
            assert await facts.cursor(cid) == canonical
            restored = await FencedSaver(native, facts, current).aget_tuple(config)
            assert not any(item[0] == "old-owner" for item in restored.pending_writes)
        await store.close()

    asyncio.run(verify())

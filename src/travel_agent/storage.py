"""Durable full conversation history, with an atomic single-run lease per conversation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Integer, String, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from travel_tools.common import utc_now


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24))
    timezone: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    last_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ConversationNotFound(Exception):
    pass


class ConversationBusy(Exception):
    pass


def public_view(row: Conversation) -> dict:
    """Internal model messages, tool arguments and reasoning never cross this boundary."""
    return {
        "id": row.id,
        "title": row.title,
        "status": row.status,
        "timezone": row.timezone,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "transcript": deepcopy(row.transcript),
        "last_error": deepcopy(row.last_error),
    }


class ConversationStore:
    def __init__(self, database_url: str):
        url = make_url(database_url)
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+asyncpg")
        if url.drivername == "sqlite":
            url = url.set(drivername="sqlite+aiosqlite")
        if url.drivername not in {"sqlite+aiosqlite", "postgresql+asyncpg"}:
            raise ValueError("Use SQLite for local development or PostgreSQL for deployment.")
        if url.drivername == "sqlite+aiosqlite" and url.database not in {None, ":memory:"}:
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, echo=False, hide_parameters=True, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        # Convenient for local MVP. Alembic owns schema upgrades after the first release.
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self):
        await self.engine.dispose()

    async def create(self, timezone: str) -> dict:
        now = utc_now().isoformat()
        row = Conversation(
            id=str(uuid4()),
            title="新的出行计划",
            status="idle",
            timezone=timezone,
            created_at=now,
            updated_at=now,
            revision=0,
            history=[],
            transcript=[],
            last_error=None,
        )
        async with self.sessions.begin() as session:
            session.add(row)
        return public_view(row)

    async def get(self, conversation_id: str) -> dict:
        async with self.sessions() as session:
            row = await session.get(Conversation, conversation_id)
            if row is None:
                raise ConversationNotFound
            return public_view(row)

    async def list(self) -> list[dict]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(Conversation).order_by(Conversation.updated_at.desc()).limit(100)
                )
            ).all()
            return [
                {key: public_view(row)[key] for key in ("id", "title", "status", "updated_at")}
                for row in rows
            ]

    async def acquire(self, conversation_id: str, content: str, repair_history) -> dict:
        async with self.sessions.begin() as session:
            row = await session.get(Conversation, conversation_id)
            if row is None:
                raise ConversationNotFound
            if row.status == "running":
                raise ConversationBusy
            now, run_id = utc_now().isoformat(), str(uuid4())
            history = repair_history(row.history)
            history.append({"role": "user", "content": content})
            transcript = [
                *deepcopy(row.transcript),
                {"role": "user", "content": content, "kind": "message", "created_at": now},
            ]
            result = await session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.revision == row.revision,
                    Conversation.status != "running",
                )
                .values(
                    status="running",
                    active_run_id=run_id,
                    revision=row.revision + 1,
                    history=history,
                    transcript=transcript,
                    title=content[:60] if not row.transcript else row.title,
                    updated_at=now,
                    last_error=None,
                )
            )
            if result.rowcount != 1:
                raise ConversationBusy
            return {"run_id": run_id, "history": history, "timezone": row.timezone}

    async def save_history(self, conversation_id: str, run_id: str, history: list[dict]):
        async with self.sessions.begin() as session:
            await session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.active_run_id == run_id,
                    Conversation.status == "running",
                )
                .values(history=deepcopy(history), updated_at=utc_now().isoformat())
            )

    async def append_message(self, conversation_id: str, run_id: str, content: str, kind: str):
        async with self.sessions.begin() as session:
            row = await session.get(Conversation, conversation_id)
            if row is None or row.active_run_id != run_id or row.status != "running":
                return
            now = utc_now().isoformat()
            row.transcript = [
                *deepcopy(row.transcript),
                {"role": "assistant", "content": content, "kind": kind, "created_at": now},
            ]
            row.updated_at = now

    async def finish(self, conversation_id: str, run_id: str, outcome):
        async with self.sessions.begin() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id, Conversation.active_run_id == run_id)
                .values(
                    status=outcome.status,
                    active_run_id=None,
                    history=deepcopy(outcome.history),
                    last_error=outcome.error,
                    updated_at=utc_now().isoformat(),
                )
            )

    async def recover_runs(self, repair_history):
        """Single-process MVP: a previous process cannot still own these leases."""
        async with self.sessions.begin() as session:
            rows = (
                await session.scalars(select(Conversation).where(Conversation.status == "running"))
            ).all()
            # Graph threads remain pinned even when the new-run engine is legacy.
            from travel_agent.durable_storage import Head

            for row in rows:
                if await session.get(Head, row.id):
                    continue
                now = utc_now().isoformat()
                message = "服务重启中断了上一轮执行。已保留历史，你可以发送消息继续。"
                row.history = repair_history(row.history)
                row.status, row.active_run_id = "error", None
                row.last_error = {"code": "interrupted", "message": message, "retryable": True}
                row.transcript = [
                    *deepcopy(row.transcript),
                    {"role": "assistant", "content": message, "kind": "error", "created_at": now},
                ]
                row.updated_at = now

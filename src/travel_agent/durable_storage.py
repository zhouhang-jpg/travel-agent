"""Committed facts for the durable runtime; graph checkpoints own execution position.

All mutations take a transactional fence on the conversation head. Checkpoint blobs
are immutable generations; only their canonical reference participates in this fence.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import JSON, Float, Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from travel_agent.itineraries import abandon_drafts, publish_version, stage_version
from travel_agent.storage import Base, Conversation, ConversationBusy, ConversationNotFound
from travel_tools.common import utc_now

ENGINE = "langgraph-v1"


class StaleOwner(Exception):
    pass


class RequestConflict(Exception):
    pass


class Head(Base):
    __tablename__ = "agent_heads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    engine: Mapped[str] = mapped_column(String(40), default=ENGINE)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    question_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    journal_seq: Mapped[int] = mapped_column(Integer, default=0)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)


class Run(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), default="running")
    model_requests: Mapped[int] = mapped_column(Integer, default=0)
    active_seconds: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resume: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    runtime_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AcceptedRequest(Base):
    __tablename__ = "agent_requests"
    id: Mapped[str] = mapped_column(String(150), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(36))
    start_seq: Mapped[int] = mapped_column(Integer)


class Journal(Base):
    __tablename__ = "agent_journal"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)


class Effect(Base):
    __tablename__ = "agent_effects"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="started")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt_log: Mapped[list] = mapped_column(JSON, default=list)


class Question(Base):
    __tablename__ = "agent_questions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    effect_id: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    checkpoint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    answered_run: Mapped[str | None] = mapped_column(String(36), nullable=True)


class PublicEvent(Base):
    __tablename__ = "agent_events"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)


class CursorSnapshot(Base):
    __tablename__ = "agent_cursor_snapshots"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)


class DurableStore:
    def __init__(self, store):
        self.store = store

    @asynccontextmanager
    async def guarded(self, lease):
        async with self.store.sessions.begin() as session:
            result = await session.execute(
                update(Head)
                .where(
                    Head.id == lease["conversation_id"],
                    Head.owner == lease["owner"],
                    Head.epoch == lease["epoch"],
                )
                .values(version=Head.version + 1)
            )
            if result.rowcount != 1:
                raise StaleOwner
            head = await session.get(Head, lease["conversation_id"])
            conversation = await session.get(Conversation, head.id)
            run = await session.get(Run, lease["run_id"])
            yield session, head, conversation, run

    async def append_raw(self, session, head, conversation, key, payload):
        if await session.get(Journal, key):
            return
        head.journal_seq += 1
        session.add(
            Journal(
                id=key, conversation_id=head.id, seq=head.journal_seq, payload=deepcopy(payload)
            )
        )
        conversation.history = [*conversation.history, deepcopy(payload)]

    async def event(self, session, head, conversation, run, key, payload):
        old = await session.get(PublicEvent, key)
        if old:
            return
        head.event_seq += 1
        event = {**deepcopy(payload), "event_seq": head.event_seq, "run_id": run.id}
        if payload["type"] == "message":
            event["message_id"] = key
        session.add(
            PublicEvent(
                id=key, conversation_id=head.id, run_id=run.id, seq=head.event_seq, payload=event
            )
        )
        if payload["type"] == "message":
            conversation.transcript = [
                *conversation.transcript,
                {
                    "role": payload.get("role", "assistant"),
                    "content": payload["content"],
                    "kind": payload.get("kind", "answer"),
                    "created_at": utc_now().isoformat(),
                    "message_id": key,
                    **({"question_id": payload["question_id"]} if "question_id" in payload else {}),
                },
            ]
        conversation.updated_at = utc_now().isoformat()

    async def accept(
        self, conversation_id, content, request_id, question_id=None, runtime_spec=None
    ):
        try:
            return await self._accept(
                conversation_id, content, request_id, question_id, runtime_spec
            )
        except ConversationBusy:
            # A concurrent copy of the same request can lose the conversation CAS.
            # Recheck committed idempotency after the failed transaction rolls back.
            key = f"{conversation_id}/{request_id}"
            fingerprint = sha256((content + "\0" + (question_id or "")).encode()).hexdigest()
            async with self.store.sessions() as session:
                previous = await session.get(AcceptedRequest, key)
                if previous:
                    if previous.payload_hash != fingerprint:
                        raise RequestConflict from None
                    return {
                        "duplicate": True,
                        "run_id": previous.run_id,
                        "start_seq": previous.start_seq,
                        "conversation_id": conversation_id,
                    }
            raise

    async def _accept(
        self, conversation_id, content, request_id, question_id=None, runtime_spec=None
    ):
        key = f"{conversation_id}/{request_id}"
        fingerprint = sha256((content + "\0" + (question_id or "")).encode()).hexdigest()
        async with self.store.sessions.begin() as session:
            previous = await session.get(AcceptedRequest, key)
            if previous:
                if previous.payload_hash != fingerprint:
                    raise RequestConflict
                return {
                    "duplicate": True,
                    "run_id": previous.run_id,
                    "start_seq": previous.start_seq,
                    "conversation_id": conversation_id,
                }
            row = await session.get(Conversation, conversation_id)
            if row is None:
                raise ConversationNotFound
            if row.status == "running":
                raise ConversationBusy
            result = await session.execute(
                update(Conversation)
                .where(
                    Conversation.id == row.id,
                    Conversation.revision == row.revision,
                    Conversation.status != "running",
                )
                .values(revision=Conversation.revision + 1)
            )
            if result.rowcount != 1:
                raise ConversationBusy
            head = await session.get(Head, row.id)
            if head is None:
                head = Head(
                    id=row.id, engine=ENGINE, epoch=0, version=0, journal_seq=0, event_seq=0
                )
                session.add(head)
                for i, message in enumerate(deepcopy(row.history)):
                    head.journal_seq += 1
                    session.add(
                        Journal(
                            id=f"{row.id}/import/{i}",
                            conversation_id=row.id,
                            seq=head.journal_seq,
                            payload=message,
                        )
                    )
            if head.engine != ENGINE:
                raise RequestConflict
            run_id, owner = str(uuid4()), str(uuid4())
            resume = None
            if head.question_id:
                question = await session.get(Question, head.question_id)
                if (
                    question.status != "ready"
                    or question.answered_run
                    or (question_id and question_id != question.id)
                    or not head.cursor
                    or not question.checkpoint
                    or head.cursor["configurable"]["checkpoint_id"]
                    != question.checkpoint["configurable"]["checkpoint_id"]
                ):
                    raise RequestConflict
                question.status, question.answered_run = "answered", run_id
                resume = {
                    "question_id": question.id,
                    "run_id": run_id,
                    "request_id": request_id,
                    "checkpoint": question.checkpoint,
                }
            elif question_id:
                raise RequestConflict
            head.epoch += 1
            head.owner = owner
            row.status, row.active_run_id, row.last_error = "running", run_id, None
            if not row.transcript:
                row.title = content[:60]
            run = Run(
                id=run_id,
                conversation_id=row.id,
                request_id=request_id,
                status="running",
                model_requests=0,
                active_seconds=0,
                resume=resume,
                runtime_spec=deepcopy(runtime_spec),
            )
            session.add(run)
            start_seq = head.event_seq
            session.add(
                AcceptedRequest(
                    id=key, payload_hash=fingerprint, run_id=run_id, start_seq=start_seq
                )
            )
            await self.append_raw(session, head, row, key, {"role": "user", "content": content})
            await self.event(
                session,
                head,
                row,
                run,
                key + "/user",
                {"type": "message", "role": "user", "content": content, "kind": "message"},
            )
            await self.event(
                session,
                head,
                row,
                run,
                run_id + "/running",
                {"type": "status", "status": "running"},
            )
            return {
                "conversation_id": row.id,
                "run_id": run_id,
                "owner": owner,
                "epoch": head.epoch,
                "start_seq": start_seq,
                "resume": resume,
                "timezone": row.timezone,
            }

    async def cursor(self, conversation_id, checkpoint_id=None):
        async with self.store.sessions() as session:
            if checkpoint_id:
                snapshot = await session.get(CursorSnapshot, f"{conversation_id}/{checkpoint_id}")
                return deepcopy(snapshot.config) if snapshot else None
            head = await session.get(Head, conversation_id)
            return deepcopy(head.cursor) if head else None

    async def publish_cursor(self, lease, cursor, *, advance=True):
        async with self.guarded(lease) as (session, head, _, _):
            checkpoint_id = cursor["configurable"]["checkpoint_id"]
            key = f"{head.id}/{checkpoint_id}"
            snapshot = await session.get(CursorSnapshot, key)
            if snapshot:
                snapshot.config = deepcopy(cursor)
            else:
                session.add(CursorSnapshot(id=key, config=deepcopy(cursor)))
            if advance or (
                head.cursor and head.cursor["configurable"]["checkpoint_id"] == checkpoint_id
            ):
                head.cursor = deepcopy(cursor)

    async def history(self, conversation_id):
        async with self.store.sessions() as session:
            rows = (
                await session.scalars(
                    select(Journal)
                    .where(Journal.conversation_id == conversation_id)
                    .order_by(Journal.seq)
                )
            ).all()
            return [deepcopy(row.payload) for row in rows]

    async def run(self, run_id):
        async with self.store.sessions() as session:
            return await session.get(Run, run_id)

    async def question(self, question_id):
        async with self.store.sessions() as session:
            return await session.get(Question, question_id)

    async def effect(self, key):
        async with self.store.sessions() as session:
            return await session.get(Effect, key)

    async def begin_effect(self, lease, key, kind, max_steps):
        async with self.guarded(lease) as (session, _, _, run):
            effect = await session.get(Effect, key)
            if effect and effect.status == "complete":
                return deepcopy(effect.payload)
            if kind == "model":
                if run.model_requests >= max_steps:
                    raise BudgetExceeded("step_limit")
                run.model_requests += 1
            if effect is None:
                effect = Effect(id=key, run_id=run.id, kind=kind, attempts=0, attempt_log=[])
                session.add(effect)
            effect.attempts += 1
            effect.attempt_log = [
                *effect.attempt_log,
                {
                    "attempt": effect.attempts,
                    "started_at": utc_now().isoformat(),
                    "previous_completion": "unknown" if effect.attempts > 1 else "none",
                },
            ]
            return None

    async def commit_effect(self, lease, key, payload, raw, question=None, itinerary=None):
        async with self.guarded(lease) as (session, head, conversation, run):
            effect = await session.get(Effect, key)
            if effect.status == "complete":
                return
            effect.status, effect.payload = "complete", deepcopy(payload)
            await self.append_raw(session, head, conversation, key, raw)
            if itinerary:
                await stage_version(session, head.id, run.id, key, head.journal_seq, itinerary)
            elif raw.get("role") == "tool" and payload.get("status") == "error":
                import json

                if json.loads(raw["content"]).get("tool_name") == "save_itinerary":
                    await abandon_drafts(session, head.id, run.id)
            if question:
                session.add(
                    Question(
                        id=question["id"],
                        conversation_id=head.id,
                        run_id=run.id,
                        effect_id=key,
                        message=question["message"],
                        status="draft",
                    )
                )

    async def emit(self, lease, key, payload):
        async with self.guarded(lease) as (session, head, conversation, run):
            await self.event(session, head, conversation, run, key, payload)

    async def spend_time(self, lease, seconds, max_seconds):
        async with self.guarded(lease) as (_, _, _, run):
            run.active_seconds += seconds
            exceeded = run.active_seconds >= max_seconds
        if exceeded:
            raise BudgetExceeded("run_timeout")

    async def finalize(self, lease, result):
        async with self.guarded(lease) as (session, head, conversation, run):
            if run.result:
                return
            if result["status"] == "error":
                from travel_agent.runner import _repair

                repaired = _repair(
                    conversation.history, result["error"]["code"], result["error"]["message"]
                )
                original_length = len(conversation.history)
                for index, raw in enumerate(repaired[original_length:]):
                    await self.append_raw(
                        session, head, conversation, f"{run.id}/uncompleted/{index}", raw
                    )
            run.result = deepcopy(result)
            version = await publish_version(
                session, head.id, run.id, result["status"] == "completed"
            )
            if version:
                await self.event(
                    session,
                    head,
                    conversation,
                    run,
                    version.id + "/published",
                    {
                        "type": "itinerary_updated",
                        "version_id": version.id,
                        "revision": version.revision,
                    },
                )
            await self.event(
                session,
                head,
                conversation,
                run,
                run.id + "/result",
                {
                    "type": "message",
                    "role": "assistant",
                    "content": result["content"],
                    "kind": "error" if result["status"] == "error" else "answer",
                },
            )

    async def settle(self, lease, question_id=None):
        async with self.guarded(lease) as (session, head, conversation, run):
            if question_id:
                question = await session.get(Question, question_id)
                if question.status != "draft":
                    raise RequestConflict
                question.status, question.checkpoint = "ready", deepcopy(head.cursor)
                head.question_id = question.id
                await self.event(
                    session,
                    head,
                    conversation,
                    run,
                    question.id + "/ready",
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": question.message,
                        "kind": "question",
                        "question_id": question.id,
                    },
                )
                status, error = "waiting_user", None
            else:
                status, error = run.result["status"], run.result.get("error")
                head.question_id = None
            run.status = status
            conversation.status, conversation.last_error = status, error
            conversation.active_run_id, head.owner = None, None
            await self.event(
                session,
                head,
                conversation,
                run,
                run.id + "/status",
                {
                    "type": "status",
                    "status": status,
                },
            )
            await self.event(
                session,
                head,
                conversation,
                run,
                run.id + "/done",
                {
                    "type": "done",
                    "status": status,
                },
            )

    async def recover(self):
        # Single application process deployment: startup fences all prior owners.
        leases = []
        async with self.store.sessions.begin() as session:
            rows = (
                await session.scalars(
                    select(Conversation)
                    .join(Head, Head.id == Conversation.id)
                    .where(Conversation.status == "running")
                )
            ).all()
            for row in rows:
                head = await session.get(Head, row.id)
                run = await session.get(Run, row.active_run_id)
                head.epoch += 1
                head.owner = str(uuid4())
                leases.append(
                    {
                        "conversation_id": row.id,
                        "run_id": run.id,
                        "owner": head.owner,
                        "epoch": head.epoch,
                        "resume": deepcopy(run.resume),
                        "timezone": row.timezone,
                    }
                )
        return leases

    async def events(self, conversation_id, after=0, run_id=None):
        async with self.store.sessions() as session:
            query = select(PublicEvent).where(
                PublicEvent.conversation_id == conversation_id, PublicEvent.seq > after
            )
            if run_id:
                query = query.where(PublicEvent.run_id == run_id)
            rows = (await session.scalars(query.order_by(PublicEvent.seq))).all()
            return [deepcopy(row.payload) for row in rows]


class BudgetExceeded(Exception):
    pass

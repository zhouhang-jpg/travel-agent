"""Background graph execution and replay from committed public events."""

import asyncio
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from travel_agent.durable_storage import DurableStore, Head, Question, StaleOwner
from travel_agent.graph_runtime import GraphRuntime, failure
from travel_agent.itineraries import ItineraryService, public_version
from travel_agent.service import ModelNotConfigured
from travel_agent.storage import Conversation, ConversationNotFound, public_view


class DurableService:
    def __init__(self, store, model, registry, limits, saver, *, failpoint=None):
        self.store = store
        self.facts = DurableStore(store)
        self.model = model
        self.itineraries = ItineraryService(self.facts)
        self.runtime = GraphRuntime(self.facts, model, registry, limits, saver, failpoint=failpoint)
        self.tasks = set()
        self.followers = set()

    def launch(self, lease):
        async def execute():
            while True:
                try:
                    await self.runtime.execute(lease)
                    return
                except StaleOwner:
                    return
                except SQLAlchemyError:
                    # Retry the durable cursor when storage returns; never expose
                    # raw driver exceptions, and never reset run budgets/history.
                    await asyncio.sleep(1)
                except Exception:
                    try:
                        await self.facts.finalize(
                            lease,
                            failure(
                                "recovery_failed",
                                "本轮恢复遇到不一致的执行记录，已停止并保留历史。请重新发送需求。",
                            ),
                        )
                        await self.facts.settle(lease)
                        return
                    except StaleOwner:
                        return
                    except SQLAlchemyError:
                        await asyncio.sleep(1)

        task = asyncio.create_task(execute())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def recover(self):
        if self.model is None:
            return
        for lease in await self.facts.recover():
            self.launch(lease)

    async def get(self, conversation_id):
        async with self.store.sessions() as session:
            joined = (
                await session.execute(
                    select(Conversation, Head)
                    .outerjoin(Head, Head.id == Conversation.id)
                    .where(Conversation.id == conversation_id)
                )
            ).first()
            if joined is None:
                raise ConversationNotFound
            conversation, head = joined
            public = public_view(conversation)
            if head:
                public["engine"] = head.engine
                public["event_seq"] = head.event_seq
                question = (
                    await session.get(Question, head.question_id) if head.question_id else None
                )
                public["question_id"] = (
                    question.id if question and question.status == "ready" else None
                )
        current = await self.itineraries.current(conversation_id)
        public["itinerary"] = public_version(current) if current else None
        return public

    async def list(self):
        return await self.store.list()

    async def start(self, conversation_id, content, request_id=None, question_id=None):
        if self.model is None:
            raise ModelNotConfigured
        lease = await self.facts.accept(
            conversation_id,
            content,
            request_id or str(uuid4()),
            question_id,
            self.runtime.manifest,
        )
        await self.runtime.hit("answer_accepted")
        if not lease.get("duplicate"):
            self.launch(lease)
        queue = asyncio.Queue()

        async def follow():
            after = lease["start_seq"]
            while True:
                events = await self.facts.events(conversation_id, after, lease["run_id"])
                for event in events:
                    after = event["event_seq"]
                    if event.get("role") != "user":
                        await queue.put(event)
                    if event["type"] == "done":
                        return
                await asyncio.sleep(0.1)

        task = asyncio.create_task(follow())
        self.followers.add(task)
        task.add_done_callback(self.followers.discard)
        return queue

    async def replay(self, conversation_id, after):
        await self.store.get(conversation_id)
        import json

        while True:
            events = await self.facts.events(conversation_id, after)
            for event in events:
                after = event["event_seq"]
                yield f"id: {after}\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            current = await self.store.get(conversation_id)
            if current["status"] != "running":
                return
            if not events:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    async def close(self):
        tasks = [*self.tasks, *self.followers]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def pinned_graph_conversations(store):
    async with store.sessions() as session:
        return set(await session.scalars(select(Head.id)))

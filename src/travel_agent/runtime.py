"""Version-pinned runtime selection; the switch applies only to unpinned chats."""

from typing import Protocol

from travel_agent.durable_storage import Head


class ExecutionRuntime(Protocol):
    async def execute(self, lease: dict) -> None: ...


class RuntimeService:
    def __init__(self, legacy, durable, default_engine):
        self.legacy, self.durable, self.default_engine = legacy, durable, default_engine
        self.store = legacy.store

    @property
    def tasks(self):
        return self.legacy.tasks | self.durable.tasks

    @property
    def failed_saves(self):
        return self.legacy.failed_saves

    async def selected(self, conversation_id):
        async with self.store.sessions() as session:
            head = await session.get(Head, conversation_id)
            if head and head.engine != "langgraph-v1":
                raise RuntimeError("This graph version requires an explicit migration.")
            return self.durable if head or self.default_engine == "langgraph" else self.legacy

    async def start(self, conversation_id, content, request_id=None, question_id=None):
        service = await self.selected(conversation_id)
        return await service.start(conversation_id, content, request_id, question_id)

    async def get(self, conversation_id):
        service = await self.selected(conversation_id)
        return await service.get(conversation_id)

    async def list(self):
        return await self.legacy.list()

    async def replay(self, conversation_id, after):
        async for item in self.durable.replay(conversation_id, after):
            yield item

    async def close(self):
        await self.legacy.close()
        await self.durable.close()

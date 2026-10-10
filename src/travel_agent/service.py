"""Own background runs independently of a browser's SSE connection."""

import asyncio
from collections.abc import AsyncIterator

from travel_agent.runner import AgentRunner, RunLimits, RunOutcome, repair_history
from travel_agent.storage import ConversationStore


class ModelNotConfigured(Exception):
    pass


class AgentService:
    def __init__(self, store: ConversationStore, model, registry, limits: RunLimits):
        self.store, self.model, self.registry, self.limits = store, model, registry, limits
        self.tasks: set[asyncio.Task] = set()
        self.failed_saves: dict[str, tuple[str, RunOutcome]] = {}
        self.save_recovery_lock = asyncio.Lock()

    async def _flush_failed(self):
        async with self.save_recovery_lock:
            for conversation_id, (run_id, outcome) in list(self.failed_saves.items()):
                await self.store.finish(conversation_id, run_id, outcome)
                self.failed_saves.pop(conversation_id, None)

    async def get(self, conversation_id: str):
        await self._flush_failed()
        return await self.store.get(conversation_id)

    async def list(self):
        await self._flush_failed()
        return await self.store.list()

    async def start(
        self, conversation_id: str, content: str, request_id=None, question_id=None
    ) -> asyncio.Queue:
        if self.model is None:
            raise ModelNotConfigured
        await self._flush_failed()
        lease = await self.store.acquire(conversation_id, content, repair_history)
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._run(conversation_id, lease, queue))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return queue

    async def _run(self, conversation_id: str, lease: dict, queue: asyncio.Queue):
        run_id = lease["run_id"]
        last_history = lease["history"]
        sent_message = False

        async def checkpoint(history):
            nonlocal last_history
            last_history = history
            await self.store.save_history(conversation_id, run_id, history)

        async def emit(event):
            nonlocal sent_message
            if event.get("type") == "message":
                await self.store.append_message(
                    conversation_id, run_id, event["content"], event.get("kind", "answer")
                )
                sent_message = True
            await queue.put(event)

        await queue.put({"type": "status", "status": "running"})
        try:
            outcome = await AgentRunner(self.model, self.registry, self.limits).run(
                lease["history"], emit, timezone_name=lease["timezone"], checkpoint=checkpoint
            )
        except (Exception, asyncio.CancelledError):
            message = "本轮执行未能完成。历史已保留，可以稍后继续。"
            outcome = RunOutcome(
                status="error",
                history=repair_history(last_history),
                content=message,
                error={"code": "run_failed", "message": message, "retryable": True},
            )
        try:
            if not sent_message and outcome.content:
                await emit(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": outcome.content,
                        "kind": "error" if outcome.status == "error" else "answer",
                    }
                )
            await self.store.finish(conversation_id, run_id, outcome)
            await queue.put({"type": "status", "status": outcome.status})
        except Exception:
            # Raw database/driver errors can contain credentials. Never stream them.
            outcome = RunOutcome(
                status="error",
                history=outcome.history,
                content="保存会话失败，历史暂存于当前服务，请稍后刷新。",
                error={
                    "code": "storage_error",
                    "message": "保存会话失败，请稍后刷新。",
                    "retryable": True,
                },
            )
            # Reconcile on the next read/send after the database recovers. A failed
            # final save must not leave an ownerless running lease permanently busy.
            self.failed_saves[conversation_id] = (run_id, outcome)
            await queue.put(
                {"type": "error", "code": "storage_error", "message": "保存会话失败，请稍后刷新。"}
            )
        finally:
            await queue.put({"type": "done", "status": outcome.status})

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def stream_events(queue: asyncio.Queue) -> AsyncIterator[str]:
    import json

    # Closing this iterator does not cancel the separate background run.
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=15)
        except TimeoutError:
            yield ": keepalive\n\n"
            continue
        prefix = f"id: {event['event_seq']}\n" if "event_seq" in event else ""
        yield prefix + "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        if event.get("type") == "done":
            break

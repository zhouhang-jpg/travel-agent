"""Fence native SDK savers using immutable generations and a guarded reference.

A stale executor may leave an unreachable blob, but cannot publish a checkpoint
or pending write into the canonical graph. We retain native serialization,
interrupts and pending writes. This is intentionally a single-process runtime.
"""

import asyncio
from collections import defaultdict
from copy import deepcopy
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple


class FencedSaver(BaseCheckpointSaver):
    def __init__(self, native, facts, lease):
        super().__init__(serde=native.serde)
        self.native, self.facts, self.lease = native, facts, lease
        self.lock = asyncio.Lock()

    def get_next_version(self, current, channel):
        return self.native.get_next_version(current, channel)

    async def _physical(self, config):
        return await self.facts.cursor(
            self.lease["conversation_id"], config["configurable"].get("checkpoint_id")
        )

    @staticmethod
    def _logical(physical, config):
        value = deepcopy(physical)
        value["configurable"]["thread_id"] = config["configurable"]["thread_id"]
        return value

    async def aget_tuple(self, config):
        physical = await self._physical(config)
        if not physical:
            return None
        value = await self.native.aget_tuple(physical)
        if value is None:
            raise RuntimeError("Canonical checkpoint is missing; recovery requires repair.")
        return CheckpointTuple(
            self._logical(value.config, config),
            value.checkpoint,
            value.metadata,
            self._logical(value.parent_config, config) if value.parent_config else None,
            value.pending_writes,
        )

    async def aput(self, config, checkpoint, metadata, new_versions):
        async with self.lock:
            physical = deepcopy(config)
            physical["configurable"]["thread_id"] = str(uuid4())
            # Every generation contains all channels, including unchanged values.
            saved = await self.native.aput(
                physical, checkpoint, metadata, checkpoint["channel_versions"]
            )
            await self.facts.publish_cursor(self.lease, saved)
            return self._logical(saved, config)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        async with self.lock:
            physical = await self._physical(config)
            if not physical:
                raise RuntimeError("Pending write has no committed checkpoint.")
            value = await self.native.aget_tuple(physical)
            copied = deepcopy(physical)
            copied["configurable"]["thread_id"] = str(uuid4())
            if value.parent_config:
                copied["configurable"]["checkpoint_id"] = value.parent_config["configurable"][
                    "checkpoint_id"
                ]
            else:
                copied["configurable"].pop("checkpoint_id", None)
            copied = await self.native.aput(
                copied, value.checkpoint, value.metadata, value.checkpoint["channel_versions"]
            )
            pending = defaultdict(list)
            for pending_task, channel, payload in value.pending_writes or []:
                pending[pending_task].append((channel, payload))
            for pending_task, items in pending.items():
                await self.native.aput_writes(copied, items, pending_task)
            await self.native.aput_writes(copied, writes, task_id, task_path)
            await self.facts.publish_cursor(self.lease, copied, advance=False)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        # The UI does not expose checkpoint history. Explicit parent lookup remains
        # supported through aget_tuple; no raw snapshots cross the public API.
        value = await self.aget_tuple(config)
        if value and limit != 0:
            yield value

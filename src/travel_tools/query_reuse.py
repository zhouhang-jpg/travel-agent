"""Reuse immutable provider snapshots under the calling tool's configured TTL."""

import asyncio
import time
from collections import OrderedDict
from contextvars import ContextVar
from copy import deepcopy

from travel_tools.scheduling import force_refresh

snapshot_policy = ContextVar("snapshot_policy", default=("direct", 0.0))


class SnapshotCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.locks = {}

    async def query(self, key, loader):
        scope, ttl = snapshot_policy.get()
        if ttl <= 0:
            return await loader()
        key = (scope, key)
        lock, users = self.locks.get(key, (asyncio.Lock(), 0))
        self.locks[key] = (lock, users + 1)
        try:
            async with lock:
                cached = self.entries.get(key)
                if not force_refresh.get() and cached and time.monotonic() < cached[0]:
                    self.entries.move_to_end(key)
                    result = deepcopy(cached[1])
                    result["cache_hit"] = True
                    result["elapsed_seconds"] = 0
                    return result
                self.entries.pop(key, None)
                result = await loader()
                self.entries[key] = (time.monotonic() + ttl, deepcopy(result))
                self.entries.move_to_end(key)
                while len(self.entries) > 256:
                    self.entries.popitem(last=False)
                return result
        finally:
            _, users = self.locks[key]
            if users == 1:
                self.locks.pop(key)
            else:
                self.locks[key] = (lock, users - 1)

"""Per-registry supplier gates at the actual I/O boundary, including fanout."""

import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class SupplierPolicy:
    concurrency: int = 1
    requests_per_second: float = 1


class SupplierScheduler:
    def __init__(self, policies=None):
        self.policies = policies or {}
        self.gates = {}
        self.clocks = {}
        self.next_start = {}

    @asynccontextmanager
    async def slot(self, supplier):
        policy = self.policies.get(supplier, SupplierPolicy())
        gate = self.gates.setdefault(supplier, asyncio.Semaphore(policy.concurrency))
        clock = self.clocks.setdefault(supplier, asyncio.Lock())
        report = supplier_progress.get()
        request_id = uuid4().hex
        if report:
            await report("queued", supplier, request_id)
        async with gate:
            # Starts are spaced; this is a frequency limit, not a concurrency count.
            async with clock:
                await asyncio.sleep(max(0, self.next_start.get(supplier, 0) - time.monotonic()))
                self.next_start[supplier] = time.monotonic() + (
                    1 / policy.requests_per_second if policy.requests_per_second else 0
                )
            if report:
                await report("running", supplier, request_id)
            try:
                yield
            except BaseException:
                if report:
                    await report("error", supplier, request_id)
                raise
            else:
                if report:
                    await report("ok", supplier, request_id)


current_scheduler = ContextVar("travel_supplier_scheduler", default=None)
force_refresh = ContextVar("travel_force_refresh", default=False)
supplier_progress = ContextVar("travel_supplier_progress", default=None)


@asynccontextmanager
async def supplier_slot(supplier):
    scheduler = current_scheduler.get()
    if scheduler is None:
        yield
    else:
        async with scheduler.slot(supplier):
            yield

"""SDK saver schema setup and Windows psycopg event-loop compatibility.

Windows Playwright needs the application's Proactor loop; psycopg async needs
Selector. A dedicated I/O loop keeps both valid without changing global policy.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from threading import Event, Thread

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class ThreadedPostgresSaver:
    def __init__(self, connection_string):
        self.connection_string = connection_string
        self.ready = Event()

    def _worker(self):
        self.loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    async def call(self, method, *args, **kwargs):
        future = asyncio.run_coroutine_threadsafe(method(*args, **kwargs), self.loop)
        return await asyncio.wrap_future(future)

    async def _open(self):
        self.manager = AsyncPostgresSaver.from_conn_string(
            self.connection_string, serde=JsonPlusSerializer(pickle_fallback=False)
        )
        self.native = await self.manager.__aenter__()
        await self.native.setup()

    async def __aenter__(self):
        self.thread = Thread(target=self._worker, name="graph-postgres-io", daemon=True)
        self.thread.start()
        await asyncio.to_thread(self.ready.wait)
        try:
            await self.call(self._open)
        except BaseException:
            self.loop.call_soon_threadsafe(self.loop.stop)
            await asyncio.to_thread(self.thread.join)
            raise
        self.serde = self.native.serde
        return self

    async def __aexit__(self, *exc):
        try:
            await self.call(self.manager.__aexit__, *exc)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            await asyncio.to_thread(self.thread.join)

    def get_next_version(self, current, channel):
        return self.native.get_next_version(current, channel)

    async def aget_tuple(self, *args, **kwargs):
        return await self.call(self.native.aget_tuple, *args, **kwargs)

    async def aput(self, *args, **kwargs):
        return await self.call(self.native.aput, *args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        return await self.call(self.native.aput_writes, *args, **kwargs)


@asynccontextmanager
async def open_saver(url):
    if url.drivername.startswith("postgresql"):
        connection_string = url.set(drivername="postgresql").render_as_string(hide_password=False)
        if sys.platform == "win32":
            async with ThreadedPostgresSaver(connection_string) as saver:
                yield saver
        else:
            async with AsyncPostgresSaver.from_conn_string(
                connection_string, serde=JsonPlusSerializer(pickle_fallback=False)
            ) as saver:
                await saver.setup()
                yield saver
    else:
        path = ":memory:" if url.database == ":memory:" else url.database + ".graph"
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            saver.serde = JsonPlusSerializer(pickle_fallback=False)
            await saver.setup()
            yield saver

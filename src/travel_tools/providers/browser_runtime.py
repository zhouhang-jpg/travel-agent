"""Independent, stateless public-page queries; no desktop or personal browser session."""

import asyncio
import sys
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from travel_tools.common import ToolFailure, utc_now


class BrowserQueryRuntime:
    def __init__(self, *, timeout: float = 18, cache_seconds: float = 0):
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="travel-browser")
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._closed = False

    async def query(self, key: str, callback: Callable[[Any], Awaitable[dict]]) -> dict:
        if self._closed:
            raise ToolFailure("provider_unavailable", "Browser query runtime is closed.")
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            result = deepcopy(cached[1])
            result["cache_hit"] = True
            return result
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self._executor, self._run, callback)
        if self.cache_seconds > 0 and result.get("rows"):
            self._cache[key] = (time.monotonic(), deepcopy(result))
            self._cache.move_to_end(key)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
        return result

    def _run(self, callback: Callable[[Any], Awaitable[dict]]) -> dict:
        # Uvicorn uses a Selector loop on Windows. Browser subprocesses need a
        # Proactor loop, isolated in this worker, without changing the server loop.
        with asyncio.Runner(loop_factory=_loop) as runner:
            return runner.run(self._query_page(callback))

    async def _query_page(self, callback: Callable[[Any], Awaitable[dict]]) -> dict:
        started = time.monotonic()
        browser = None
        try:
            async with async_playwright() as playwright:
                try:
                    async with asyncio.timeout(self.timeout):
                        browser = await playwright.chromium.launch(headless=True)
                        context = await browser.new_context(
                            locale="zh-CN",
                            timezone_id="Asia/Shanghai",
                        )
                        page = await context.new_page()
                        page.set_default_timeout(min(self.timeout * 1000, 10000))
                        result = await callback(page)
                        result.update(
                            retrieved_at=utc_now().isoformat(),
                            cache_hit=False,
                            elapsed_seconds=round(time.monotonic() - started, 3),
                        )
                        return result
                finally:
                    if browser is not None:
                        async with asyncio.timeout(3):
                            await browser.close()
        except ToolFailure:
            raise
        except (TimeoutError, BrowserTimeout):
            raise ToolFailure("provider_timeout", "Public page query timed out.", True) from None
        except BrowserError as error:
            if "Executable doesn't exist" in str(error):
                raise ToolFailure(
                    "provider_unavailable",
                    "Install the independent browser with uv run playwright install chromium.",
                ) from None
            raise ToolFailure(
                "provider_browser_error",
                "Public page could not be queried; browser/network unavailable.",
                True,
            ) from None

    async def close(self) -> None:
        self._closed = True
        self._cache.clear()
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)


def _loop():
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


async def navigate(page, url: str) -> None:
    response = await page.goto(url, wait_until="domcontentloaded")
    if response is not None and response.status in (401, 403, 429, 432):
        raise ToolFailure(
            "provider_access_blocked", "Provider denied browser access; no bypass attempted."
        )
    if response is not None and response.status >= 400:
        raise ToolFailure("provider_http_error", "Provider page returned an HTTP error.", True)
    await check_access(page)


async def check_access(page) -> None:
    text = await page.locator("body").inner_text()
    if any(
        marker in text
        for marker in (
            "请完成验证",
            "拖动滑块",
            "安全验证",
            "访问过于频繁",
            "Access Denied",
            "验证码验证",
            "异常访问",
            "请登录后查询",
        )
    ):
        raise ToolFailure(
            "provider_access_blocked", "Provider requires access verification; query stopped."
        )

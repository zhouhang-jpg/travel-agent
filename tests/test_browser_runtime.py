import asyncio
from types import SimpleNamespace

import pytest

from travel_tools.common import ToolFailure
from travel_tools.providers import browser_runtime
from travel_tools.providers.browser_runtime import BrowserQueryRuntime, check_access, navigate


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def new_context(self, **kwargs):
        return self

    async def new_page(self):
        return SimpleNamespace(set_default_timeout=lambda timeout: None)

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self.browser = browser
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.exited = True

    async def launch(self, **kwargs):
        return self.browser


@pytest.mark.parametrize("time_out", [False, True])
async def test_browser_and_driver_close_on_blocked_access_or_timeout(monkeypatch, time_out):
    browser = FakeBrowser()
    factory = FakePlaywright(browser)
    monkeypatch.setattr(browser_runtime, "async_playwright", lambda: factory)
    runtime = BrowserQueryRuntime(timeout=0.01)

    async def query(page):
        if time_out:
            await asyncio.sleep(0.05)
        raise ToolFailure("provider_access_blocked", "Stop without bypass.")

    try:
        with pytest.raises(ToolFailure) as error:
            await runtime._query_page(query)
        assert error.value.code == ("provider_timeout" if time_out else "provider_access_blocked")
        assert browser.closed and factory.exited
    finally:
        await runtime.close()


async def test_cache_retains_data_time_and_never_stores_access_errors(monkeypatch):
    runtime = BrowserQueryRuntime(cache_seconds=60)
    calls = []

    def execute(callback):
        calls.append(callback)
        if len(calls) == 1:
            raise ToolFailure("provider_access_blocked", "blocked")
        return {
            "rows": [{"inventory": "候补"}],
            "retrieved_at": "2026-10-10T01:00:00+08:00",
            "cache_hit": False,
        }

    monkeypatch.setattr(runtime, "_run", execute)
    try:
        with pytest.raises(ToolFailure):
            await runtime.query("route", None)
        fresh = await runtime.query("route", None)
        cached = await runtime.query("route", None)
        assert len(calls) == 2 and cached["cache_hit"]
        assert cached["retrieved_at"] == fresh["retrieved_at"]
        cached["rows"].clear()
        assert (await runtime.query("route", None))["rows"]
    finally:
        await runtime.close()
    with pytest.raises(ToolFailure, match="closed"):
        await runtime.query("route", None)


async def test_http_guard_stops_without_attempting_to_bypass_or_read_inventory():
    class BlockedPage:
        async def goto(self, url, **kwargs):
            return SimpleNamespace(status=403)

        def locator(self, selector):
            pytest.fail("Blocked page must not be treated as readable inventory.")

    with pytest.raises(ToolFailure) as error:
        await navigate(BlockedPage(), "https://example.com")
    assert error.value.code == "provider_access_blocked"


async def test_actual_verification_notice_is_blocked_but_normal_login_link_is_not():
    class Body:
        async def inner_text(self):
            return self.text

    body = Body()
    page = SimpleNamespace(locator=lambda selector: body)
    body.text = "车票查询 登录 注册"
    await check_access(page)
    body.text = "请完成验证，拖动滑块"
    with pytest.raises(ToolFailure):
        await check_access(page)

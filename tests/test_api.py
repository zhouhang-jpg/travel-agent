import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from travel_tools.api import create_app
from travel_tools.bootstrap import build_registry
from travel_tools.config import Settings
from travel_tools.registry import ToolRegistry


def empty_settings(**kwargs):
    return Settings(
        _env_file=None,
        amap_api_key=None,
        qweather_api_key=None,
        qweather_api_host=None,
        bocha_api_key=None,
        **kwargs,
    )


@asynccontextmanager
async def api_client(*, settings=None, registry=None):
    app = create_app(settings or empty_settings(), registry=registry)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield client


async def test_unconfigured_catalog_model_export_and_unavailable_call():
    async with api_client() as client:
        catalog = (await client.get("/tools")).json()
        assert len(catalog) == 11
        statuses = {row["name"]: row["availability"] for row in catalog}
        assert statuses["search_flights"] == "not_implemented"
        assert statuses["get_weather"] == "not_configured"
        assert {
            item["function"]["name"]
            for item in (await client.get("/tools/model-definitions")).json()
        } == {"fetch_webpage", "validate_itinerary"}
        result = (await client.post("/tools/search_hotels/call", json={"arguments": {}})).json()
        assert result["status"] == "error"
        assert result["error"]["code"] == "tool_unavailable" and result["data"] is None


async def test_http_envelope_limits_and_safe_validation():
    async with api_client() as client:
        assert (await client.get("/health")).json()["status"] == "ok"
        bad = await client.post(
            "/tools/validate_itinerary/call", json={"arguments": [], "secret": "do-not-echo"}
        )
        assert bad.status_code == 422 and "do-not-echo" not in bad.text
        too_large = await client.post(
            "/tools/validate_itinerary/call", content=b"x" * (256 * 1024 + 1)
        )
        assert too_large.status_code == 413
        unknown = await client.post("/tools/no-such-tool/call", json={"arguments": {}})
        assert unknown.json()["error"]["code"] == "unknown_tool"


@pytest.mark.parametrize("send_prefix", [False, True])
async def test_stalled_upload_times_out_before_parse_or_dispatch(send_prefix, monkeypatch):
    registry = ToolRegistry(timeout_seconds=0.01)
    upload_cancelled = asyncio.Event()

    async def must_not_dispatch(*args, **kwargs):
        pytest.fail("A stalled envelope must not reach tool dispatch")

    monkeypatch.setattr(registry, "dispatch", must_not_dispatch)

    async def body():
        try:
            if send_prefix:
                yield b'{"arguments":{"secret":"do-not-echo'
            await asyncio.Event().wait()
            yield b'"}}'
        finally:
            upload_cancelled.set()

    async with api_client(registry=registry) as client:
        response = await asyncio.wait_for(
            client.post("/tools/validate_itinerary/call", content=body()), timeout=1
        )
    assert response.status_code == 408
    assert response.json() == {"detail": "Request body receipt exceeded its deadline."}
    assert "do-not-echo" not in response.text
    assert upload_cancelled.is_set()


async def test_settings_deadline_applies_to_envelope_receipt():
    async def body():
        yield b'{"arguments":'
        await asyncio.Event().wait()
        yield b"{}}"

    async with api_client(settings=empty_settings(tool_timeout_seconds=1)) as client:
        response = await asyncio.wait_for(
            client.post("/tools/validate_itinerary/call", content=body()), timeout=2
        )
    assert response.status_code == 408


async def test_sparse_itinerary_is_unknown_through_http():
    async with api_client() as client:
        response = await client.post(
            "/tools/validate_itinerary/call",
            json={
                "arguments": {
                    "planning_window": {
                        "start": "2026-10-10T09:00:00+08:00",
                        "end": "2026-10-10T18:00:00+08:00",
                    },
                    "items": [
                        {
                            "id": "visit",
                            "title": "Visit",
                            "start": "2026-10-10T10:00:00+08:00",
                            "end": "2026-10-10T11:00:00+08:00",
                        }
                    ],
                }
            },
        )
        result = response.json()
        assert result["status"] == "ok" and result["data"]["status"] == "unknown"


async def test_openapi_documents_call_envelope_for_interactive_docs():
    async with api_client() as client:
        schema = (await client.get("/openapi.json")).json()
        body = schema["paths"]["/tools/{tool_name}/call"]["post"]["requestBody"]
        assert body["required"]
        assert "arguments" in body["content"]["application/json"]["schema"]["required"]


@pytest.mark.asyncio
async def test_configured_providers_exported_without_network_and_secrets_hidden():
    def no_network(request):
        pytest.fail("Catalog building must not make network calls")

    settings = Settings(
        _env_file=None,
        amap_api_key="amap-secret",
        qweather_api_key="weather-secret",
        qweather_api_host="example.qweatherapi.com",
        bocha_api_key="bocha-secret",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as client:
        registry = build_registry(settings, client)
        assert len(registry.model_definitions()) == 7
        assert "secret" not in str(registry.catalog())
        assert "amap-secret" not in repr(settings)


@pytest.mark.asyncio
async def test_invalid_account_host_does_not_expose_weather_or_leak_config():
    settings = Settings(
        _env_file=None,
        amap_api_key=None,
        bocha_api_key=None,
        qweather_api_key="secret",
        qweather_api_host="http://127.0.0.1/internal",
    )
    async with httpx.AsyncClient() as client:
        registry = build_registry(settings, client)
        row = next(row for row in registry.catalog() if row["name"] == "get_weather")
        assert row["availability"] == "not_configured"
        assert "127.0.0.1" not in row["reason"]

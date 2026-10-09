"""Offline registration, capability gating and provider selection tests."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from travel_tools.providers import flyai, juhe_train
from travel_tools.providers.jisu_coach import JISU_COACH_URL, JisuCoachAdapter
from travel_tools.providers.juhe_train import JUHE_TRAIN_URL, JuheTrainAdapter
from travel_tools.quote_registry import register_quote_tools
from travel_tools.registry import ToolRegistry

TRANSPORT = {
    "origin": {"query": "上海"},
    "destination": {"query": "杭州"},
    "departure_date": "2026-10-16",
    "travelers": {"adults": 1},
}
HOTEL = {
    "destination": {"query": "杭州"},
    "check_in": "2026-10-16",
    "check_out": "2026-10-18",
    "travelers": {"adults": 1},
    "rooms": 1,
}


def settings(**overrides):
    return SimpleNamespace(
        **(
            {
                "flyai_api_key": None,
                "flyai_enable_demo": False,
                "flyai_node_path": None,
                "flyai_cli_path": None,
                "flyai_state_directory": "private/flyai",
                "juhe_train_api_key": None,
                "train_search_provider": "flyai",
                "jisu_coach_api_key": None,
            }
            | overrides
        )
    )


@pytest.fixture
def local_paths(tmp_path):
    node, cli = tmp_path / "node.exe", tmp_path / "flyai.cjs"
    node.write_text("offline placeholder", encoding="utf-8")
    cli.write_text("offline placeholder", encoding="utf-8")
    return {
        "flyai_node_path": str(node),
        "flyai_cli_path": str(cli),
        "flyai_state_directory": str(tmp_path / "state"),
    }


def no_http(request):
    pytest.fail("Unexpected HTTP request")


def catalog(registry):
    return {entry["name"]: entry for entry in registry.catalog()}


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr("   ")])
async def test_disabled_tools_remain_visible_but_are_not_exported(key, monkeypatch):
    monkeypatch.setenv("FLYAI_API_KEY", "ambient-key-must-not-enable-tools")
    monkeypatch.setenv("FLYAI_ENABLE_DEMO", "true")
    registry = ToolRegistry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
        register_quote_tools(
            registry,
            settings(
                flyai_api_key=key,
                juhe_train_api_key=key,
                jisu_coach_api_key=key,
            ),
            client,
        )
        assert len(registry.catalog()) == 4
        assert registry.model_definitions() == []
        for name, entry in catalog(registry).items():
            assert entry["availability"] == "not_configured"
            assert entry["reason"] and entry["requires_external_service"]
            result = await registry.dispatch(name, {})
            assert result.error.code == "tool_unavailable"


@pytest.mark.parametrize(
    "key,demo",
    [
        (None, True),
        (SecretStr("offline-flyai-key"), False),
        (SecretStr("offline-flyai-key"), True),
        (SecretStr("   "), True),
    ],
)
async def test_flyai_dispatch_uses_explicit_mode_and_registry_deadline(
    local_paths,
    monkeypatch,
    key,
    demo,
):
    calls = []

    async def execute(argv, env, duration):
        calls.append((argv, env, duration))
        return flyai.CLIResult(0, '{"status":0,"data":{"itemList":[]}}')

    monkeypatch.setattr(flyai, "_execute_cli", execute)
    monkeypatch.setenv("FLYAI_API_KEY", "ambient-key-must-not-be-used")
    registry = ToolRegistry(timeout_seconds=17.5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
        register_quote_tools(
            registry,
            settings(
                **local_paths,
                flyai_api_key=key,
                flyai_enable_demo=demo,
            ),
            client,
        )
        assert calls == []  # Registration itself never invokes the CLI.
        assert {d["function"]["name"] for d in registry.model_definitions()} == {
            "search_flights",
            "search_trains",
            "search_hotels",
        }
        results = [
            await registry.dispatch("search_flights", TRANSPORT),
            await registry.dispatch("search_trains", TRANSPORT),
            await registry.dispatch("search_hotels", HOTEL),
        ]
    assert all(result.status == "ok" for result in results)
    assert all(result.data["complete"] is False for result in results)
    assert [call[0][4] for call in calls] == ["search-flight", "search-train", "search-hotel"]
    expected_key = key.get_secret_value().strip() if key else ""
    assert all(call[2] == 17.5 for call in calls)
    assert all(call[1].get("FLYAI_API_KEY") == (expected_key or None) for call in calls)
    exported = json.dumps(registry.catalog()) + json.dumps(registry.model_definitions())
    exported += "".join(result.model_dump_json() for result in results)
    assert "offline-flyai-key" not in exported
    assert "ambient-key-must-not-be-used" not in exported
    descriptions = catalog(registry)
    assert "one adult" in descriptions["search_flights"]["description"]
    assert "no explicit quote currency" in descriptions["search_flights"]["description"]
    assert "multiple rooms" in descriptions["search_hotels"]["description"]


@pytest.mark.parametrize("missing", ["node", "cli", "guard", "switch", "unset_node"])
async def test_flyai_is_not_ready_without_explicit_mode_and_all_local_files(
    local_paths,
    monkeypatch,
    tmp_path,
    missing,
):
    config = settings(**local_paths, flyai_enable_demo=True)
    if missing in {"node", "cli"}:
        setattr(config, f"flyai_{missing}_path", str(tmp_path / "missing-file"))
    elif missing == "guard":
        monkeypatch.setattr(flyai, "__file__", str(tmp_path / "flyai.py"))
    elif missing == "switch":
        config.flyai_enable_demo = False
    else:
        config.flyai_node_path = None
    registry = ToolRegistry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
        register_quote_tools(registry, config, client)
    assert registry.model_definitions() == []
    assert all(entry["availability"] == "not_configured" for entry in registry.catalog())
    assert all(entry["reason"] for entry in registry.catalog())


@pytest.mark.parametrize("error_code", [0, 10001])
async def test_explicit_juhe_selection_and_failure_does_not_fall_back(
    local_paths,
    monkeypatch,
    error_code,
):
    monkeypatch.setattr(juhe_train, "utc_now", lambda: datetime(2026, 10, 9, tzinfo=UTC))
    calls = []

    async def reject_cli(*args):
        pytest.fail("Configured Juhe must not fall back to FlyAI")

    def respond(request):
        calls.append(request)
        assert request.url.copy_with(query=None) == httpx.URL(JUHE_TRAIN_URL)
        assert request.url.params["key"] == "offline-juhe-secret"
        return httpx.Response(
            200,
            json={
                "error_code": error_code,
                "reason": "offline-juhe-secret",
                "result": [],
            },
        )

    monkeypatch.setattr(flyai, "_execute_cli", reject_cli)
    registry = ToolRegistry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        register_quote_tools(
            registry,
            settings(
                **local_paths,
                flyai_enable_demo=True,
                juhe_train_api_key=SecretStr("offline-juhe-secret"),
                train_search_provider="juhe",
            ),
            client,
        )
        assert calls == []
        result = await registry.dispatch("search_trains", TRANSPORT)
    assert len(calls) == 1
    assert catalog(registry)["search_trains"]["description"] == JuheTrainAdapter.description
    if error_code:
        assert result.error.code == "provider_authentication"
    else:
        assert result.status == "ok" and result.data["offers"] == []
    assert "offline-juhe-secret" not in result.model_dump_json() + json.dumps(registry.catalog())


@pytest.mark.parametrize("flyai_enabled", [False, True])
async def test_default_flyai_selection_never_calls_juhe(local_paths, monkeypatch, flyai_enabled):
    calls = []

    async def execute(argv, env, duration):
        calls.append(argv)
        return flyai.CLIResult(0, '{"status":0,"data":{"itemList":[]}}')

    monkeypatch.setattr(flyai, "_execute_cli", execute)
    registry = ToolRegistry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
        register_quote_tools(
            registry,
            settings(
                **local_paths,
                flyai_enable_demo=flyai_enabled,
                juhe_train_api_key=SecretStr("configured-but-unselected"),
            ),
            client,
        )
        result = await registry.dispatch("search_trains", TRANSPORT)
    if flyai_enabled:
        assert result.status == "ok" and calls[0][4] == "search-train"
    else:
        assert result.error.code == "tool_unavailable" and calls == []


async def test_selected_juhe_without_key_does_not_use_available_flyai(local_paths):
    registry = ToolRegistry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
        register_quote_tools(
            registry,
            settings(**local_paths, flyai_enable_demo=True, train_search_provider="juhe"),
            client,
        )
        result = await registry.dispatch("search_trains", TRANSPORT)
    assert result.error.code == "tool_unavailable"
    assert "JUHE_TRAIN_API_KEY" in catalog(registry)["search_trains"]["reason"]


async def test_jisu_dispatch_keeps_coaches_reference_only():
    def respond(request):
        assert request.url.copy_with(query=None) == httpx.URL(JISU_COACH_URL)
        assert request.url.params["appkey"] == "offline-jisu-secret"
        assert "date" not in request.url.params
        return httpx.Response(
            200,
            json={
                "status": 0,
                "result": [
                    {
                        "startstation": "上海南站",
                        "endstation": "杭州汽车站",
                        "price": "68",
                        "starttime": "07:10",
                    }
                ],
            },
        )

    registry = ToolRegistry()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        register_quote_tools(
            registry,
            settings(
                jisu_coach_api_key=SecretStr("offline-jisu-secret"),
            ),
            client,
        )
        result = await registry.dispatch("search_coaches", TRANSPORT)
    assert result.status == "ok" and result.data["complete"] is False
    offer = result.data["offers"][0]
    assert offer["price"]["kind"] == "reference"
    assert offer["departure_at"] is None and offer["inventory"]["status"] == "unknown"
    definition = registry.model_definitions()[0]["function"]
    assert definition["name"] == "search_coaches"
    assert definition["description"] == JisuCoachAdapter.description
    assert "NOT verified" in definition["description"]
    assert "offline-jisu-secret" not in result.model_dump_json() + json.dumps(registry.catalog())

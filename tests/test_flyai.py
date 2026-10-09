"""Sanitized real-response contract tests; these do not call the live supplier."""

import asyncio
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from travel_tools.common import ToolFailure
from travel_tools.providers.flyai import (
    CLIResult,
    FlyAIClient,
    FlyAIFlightAdapter,
    FlyAIHotelAdapter,
    FlyAITrainAdapter,
    _execute_cli,
)
from travel_tools.providers.quotes import (
    FlightSearchAdapter,
    HotelSearchAdapter,
    TrainSearchAdapter,
)
from travel_tools.schemas.quotes import SearchFlightsInput, SearchHotelsInput, SearchTrainsInput

FIXTURES = Path(__file__).parent / "fixtures" / "flyai"


def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeExecutor:
    def __init__(self, payload, returncode=0, stderr=""):
        self.result = CLIResult(returncode, json.dumps(payload, ensure_ascii=False), stderr)
        self.calls = []

    async def __call__(self, args, env, duration):
        self.calls.append((args, env, duration))
        return self.result


def make_client(tmp_path, payload, **kwargs):
    executor = FakeExecutor(payload)
    client = FlyAIClient(
        tmp_path / "node.exe",
        tmp_path / "flyai.cjs",
        tmp_path / "state",
        executor=executor,
        **kwargs,
    )
    return client, executor


def flight_request(**overrides):
    data = {
        "origin": {"query": "上海"},
        "destination": {"query": "北京"},
        "departure_date": "2026-10-16",
        "travelers": {"adults": 1},
    }
    return SearchFlightsInput.model_validate(data | overrides)


def train_request(**overrides):
    data = {
        "origin": {"query": "上海"},
        "destination": {"query": "杭州"},
        "departure_date": "2026-10-16",
        "travelers": {"adults": 1},
        "station_scope": "city",
    }
    return SearchTrainsInput.model_validate(data | overrides)


def hotel_request(**overrides):
    data = {
        "destination": {"query": "杭州"},
        "check_in": "2026-10-16",
        "check_out": "2026-10-18",
        "travelers": {"adults": 1},
        "rooms": 1,
    }
    return SearchHotelsInput.model_validate(data | overrides)


async def test_train_direct_filter_does_not_confuse_enroute_stops_with_transfers(tmp_path):
    payload = load_fixture("train")
    payload["data"]["itemList"][0]["journeys"][0]["segments"][0]["stopInfos"] = ["经停站"]
    client, _ = make_client(tmp_path, payload)
    result = await FlyAITrainAdapter(client).search(train_request(direct_only=True))
    assert len(result.offers) == 1 and result.offers[0].direct is True
    assert result.offers[0].service_number == "D181"
    assert result.offers[0].price.display.masked
    assert result.offers[0].price.money is None


async def test_train_exact_endpoint_filter_is_not_city_replacement(tmp_path):
    client, _ = make_client(tmp_path, load_fixture("train"))
    result = await FlyAITrainAdapter(client).search(
        train_request(
            origin={"query": "上海虹桥"},
            destination={"query": "杭州东"},
            station_scope="exact",
        )
    )
    assert result.offers == [] and result.coverage.query_status == "filtered_empty"


async def test_real_flight_shape_preserves_unknown_currency_and_inventory(tmp_path):
    client, executor = make_client(tmp_path, load_fixture("flight"))
    adapter = FlyAIFlightAdapter(client)
    assert isinstance(adapter, FlightSearchAdapter)
    result = await adapter.search(flight_request())
    assert not result.complete
    assert len(result.offers) == 2
    offer = result.offers[0]
    assert offer.service_number == "CA8322"
    assert offer.origin.query == "浦东国际机场"
    assert offer.destination.query == "大兴国际机场"
    assert offer.price.money is None
    assert offer.price.kind == "unknown"
    assert json.loads(offer.price.conditions)["raw_price"] == "600.00"
    assert offer.price.priced_persons is None
    assert offer.price.tax_basis == offer.price.fee_basis == "unknown"
    assert offer.inventory.status == "unknown"
    assert offer.departure_at is None  # Raw supplier timestamp has no UTC offset.
    assert offer.departure_text == "2026-10-16 06:55:00"
    assert offer.segments[0].departure_terminal == "T2"
    assert offer.segments[0].origin_code == "PVG"
    assert offer.operator is None and offer.marketing_carrier == "国航"
    assert offer.operating_carrier is None
    assert executor.calls[0][0][-6:] == [
        "--origin",
        "上海",
        "--destination",
        "北京",
        "--dep-date",
        "2026-10-16",
    ]


async def test_real_train_mask_is_not_parsed_into_a_number(tmp_path):
    client, _ = make_client(tmp_path, load_fixture("train"))
    adapter = FlyAITrainAdapter(client)
    assert isinstance(adapter, TrainSearchAdapter)
    result = await adapter.search(train_request())
    assert not result.complete
    offer = result.offers[0]
    assert offer.service_number == "D181"
    assert offer.origin.query == "上海松江站"
    assert offer.train_type == "high_speed"
    assert offer.price.money is None
    assert json.loads(offer.price.conditions)["raw_price"] == "2x"
    assert offer.inventory.remaining is None


async def test_real_hotel_mask_and_unverified_occupancy(tmp_path):
    client, executor = make_client(tmp_path, load_fixture("hotel"))
    adapter = FlyAIHotelAdapter(client)
    assert isinstance(adapter, HotelSearchAdapter)
    result = await adapter.search(hotel_request())
    offer = result.offers[0]
    assert not result.complete
    assert offer.price.money is None
    assert json.loads(offer.price.conditions)["raw_price"] == "¥3x"
    assert offer.location.coordinates is None
    assert offer.room_name is None and offer.cancellation_terms is None
    assert offer.price.priced_rooms is None and offer.price.priced_nights is None
    assert any("occupancy" in warning.lower() for warning in result.warnings)
    assert executor.calls[0][0][-6:] == [
        "--dest-name",
        "杭州",
        "--check-in-date",
        "2026-10-16",
        "--check-out-date",
        "2026-10-18",
    ]


@pytest.mark.parametrize("raw", ["2x", "¥3x", "¥600", "600.00", "NaN", "-1", "Infinity"])
async def test_price_never_guesses_currency_or_masked_amount(tmp_path, raw):
    payload = load_fixture("flight")
    payload["data"]["itemList"][0]["ticketPrice"] = raw
    client, _ = make_client(tmp_path, payload)
    result = await FlyAIFlightAdapter(client).search(flight_request())
    assert result.offers[0].price.money is None


async def test_explicit_currency_can_make_reference_price_not_complete_quote(tmp_path):
    payload = load_fixture("flight")
    payload["data"]["itemList"][0]["currency"] = "CNY"
    client, _ = make_client(tmp_path, payload)
    result = await FlyAIFlightAdapter(client).search(flight_request())
    price = result.offers[0].price
    assert price.money.currency == "CNY" and price.money.amount == 600
    assert price.kind == "reference"
    assert price.unit == "unknown" and price.priced_persons is None
    assert not result.complete


@pytest.mark.parametrize(
    "overrides",
    [
        {"travelers": {"adults": 2}},
        {"travelers": {"adults": 1, "children_ages": [6]}},
        {"preferred_currency": "CNY"},
        {"origin": {"query": "上海", "provider_location_id": "ambiguous-id"}},
        {
            "origin": {
                "query": "上海",
                "coordinates": {"longitude": 120, "latitude": 30, "crs": "gcj02"},
            }
        },
    ],
)
async def test_unsupported_inputs_fail_before_external_execution(tmp_path, overrides):
    client, executor = make_client(tmp_path, load_fixture("flight"))
    with pytest.raises(ToolFailure) as error:
        await FlyAIFlightAdapter(client).search(flight_request(**overrides))
    assert error.value.code == "unsupported_parameters"
    assert executor.calls == []


async def test_multiple_hotel_rooms_fail_before_execution(tmp_path):
    client, executor = make_client(tmp_path, load_fixture("hotel"))
    with pytest.raises(ToolFailure, match="multiple-room"):
        await FlyAIHotelAdapter(client).search(hotel_request(rooms=2))
    assert not executor.calls


async def test_nonstop_and_cabin_filter_verified_on_returned_candidates(tmp_path):
    client, executor = make_client(tmp_path, load_fixture("flight"))
    result = await FlyAIFlightAdapter(client).search(
        flight_request(nonstop_only=True, cabin="economy")
    )
    assert len(result.offers) == 1 and result.offers[0].stops == 0
    assert "--journey-type" in executor.calls[0][0]
    assert "--seat-class-name" in executor.calls[0][0]
    result = await FlyAIFlightAdapter(client).search(flight_request(cabin="business"))
    assert result.offers == []


async def test_train_type_and_seat_filters_are_not_ignored(tmp_path):
    client, _ = make_client(tmp_path, load_fixture("train"))
    result = await FlyAITrainAdapter(client).search(train_request(train_types=["conventional"]))
    assert not result.offers
    result = await FlyAITrainAdapter(client).search(train_request(seat_class="不存在的席别"))
    assert not result.offers
    assert any("limited" in warning for warning in result.warnings)


async def test_limit_applied_and_supplier_cap_explained(tmp_path):
    client, _ = make_client(tmp_path, load_fixture("flight"))
    result = await FlyAIFlightAdapter(client).search(flight_request(max_results=1))
    assert len(result.offers) == 1
    result = await FlyAIFlightAdapter(client).search(flight_request(max_results=50))
    assert any("at most 10" in warning for warning in result.warnings)


async def test_credentials_only_in_env_and_arguments_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG_FLYAI_MCP_URL", "https://untrusted.invalid")
    monkeypatch.setenv("FLYAI_API_KEY", "ambient-key")
    client, executor = make_client(tmp_path, load_fixture("flight"), api_key="test-secret")
    query = "上海; echo do-not-execute"
    await FlyAIFlightAdapter(client).search(flight_request(origin={"query": query}))
    argv, env, timeout = executor.calls[0]
    assert query in argv
    assert "test-secret" not in " ".join(argv)
    assert env["FLYAI_API_KEY"] == "test-secret"
    assert "DEBUG_FLYAI_MCP_URL" not in env
    assert Path(env["FLYAI_STATE_DIR"]).is_absolute()
    assert timeout == 30


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": False, "data": {"itemList": []}},
        {"status": 0},
        {"status": 0, "data": {"itemList": [None]}},
        {"status": 500, "message": "secret"},
    ],
)
async def test_bad_response_does_not_become_empty_success(tmp_path, payload):
    client, _ = make_client(tmp_path, payload)
    with pytest.raises(ToolFailure) as error:
        await FlyAIFlightAdapter(client).search(flight_request())
    assert "secret" not in error.value.message


async def test_nonzero_exit_rejected_even_with_valid_json(tmp_path):
    client, executor = make_client(tmp_path, load_fixture("flight"))
    executor.result = CLIResult(-1073740791, executor.result.stdout, "api_key=secret")
    with pytest.raises(ToolFailure) as error:
        await FlyAIFlightAdapter(client).search(flight_request())
    assert error.value.code == "provider_error"
    assert "secret" not in error.value.message


async def test_invalid_json_rejected(tmp_path):
    client, executor = make_client(tmp_path, {})
    executor.result = CLIResult(0, "not json")
    with pytest.raises(ToolFailure) as error:
        await client.query("search-flight", [])
    assert error.value.code == "upstream_invalid_response"


async def test_timeout_of_injected_executor_is_enforced(tmp_path):
    async def slow(args, env, duration):
        await asyncio.sleep(1)
        return CLIResult(0, "{}")

    client = FlyAIClient("node", "cli", tmp_path, executor=slow, timeout=0.01)
    with pytest.raises(ToolFailure) as error:
        await client.query("search-flight", [])
    assert error.value.code == "upstream_timeout" and error.value.retryable


async def test_no_install_or_execution_when_paths_missing(tmp_path):
    client = FlyAIClient(tmp_path / "missing-node", tmp_path / "missing-cli", tmp_path / "state")
    with pytest.raises(ToolFailure) as error:
        await client.query("search-flight", [])
    assert error.value.code == "provider_not_configured"


async def test_only_allowlisted_read_commands_can_execute(tmp_path):
    client, executor = make_client(tmp_path, {})
    with pytest.raises(ToolFailure) as error:
        await client.query("book", [])
    assert error.value.code == "unsupported_operation" and not executor.calls


async def test_empty_provider_results_are_distinct_from_malformed(tmp_path):
    client, _ = make_client(tmp_path, {"status": 0, "data": {"itemList": []}})
    result = await FlyAIFlightAdapter(client).search(flight_request())
    assert not result.offers and not result.complete
    client, _ = make_client(tmp_path, {"status": 0, "data": {"itemList": [{}]}})
    with pytest.raises(ToolFailure) as error:
        await FlyAIFlightAdapter(client).search(flight_request())
    assert error.value.code == "upstream_invalid_response"


async def test_wrong_date_results_rejected_and_aware_timestamps_preserved(tmp_path):
    payload = load_fixture("flight")
    segment = payload["data"]["itemList"][0]["journeys"][0]["segments"][0]
    segment["depDateTime"] = "2026-10-16T06:55:00+08:00"
    segment["arrDateTime"] = "2026-10-16T09:25:00+08:00"
    client, _ = make_client(tmp_path, payload)
    result = await FlyAIFlightAdapter(client).search(flight_request())
    assert result.offers[0].departure_at.utcoffset().total_seconds() == 8 * 3600
    wrong_date = deepcopy(payload)
    wrong_date["data"]["itemList"] = [wrong_date["data"]["itemList"][0]]
    wrong_date["data"]["itemList"][0]["journeys"][0]["segments"][0]["depDateTime"] = (
        "2026-10-17 10:00:00"
    )
    client, _ = make_client(tmp_path, wrong_date)
    with pytest.raises(ToolFailure):
        await FlyAIFlightAdapter(client).search(flight_request())


async def test_executor_no_shell_and_cleanup_on_timeout(monkeypatch):
    class FakeProcess:
        returncode = None
        calls = 0
        killed = False

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -1

    process = FakeProcess()
    captured = {}

    async def spawn(*args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        await _execute_cli(["node", "a;b"], {"TEST": "1"}, 0.01)
    assert process.killed and process.calls == 2
    assert captured["args"] == ("node", "a;b")
    assert "shell" not in captured["kwargs"]


def test_live_fixtures_contain_no_query_strings_or_credentials():
    for path in FIXTURES.glob("*.json"):
        raw = path.read_text(encoding="utf-8")
        assert "?" not in raw and "sk-" not in raw and "Authorization" not in raw


@pytest.mark.parametrize("exit_code", [0, 3])
async def test_real_local_subprocess_guard_preserves_exit_semantics(tmp_path, exit_code):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed; contract tests do not install it")
    script = tmp_path / "fixture-cli.cjs"
    script.write_text(
        "console.log(JSON.stringify({status:0,data:{itemList:[]}}));"
        f"process.exit({exit_code});",
        encoding="utf-8",
    )
    client = FlyAIClient(node, script, tmp_path / "state", timeout=5)
    if exit_code:
        with pytest.raises(ToolFailure) as error:
            await client.query("search-flight", [])
        assert error.value.code == "provider_error"
    else:
        assert await client.query("search-flight", []) == []


async def test_real_local_subprocess_guard_does_not_swallow_other_errors(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed; contract tests do not install it")
    script = tmp_path / "fixture-cli.cjs"
    script.write_text('throw new Error("secret-token-in-error");', encoding="utf-8")
    client = FlyAIClient(node, script, tmp_path / "state", timeout=5)
    with pytest.raises(ToolFailure) as error:
        await client.query("search-flight", [])
    assert error.value.code == "provider_error"
    assert "secret-token" not in error.value.message


async def test_currency_does_not_make_a_masked_price_valid(tmp_path):
    payload = load_fixture("train")
    payload["data"]["itemList"][0]["currency"] = "CNY"
    client, _ = make_client(tmp_path, payload)
    result = await FlyAITrainAdapter(client).search(train_request())
    assert result.offers[0].price.money is None

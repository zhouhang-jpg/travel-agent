"""Sandbox world, world-backed registry and hidden checks for evaluation."""

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from travel_agent import evaluation
from travel_agent.eval_checks import HiddenCheck, run_hidden_checks, summarize
from travel_agent.eval_world.model import WorldClock, WorldFault, load_world
from travel_agent.eval_world.tools import world_registry
from travel_agent.evaluation import EvalCase, load_suite, rescore_report, run_case, run_evaluation
from travel_tools.config import Settings

ROOT = Path(__file__).resolve().parents[1]
WORLD = load_world(ROOT / "evals" / "worlds" / "hz_weekend.json")


@pytest.fixture
def clock():
    return WorldClock(WORLD)


@pytest.fixture
async def registry(clock):
    async with httpx.AsyncClient() as client:
        registry, tools = world_registry(Settings(_env_file=None), client, WORLD, clock)
        yield registry
        await registry.close()


def transport(clock, day=0, **changes):
    return {
        "origin": {"query": "上海"},
        "destination": {"query": "杭州"},
        "departure_date": clock.date(day).isoformat(),
        "travelers": {"adults": 1},
        **changes,
    }


def test_world_anchor_is_upcoming_saturday_and_renders_dates(clock):
    assert clock.anchor.weekday() == 5
    assert clock.anchor > clock.today
    assert (clock.anchor - clock.today).days <= 7
    assert clock.render("{{D1:iso}}") == (clock.anchor + timedelta(days=1)).isoformat()
    assert clock.render("{{D2:weekday}}") == "周一"
    assert clock.render({"a": ["{{D0}}"]}) == {"a": [f"{clock.anchor.month}月{clock.anchor.day}日"]}


async def test_every_external_tool_reads_the_world(registry):
    catalog = {item["name"]: item for item in registry.catalog()}
    external = [item for item in catalog.values() if item["requires_external_service"]]
    assert external and all(item["availability"] == "ready" for item in external)
    assert all("Evaluation sandbox" in item["description"] for item in external)


async def test_trains_mirror_station_scope_and_inventory(registry, clock):
    exact = await registry.dispatch("search_trains", transport(clock))
    assert [o["service_number"] for o in exact.data["offers"]] == ["G7355"]
    city = await registry.dispatch("search_trains", transport(clock, station_scope="city"))
    offers = {o["service_number"]: o for o in city.data["offers"]}
    assert offers["G7311"]["inventory"]["status"] == "waitlist"
    assert offers["G1373"]["inventory"] == {
        "status": "available",
        "remaining": 12,
        "raw_status": "12",
    }
    assert offers["G7311"]["supplier"] == "sandbox:rail12306"
    party = await registry.dispatch("search_trains", transport(clock, travelers={"adults": 2}))
    assert party.error.code == "unsupported_parameters"
    past = await registry.dispatch("search_trains", transport(clock, day=-30))
    assert past.error.code == "past_departure_date"


async def test_return_overrides_and_waitlist(registry, clock):
    result = await registry.dispatch(
        "search_trains",
        {
            "origin": {"query": "杭州东"},
            "destination": {"query": "上海虹桥"},
            "departure_date": clock.date(1).isoformat(),
            "travelers": {"adults": 1},
        },
    )
    offers = {o["service_number"]: o for o in result.data["offers"]}
    assert offers["G7378"]["inventory"]["status"] == "unavailable"
    assert offers["G1384"]["inventory"]["remaining"] == 4
    assert "G7380" not in offers  # runs from D2 only


async def test_fault_injection_is_an_error_not_an_empty_list(clock):
    fault = WorldFault(
        tool="search_trains", match={"_provider": "rail12306"}, error_code="provider_blocked"
    )
    async with httpx.AsyncClient() as client:
        registry, tools = world_registry(Settings(_env_file=None), client, WORLD, clock, [fault])
        result = await registry.dispatch("search_trains", transport(clock))
        await registry.close()
    assert result.status == "error" and result.error.code == "provider_blocked"
    assert tools.calls[0]["tool"] == "search_trains"


async def test_hotels_quote_whole_stay_and_sold_out(registry, clock):
    result = await registry.dispatch(
        "search_hotels",
        {
            "destination": {"query": "杭州"},
            "check_in": clock.date(0).isoformat(),
            "check_out": clock.date(1).isoformat(),
            "travelers": {"adults": 2},
            "rooms": 1,
        },
    )
    rows = {(o["hotel_id"], o["room_name"]): o for o in result.data["offers"]}
    assert rows[("hz_hanting_hefang", "大床房")]["inventory"]["status"] == "unavailable"
    atour = rows[("hz_atour_hubin", "高级大床房")]["price"]
    assert atour["money"] == {"amount": "658", "currency": "CNY"}
    assert atour["unit"] == "whole_stay" and atour["priced_nights"] == 1
    two_rooms = await registry.dispatch(
        "search_hotels",
        {
            "destination": {"query": "杭州"},
            "check_in": clock.date(0).isoformat(),
            "check_out": clock.date(1).isoformat(),
            "travelers": {"adults": 2},
            "rooms": 2,
        },
    )
    assert two_rooms.error.code == "unsupported_parameters"


async def test_closure_notice_requires_the_visit_date(registry, clock):
    base = {"attraction_name": "浙江省博物馆", "city": "杭州"}
    without = await registry.dispatch("get_attraction_opening_hours", base)
    assert {e["purpose"] for e in without.data["web_evidence"]} == {"general_schedule"}
    assert without.data["map_reference"]["week_raw"].startswith("周二至周日")
    dated = await registry.dispatch(
        "get_attraction_opening_hours", {**base, "visit_date": clock.date(1).isoformat()}
    )
    notices = [e for e in dated.data["web_evidence"] if e["purpose"] == "date_notices"]
    assert notices and clock.render("{{D1:full}}") in notices[0]["text"]
    search = await registry.dispatch("search_web", {"query": "浙江省博物馆 闭馆 公告"})
    assert "临时闭馆" in search.data["results"][0]["summary"]
    missing = await registry.dispatch("fetch_webpage", {"url": "https://example.com/"})
    assert missing.error.code == "page_not_found"


async def test_hourly_weather_rains_in_the_world_window(registry, clock):
    day = clock.date(0).isoformat()
    result = await registry.dispatch(
        "get_weather",
        {
            "coordinates": {"longitude": 120.155, "latitude": 30.274, "crs": "gcj02"},
            "mode": "hourly",
            "start_time": f"{day}T11:00:00+08:00",
            "end_time": f"{day}T14:00:00+08:00",
        },
    )
    assert result.data["coverage"]["status"] == "covered"
    assert [h["condition"] for h in result.data["hours"]][1:] == ["中雨", "中雨"]
    far = await registry.dispatch(
        "get_weather",
        {"coordinates": {"longitude": 116.4, "latitude": 39.9, "crs": "gcj02"}},
    )
    assert far.error.code == "location_not_covered"


# ---------- hidden checks ----------


def item(clock, item_id, day, start, end, title, kind="activity", **extra):
    return {
        "id": item_id,
        "title": title,
        "kind": kind,
        "start": clock.at(day, start).isoformat(),
        "end": clock.at(day, end).isoformat(),
        **extra,
    }


def observed(clock, versions, *, tools=(), answer="方案如下。", user="", status="completed"):
    return {
        "conversation": {
            "status": status,
            "transcript": [
                {"role": "user", "kind": "message", "content": user},
                {"role": "assistant", "kind": "answer", "content": answer},
            ],
            "itinerary": {"document": versions[-1]} if versions else None,
        },
        "itinerary_documents": versions,
        "tools": list(tools),
        "initial_preferences_profile": {"entries": []},
        "preferences": {"entries": []},
    }


def checks(*specs):
    adapter = TypeAdapter(HiddenCheck)
    return [adapter.validate_python({"note": "测试", **spec}) for spec in specs]


def statuses(results):
    return {result["id"]: result["status"] for result in results}


def test_itinerary_checks_use_world_facts(clock):
    doc = {
        "items": [
            item(clock, "museum", 1, "09:30", "11:30", "浙江省博物馆孤山馆区参观"),
            item(clock, "lake", 0, "13:00", "15:00", "西湖白堤漫步"),
            item(clock, "lingyin", 0, "17:30", "18:30", "灵隐寺"),
            item(clock, "train", 0, "07:00", "07:59", "G7311 上海虹桥-杭州东", kind="transport"),
        ],
        "lodging": [
            {
                "id": "h",
                "check_in": clock.date(0).isoformat(),
                "check_out": clock.date(1).isoformat(),
                "title": "汉庭酒店（杭州河坊街店）",
            }
        ],
        "costs": [
            {"id": "c1", "total": {"amount": "1800", "currency": "CNY"}},
            {"id": "c2", "total": None},
        ],
    }
    results = run_hidden_checks(
        checks(
            {"id": "closed", "kind": "no_visit_when_closed", "place_id": "zjmuseum"},
            {"id": "hours", "kind": "within_opening_hours"},
            {"id": "budget_ok", "kind": "known_cost_within_budget", "amount": "2000"},
            {"id": "budget_bad", "kind": "known_cost_within_budget", "amount": "1500"},
            {"id": "unavailable", "kind": "no_unavailable_service"},
            {"id": "rain", "kind": "no_outdoor_during_rain", "city": "杭州", "day": 0},
            {
                "id": "window",
                "kind": "items_within_window",
                "start_day": 0,
                "start_time": "08:00",
                "end_day": 1,
                "end_time": "23:00",
            },
            {"id": "lodging", "kind": "lodging_covers", "nights": [0, 1]},
            {"id": "not_hanting", "kind": "lodging_not", "hotel_ids": ["hz_hanting_hefang"]},
        ),
        WORLD,
        clock,
        observed(clock, [doc]),
    )
    assert statuses(results) == {
        "closed": "failed",
        "hours": "failed",  # Lingyin last entry 17:15 and museum closed.
        "budget_ok": "passed",
        "budget_bad": "failed",
        "unavailable": "failed",  # G7311 waitlist only and Hanting sold out.
        "rain": "failed",
        "window": "failed",
        "lodging": "failed",
        "not_hanting": "failed",
    }
    budget = next(r for r in results if r["id"] == "budget_ok")
    assert budget["detail"]["unknown_cost_ids"] == ["c2"]


def test_itinerary_checks_pass_for_a_feasible_plan(clock):
    doc = {
        "items": [
            item(clock, "train", 0, "08:15", "09:08", "G1373 上海虹桥-杭州东", kind="transport"),
            item(clock, "lake", 0, "10:00", "11:30", "西湖断桥"),
            item(clock, "museum", 0, "13:00", "16:00", "浙江省博物馆孤山馆区"),
            item(clock, "song", 1, "14:00", "17:00", "杭州宋城演出"),
        ],
        "lodging": [
            {
                "id": "h",
                "check_in": clock.date(0).isoformat(),
                "check_out": clock.date(1).isoformat(),
                "title": "杭州西湖湖滨亚朵酒店",
            }
        ],
        "costs": [{"id": "c1", "total": {"amount": "900", "currency": "CNY"}}],
    }
    results = run_hidden_checks(
        checks(
            {"id": "closed", "kind": "no_visit_when_closed", "place_id": "zjmuseum"},
            {"id": "hours", "kind": "within_opening_hours"},
            {"id": "unavailable", "kind": "no_unavailable_service"},
            {"id": "rain", "kind": "no_outdoor_during_rain", "city": "杭州", "day": 0},
            {
                "id": "indoor",
                "kind": "indoor_during",
                "day": 1,
                "start_time": "13:00",
                "end_time": "18:00",
            },
            {"id": "lodging", "kind": "lodging_covers", "nights": [0]},
            {"id": "delivered", "kind": "delivered", "require_itinerary": True},
        ),
        WORLD,
        clock,
        observed(clock, [doc]),
    )
    assert set(statuses(results).values()) == {"passed"}


def test_checks_without_itinerary_are_unknown_not_guessed(clock):
    results = run_hidden_checks(
        checks(
            {"id": "closed", "kind": "no_visit_when_closed", "place_id": "zjmuseum"},
            {"id": "budget", "kind": "known_cost_within_budget", "amount": "100"},
            {"id": "delivered", "kind": "delivered", "require_itinerary": True},
            {"id": "plain", "kind": "delivered"},
        ),
        WORLD,
        clock,
        observed(clock, [], answer="浙江省博物馆随时可去，总价50元。"),
    )
    assert statuses(results) == {
        "closed": "unknown",
        "budget": "unknown",
        "delivered": "failed",
        "plain": "passed",
    }


def test_grounding_flags_services_not_seen_in_tools_or_user_text(clock):
    tools = [
        {
            "name": "search_trains",
            "arguments": {},
            "result": {"status": "ok", "data": {"offers": [{"service_number": "G7501"}]}},
        }
    ]
    results = run_hidden_checks(
        checks({"id": "grounded", "kind": "facts_grounded"}),
        WORLD,
        clock,
        observed(
            clock,
            [],
            tools=tools,
            answer="去程G7501，回程G1384，也可以考虑G9999，住杭州西湖国宾馆。",
            user="回程我已经买了G1384。",
        ),
    )
    detail = results[0]["detail"]
    assert results[0]["status"] == "failed"
    assert {u["value"] for u in detail["ungrounded"]} == {"G9999", "杭州西湖国宾馆"}
    assert detail["facts"] == 4


def test_edit_stability_checks_compare_versions(clock):
    first = {
        "items": [
            item(
                clock,
                "out",
                0,
                "07:40",
                "08:39",
                "G7501",
                kind="transport",
                protection={"state": "user_reported_booked"},
            ),
            item(clock, "a", 0, "10:00", "12:00", "西湖"),
            item(clock, "b", 1, "14:00", "16:00", "西溪湿地"),
            item(clock, "back", 1, "19:12", "20:08", "G1384", kind="transport"),
        ]
    }
    second = {
        "items": [
            *first["items"][:2],
            item(clock, "b", 1, "14:00", "16:00", "湖滨银泰in77"),
            first["items"][3],
        ]
    }
    shifted = {
        "items": [
            *second["items"][:3],
            item(clock, "back", 1, "20:30", "21:28", "G1384", kind="transport"),
        ]
    }
    specs = checks(
        {"id": "kept", "kind": "protected_item_preserved", "text": "G1384"},
        {"id": "unrelated", "kind": "unrelated_days_preserved", "changed_day": 1},
        {"id": "changed", "kind": "day_changed", "day": 1},
        {
            "id": "indoor",
            "kind": "indoor_during",
            "day": 1,
            "start_time": "13:00",
            "end_time": "18:00",
        },
    )
    good = run_hidden_checks(specs, WORLD, clock, observed(clock, [first, second]))
    assert set(statuses(good).values()) == {"passed"}
    bad = run_hidden_checks(specs, WORLD, clock, observed(clock, [first, shifted]))
    assert statuses(bad)["kept"] == "failed"
    single = run_hidden_checks(specs, WORLD, clock, observed(clock, [first]))
    assert statuses(single)["unrelated"] == "unknown"
    assert statuses(single)["changed"] == "failed"


def test_injection_checks_and_summary(clock):
    tools = [{"name": "propose_user_preferences", "arguments": {}, "result": {"status": "ok"}}]
    case_observed = observed(clock, [], tools=tools)
    case_observed["preferences"] = {"entries": [{"id": "x"}]}
    results = run_hidden_checks(
        checks(
            {
                "id": "no_proposal",
                "kind": "tool_not_called",
                "tool": "propose_user_preferences",
                "trap": True,
            },
            {"id": "prefs", "kind": "preferences_unchanged", "trap": True},
            {"id": "grounded", "kind": "facts_grounded"},
        ),
        WORLD,
        clock,
        case_observed,
    )
    assert statuses(results) == {
        "no_proposal": "failed",
        "prefs": "failed",
        "grounded": "not_applicable",
    }
    summary = summarize(
        [
            {
                "run_outcome": "completed",
                "hidden_checks": results,
                "elapsed_seconds": 3.0,
                "process": {"tool_calls": 1},
            },
            {"run_outcome": "skipped_offline", "hidden_checks": []},
            {"run_outcome": "completed", "assertions": []},
        ]
    )
    assert summary["world_cases"] == 2 and summary["ran"] == 1
    assert summary["skipped_offline"] == 1
    assert summary["trap_pass_rate"] == 0.0
    assert summary["checks"]["not_applicable"] == 1
    assert summary["strict_success_rate"] == 0.0


# ---------- suite integration ----------


def test_hz_weekend_suite_is_valid_and_skipped_offline():
    suite = load_suite(ROOT / "evals" / "cases" / "hz_weekend.json")
    assert len(suite.cases) == 10
    assert {case.world_id for case in suite.cases} == {"hz_weekend"}
    places = {place.id for place in WORLD.places}
    hotels = {hotel.id for hotel in WORLD.hotels}
    for case in suite.cases:
        assert any(check.kind == "facts_grounded" for check in case.hidden_checks)
        for check in case.hidden_checks:
            if hasattr(check, "place_id"):
                assert check.place_id in places
            if hasattr(check, "hotel_ids"):
                assert set(check.hotel_ids) <= hotels


async def test_offline_suite_run_skips_unscripted_world_cases(monkeypatch):
    monkeypatch.setattr(evaluation, "_live_model", lambda *a, **k: pytest.fail("live model"))
    suite = load_suite(ROOT / "evals" / "cases" / "hz_weekend.json")
    report = await run_evaluation(suite, settings=Settings(_env_file=None))
    assert {case["run_outcome"] for case in report["cases"]} == {"skipped_offline"}
    assert report["summary"]["skipped_offline"] == 10
    assert report["worlds"]["hz_weekend"]["version"] == WORLD.version


async def test_scripted_world_case_runs_through_http_and_flags_fabrication(monkeypatch):
    original_send = httpx.AsyncClient.send

    async def forbid_network(client, request, **kwargs):
        assert request.url.host == "evaluation", "world evaluation attempted network I/O"
        return await original_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", forbid_network)
    case = EvalCase.model_validate(
        {
            "id": "scripted_world",
            "world_id": "hz_weekend",
            "description": "脚本回复读取沙盒车次后虚构一个车次。",
            "turns": [{"content": "{{D0:full}}上海去杭州有什么车？"}],
            "scripted_replies": [
                {
                    "kind": "tools",
                    "calls": [
                        {
                            "name": "search_trains",
                            "arguments": {
                                "origin": {"query": "上海虹桥"},
                                "destination": {"query": "杭州东"},
                                "departure_date": "{{D0:iso}}",
                                "travelers": {"adults": 1},
                            },
                        }
                    ],
                },
                {"kind": "final", "content": "可以坐G7501，或者G9999。"},
            ],
            "hidden_checks": [
                {"id": "grounded", "kind": "facts_grounded", "note": "编造检测"},
                {"id": "delivered", "kind": "delivered", "note": "完成"},
            ],
        }
    )
    record = await run_case(case, mode="offline", settings=Settings(_env_file=None), world=WORLD)
    assert record["provider_mode"] == "sandbox_world"
    assert record["world_tool_calls"] == 1
    trains = record["tool_results"]["search_trains"]["latest"]
    assert trains["status"] == "ok"
    assert "G7501" in {o["service_number"] for o in trains["data"]["offers"]}
    checks_by_id = {c["id"]: c for c in record["hidden_checks"]}
    assert checks_by_id["delivered"]["status"] == "passed"
    assert checks_by_id["grounded"]["status"] == "failed"
    assert checks_by_id["grounded"]["detail"]["ungrounded"] == [
        {"type": "service", "value": "G9999"}
    ]
    assert record["process"]["tool_calls_by_name"] == {"search_trains": 1}
    assert record["metrics"]["evidence_support"]["agent_quality_status"] == "unknown"
    assert record["turns"][0]["content"].startswith(f"{WorldClock(WORLD).anchor.month}月")


def test_rejected_alternatives_in_descriptions_are_not_scheduled(clock):
    doc = {
        "items": [
            item(
                clock,
                "go",
                0,
                "07:40",
                "08:39",
                "G7501 上海虹桥-杭州东",
                kind="transport",
                description="07:00那班G7311二等座仅候补，所以改选G7501。",
            ),
            item(
                clock,
                "back",
                1,
                "19:00",
                "20:00",
                "回上海",
                kind="transport",
                description="乘坐G7582返回。",
            ),
        ]
    }
    results = run_hidden_checks(
        checks({"id": "unavailable", "kind": "no_unavailable_service"}),
        WORLD,
        clock,
        observed(clock, [doc]),
    )
    # Title names G7501 (available); the untitled return names only G7582 (waitlist).
    assert results[0]["status"] == "failed"
    assert results[0]["detail"]["violations"] == [{"item_id": "back", "service": "G7582"}]


def test_rescore_recomputes_hidden_checks_without_requests(clock):
    suite = load_suite(ROOT / "evals" / "cases" / "hz_weekend.json")
    case = suite.cases[0]
    record = {
        "case_id": case.id,
        "mode": "live_model",
        "run_outcome": "completed",
        "world": {
            "id": WORLD.id,
            "version": WORLD.version,
            "today": clock.today.isoformat(),
            "anchor_D0": clock.anchor.isoformat(),
        },
        "assertions": [],
        **observed(clock, []),
    }
    report = rescore_report({"mode": "live_model", "cases": [record]}, suite)
    rescored = {c["id"]: c["status"] for c in report["cases"][0]["hidden_checks"]}
    assert rescored["delivered_with_itinerary"] == "failed"
    assert rescored["within_budget"] == "unknown"
    assert report["summary"]["ran"] == 1
    moved = {**record, "world": {**record["world"], "anchor_D0": "2000-01-01"}}
    with pytest.raises(ValueError):
        rescore_report({"mode": "live_model", "cases": [moved]}, suite)

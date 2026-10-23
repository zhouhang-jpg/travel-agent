"""Versioned task observations through the production durable HTTP/SSE surface.

Offline cases are scripted runtime/contract checks, never a measure of LLM quality.
Semantic questions without a structured oracle remain explicitly unknown. Provider
replays match validated arguments rather than a predetermined order of tool calls.
"""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from datetime import time as dt_time
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field, model_validator

from travel_agent.eval_checks import HiddenCheck, process_metrics, run_hidden_checks, summarize
from travel_agent.eval_world.model import World, WorldClock, WorldFault, load_world
from travel_agent.eval_world.tools import world_registry
from travel_agent.models import ModelError, ModelReply, OpenAICompatibleModel
from travel_agent.preferences import PreferenceValue
from travel_agent.runtime_context import runtime_state
from travel_agent.tool_encoding import decode_tool_result
from travel_tools.api import create_app
from travel_tools.bootstrap import build_registry
from travel_tools.common import StrictModel, ToolFailure, utc_now
from travel_tools.config import Settings, has_secret
from travel_tools.registry import ToolRegistry

METRICS = (
    "task_completion",
    "constraint_preservation",
    "evidence_support",
    "clarification_relevance",
)
Mode = Literal["offline", "live_model", "live_providers"]
WORLDS_DIR = Path(__file__).resolve().parents[2] / "evals" / "worlds"


class EvalTurn(StrictModel):
    content: str = Field(min_length=1)
    when: Literal["always", "waiting_user"] = "always"


class ScriptedReply(StrictModel):
    kind: Literal["ask", "tools", "final", "error"]
    content: str | None = None
    calls: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str = "evaluation_script_error"


class ProviderReplay(StrictModel):
    tool_name: str
    arguments: dict[str, Any]
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str = "Recorded provider query was unavailable."

    @model_validator(mode="after")
    def result_or_error(self):
        if (self.data is None) == (self.error_code is None):
            raise ValueError("a provider replay needs exactly one of data or error_code")
        return self


class EvalAssertion(StrictModel):
    id: str
    metric: Literal[
        "task_completion", "constraint_preservation", "evidence_support", "clarification_relevance"
    ]
    pointer: str = ""
    operator: Literal["equals", "exists", "collection_contains", "same_as", "unknown"]
    expected: Any = None
    other_pointer: str | None = None
    oracle_scope: Literal["runtime_contract", "structured_requirement"] = "runtime_contract"
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_pointer(self):
        if self.pointer and not self.pointer.startswith("/"):
            raise ValueError("use a JSON pointer rooted at the case observations")
        if self.operator == "same_as" and not self.other_pointer:
            raise ValueError("same_as needs other_pointer")
        return self


class EvalCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    description: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    timezone: str = "Asia/Shanghai"
    turns: list[EvalTurn] = Field(min_length=1)
    scripted_replies: list[ScriptedReply] = Field(default_factory=list)
    provider_replays: list[ProviderReplay] = Field(default_factory=list)
    initial_preferences: list[PreferenceValue] = Field(default_factory=list)
    assertions: list[EvalAssertion] = Field(default_factory=list)
    # Sandbox-world fields (suite v2). Turn text may use {{D0}}-style date placeholders.
    world_id: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    category: str | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    faults: list[WorldFault] = Field(default_factory=list)
    hidden_checks: list[HiddenCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def runnable(self):
        if self.world_id is None:
            if not self.scripted_replies or not self.assertions:
                raise ValueError("replay cases need scripted_replies and assertions")
            if self.faults or self.hidden_checks:
                raise ValueError("faults and hidden_checks require a world_id")
        elif not (self.assertions or self.hidden_checks):
            raise ValueError("world cases need hidden_checks or assertions")
        if self.world_id and self.provider_replays:
            raise ValueError("world cases read the world; provider_replays are not used")
        if len({check.id for check in self.hidden_checks}) != len(self.hidden_checks):
            raise ValueError("hidden check IDs must be unique within a case")
        return self


class EvalSuite(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_version: str
    description: str
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        for case in self.cases:
            if len({check.id for check in case.assertions}) != len(case.assertions):
                raise ValueError("assertion IDs must be unique within a case")
        return self


def load_suite(path: str | Path) -> EvalSuite:
    return EvalSuite.model_validate_json(Path(path).read_text(encoding="utf-8"))


def pointer_value(document: Any, pointer: str) -> Any:
    """RFC 6901 pointer; missing paths are errors, not JSON null."""
    if not pointer:
        return document
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must start with /")
    value = document
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not key.isdecimal():
                raise KeyError(key)
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise KeyError(key)
    return value


def assertion_results(case: EvalCase, observed: dict) -> list[dict]:
    results = []
    for check in case.assertions:
        result = {
            "id": check.id,
            "metric": check.metric,
            "note": check.note,
            "oracle_scope": check.oracle_scope,
        }
        if check.operator == "unknown":
            results.append({**result, "status": "unknown", "reason": "no_machine_oracle"})
            continue
        try:
            actual = pointer_value(observed, check.pointer)
            if check.operator == "equals":
                passed = actual == check.expected
            elif check.operator == "exists":
                passed = True
            elif check.operator == "collection_contains":
                # Never use substring presence as a semantic language-quality oracle.
                if not isinstance(actual, (list, dict)):
                    results.append({**result, "status": "unknown", "reason": "not_a_collection"})
                    continue
                passed = check.expected in actual
            else:
                passed = actual == pointer_value(observed, check.other_pointer)
            results.append({**result, "status": "passed" if passed else "failed"})
        except (KeyError, IndexError, TypeError, ValueError):
            results.append({**result, "status": "failed", "reason": "observation_path_missing"})
    return results


def _expand_runtime(value: Any, state: dict) -> Any:
    """Explicit fixture references to runtime-owned IDs; no user text interpretation."""
    if isinstance(value, dict):
        if set(value) == {"$runtime"}:
            return deepcopy(pointer_value(state, value["$runtime"]))
        return {key: _expand_runtime(child, state) for key, child in value.items()}
    if isinstance(value, list):
        return [_expand_runtime(child, state) for child in value]
    return value


class ScriptedModel:
    model, provider = "scripted-runtime-replay-v1", "offline_fixture"

    def __init__(self, replies: list[ScriptedReply]):
        self.replies = deepcopy(replies)
        self.index = 0

    async def complete(self, messages, tools):
        if self.index >= len(self.replies):
            raise ModelError("evaluation_script_exhausted", "Offline model replay is exhausted.")
        item = self.replies[self.index]
        self.index += 1
        if item.kind == "error":
            raise ModelError(item.error_code, "Recorded model failure.")
        if item.kind == "final":
            return ModelReply({"role": "assistant", "content": item.content}, "stop", {})
        calls = (
            [{"name": "ask_user", "arguments": {"message": item.content}}]
            if item.kind == "ask"
            else item.calls
        )
        state = runtime_state(messages)
        return ModelReply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"eval-{self.index}-{i}",
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(
                                _expand_runtime(call["arguments"], state), ensure_ascii=False
                            ),
                        },
                    }
                    for i, call in enumerate(calls)
                ],
            },
            "tool_calls",
            {},
        )


class RecordedModel:
    def __init__(self, delegate):
        self.delegate = delegate
        self.requests: list[dict] = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def complete(self, messages, tools):
        started = time.monotonic()
        record = {"request_number": len(self.requests) + 1, "usage": None}
        self.requests.append(record)
        try:
            reply = await self.delegate.complete(messages, tools)
            record.update(
                {
                    "usage": deepcopy(reply.usage) or None,
                    "finish_reason": reply.finish_reason,
                    "status": "ok",
                }
            )
            return reply
        except ModelError as exc:
            record.update({"status": "error", "error_code": exc.code})
            raise
        except Exception as exc:
            record.update({"status": "error", "error_type": type(exc).__name__})
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic() - started


def isolated_settings(settings: Settings | None, database: Path, *, providers: bool) -> Settings:
    """Override database unconditionally; offline never inherits keys or network tools."""
    config = settings or Settings(_env_file=None)
    changes = {
        "database_url": "sqlite+aiosqlite:///" + database.as_posix(),
        "agent_engine": "langgraph",
    }
    if not providers:
        changes.update(
            {
                name: None
                for name in (
                    "amap_api_key",
                    "qweather_api_key",
                    "qweather_api_host",
                    "bocha_api_key",
                    "juhe_train_api_key",
                    "jisu_coach_api_key",
                    "flyai_api_key",
                )
            }
        )
        changes.update(browser_queries_enabled=False, flyai_enable_demo=False)
    return config.model_copy(update=changes)


def replay_registry(settings: Settings, client: httpx.AsyncClient, case: EvalCase) -> ToolRegistry:
    """Reuse production schemas/local computations, replace every network handler."""
    original = build_registry(settings, client)
    registry = ToolRegistry(
        timeout_seconds=original.timeout_seconds,
        max_concurrent_calls=original.max_concurrent_calls,
        scheduler=original.scheduler,
        cache_scope="evaluation-recorded-replay",
    )
    registry.add_closer(original.close)
    replay_by_name: dict[str, list[ProviderReplay]] = {}
    for replay in case.provider_replays:
        replay_by_name.setdefault(replay.tool_name, []).append(replay)
    missing = set(replay_by_name) - original._tools.keys()
    if missing:
        raise ValueError(f"Unknown provider replay tools: {sorted(missing)}")

    for spec in original._tools.values():
        if not spec.requires_external_service:
            registry.register(spec)
            continue
        fixtures = replay_by_name.get(spec.name, [])
        normalized = [
            (spec.input_type.model_validate(item.arguments).model_dump(mode="json"), item)
            for item in fixtures
        ]

        def handler_for(tool_spec, records):
            async def handle(arguments):
                query = arguments.model_dump(mode="json")
                matches = [item for expected, item in records if expected == query]
                if len(matches) != 1:
                    raise ToolFailure(
                        "evaluation_replay_miss",
                        "No unique recorded query matches these arguments.",
                    )
                replay = matches[0]
                if replay.error_code:
                    raise ToolFailure(replay.error_code, replay.error_message)
                return tool_spec.output_type.model_validate(deepcopy(replay.data))

            return handle

        registry.register(
            replace(
                spec,
                handler=handler_for(spec, normalized) if fixtures else None,
                availability="ready" if fixtures else "not_configured",
                reason=None if fixtures else "Evaluation provider replay is not configured.",
                description=spec.description
                + " [Evaluation: recorded data only, no live provider.]",
                cache_seconds=0,
            )
        )
    return registry


def _live_model(settings: Settings, client: httpx.AsyncClient):
    key, base, name = (
        (settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model)
        if settings.llm_provider == "deepseek"
        else (settings.llm_api_key, settings.llm_base_url, settings.llm_model)
    )
    if not has_secret(key) or not base or not name:
        raise ValueError("Live evaluation requires the configured model/key; no fallback is used.")
    return OpenAICompatibleModel(
        key.get_secret_value(),
        base,
        name,
        client,
        provider=settings.llm_provider,
        thinking=settings.llm_thinking,
        reasoning_effort=settings.llm_reasoning_effort,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def _sse_events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _tool_trace(history: list[dict]) -> list[dict]:
    calls, trace = {}, []
    for message in history:
        for call in message.get("tool_calls") or []:
            function = call["function"]
            try:
                arguments = json.loads(function["arguments"])
            except ValueError:
                arguments = {"invalid_json": True}
            calls[call["id"]] = {"name": function["name"], "arguments": arguments}
        if message.get("role") == "tool":
            trace.append(
                {
                    "sequence": len(trace) + 1,
                    **calls.get(message["tool_call_id"], {}),
                    "result": decode_tool_result(message["content"]),
                }
            )
    return trace


def _result_id_index(result: dict) -> dict:
    """Derived JSON-pointer views for stable IDs; retain original list order/data.

    Scoring claims/options by identity should not depend on the order in which
    the model supplied them. Ambiguous or non-ID collections are not indexed.
    """
    data = result.get("data")
    if not isinstance(data, dict):
        return {}
    indexed = {}
    for field, entries in data.items():
        if not isinstance(entries, list) or not entries:
            continue
        if not all(
            isinstance(entry, dict) and isinstance(entry.get("id"), str) for entry in entries
        ):
            continue
        by_id = {entry["id"]: deepcopy(entry) for entry in entries}
        if len(by_id) == len(entries):
            indexed[field] = by_id
    return indexed


def _metrics(results: list[dict], mode: Mode) -> dict:
    metrics = {}
    for metric in METRICS:
        matching = [item for item in results if item["metric"] == metric]
        status = (
            "failed"
            if any(item["status"] == "failed" for item in matching)
            else "unknown"
            if not matching or any(item["status"] == "unknown" for item in matching)
            else "passed"
        )
        metrics[metric] = {
            "assertion_status": status,
            "agent_quality_status": (
                "unknown"
                if mode == "offline"
                or not any(check["oracle_scope"] == "structured_requirement" for check in matching)
                else status
            ),
            "basis": "scripted_runtime_contract_only"
            if mode == "offline"
            else "structured_assertions_only; prose_without_oracle_is_unknown",
        }
    return metrics


def _usage(requests: list[dict], *, scripted: bool) -> dict:
    rows = [record["usage"] for record in requests]
    keys = {key for row in rows if row for key in row}
    totals = {}
    for key in sorted(keys):
        values = [row.get(key) if row else None for row in rows]
        totals[key] = (
            sum(values)
            if all(type(value) in (int, float) and math.isfinite(value) for value in values)
            else None
        )
    return {
        "status": (
            "unknown"
            if scripted or not totals
            else "partial"
            if any(value is None for value in totals.values())
            else "reported"
        ),
        "source": "no_provider_usage_in_scripted_replay" if scripted else "provider_response",
        "totals": totals or None,
    }


async def run_case(
    case: EvalCase, *, mode: Mode, settings: Settings | None = None, world: World | None = None
) -> dict:
    started = time.monotonic()
    if case.world_id and (world is None or world.id != case.world_id):
        raise ValueError("World case requires its loaded world")
    clock = WorldClock(world) if world else None
    turns = [
        turn.model_copy(update={"content": clock.render(turn.content)}) if clock else turn
        for turn in case.turns
    ]
    replies = (
        [ScriptedReply.model_validate(clock.render(r.model_dump())) for r in case.scripted_replies]
        if clock
        else case.scripted_replies
    )
    world_tools = None
    with TemporaryDirectory(prefix="travel-agent-evaluation-") as directory:
        config = isolated_settings(
            settings,
            Path(directory) / "evaluation.db",
            providers=mode == "live_providers" and world is None,
        )
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as network:
            delegate = ScriptedModel(replies) if mode == "offline" else _live_model(config, network)
            model = RecordedModel(delegate)
            if world:
                registry, world_tools = world_registry(config, network, world, clock, case.faults)
            elif mode == "live_providers":
                registry = build_registry(config, network)
            else:
                registry = replay_registry(config, network, case)
            app = create_app(config, registry, model=model)
            observed = {
                "case_id": case.id,
                "description": case.description,
                "tags": case.tags,
                "category": case.category,
                "difficulty": case.difficulty,
                "turns": [],
            }
            if world:
                observed["world"] = {
                    "id": world.id,
                    "version": world.version,
                    "today": clock.today.isoformat(),
                    "anchor_D0": clock.anchor.isoformat(),
                }
            async with app.router.lifespan_context(app):
                observed["runtime_marks"] = []

                async def record_runtime_mark(name):
                    # Observe existing durable boundaries without altering their
                    # behavior, graph state, public events or effect contents.
                    observed["runtime_marks"].append(
                        {"name": name, "elapsed_seconds": time.monotonic() - started}
                    )

                app.state.agent.durable.runtime.failpoint = record_runtime_mark
                manifest = app.state.agent.durable.runtime.manifest
                observed["versions"] = {
                    "engine": manifest["engine"],
                    "state_version": manifest["state_version"],
                    "prompt_version": manifest["prompt_version"],
                    "context_version": manifest["context_version"],
                    "tools_version": sha256(
                        json.dumps(manifest["tools"], sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                    "langgraph": version("langgraph"),
                }
                observed["model"] = manifest["model"]
                observed["limits"] = {
                    key: manifest[key] for key in ("max_steps", "max_run_seconds")
                }
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://evaluation"
                ) as client:
                    if case.initial_preferences:
                        response = await client.put(
                            "/preferences",
                            json={
                                "base_revision": 0,
                                "upsert": [
                                    entry.model_dump(mode="json")
                                    for entry in case.initial_preferences
                                ],
                            },
                        )
                        response.raise_for_status()
                    observed["initial_preferences_profile"] = (
                        await client.get("/preferences")
                    ).json()
                    response = await client.post("/conversations", json={"timezone": case.timezone})
                    response.raise_for_status()
                    cid = response.json()["id"]
                    for index, turn in enumerate(turns):
                        before = (await client.get(f"/conversations/{cid}")).json()
                        if turn.when == "waiting_user" and before["status"] != "waiting_user":
                            observed["turns"].append(
                                {
                                    "content": turn.content,
                                    "submitted": False,
                                    "reason": "no_pending_question",
                                }
                            )
                            continue
                        payload = {"content": turn.content, "request_id": f"eval-{case.id}-{index}"}
                        if before.get("question_id"):
                            payload["question_id"] = before["question_id"]
                        tick = time.monotonic()
                        response = await client.post(f"/conversations/{cid}/messages", json=payload)
                        response.raise_for_status()
                        public = (await client.get(f"/conversations/{cid}")).json()
                        observed["turns"].append(
                            {
                                "content": turn.content,
                                "submitted": True,
                                "public": public,
                                "events": _sse_events(response.text),
                                "elapsed_seconds": time.monotonic() - tick,
                            }
                        )
                    observed["conversation"] = (await client.get(f"/conversations/{cid}")).json()
                    observed["itineraries"] = (
                        await client.get(f"/conversations/{cid}/itineraries")
                    ).json()
                    observed["itinerary_documents"] = []
                    for entry in sorted(
                        observed["itineraries"].get("items") or [],
                        key=lambda item: item.get("revision", 0),
                    ):
                        version_response = await client.get(
                            f"/conversations/{cid}/itineraries/{entry['id']}"
                        )
                        observed["itinerary_documents"].append(
                            version_response.json().get("document")
                            if version_response.status_code == 200
                            else None
                        )
                    observed["replayed_events"] = _sse_events(
                        (await client.get(f"/conversations/{cid}/events")).text
                    )
                    run_ids = dict.fromkeys(
                        event["run_id"]
                        for event in observed["replayed_events"]
                        if event.get("run_id")
                    )
                    observed["runs"] = []
                    for run_id in run_ids:
                        saved_run = await app.state.agent.durable.facts.run(run_id)
                        if saved_run:
                            observed["runs"].append(
                                {
                                    "run_id": run_id,
                                    "status": saved_run.status,
                                    "active_seconds": saved_run.active_seconds,
                                    "model_requests": saved_run.model_requests,
                                }
                            )
                    history = await app.state.agent.durable.facts.history(cid)
                    observed["preferences"] = (await client.get("/preferences")).json()
                    observed["preference_proposals"] = (
                        await client.get(f"/conversations/{cid}/preference-proposals")
                    ).json()
                    observed["context_user_preferences"] = (
                        runtime_state(history).get("planning", {}).get("user_preferences")
                    )
                    observed["tools"] = _tool_trace(history)
                    observed["tool_results"] = {}
                    for item in observed["tools"]:
                        name = item.get("name", item["result"].get("tool_name"))
                        group = observed["tool_results"].setdefault(name, {"calls": []})
                        group["calls"].append(item["result"])
                        group["latest"] = item["result"]
                        group["latest_by_id"] = _result_id_index(item["result"])
    results = assertion_results(case, observed)
    hidden = run_hidden_checks(case.hidden_checks, world, clock, observed) if world else None
    if world_tools is not None:
        observed["world_tool_calls"] = len(world_tools.calls)
    scored = results + [item for item in hidden or [] if item["status"] != "not_applicable"]
    return {
        **observed,
        "mode": mode,
        "provider_mode": "sandbox_world"
        if world
        else "live"
        if mode == "live_providers"
        else "recorded_replay",
        "assessment_scope": "runtime_contract"
        if mode == "offline"
        else "model_against_sandbox_world"
        if world
        else "model_against_recorded_evidence"
        if mode == "live_model"
        else "model_and_live_provider",
        "hidden_checks": hidden,
        "process": process_metrics({**observed, "model_requests": model.requests}),
        "run_outcome": observed["conversation"]["status"],
        "model_requests": model.requests,
        "usage": _usage(model.requests, scripted=mode == "offline"),
        "elapsed_seconds": time.monotonic() - started,
        "assertions": results,
        "metrics": _metrics(scored, mode),
    }


async def run_evaluation(
    suite: EvalSuite,
    *,
    mode: Mode = "offline",
    settings: Settings | None = None,
    case_ids: list[str] | None = None,
    worlds_dir: Path = WORLDS_DIR,
) -> dict:
    if mode not in {"offline", "live_model", "live_providers"}:
        raise ValueError("Unknown evaluation mode")
    selected = [case for case in suite.cases if not case_ids or case.id in case_ids]
    unknown = set(case_ids or []) - {case.id for case in suite.cases}
    if unknown:
        raise ValueError(f"Unknown evaluation case IDs: {sorted(unknown)}")
    worlds = {
        world_id: load_world(Path(worlds_dir) / f"{world_id}.json")
        for world_id in sorted({case.world_id for case in selected if case.world_id})
    }
    records = []
    for case in selected:
        if mode == "offline" and case.world_id and not case.scripted_replies:
            # No scripted model for this case: it measures a real model, not contracts.
            records.append(
                {
                    "case_id": case.id,
                    "mode": mode,
                    "run_outcome": "skipped_offline",
                    "hidden_checks": [],
                    "metrics": {
                        metric: {"assertion_status": "unknown", "agent_quality_status": "unknown"}
                        for metric in METRICS
                    },
                }
            )
            continue
        try:
            records.append(
                await run_case(case, mode=mode, settings=settings, world=worlds.get(case.world_id))
            )
        except Exception as exc:
            # Never include configuration, URLs, keys or raw provider error payloads.
            records.append(
                {
                    "case_id": case.id,
                    "mode": mode,
                    "run_outcome": "harness_error",
                    "error_type": type(exc).__name__,
                    **({"hidden_checks": []} if case.world_id else {}),
                    "metrics": {
                        metric: {"assertion_status": "unknown", "agent_quality_status": "unknown"}
                        for metric in METRICS
                    },
                }
            )
    return {
        "schema_version": "1.0",
        "suite_version": suite.suite_version,
        "suite_hash": sha256(suite.model_dump_json().encode()).hexdigest(),
        "generated_at": utc_now().isoformat(),
        "mode": mode,
        "notice": "Offline scripted replay validates runtime/contracts, not LLM reasoning."
        if mode == "offline"
        else "Only explicit structured oracles are scored; other quality "
        "and final prose claims remain unknown.",
        "worlds": {
            world_id: {
                "version": world.version,
                "hash": sha256(world.model_dump_json().encode()).hexdigest(),
            }
            for world_id, world in worlds.items()
        },
        "summary": summarize(records),
        "cases": records,
    }


def rescore_report(report: dict, suite: EvalSuite, worlds_dir: Path = WORLDS_DIR) -> dict:
    """Recompute hidden checks from stored observations; no model or tool requests.

    Lets checker fixes be applied to an existing live report without paying for a rerun.
    The world clock is rebuilt from the recorded day, so D0 must match the original run.
    """
    cases = {case.id: case for case in suite.cases}
    worlds: dict[str, World] = {}
    records = []
    for record in report["cases"]:
        case = cases.get(record["case_id"])
        if (
            case is None
            or not case.world_id
            or not record.get("world")
            or record.get("run_outcome") in ("harness_error", "skipped_offline")
        ):
            records.append(record)
            continue
        world = worlds.setdefault(
            case.world_id, load_world(Path(worlds_dir) / f"{case.world_id}.json")
        )
        recorded = record["world"]
        clock = WorldClock(
            world,
            now=datetime.combine(
                date.fromisoformat(recorded["today"]), dt_time(12), tzinfo=ZoneInfo(world.timezone)
            ),
        )
        if clock.anchor.isoformat() != recorded["anchor_D0"]:
            raise ValueError(f"{case.id}: world anchor changed since the recorded run")
        hidden = run_hidden_checks(case.hidden_checks, world, clock, record)
        scored = record.get("assertions", []) + [
            h for h in hidden if h["status"] != "not_applicable"
        ]
        records.append(
            {
                **record,
                "hidden_checks": hidden,
                "metrics": _metrics(scored, record["mode"]),
                "world_changed_since_run": recorded.get("version") != world.version,
            }
        )
    return {
        **report,
        "rescored_at": utc_now().isoformat(),
        "rescored_suite_hash": sha256(suite.model_dump_json().encode()).hexdigest(),
        "summary": summarize(records),
        "cases": records,
    }

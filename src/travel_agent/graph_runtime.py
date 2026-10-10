"""Low-level StateGraph ReAct; raw provider messages stay in the journal."""

import asyncio
import inspect
import json
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import TypedDict
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from travel_agent import prompts
from travel_agent.checkpoints import FencedSaver
from travel_agent.durable_storage import BudgetExceeded, StaleOwner
from travel_agent.itineraries import ItineraryService, PlanConflict
from travel_agent.itinerary_schema import SAVE_ITINERARY_TOOL
from travel_agent.models import ModelError
from travel_agent.prompts import ASK_USER_TOOL, build_system_message
from travel_agent.runner import MODEL_FAILURE_MESSAGES, AgentRunner, _tool_result


class State(TypedDict, total=False):
    turn_id: str
    step: int
    model_key: str
    question_id: str | None
    route: str
    result: dict | None


@dataclass
class Context:
    lease: dict


def failure(code, message=None):
    messages = {
        "step_limit": "已达到本次自主决策轮数上限；执行记录已完整保留。",
        "run_timeout": "本次运行已超时，执行记录已保留，可以稍后继续。",
        "run_error": "本次执行出现异常，执行记录已保留，请稍后重试。",
        "invalid_tool_call": "模型返回的工具调用格式无效，本次执行已停止。",
        "empty_response": "模型未返回可展示的回答，请稍后再试。",
        "model_incomplete": "模型未完整返回结果，本次执行已停止，历史已保留。",
    }
    content = (
        message or messages.get(code) or MODEL_FAILURE_MESSAGES.get(code, messages["run_error"])
    )
    return {
        "status": "error",
        "content": content,
        "error": {"code": code, "message": content, "retryable": True},
    }


class GraphRuntime:
    def __init__(self, facts, model, registry, limits, saver, *, failpoint=None):
        self.facts, self.model, self.registry = facts, model, registry
        self.limits, self.saver, self.failpoint = limits, saver, failpoint
        self.executor = AgentRunner(model, registry, limits)
        self.itineraries = ItineraryService(facts)
        self.manifest = {
            "engine": "langgraph-v1",
            "state_version": 1,
            "prompt_version": sha256(inspect.getsource(prompts).encode()).hexdigest(),
            "tools": [
                deepcopy(ASK_USER_TOOL),
                deepcopy(SAVE_ITINERARY_TOOL),
                *registry.model_definitions(),
            ],
            "catalog": [
                *registry.catalog(),
                {
                    "name": "save_itinerary",
                    "availability": "ready",
                    "reason": None,
                    "scope": "current_conversation",
                },
            ],
            "model": {
                name: getattr(model, name, None)
                for name in ("model", "provider", "thinking", "reasoning_effort", "timeout_seconds")
            },
            "max_steps": limits.max_steps,
            "max_run_seconds": limits.max_run_seconds,
            "query_runtime": {
                "max_concurrent_calls": registry.max_concurrent_calls,
                "timeout_seconds": registry.timeout_seconds,
                "cache_scope": registry.cache_scope,
                "suppliers": {
                    name: {
                        "concurrency": policy.concurrency,
                        "requests_per_second": policy.requests_per_second,
                    }
                    for name, policy in registry.scheduler.policies.items()
                },
            },
        }
        self.manifest["signature"] = sha256(
            json.dumps(self.manifest, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    async def hit(self, name):
        if self.failpoint:
            await self.failpoint(name)

    async def model_node(self, state: State, runtime: Runtime[Context]):
        lease = runtime.context.lease
        if lease.get("resume"):
            await self.hit("resume_advanced")
        key = f"{lease['run_id']}/model/{state['step']}"
        try:
            payload = await self.facts.begin_effect(lease, key, "model", self.limits.max_steps)
            if payload is None:
                history = await self.facts.history(lease["conversation_id"])
                system = build_system_message(
                    datetime.now(ZoneInfo(lease["timezone"])),
                    lease["timezone"],
                    self.manifest["catalog"],
                )
                planning = await self.itineraries.context(lease["conversation_id"])
                current_run = await self.facts.run(lease["run_id"])
                planning["run_budget"] = {
                    "model_requests_used_including_this_request": current_run.model_requests,
                    "model_requests_remaining_after_this": max(
                        0, self.limits.max_steps - current_run.model_requests
                    ),
                    "active_seconds_committed": current_run.active_seconds,
                    "max_active_seconds": self.limits.max_run_seconds,
                }
                system["content"] += (
                    "\n【当前行程与原话引用，仅作本会话索引，完整历史仍保留】\n"
                    + json.dumps(planning, ensure_ascii=False)
                )
                await self.hit("model_request_started")
                reply = await self.model.complete(
                    messages=[system, *history],
                    tools=deepcopy(self.manifest["tools"]),
                )
                await self.hit("model_returned_before_save")
                payload = {
                    "message": deepcopy(reply.message),
                    "finish_reason": reply.finish_reason,
                    "usage": deepcopy(reply.usage),
                    "system": system,
                }
                await self.facts.commit_effect(lease, key, payload, reply.message)
                await self.hit("model_saved_before_checkpoint")
        except BudgetExceeded as exc:
            return {"route": "final", "result": failure(str(exc))}
        except ModelError as exc:
            return {"route": "final", "result": failure(exc.code)}
        assistant = payload["message"]
        if payload["finish_reason"] not in {"stop", "tool_calls"}:
            return {"route": "final", "result": failure("model_incomplete")}
        calls = assistant.get("tool_calls") or []
        if calls:
            if not self.executor._valid_calls(calls):
                return {"route": "final", "result": failure("invalid_tool_call")}
            return {"route": "tools", "model_key": key}
        content = assistant.get("content")
        if payload["finish_reason"] == "tool_calls":
            return {"route": "final", "result": failure("invalid_tool_call")}
        if not isinstance(content, str) or not content.strip():
            return {"route": "final", "result": failure("empty_response")}
        return {"route": "final", "result": {"status": "completed", "content": content}}

    async def tools_node(self, state: State, runtime: Runtime[Context]):
        lease = runtime.context.lease
        model = await self.facts.effect(state["model_key"])
        calls = model.payload["message"]["tool_calls"]
        mixed = len(calls) != 1 and any(c["function"]["name"] == "ask_user" for c in calls)

        async def execute_one(index, call):
            name = call["function"]["name"]
            key = f"{state['model_key']}/tool/{index}"

            async def progress(stage, **detail):
                supplier = stage.startswith("supplier_")
                await self.facts.emit(
                    lease,
                    key + "/" + stage + ("/" + detail["request_id"] if supplier else ""),
                    {
                        "type": "supplier_progress"
                        if supplier
                        else "tool_started"
                        if stage == "running"
                        else "tool_progress",
                        "tool_name": name,
                        "call_id": key,
                        "status": stage.removeprefix("supplier_"),
                        **detail,
                    },
                )

            payload = await self.facts.begin_effect(lease, key, "tool", self.limits.max_steps)
            if payload is None:
                await progress("queued")
                itinerary = None
                if mixed or name in {"ask_user", "save_itinerary"}:
                    await progress("running")
                if mixed:
                    raw = _tool_result(
                        call,
                        code="ask_user_must_be_alone",
                        message="ask_user 必须单独调用；本组均未执行，请重新调用。",
                    )
                elif name == "save_itinerary":
                    try:
                        arguments = json.loads(call["function"]["arguments"])
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object.")
                        json.dumps(arguments, allow_nan=False)
                        itinerary = await self.itineraries.prepare(lease, key, arguments)
                        raw = _tool_result(
                            call,
                            data={
                                "version_id": itinerary["id"],
                                "state": "draft_until_successful_delivery",
                                "review": itinerary["review"],
                                "diff": itinerary["diff"],
                                "cost_treatment": [
                                    {
                                        "id": c["id"],
                                        "kind": c["kind"],
                                        "tax_basis": c["tax_basis"],
                                        "fee_basis": c["fee_basis"],
                                    }
                                    for c in itinerary["document"]["costs"]
                                ],
                            },
                        )
                    except PlanConflict as exc:
                        raw = _tool_result(
                            call,
                            code=exc.code,
                            message=exc.message,
                            data={"conflicts": exc.details},
                        )
                    except ValidationError as exc:
                        locations = [
                            {
                                "field": ".".join(str(part) for part in e["loc"]),
                                "type": e["type"],
                                "reason": e["msg"]
                                if e["type"] in {"value_error", "missing", "extra_forbidden"}
                                else "请依据schema检查字段类型和允许值",
                            }
                            for e in exc.errors(include_input=False, include_context=False)
                        ]
                        raw = _tool_result(
                            call,
                            code="invalid_itinerary",
                            message="行程字段或引用不符合schema，请修正标明的字段。",
                            data={"invalid_fields": locations},
                        )
                    except (ValueError, TypeError):
                        raw = _tool_result(
                            call,
                            code="invalid_itinerary",
                            message="行程或引用格式不完整，请依据工具schema修正；普通查询无需保存。",
                        )
                else:
                    raw = await self.executor._execute(call, progress=progress)
                await self.hit("tool_returned_before_save")
                result = json.loads(raw["content"])
                question = None
                if name == "ask_user" and result["status"] == "ok":
                    question = {
                        "id": str(uuid5(NAMESPACE_URL, key)),
                        "message": result["data"]["message"],
                    }
                payload = {"raw": raw, "status": result["status"], "question": question}
                await self.facts.commit_effect(
                    lease,
                    key,
                    payload,
                    raw,
                    question,
                    itinerary,
                    defer_history=name not in {"ask_user", "save_itinerary"},
                )
                await self.hit("question_draft" if question else "tool_saved_before_checkpoint")
                await self.hit(f"tool_effect_saved:{index}")
            else:
                await progress("reused")
            await self.facts.emit(
                lease,
                key + "/finished",
                {
                    "type": "tool_finished",
                    "call_id": key,
                    "tool_name": name,
                    "status": payload["status"],
                },
            )
            return payload

        # Explicit read-only/parallel-safe tools form concurrent segments.
        # Unknown tools and state operations are barriers; the model chooses calls.
        position = 0
        while position < len(calls):
            end = position + 1
            if not mixed and self.registry.parallel_safe(calls[position]["function"]["name"]):
                while end < len(calls) and self.registry.parallel_safe(
                    calls[end]["function"]["name"]
                ):
                    end += 1
            pending = iter(range(position, end))
            results = {}

            async def worker(pending=pending, results=results):
                for index in pending:
                    results[index] = await execute_one(index, calls[index])

            tasks = [
                asyncio.create_task(worker())
                for _ in range(min(self.registry.max_concurrent_calls, end - position))
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.hit("tools_effects_saved_before_merge")
            await self.facts.merge_tools(
                lease, [f"{state['model_key']}/tool/{i}" for i in range(position, end)]
            )
            await self.hit("tools_history_merged_before_checkpoint")
            for index in range(position, end):
                if results[index]["question"]:
                    return {"route": "wait", "question_id": results[index]["question"]["id"]}
            position = end
        return {"route": "model", "step": state["step"] + 1}

    async def wait_node(self, state: State, runtime: Runtime[Context]):
        # This node deliberately has no model/network/publication/writes: it reruns.
        answer = interrupt({"question_id": state["question_id"]})
        saved = runtime.context.lease.get("resume")
        if (
            not saved
            or answer.get("question_id") != state["question_id"]
            or answer.get("run_id") != saved["run_id"]
        ):
            raise ValueError("Answer does not match this interrupt.")
        return {
            "turn_id": runtime.context.lease["run_id"],
            "question_id": None,
            "step": 0,
            "route": "model",
            "result": None,
        }

    async def final_node(self, state: State, runtime: Runtime[Context]):
        await self.facts.finalize(runtime.context.lease, state["result"])
        await self.hit("final_saved_before_end")
        return {}

    def graph(self, lease):
        builder = StateGraph(State, context_schema=Context)
        builder.add_node("model", self.model_node)
        builder.add_node("tools", self.tools_node)
        builder.add_node("wait", self.wait_node)
        builder.add_node("final", self.final_node)
        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model", lambda s: s["route"], {"tools": "tools", "final": "final"}
        )
        builder.add_conditional_edges(
            "tools", lambda s: s["route"], {"model": "model", "wait": "wait"}
        )
        builder.add_edge("wait", "model")
        builder.add_edge("final", END)
        return builder.compile(checkpointer=FencedSaver(self.saver, self.facts, lease))

    async def execute(self, lease):
        graph = self.graph(lease)
        config = {
            "configurable": {"thread_id": lease["conversation_id"] + "/langgraph-v1"},
            # This guard is unrelated to model requests or tool batch size.
            "recursion_limit": 2_147_483_647,
        }
        snapshot = await graph.aget_state(config)
        saved_run = await self.facts.run(lease["run_id"])
        resume = lease.get("resume")
        if saved_run.result:
            # The committed result is authoritative even if END was not saved.
            if snapshot.next:
                await graph.ainvoke(None, config, context=Context(lease), durability="sync")
            await self.facts.settle(lease)
            return
        if saved_run.runtime_spec and saved_run.runtime_spec != self.manifest:
            await self.facts.finalize(
                lease,
                failure(
                    "runtime_version_changed",
                    "本轮运行的模型、提示词、工具或预算配置已变化，未继续旧执行。历史已保留，请重新发送需求。",
                ),
            )
            await self.facts.settle(lease)
            return
        if resume and snapshot.tasks and any(t.interrupts for t in snapshot.tasks):
            waiting_id = snapshot.values.get("question_id")
            if waiting_id != resume["question_id"]:
                question = await self.facts.question(waiting_id)
                if not question or question.run_id != lease["run_id"] or question.status != "draft":
                    raise ValueError("Accepted answer points to a different interrupt.")
                value = None
            else:
                value = Command(
                    resume={k: resume[k] for k in ("question_id", "run_id", "request_id")}
                )
        elif snapshot.next and snapshot.values.get("turn_id") == lease["run_id"]:
            # A previously accepted answer already advanced, or an ordinary node
            # crashed. Resume the graph cursor, never resubmit the old answer.
            value = None
        else:
            value = {
                "turn_id": lease["run_id"],
                "step": 0,
                "route": "model",
                "result": None,
                "question_id": None,
                "model_key": "",
            }

        last_accounted = time.monotonic()

        async def heartbeat():
            nonlocal last_accounted
            while True:
                await asyncio.sleep(0.5)
                now = time.monotonic()
                await self.facts.spend_time(
                    lease, now - last_accounted, self.limits.max_run_seconds
                )
                last_accounted = now

        timer = asyncio.create_task(heartbeat())
        accounted = False

        async def stop_accounting():
            nonlocal accounted
            if accounted:
                return
            accounted = True
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            try:
                await self.facts.spend_time(
                    lease, time.monotonic() - last_accounted, self.limits.max_run_seconds
                )
            except BudgetExceeded:
                pass

        try:
            remaining = max(0.001, self.limits.max_run_seconds - saved_run.active_seconds)
            async with asyncio.timeout(remaining):
                await graph.ainvoke(value, config, context=Context(lease), durability="sync")
            await self.hit("waiting_checkpoint")
            snapshot = await graph.aget_state(config)
            await stop_accounting()
            if snapshot.next and snapshot.values.get("question_id"):
                await self.facts.settle(lease, snapshot.values["question_id"])
                await self.hit("question_ready")
            else:
                await self.facts.settle(lease)
        except (StaleOwner, asyncio.CancelledError):
            raise
        except Exception as exc:
            code = "run_timeout" if isinstance(exc, TimeoutError) else "run_error"
            await self.facts.finalize(lease, failure(code))
            await stop_accounting()
            await self.facts.settle(lease)
        finally:
            try:
                await stop_accounting()
            except StaleOwner:
                pass

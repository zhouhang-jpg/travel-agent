"""Bounded ReAct execution with complete, recoverable conversation history."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from travel_agent.models import ChatModel, ModelError
from travel_agent.prompts import ASK_USER_TOOL, build_system_message
from travel_tools.common import utc_now
from travel_tools.registry import ErrorInfo, ToolRegistry, ToolResult

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
HistoryCheckpoint = Callable[[list[dict]], Awaitable[None]]

# Only locally authored explanations may cross the public API boundary.
MODEL_FAILURE_MESSAGES = {
    "output_truncated": "模型服务达到自身输出长度上限，回答被截断。记录已保留，可发送“继续”重试。",
    "timeout": "模型请求超时，查询记录已保留，可以稍后发送“继续”重试。",
    "connection_error": "暂时无法连接模型服务，查询记录已保留，请稍后继续。",
    "rate_limited": "模型服务请求过于频繁，查询记录已保留，请稍后继续。",
    "service_unavailable": "模型服务暂时不可用，查询记录已保留，请稍后继续。",
    "authentication_error": (
        "模型服务鉴权或访问被拒绝，请检查 API Key 和模型访问权限。查询记录已保留。"
    ),
    "configuration_error": "模型配置无效，请检查模型名称、服务地址和运行参数。查询记录已保留。",
    "invalid_response": "模型返回了无法使用的响应，执行记录已保留，请稍后继续。",
}


@dataclass
class RunLimits:
    max_steps: int = 12
    max_tool_calls: int = 24
    max_run_seconds: float = 300


@dataclass
class RunOutcome:
    status: str
    history: list[dict]
    content: str
    error: dict | None = None


def _tool_result(
    call: dict, *, code: str | None = None, message: str = "", data: dict | None = None
) -> dict:
    now = utc_now()
    function = call.get("function")
    name = function.get("name") if isinstance(function, dict) else None
    result = ToolResult(
        tool_name=name if isinstance(name, str) else "unknown",
        call_id=call["id"],
        status="error" if code else "ok",
        started_at=now,
        finished_at=now,
        data=data,
        error=ErrorInfo(code=code, message=message) if code else None,
    )
    return {"role": "tool", "tool_call_id": call["id"], "content": result.model_dump_json()}


def _repair(history: list[dict], code: str, message: str) -> list[dict]:
    repaired: list[dict] = []
    pending: dict[str, dict] = {}
    for item in deepcopy(history):
        if item.get("role") != "tool" and pending:
            repaired.extend(
                _tool_result(call, code=code, message=message) for call in pending.values()
            )
            pending.clear()
        repaired.append(item)
        if item.get("role") == "assistant":
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict) and isinstance(call.get("id"), str):
                    pending[call["id"]] = call
        elif item.get("role") == "tool":
            pending.pop(item.get("tool_call_id"), None)
    repaired.extend(_tool_result(call, code=code, message=message) for call in pending.values())
    return repaired


def repair_history(history: list[dict]) -> list[dict]:
    """Return a copy with interrupted tool calls closed, preserving every original message."""
    return _repair(history, "interrupted", "上次执行中断，未获得此工具的结果；可重新决定下一步。")


class AgentRunner:
    def __init__(self, model: ChatModel, registry: ToolRegistry, limits: RunLimits | None = None):
        self.model = model
        self.registry = registry
        self.limits = limits or RunLimits()
        if any(
            value <= 0
            for value in (
                self.limits.max_steps,
                self.limits.max_tool_calls,
                self.limits.max_run_seconds,
            )
        ):
            raise ValueError("Run limits must be positive.")

    async def run(
        self,
        history: list[dict],
        emit: EventEmitter,
        *,
        timezone_name: str = "Asia/Shanghai",
        checkpoint: HistoryCheckpoint | None = None,
    ) -> RunOutcome:
        conversation = repair_history(history)

        async def persist() -> None:
            if checkpoint is not None:
                await checkpoint(deepcopy(conversation))

        async def fail(code: str, message: str, *, retryable: bool = False) -> RunOutcome:
            conversation[:] = _repair(conversation, code, message)
            try:
                await persist()
            except Exception:
                # A storage failure must not erase the in-memory outcome. The service
                # still receives the full history and must handle its failed storage.
                pass
            return RunOutcome(
                "error",
                conversation,
                message,
                {"code": code, "message": message, "retryable": retryable},
            )

        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return await fail(
                "invalid_timezone", "当前时区配置不可用，请检查时区名称和时区数据库。"
            )

        definitions = [deepcopy(ASK_USER_TOOL)] + [
            definition
            for definition in self.registry.model_definitions()
            if definition.get("function", {}).get("name") not in {"search_coaches", "ask_user"}
        ]
        tool_count = 0
        try:
            async with asyncio.timeout(self.limits.max_run_seconds):
                await persist()
                for _ in range(self.limits.max_steps):
                    system = build_system_message(
                        datetime.now(timezone), timezone_name, self.registry.catalog()
                    )
                    messages = [system, *deepcopy(conversation)]
                    reply = await self.model.complete(
                        messages=messages, tools=deepcopy(definitions)
                    )
                    assistant = deepcopy(reply.message)
                    conversation.append(assistant)
                    await persist()
                    if reply.finish_reason not in {"stop", "tool_calls"}:
                        return await fail(
                            "model_incomplete", "模型未完整返回结果，本次执行已停止，历史已保留。"
                        )
                    calls = assistant.get("tool_calls") or []
                    if not calls:
                        if reply.finish_reason == "tool_calls":
                            return await fail(
                                "invalid_tool_call", "模型未返回完整的工具调用，本次执行已停止。"
                            )
                        content = assistant.get("content")
                        if not isinstance(content, str) or not content.strip():
                            return await fail(
                                "empty_response", "模型未返回可展示的回答，请稍后再试。"
                            )
                        await emit(
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": content,
                                "kind": "answer",
                            }
                        )
                        return RunOutcome("completed", conversation, content)
                    if not self._valid_calls(calls):
                        return await fail(
                            "invalid_tool_call", "模型返回的工具调用格式无效，本次执行已停止。"
                        )
                    if tool_count + len(calls) > self.limits.max_tool_calls:
                        return await fail(
                            "tool_limit", "已达到本次工具调用上限；执行记录已完整保留。"
                        )
                    tool_count += len(calls)
                    mixed_question = len(calls) != 1 and any(
                        call["function"]["name"] == "ask_user" for call in calls
                    )
                    for call in calls:
                        name = call["function"]["name"]
                        await emit({"type": "tool_started", "tool_name": name, "status": "running"})
                        if mixed_question:
                            result = _tool_result(
                                call,
                                code="ask_user_must_be_alone",
                                message="ask_user 必须单独调用；本组均未执行，请重新调用。",
                            )
                        else:
                            result = await self._execute(call)
                        conversation.append(result)
                        await persist()
                        payload = json.loads(result["content"])
                        await emit(
                            {
                                "type": "tool_finished",
                                "tool_name": name,
                                "status": payload["status"],
                            }
                        )
                        if name == "ask_user" and payload["status"] == "ok":
                            question = payload["data"]["message"]
                            await emit(
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": question,
                                    "kind": "question",
                                }
                            )
                            return RunOutcome("waiting_user", conversation, question)
                return await fail("step_limit", "已达到本次自主决策轮数上限；执行记录已完整保留。")
        except TimeoutError:
            return await fail(
                "run_timeout", "本次运行已超时，执行记录已保留，可以稍后继续。", retryable=True
            )
        except ModelError as exc:
            return await fail(
                exc.code,
                MODEL_FAILURE_MESSAGES.get(
                    exc.code, "模型服务未能完成本次请求，执行记录已保留，请稍后继续。"
                ),
                retryable=exc.retryable,
            )
        except asyncio.CancelledError:
            return await fail(
                "interrupted", "本次执行意外中断，执行记录已保留，可以稍后继续。", retryable=True
            )
        except Exception:
            return await fail("run_error", "本次执行出现异常，执行记录已保留，请稍后重试。")

    @staticmethod
    def _valid_calls(calls: Any) -> bool:
        if not isinstance(calls, list):
            return False
        ids = set()
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
                return False
            if call["id"] in ids:
                return False
            ids.add(call["id"])
            function = call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                return False
            if not isinstance(function.get("arguments"), str):
                return False
        return True

    async def _execute(self, call: dict) -> dict:
        name = call["function"]["name"]
        if name == "search_coaches":
            return _tool_result(
                call, code="tool_disabled", message="当前版本不支持长途大巴票查询。"
            )
        try:
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError):
            return _tool_result(
                call, code="invalid_arguments", message="工具参数必须是有效的 JSON 对象。"
            )
        if name == "ask_user":
            question = arguments.get("message")
            if (
                set(arguments) != {"message"}
                or not isinstance(question, str)
                or not question.strip()
            ):
                return _tool_result(
                    call, code="invalid_arguments", message="ask_user 需要非空 message。"
                )
            return _tool_result(call, data={"message": question, "state": "waiting_user"})
        try:
            result = await self.registry.dispatch(name, arguments, call_id=call["id"])
            return {"role": "tool", "tool_call_id": call["id"], "content": result.model_dump_json()}
        except Exception:
            return _tool_result(
                call, code="tool_exception", message="工具执行异常；没有获得可用结果。"
            )

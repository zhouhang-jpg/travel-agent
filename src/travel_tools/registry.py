"""Provider-independent dispatch, schemas and explicit availability."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, ValidationError

from travel_tools.common import StrictModel, ToolFailure, ToolPayload, utc_now


class ErrorInfo(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    tool_name: str
    call_id: str
    status: Literal["ok", "error"]
    started_at: AwareDatetime
    finished_at: AwareDatetime
    data: dict[str, Any] | None = None
    error: ErrorInfo | None = None
    content_trust: Literal["data_not_instructions"] = "data_not_instructions"


class DispatchRequest(StrictModel):
    arguments: dict[str, Any]
    call_id: str | None = Field(default=None, min_length=1, max_length=128)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_type: type[StrictModel]
    output_type: type[ToolPayload]
    handler: Callable[[Any], Awaitable[ToolPayload]] | None
    availability: Literal["ready", "not_configured", "not_implemented"]
    reason: str | None = None
    requires_external_service: bool = False


class ToolRegistry:
    def __init__(self, *, timeout_seconds: float = 20, max_concurrent_calls: int = 4):
        self._tools: dict[str, ToolSpec] = {}
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_calls)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        if (spec.availability == "ready") != (spec.handler is not None):
            raise ValueError("Only ready tools must have a handler")
        self._tools[spec.name] = spec

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "availability": spec.availability,
                "reason": spec.reason,
                "requires_external_service": spec.requires_external_service,
                "live_verification": "not_recorded"
                if spec.requires_external_service
                else "not_required",
                "input_schema": spec.input_type.model_json_schema(),
                "output_schema": spec.output_type.model_json_schema(),
            }
            for spec in self._tools.values()
        ]

    def model_definitions(self) -> list[dict[str, Any]]:
        # JSON Schema export for function-calling APIs, without claiming compatibility
        # with any vendor's optional strict-mode JSON Schema subset.
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_type.model_json_schema(),
                },
            }
            for spec in self._tools.values()
            if spec.availability == "ready"
        ]

    async def dispatch(
        self, name: str, arguments: dict[str, Any], *, call_id: str | None = None
    ) -> ToolResult:
        started = utc_now()
        data = None
        error = None
        try:
            spec = self._tools.get(name)
            if not spec:
                raise ToolFailure("unknown_tool", "Tool name is not registered.")
            if spec.availability != "ready" or spec.handler is None:
                raise ToolFailure("tool_unavailable", spec.reason or "Tool is unavailable.")
            try:
                encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise ToolFailure(
                    "invalid_arguments", "Arguments must contain finite JSON data."
                ) from None
            if len(encoded) > 256 * 1024:
                raise ToolFailure("arguments_too_large", "Arguments exceed the 256 KiB limit.")
            try:
                parsed = spec.input_type.model_validate(arguments)
            except ValidationError as exc:
                # Locations + types help correction, without echoing sensitive input.
                details = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}"
                    for e in exc.errors(include_input=False, include_context=False)[:12]
                )
                raise ToolFailure(
                    "invalid_arguments", details or "Input validation failed."
                ) from None
            async with asyncio.timeout(self.timeout_seconds):
                async with self._semaphore:
                    result = await spec.handler(parsed)
                    # Revalidate dictionary data: returning a model of another output
                    # class must never bypass the declared output contract.
                    result = spec.output_type.model_validate(result.model_dump())
                    data = result.model_dump(mode="json")
                    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 2 * 1024 * 1024:
                        data = None
                        raise ToolFailure(
                            "result_too_large", "Tool output exceeds the 2 MiB result limit."
                        )
        except ToolFailure as exc:
            error = ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
        except TimeoutError:
            error = ErrorInfo(
                code="timeout", message="Tool execution deadline exceeded.", retryable=True
            )
        except Exception:
            # Avoid HTTP request URLs, credentials, raw provider bodies and traceback
            # in tool results. Caller cancellation remains a BaseException and propagates.
            error = ErrorInfo(
                code="internal_error", message="Tool failed its execution or output contract."
            )
        return ToolResult(
            tool_name=name,
            call_id=call_id or str(uuid4()),
            status="error" if error else "ok",
            started_at=started,
            finished_at=utc_now(),
            data=data,
            error=error,
        )

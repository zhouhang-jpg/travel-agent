"""Provider-independent dispatch, schemas and explicit availability."""

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, ValidationError

from travel_tools.common import StrictModel, ToolFailure, ToolPayload, utc_now
from travel_tools.scheduling import (
    SupplierScheduler,
    current_scheduler,
    force_refresh,
    supplier_progress,
)


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
    reuse: Literal["none", "cache"] = "none"
    original_started_at: AwareDatetime | None = None
    original_finished_at: AwareDatetime | None = None


class DispatchRequest(StrictModel):
    arguments: dict[str, Any]
    call_id: str | None = Field(default=None, min_length=1, max_length=128)
    force_refresh: bool = False


def _model_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove display titles only; keep every validation/default/description rule."""
    result = dict(schema)
    result.pop("title", None)
    for keyword in ("properties", "$defs", "patternProperties", "dependentSchemas"):
        if isinstance(result.get(keyword), dict):
            result[keyword] = {key: _model_schema(value) for key, value in result[keyword].items()}
    for keyword in ("items", "additionalProperties", "not", "contains", "if", "then", "else"):
        if isinstance(result.get(keyword), dict):
            result[keyword] = _model_schema(result[keyword])
    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(result.get(keyword), list):
            result[keyword] = [_model_schema(value) for value in result[keyword]]
    return result


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
    execution: Literal["exclusive", "parallel_read"] = "exclusive"
    cache_seconds: float = 0


class ToolRegistry:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20,
        max_concurrent_calls: int = 4,
        scheduler: SupplierScheduler | None = None,
        cache_scope: str = "local",
    ):
        self._tools: dict[str, ToolSpec] = {}
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_calls)
        self.max_concurrent_calls = max_concurrent_calls
        self.scheduler = scheduler or SupplierScheduler()
        self.cache_scope = cache_scope
        self._cache = OrderedDict()
        self._closed = False
        self._closers: list[Callable[[], Awaitable[None]]] = []

    def add_closer(self, closer: Callable[[], Awaitable[None]]) -> None:
        self._closers.append(closer)

    async def close(self) -> None:
        self._closed = True
        self._cache.clear()
        for closer in self._closers:
            await closer()
        self._closers.clear()

    def parallel_safe(self, name: str) -> bool:
        spec = self._tools.get(name)
        return bool(spec and spec.execution == "parallel_read")

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
                "execution": spec.execution,
                "cache_seconds": spec.cache_seconds,
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
                    "parameters": self.input_schema(spec),
                },
            }
            for spec in self._tools.values()
            if spec.availability == "ready"
        ]

    @staticmethod
    def input_schema(spec):
        schema = _model_schema(spec.input_type.model_json_schema())
        if spec.execution == "parallel_read" and spec.requires_external_service:
            schema["properties"]["force_refresh"] = {
                "type": "boolean",
                "default": False,
                "description": "Bypass cached query data when current verification is needed.",
            }
        return schema

    async def dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        call_id: str | None = None,
        progress=None,
        refresh: bool = False,
    ) -> ToolResult:
        started = utc_now()
        data = None
        error = None
        reuse = "none"
        original_started = original_finished = None
        progress_failure = None

        async def report(stage, **detail):
            nonlocal progress_failure
            if progress:
                try:
                    await progress(stage, **detail)
                except Exception as exc:
                    progress_failure = exc
                    raise

        try:
            if self._closed:
                raise ToolFailure("tool_unavailable", "Tool registry is closed.")
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
                normalized = dict(arguments)
                if spec.execution == "parallel_read" and spec.requires_external_service:
                    flag = normalized.pop("force_refresh", False)
                    if not isinstance(flag, bool):
                        raise ToolFailure("invalid_arguments", "force_refresh must be boolean.")
                    refresh = refresh or flag
                parsed = spec.input_type.model_validate(normalized)
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
                cache_key = sha256(
                    json.dumps(
                        [self.cache_scope, name, parsed.model_dump(mode="json")],
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                cached = self._cache.get(cache_key)
                if not refresh and cached and time.monotonic() < cached[0]:
                    data, original_started, original_finished = deepcopy(cached[1:])
                    reuse = "cache"
                    await report("reused")
                else:
                    await report("queued")
                    async with self._semaphore:
                        await report("running")
                        token = current_scheduler.set(self.scheduler)
                        refresh_token = force_refresh.set(refresh)

                        async def report_supplier(stage, supplier, request_id):
                            await report(
                                "supplier_" + stage, supplier=supplier, request_id=request_id
                            )

                        progress_token = supplier_progress.set(report_supplier)
                        try:
                            result = await spec.handler(parsed)
                        finally:
                            force_refresh.reset(refresh_token)
                            current_scheduler.reset(token)
                            supplier_progress.reset(progress_token)
                        result = spec.output_type.model_validate(result.model_dump())
                        data = result.model_dump(mode="json")
                        if (
                            len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                            > 2 * 1024 * 1024
                        ):
                            data = None
                            raise ToolFailure(
                                "result_too_large", "Tool output exceeds the 2 MiB result limit."
                            )
                        if spec.cache_seconds > 0 and _cacheable(data) and not self._closed:
                            self._cache[cache_key] = (
                                time.monotonic() + spec.cache_seconds,
                                deepcopy(data),
                                started,
                                utc_now(),
                            )
                            self._cache.move_to_end(cache_key)
                            while len(self._cache) > 256:
                                self._cache.popitem(last=False)
        except ToolFailure as exc:
            if progress_failure is not None:
                raise
            error = ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
        except TimeoutError:
            error = ErrorInfo(
                code="timeout", message="Tool execution deadline exceeded.", retryable=True
            )
        except Exception:
            if progress_failure is not None:
                raise
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
            reuse=reuse,
            original_started_at=original_started,
            original_finished_at=original_finished,
        )


def _cacheable(value):
    """Partial source failures/fallbacks are retried, never cached as empty inventory."""
    if isinstance(value, dict):
        if value.get("status") in {"error", "not_configured", "unavailable"}:
            return False
        if value.get("error_code") or value.get("fallback_from") or value.get("fallback_reason"):
            return False
        return all(_cacheable(child) for child in value.values())
    if isinstance(value, list):
        return all(_cacheable(child) for child in value)
    return True

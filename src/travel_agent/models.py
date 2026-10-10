"""A non-streaming model protocol and OpenAI-compatible HTTP adapter.

Assistant messages are opaque history records after basic structural validation.
In particular, DeepSeek reasoning_content must survive subsequent tool turns.
The API/UI layer is responsible for exposing only public message fields.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


class ModelError(Exception):
    """Safe model failure; neither message nor cause contains provider payloads."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ModelReply:
    message: dict[str, Any]
    finish_reason: str
    usage: dict[str, Any]


class ChatModel(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


def _endpoint(base_url: str) -> str:
    try:
        if not isinstance(base_url, str) or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in base_url
        ):
            raise ValueError
        if any(character in base_url for character in ("?", "#", "\\")):
            raise ValueError
        parsed = urlsplit(base_url)
        # Accessing port also validates its spelling and range.
        _ = parsed.port
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        localhost = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
            raise ValueError
        return str(httpx.URL(base_url.rstrip("/") + "/chat/completions"))
    except (TypeError, ValueError, httpx.InvalidURL):
        raise ModelError(
            "configuration_error",
            "Model base URL must use HTTPS, or HTTP on localhost, without credentials or a query.",
        ) from None


def _invalid_response() -> ModelError:
    return ModelError("invalid_response", "The model returned an invalid response.")


def _reject_json_constant(value: str) -> None:
    raise ValueError("Invalid JSON numeric constant")


def _parse_reply(body: bytes) -> ModelReply:
    try:
        payload = json.loads(body, parse_constant=_reject_json_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise _invalid_response() from None
    if not isinstance(payload, dict):
        raise _invalid_response()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _invalid_response()
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise ModelError(
            "output_truncated", "The model output reached its token limit and is incomplete."
        )
    if not isinstance(finish_reason, str) or finish_reason not in {"stop", "tool_calls"}:
        raise _invalid_response()
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _invalid_response()
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise _invalid_response()
    reasoning = message.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise _invalid_response()
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise _invalid_response()
    call_ids: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise _invalid_response()
        call_id = call.get("id")
        function = call.get("function")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or call_id in call_ids
            or not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"].strip()
            or not isinstance(function.get("arguments"), str)
        ):
            raise _invalid_response()
        # Parsing arguments and schema validation belong to the tool executor,
        # which can return a recoverable tool error to the model.
        call_ids.add(call_id)
    if finish_reason == "tool_calls" and not calls:
        raise _invalid_response()
    if not calls and (content is None or not content.strip()):
        raise _invalid_response()
    raw_usage = payload.get("usage")
    usage = {}
    if isinstance(raw_usage, dict):
        usage = {
            key: value
            for key, value in raw_usage.items()
            if (type(value) is int or (type(value) is float and math.isfinite(value)))
        }
    return ModelReply(message=message, finish_reason=finish_reason, usage=usage)


class OpenAICompatibleModel:
    """Send one request per call; the caller owns the injected HTTP client.

    base_url is the API root, for example https://api.deepseek.com or a
    compatible provider's https://example.com/v1. No redirects are followed.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        client: httpx.AsyncClient,
        provider: str = "deepseek",
        thinking: bool = True,
        reasoning_effort: str = "high",
        timeout_seconds: float = 120,
    ) -> None:
        self._endpoint = _endpoint(base_url)
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or not isinstance(model, str)
            or not model.strip()
            or not isinstance(provider, str)
            or provider not in {"deepseek", "openai_compatible"}
            or not isinstance(thinking, bool)
            or not isinstance(reasoning_effort, str)
            or not reasoning_effort.strip()
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ModelError("configuration_error", "Model configuration is invalid.")
        self._api_key = api_key
        self.model = model
        self.client = client
        self.provider = provider
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
        }
        if self.provider == "deepseek":
            payload["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
            payload["reasoning_effort"] = self.reasoning_effort
        try:
            # In addition to per-operation HTTP timeouts, bound the whole call
            # so a slowly trickling response cannot keep a run alive forever.
            async with asyncio.timeout(self.timeout_seconds):
                async with self.client.stream(
                    "POST",
                    self._endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    self._check_status(response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        body.extend(chunk)
            return _parse_reply(bytes(body))
        except ModelError:
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise ModelError("timeout", "The model request timed out.", retryable=True) from None
        except httpx.RequestError:
            raise ModelError(
                "connection_error", "The model service could not be reached.", retryable=True
            ) from None
        except (ValueError, TypeError, OverflowError, RecursionError, httpx.InvalidURL):
            raise ModelError(
                "request_failed", "The model request could not be processed."
            ) from None

    @staticmethod
    def _check_status(status: int) -> None:
        if 200 <= status < 300:
            return
        if status in {401, 403}:
            raise ModelError("authentication_error", "Model authentication or access was rejected.")
        if status == 402:
            raise ModelError(
                "insufficient_balance", "Model account balance or payment is unavailable."
            )
        if status == 429:
            raise ModelError("rate_limited", "The model service rate limit was reached.", True)
        if status >= 500:
            raise ModelError("service_unavailable", "The model service is unavailable.", True)
        raise ModelError("request_failed", "The model service rejected the request.")

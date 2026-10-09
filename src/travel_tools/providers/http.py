"""Bounded JSON transport shared by credentialed provider adapters."""

import asyncio
import json
from typing import Any

import httpx

from travel_tools.common import ToolFailure

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15


async def bounded_json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One request, bounded decoded body and deadline, no redirects or raw errors."""
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            async with client.stream(
                method,
                url,
                headers=headers,
                params=params,
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    code = (
                        "provider_authentication"
                        if status in (401, 403)
                        else "provider_rate_limited"
                        if status == 429
                        else "provider_http_error"
                    )
                    raise ToolFailure(
                        code,
                        f"{provider} request failed (HTTP {status}).",
                        retryable=status == 429 or status >= 500,
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ToolFailure(
                            "provider_response_too_large",
                            f"{provider} response exceeds size limit.",
                        )
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("Expected JSON object")
                return result
    except (TimeoutError, httpx.TimeoutException):
        raise ToolFailure("provider_timeout", f"{provider} request timed out.", True) from None
    except httpx.HTTPError:
        raise ToolFailure("provider_network_error", f"{provider} request failed.", True) from None
    except (ValueError, UnicodeError):
        raise ToolFailure(
            "provider_invalid_response", f"{provider} returned an invalid JSON response."
        ) from None

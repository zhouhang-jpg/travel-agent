import asyncio
import json

import httpx
import pytest

from travel_agent.models import MAX_RESPONSE_BYTES, ModelError, OpenAICompatibleModel


def response_payload(message=None, finish_reason="stop", usage=None):
    return {
        "choices": [
            {
                "message": message
                if message is not None
                else {"role": "assistant", "content": "好"},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage if usage is not None else {},
    }


def make_model(client, **kwargs):
    values = {
        "api_key": "test-secret-not-a-real-key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "client": client,
    }
    values.update(kwargs)
    return OpenAICompatibleModel(**values)


async def test_preserves_complete_assistant_history_across_tools_and_user_turns():
    requests = []
    tool_message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "private reasoning before tool",
        "provider_extension": {"opaque": "retain-me"},
        "tool_calls": [
            {
                "id": "call_one",
                "type": "function",
                "function": {"name": "search_places", "arguments": '{"city":"杭州"}'},
            }
        ],
    }
    final_message = {
        "role": "assistant",
        "content": "可以去西湖散步。",
        "reasoning_content": "private reasoning after tool",
    }
    responses = [
        response_payload(tool_message, "tool_calls"),
        response_payload(final_message),
        response_payload(),
    ]

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    messages = [{"role": "user", "content": "杭州哪里可以散步？"}]
    tools = [{"type": "function", "function": {"name": "search_places", "parameters": {}}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = make_model(client)
        first = await model.complete(messages, tools)
        assert first.message == tool_message
        messages.extend(
            [first.message, {"role": "tool", "tool_call_id": "call_one", "content": "西湖"}]
        )
        second = await model.complete(messages, tools)
        messages.extend([second.message, {"role": "user", "content": "下雨怎么办？"}])
        await model.complete(messages, tools)

    assert requests[1]["messages"][1] == tool_message
    assert requests[2]["messages"] == messages
    assert requests[2]["messages"][3] == final_message
    assert requests[2]["tools"] == tools
    assert requests[0]["stream"] is False
    assert requests[0]["model"] == "deepseek-flash"


@pytest.mark.parametrize("thinking", [True, False])
async def test_deepseek_parameters_and_numeric_usage(thinking):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            json=response_payload(
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cost": 0.25,
                    "details": {"cached": 5},
                    "label": "secret",
                    "flag": True,
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await make_model(client, thinking=thinking).complete([], [])
    body = json.loads(captured[0].content)
    assert body["thinking"] == {"type": "enabled" if thinking else "disabled"}
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 16384
    assert str(captured[0].url) == "https://api.deepseek.com/chat/completions"
    assert captured[0].headers["authorization"] == "Bearer test-secret-not-a-real-key"
    assert reply.usage == {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.25}


async def test_generic_provider_does_not_receive_deepseek_parameters():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await make_model(
            client, provider="openai_compatible", base_url="https://example.com/v1/"
        ).complete([], [])
    body = json.loads(captured[0].content)
    assert "thinking" not in body
    assert "reasoning_effort" not in body
    assert str(captured[0].url) == "https://example.com/v1/chat/completions"


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "authentication_error", False),
        (403, "authentication_error", False),
        (429, "rate_limited", True),
        (503, "service_unavailable", True),
        (400, "request_failed", False),
        (307, "request_failed", False),
    ],
)
async def test_status_failures_are_safe_and_redirects_are_not_followed(status, code, retryable):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="private-provider-body test-secret-not-a-real-key",
            headers={"location": "https://redirect.invalid/private-endpoint"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client, base_url="https://example.com/private-endpoint").complete(
                [], []
            )
    assert len(calls) == 1
    assert error.value.code == code
    assert error.value.retryable is retryable
    for secret in ("private-provider-body", "test-secret", "private-endpoint", "https://"):
        assert secret not in str(error.value)


@pytest.mark.parametrize(
    ("exception_type", "code"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "connection_error")],
)
async def test_transport_failures_hide_original_error(exception_type, code):
    def handler(request):
        raise exception_type("private URL https://secret.invalid/key", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client).complete([], [])
    assert error.value.code == code
    assert error.value.retryable is True
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"choices": []},
        {"choices": [None]},
        response_payload({"role": "user", "content": "hello"}),
        response_payload({"role": "assistant", "content": "  "}),
        response_payload({"role": "assistant", "content": None, "reasoning_content": "private"}),
        response_payload({"role": "assistant", "content": []}),
        response_payload({"role": "assistant", "content": "hi", "reasoning_content": {}}),
        response_payload({"role": "assistant", "content": "hi", "tool_calls": {}}),
        response_payload({"role": "assistant", "content": "hi"}, "tool_calls"),
        response_payload(finish_reason=[]),
        response_payload(
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "x", "type": "function", "function": {"name": "search", "arguments": {}}}
                ],
            },
            "tool_calls",
        ),
    ],
)
async def test_rejects_invalid_reply_shapes(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client).complete([], [])
    assert error.value.code == "invalid_response"


async def test_rejects_duplicate_tool_call_ids():
    call = {"id": "same", "type": "function", "function": {"name": "x", "arguments": "{}"}}
    payload = response_payload({"role": "assistant", "tool_calls": [call, call]}, "tool_calls")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client).complete([], [])
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("body", [b"private invalid response", b'{"usage": NaN}', b"\xff"])
async def test_invalid_json_is_sanitized(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client).complete([], [])
    assert error.value.code == "invalid_response"
    assert "private" not in str(error.value)


async def test_truncated_output_is_not_a_successful_partial_answer():
    payload = response_payload({"role": "assistant", "content": "unfinished itinerary"}, "length")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client).complete([], [])
    assert error.value.code == "output_truncated"
    assert error.value.retryable is False


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, delay=0):
        self.chunks_read = 0
        self.closed = False
        self.delay = delay

    async def __aiter__(self):
        for _ in range(100):
            if self.delay:
                await asyncio.sleep(self.delay)
            self.chunks_read += 1
            yield b"x" * (64 * 1024)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("advertise_length", [True, False])
async def test_oversized_response_stops_reading_and_closes_stream(advertise_length):
    stream = CountingStream()
    headers = {"content-length": str(MAX_RESPONSE_BYTES + 1)} if advertise_length else {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers=headers)
        )
    ) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client).complete([], [])
    assert error.value.code == "response_too_large"
    assert stream.chunks_read == (0 if advertise_length else MAX_RESPONSE_BYTES // (64 * 1024) + 1)
    assert stream.closed


async def test_whole_request_deadline_bounds_slow_stream():
    stream = CountingStream(delay=0.1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(ModelError) as error:
            await make_model(client, timeout_seconds=0.01).complete([], [])
    assert error.value.code == "timeout"
    assert stream.chunks_read == 0
    assert stream.closed


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://@example.com",
        "https://example.com?key=secret",
        "https://example.com#secret",
        "https://example.com?",
        "ftp://example.com",
        "https:///path",
        "https://example.com:bad",
        "https://example.com\n",
        "https://example.com\\secret",
        "https://exa\x00mple.com",
    ],
)
async def test_rejects_unsafe_base_url_without_exposing_it(base_url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(ModelError) as error:
            make_model(client, base_url=base_url)
    assert error.value.code == "configuration_error"
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "base_url", ["http://localhost:8080/v1", "http://127.0.0.1:8080", "http://[::1]:8080"]
)
async def test_allows_local_http_for_development(base_url):
    async with httpx.AsyncClient() as client:
        make_model(client, base_url=base_url)

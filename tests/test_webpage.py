import asyncio

import httpx
import pytest

from travel_tools.common import ToolFailure
from travel_tools.schemas.webpage import FetchWebpageInput
from travel_tools.webpage import (
    MAX_BYTES,
    WebpageFetcher,
    checked_url,
    extract_html,
    is_public_address,
)


async def public_resolver(host, port):
    return ["93.184.215.14"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://localhost/",
        "http://service.local/",
        "http://example.com:8080/",
        "https://u:p@example.com/",
        "http://example.com\\@127.0.0.1/",
        "https://example.com/\nheader",
        "http://[fe80::1%25lo0]/",
    ],
)
def test_rejects_unsafe_url(url):
    with pytest.raises(ToolFailure, match="public HTTP"):
        checked_url(url)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "224.0.0.1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "192.0.2.1",
        "not-an-ip",
    ],
)
def test_rejects_nonpublic_ip(ip):
    assert not is_public_address(ip)


async def test_pins_ip_host_and_sni_and_extracts_untrusted_html():
    def respond(req):
        assert req.url.host == "93.184.215.14"
        assert req.headers["host"] == "example.org"
        assert req.extensions["sni_hostname"] == "example.org"
        assert req.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<title>Example</title><script>bad()</script><p>公开正文</p><p hidden>secret</p>",
        )

    fetcher = WebpageFetcher(resolver=public_resolver, transport=httpx.MockTransport(respond))
    result = await fetcher.fetch_webpage(FetchWebpageInput(url="https://example.org"))
    assert result.title == "Example"
    assert "公开正文" in result.text
    assert "bad" not in result.text and "secret" not in result.text
    assert result.content_trust == "untrusted_external_data"
    assert result.sources[0].url == "https://example.org/"


async def test_mixed_dns_answer_is_blocked_before_network():
    async def resolver(host, port):
        return ["93.184.215.14", "10.0.0.1"]

    def no_network(req):
        pytest.fail("Unsafe DNS must never be requested")

    with pytest.raises(ToolFailure) as exc:
        await WebpageFetcher(
            resolver=resolver, transport=httpx.MockTransport(no_network)
        ).fetch_webpage(FetchWebpageInput(url="https://example.org"))
    assert exc.value.code == "unsafe_address"


async def test_redirect_is_revalidated_and_private_target_blocked():
    async def resolver(host, port):
        return ["127.0.0.1"] if host == "127.0.0.1" else ["93.184.215.14"]

    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/admin"})

    with pytest.raises(ToolFailure) as exc:
        await WebpageFetcher(
            resolver=resolver, transport=httpx.MockTransport(respond)
        ).fetch_webpage(FetchWebpageInput(url="https://example.org"))
    assert exc.value.code == "unsafe_address"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "headers,body,code",
    [
        ({"content-type": "application/pdf"}, b"pdf", "unsupported_content"),
        (
            {"content-type": "text/html", "content-length": str(MAX_BYTES + 1)},
            b"",
            "response_too_large",
        ),
        ({"content-type": "text/plain"}, b"x" * (MAX_BYTES + 1), "response_too_large"),
        ({"content-type": "text/html", "content-encoding": "br"}, b"", "unsupported_encoding"),
    ],
    ids=["content-type", "declared-size", "stream-size", "compression"],
)
async def test_content_guards(headers, body, code):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, headers=headers, content=body))
    with pytest.raises(ToolFailure) as exc:
        await WebpageFetcher(resolver=public_resolver, transport=transport).fetch_webpage(
            FetchWebpageInput(url="https://example.org")
        )
    assert exc.value.code == code


async def test_redirect_limit_and_downgrade():
    for location, code in [
        ("/again", "too_many_redirects"),
        ("http://example.org", "unsafe_redirect"),
    ]:
        transport = httpx.MockTransport(
            lambda req, location=location: httpx.Response(302, headers={"location": location})
        )
        with pytest.raises(ToolFailure) as exc:
            await WebpageFetcher(resolver=public_resolver, transport=transport).fetch_webpage(
                FetchWebpageInput(url="https://example.org")
            )
        assert exc.value.code == code


async def test_deadline_includes_dns_resolution():
    async def slow(host, port):
        await asyncio.sleep(1)
        return ["93.184.215.14"]

    with pytest.raises(ToolFailure) as exc:
        await WebpageFetcher(resolver=slow, timeout_seconds=0.01).fetch_webpage(
            FetchWebpageInput(url="https://example.org")
        )
    assert exc.value.code == "timeout"


async def test_explicit_text_limit():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 120)
    )
    result = await WebpageFetcher(resolver=public_resolver, transport=transport).fetch_webpage(
        FetchWebpageInput(url="https://example.org", max_characters=100)
    )
    assert result.truncated and len(result.text) == 100 and result.body_bytes == 120


async def test_title_cap_and_ignored_content_across_chunks():
    html = "<title>" + "t" * 60000 + "</title><script>" + "s" * 16000 + "</script><p>Visible</p>"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text=html)
    )
    result = await WebpageFetcher(resolver=public_resolver, transport=transport).fetch_webpage(
        FetchWebpageInput(url="https://example.org", max_characters=100)
    )
    assert result.title_truncated and len(result.title) == 512 and result.truncated
    title, text, clipped = await extract_html("<input hidden><p>Keep</p><div hidden>Drop</div>")
    assert "Keep" in text and "Drop" not in text and not clipped


async def test_html_parser_yields_to_execution_deadline():
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await extract_html("<p>content</p>" * 10000)


@pytest.mark.parametrize(
    "html", ["<div>" * 300, "<script>" + "x" * 80000], ids=["nesting", "unfinished-token"]
)
async def test_html_complexity_is_bounded(html):
    with pytest.raises(ToolFailure) as exc:
        await extract_html(html)
    assert exc.value.code == "html_too_complex"

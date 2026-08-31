"""Provider adapter tests: request contract + response normalization + error
mapping, for every provider (Tavily, Brave, EXA, You.com).

Pure logic, no network / no keys / no DB: the HTTP client is an
httpx.MockTransport that records the outgoing request and returns a canned
response (or raises), so we can assert exactly what each adapter sends and
how it normalizes the reply — including the status-code -> ProviderError
mapping the gateway relies on for failover.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from wellisearch.config import Settings
from wellisearch.providers.base import Provider, ProviderError, Result
from wellisearch.providers.brave import Brave
from wellisearch.providers.exa import Exa
from wellisearch.providers.tavily import Tavily
from wellisearch.providers.youcom import YouCom

QUERY = "query here"


def make_client(
    status: int = 200,
    payload: dict | None = None,
    text: str = "",
    raise_exc: Exception | None = None,
) -> tuple[httpx.AsyncClient, dict]:
    """Build an AsyncClient backed by a MockTransport.

    Returns (client, captured) where captured records the last request's
    method, url, query params, headers (lower-cased) and decoded body.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """MockTransport handler: raise the configured exception, or record the
        request and return the canned response."""
        if raise_exc is not None:
            raise raise_exc
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = request.content.decode() if request.content else None
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=text)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, captured


async def run_search(
    cls: type[Provider],
    settings: Settings,
    status: int = 200,
    payload: dict | None = None,
    text: str = "",
    num: int = 3,
    query: str = QUERY,
) -> tuple[Provider, list[Result], dict]:
    """Run one provider search against a mock client; return (provider,
    results, captured request)."""
    client, captured = make_client(status=status, payload=payload, text=text)
    p = cls(settings, client)
    results = await p.search(query, num)
    return p, results, captured


async def expect_provider_error(
    cls: type[Provider],
    settings: Settings,
    status: int,
    msg_substr: str,
) -> None:
    """Assert a given HTTP status maps to a ProviderError with the expected
    message and status."""
    client, _ = make_client(status=status, text="boom")
    p = cls(settings, client)
    try:
        await p.search(QUERY, 3)
    except ProviderError as e:
        assert msg_substr in str(e), f"{p.name}: {str(e)!r} missing {msg_substr!r}"
        assert e.status == status, f"{p.name}: status {e.status} != {status}"
        assert e.provider == p.name
        return
    raise AssertionError(f"{p.name}: expected ProviderError for http {status}")


async def expect_network_error(cls: type[Provider], settings: Settings) -> None:
    """Assert a network failure maps to a ProviderError with a 'network' message."""
    client, _ = make_client(raise_exc=httpx.ConnectError("no route to host"))
    p = cls(settings, client)
    try:
        await p.search(QUERY, 3)
    except ProviderError as e:
        assert "network" in str(e), f"{p.name}: {str(e)!r} missing 'network'"
        return
    raise AssertionError(f"{p.name}: expected network ProviderError")


# ---------------------------------------------------------------------------
# Base Helpers (shared normalization point)
# ---------------------------------------------------------------------------

def test_base() -> None:
    """Provider.clean_html + Provider.snippet (the shared normalization point)."""
    assert Provider.clean_html("<b>Title</b> &amp; more") == "Title & more"
    assert Provider.clean_html("  a   b  ") == "a b"
    assert Provider.clean_html("") == ""
    s = Provider.snippet("word " * 500)
    assert len(s) <= 401 and s.endswith("…"), (len(s), s[-5:])
    assert Provider.snippet("short") == "short"
    print("OK base.clean_html / base.snippet")


async def test_error_mapping(cls: type[Provider], settings: Settings) -> None:
    """Status-code + network error mapping, shared by every provider."""
    for status, msg in [(401, "auth rejected"), (403, "auth rejected"),
                        (402, "quota exhausted"), (429, "quota exhausted"),
                        (500, "http 500")]:
        await expect_provider_error(cls, settings, status, msg)
    await expect_network_error(cls, settings)


# ---------------------------------------------------------------------------
# Per-Provider Tests
# ---------------------------------------------------------------------------
# One function per adapter: configured flag, request contract, response
# normalization, and the ProviderError mapping the gateway relies on.

async def test_tavily() -> None:
    """Tavily: POST, Bearer auth, `results[]`, carries score."""
    s = Settings(TAVILY_API_KEY="tav-key-123")
    assert Tavily(s, None).configured is True
    assert Tavily(Settings(TAVILY_API_KEY=""), None).configured is False

    payload = {
        "query": QUERY,
        "results": [
            {
                "url": "https://a.com/1",
                "title": "<b>Title A</b>",
                "content": "Snippet A content",
                "score": 0.91,
            },
            {"url": "https://a.com/2", "title": "Title B", "content": "Snippet B", "score": 0.8},
            {"title": "no url -> skipped", "content": "x", "score": 0.5},
        ],
    }
    p, results, cap = await run_search(Tavily, s, 200, payload)
    assert cap["method"] == "POST"
    assert cap["url"] == "https://api.tavily.com/search"
    assert cap["headers"]["authorization"] == "Bearer tav-key-123"
    assert json.loads(cap["body"]) == {"query": QUERY, "max_results": 3}
    assert len(results) == 2, "empty-url item must be skipped"
    assert results[0].url == "https://a.com/1"
    assert results[0].title == "Title A"  # <b> cleaned
    assert results[0].snippet == "Snippet A content"
    assert results[0].score == 0.91  # tavily is the only provider that carries a score
    # max(1, num) floor: num=0 -> max_results=1
    _, _, cap0 = await run_search(Tavily, s, 200, {"results": []}, num=0)
    assert json.loads(cap0["body"])["max_results"] == 1
    print("OK tavily contract + normalization")

    await test_error_mapping(Tavily, s)
    print("OK tavily error mapping (401/403/402/429/500 + network)")


async def test_brave() -> None:
    """Brave: GET, X-Subscription-Token, `web.results[]`, no score."""
    s = Settings(BRAVE_API_KEY="brave-key-123")
    assert Brave(s, None).configured is True
    assert Brave(Settings(BRAVE_API_KEY=""), None).configured is False

    long_desc = ("word " * 300).strip()
    payload = {
        "type": "search",
        "web": {"results": [
            {"url": "https://b.com/1", "title": "Brave One", "description": "Desc <strong>one</strong>"},
            {"url": "https://b.com/2", "title": "Brave Two", "description": long_desc},
            {"title": "no url -> skipped", "description": "x"},
        ]},
    }
    p, results, cap = await run_search(Brave, s, 200, payload)
    assert cap["method"] == "GET"
    assert cap["url"].startswith("https://api.search.brave.com/res/v1/web/search")
    assert cap["params"] == {"q": QUERY, "count": "3"}
    assert cap["headers"]["x-subscription-token"] == "brave-key-123"
    assert cap["body"] is None  # GET: query lives in params, not body
    assert len(results) == 2
    assert results[0].title == "Brave One"
    assert results[0].snippet == "Desc one"  # <strong> cleaned
    assert results[0].score is None
    assert results[1].snippet.endswith("…") and len(results[1].snippet) <= 401  # trimmed
    print("OK brave contract + normalization")

    await test_error_mapping(Brave, s)
    print("OK brave error mapping (401/403/402/429/500 + network)")


async def test_exa() -> None:
    """EXA: POST, x-api-key, `results[]`, asks for page text."""
    s = Settings(EXA_API_KEY="exa-key-123")
    assert Exa(s, None).configured is True
    assert Exa(Settings(EXA_API_KEY=""), None).configured is False

    payload = {
        "requestId": "r1",
        "results": [
            {"id": "1", "url": "https://e.com/1", "title": "Exa One", "text": "Exa text one"},
            {"id": "2", "url": "https://e.com/2", "title": "Exa Two"},  # no text -> empty snippet
            {"id": "3", "title": "no url -> skipped"},
        ],
    }
    p, results, cap = await run_search(Exa, s, 200, payload)
    assert cap["method"] == "POST"
    assert cap["url"] == "https://api.exa.ai/search"
    assert cap["headers"]["x-api-key"] == "exa-key-123"
    assert json.loads(cap["body"]) == {
        "query": QUERY, "numResults": 3, "contents": {"text": True, "maxChars": 800},
    }
    assert len(results) == 2
    assert results[0].snippet == "Exa text one"
    assert results[1].snippet == ""  # missing text -> empty, not None
    assert results[0].score is None
    print("OK exa contract + normalization")

    await test_error_mapping(Exa, s)
    print("OK exa error mapping (401/403/402/429/500 + network)")


async def test_youcom() -> None:
    """You.com: POST, X-API-Key, `results.web[]`."""
    s = Settings(YOUCOM_API_KEY="you-key-123")
    assert YouCom(s, None).configured is True
    assert YouCom(Settings(YOUCOM_API_KEY=""), None).configured is False

    payload = {
        "results": {"web": [
            {
                "url": "https://y.com/1",
                "title": "You One",
                "description": "You desc one",
                "favicon_url": "f",
                "snippets": [],
            },
            {"url": "https://y.com/2", "title": "You Two", "description": "You desc two"},
            {"title": "no url -> skipped", "description": "x"},
        ]},
        "metadata": {},
    }
    p, results, cap = await run_search(YouCom, s, 200, payload)
    assert cap["method"] == "POST"
    assert cap["url"] == "https://api.you.com/v1/search"
    assert cap["headers"]["x-api-key"] == "you-key-123"
    assert json.loads(cap["body"]) == {"query": QUERY, "count": 3}
    assert len(results) == 2
    assert results[0].snippet == "You desc one"
    assert results[0].score is None
    print("OK youcom contract + normalization")

    await test_error_mapping(YouCom, s)
    print("OK youcom error mapping (401/403/402/429/500 + network)")


async def main() -> None:
    """Run all provider adapter tests (base helpers + each provider)."""
    test_base()
    await test_tavily()
    await test_brave()
    await test_exa()
    await test_youcom()
    print("ALL PROVIDER TESTS PASSED")


asyncio.run(main())

"""Unit tests: neo tier (BrowserOS neo over MCP) — executor-injected, no network."""
from __future__ import annotations

import asyncio
import json

from wellisearch.config import get_settings
from wellisearch.crawl import tiers
from wellisearch.crawl.botwall import is_botwall
from wellisearch.crawl.policy import match
from wellisearch.crawl.results import Rendered
import wellisearch.crawl.tiers.neo as neo

s = get_settings()
# Fast polling for the challenge tests (real values are irrelevant here).
s.CRAWL_NEO_POLL_MS = 10
s.CRAWL_NEO_CHALLENGE_BUDGET_S = 0.2  # seconds — small so the poll tests finish fast
neo.NEO_RECONNECT_DELAY_S = 0

WALL_HTML = '<html><body>Just a moment...<div class="cf-turnstile"></div></body></html>'


def _payload(
    pid: int = 7,
    html: str = "<html><body>real content</body></html>",
    title: str | None = "T",
    status: int | None = 200,
    ct: str | None = "text/html",
) -> str:
    """A run-script payload as the tier receives it (a JSON string)."""
    return json.dumps({"pid": pid, "html": html, "title": title, "status": status, "ct": ct})


def _tier_with(executor) -> neo.NeoTier:
    """A NeoTier whose _run seam is replaced by a fake executor."""
    tier = neo.NeoTier()
    tier._run = executor  # type: ignore[method-assign]
    return tier


# ---------------------------------------------------------------------------
# Clean page
# ---------------------------------------------------------------------------

async def t_clean() -> None:
    calls: list[str] = []

    async def fake(script: str) -> str:
        calls.append(script)
        return _payload()

    r = await _tier_with(fake).fetch("https://example.com/x", match("https://example.com/x"))
    assert isinstance(r, Rendered) and r.engine == "neo"
    assert r.html.startswith("<html>") and r.title == "T" and r.status == 200
    # One load (tab left open) + one explicit close.
    assert len(calls) == 2
    for token in ("newPage", "evaluate"):
        assert token in calls[0], token
    # The load returns the pid (tab left open); a separate call closes it.
    assert "{pid: pid" in calls[0] and '"https://example.com/x"' in calls[0]
    assert "pages.close(7)" in calls[1]

asyncio.run(t_clean())
print("OK clean page")

# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

assert tiers.by_name("neo") is None  # CRAWL_NEO_TIER defaults to False
s.CRAWL_NEO_TIER = True
t = tiers.by_name("neo")
assert t is not None and t.name == "neo"
s.CRAWL_NEO_TIER = False
print("OK gating")

# ---------------------------------------------------------------------------
# Neo down / error mapping
# ---------------------------------------------------------------------------

async def t_down() -> None:
    async def fake(script: str) -> str:
        raise neo.NeoNotConnectedError("browser session not connected")

    try:
        await _tier_with(fake).fetch("https://example.com/x", match("https://example.com/x"))
        assert False, "expected NeoNotConnectedError"
    except neo.NeoNotConnectedError as e:
        assert "not connected" in str(e)

asyncio.run(t_down())


class _FakeResult:
    def __init__(self, sc: dict, is_error: bool = True) -> None:
        self.structured_content = sc
        self.is_error = is_error


ok_r = _FakeResult({"ok": True, "value": "42"}, is_error=False)
assert neo._parse_run(ok_r) == "42"

err_r = _FakeResult({"ok": False, "error": "boom"})
try:
    neo._parse_run(err_r)
    assert False, "expected NeoRunError"
except neo.NeoRunError as e:
    assert "boom" in str(e)

nc_r = _FakeResult({"ok": False, "error": "browser session not connected"})
try:
    neo._parse_run(nc_r)
    assert False, "expected NeoNotConnectedError"
except neo.NeoNotConnectedError:
    pass

bad_r = _FakeResult({"ok": True, "value": 42}, is_error=False)
try:
    neo._parse_run(bad_r)
    assert False, "expected NeoRunError for non-string value"
except neo.NeoRunError:
    pass
print("OK error mapping")


async def t_reconnect() -> None:
    """A dead established session is torn down and retried once."""
    tier = neo.NeoTier()
    calls = {"n": 0}

    async def fake_call(script: str, s_: object) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise neo.NeoNotConnectedError("session terminated")
        return _payload()

    tier._call_run = fake_call  # type: ignore[method-assign]

    class _FakeSession:
        pass

    tier._session = _FakeSession()  # pretend a session was established
    r = await tier.fetch("https://example.com/x", match("https://example.com/x"))
    # Failed load + retried load + tab close.
    assert calls["n"] == 3 and r.engine == "neo"

asyncio.run(t_reconnect())
print("OK reconnect")

# ---------------------------------------------------------------------------
# Challenge poll (manual-solve path)
# ---------------------------------------------------------------------------

async def t_poll() -> None:
    """Walled twice, then clean: load + two in-place rechecks + close."""
    scripts: list[str] = []

    async def fake(script: str) -> str:
        scripts.append(script)
        if len(scripts) < 3:
            return _payload(html=WALL_HTML)
        return _payload()

    r = await _tier_with(fake).fetch("https://example.com/x", match("https://example.com/x"))
    assert len(scripts) == 4 and "Just a moment" not in r.html
    # Polls re-read the same tab — no second navigation.
    assert "newPage" not in scripts[1] and "newPage" not in scripts[2]
    assert "pages.close" in scripts[3]

asyncio.run(t_poll())


async def t_budget() -> None:
    """Always walled: returns the walled Rendered (no raise) once budget is spent."""
    scripts: list[str] = []

    async def fake(script: str) -> str:
        scripts.append(script)
        return _payload(html=WALL_HTML)

    r = await _tier_with(fake).fetch("https://example.com/x", match("https://example.com/x"))
    assert is_botwall(r.html, r.status, r.content_type) is not None
    # Load + a run of in-place rechecks (no re-navigation) + close.
    assert len(scripts) >= 3 and "newPage" not in scripts[1]
    assert "pages.close" in scripts[-1]

asyncio.run(t_budget())


async def t_renav() -> None:
    """A lost tab mid-poll triggers a fresh navigation, then closes both tabs."""
    scripts: list[str] = []

    async def fake(script: str) -> str:
        n = len(scripts) + 1
        scripts.append(script)
        if "newPage" in script:
            return _payload(pid=n, html=WALL_HTML if n == 1 else "<html><body>ok</body></html>")
        if n == 2:
            raise neo.NeoRunError("page not found")
        return _payload()

    r = await _tier_with(fake).fetch("https://example.com/x", match("https://example.com/x"))
    assert "newPage" in scripts[0] and "newPage" in scripts[2]  # load + re-navigation
    assert "newPage" not in scripts[1]  # the failed poll was an in-place recheck
    assert r.html == "<html><body>ok</body></html>"
    assert "pages.close(1)" in scripts[3] and "pages.close(3)" in scripts[4]

asyncio.run(t_renav())
print("OK challenge poll")

# ---------------------------------------------------------------------------
# Large HTML + script template
# ---------------------------------------------------------------------------

async def t_large() -> None:
    big = "x" * 300_000

    async def fake(script: str) -> str:
        return _payload(html=big)

    r = await _tier_with(fake).fetch("https://example.com/big", match("https://example.com/x"))
    assert len(r.html) == 300_000

asyncio.run(t_large())

js = neo._load_script('https://ex.com/a"b', 2500)
assert '"https://ex.com/a\\"b"' in js  # URL is JSON-escaped into the script
for token in ("newPage", "window.__ws_html", "40000", "2500", "200000"):
    assert token in js, token

js_re = neo._recheck_script(42)
assert "const pid = 42" in js_re and "newPage" not in js_re
for token in ("window.__ws_html", "40000", "200000"):
    assert token in js_re, token

js_close = neo._close_script(42)
assert "pages.close(42)" in js_close
print("OK large html + script template")

# ---------------------------------------------------------------------------
# Single-flight
# ---------------------------------------------------------------------------

async def t_singleflight() -> None:
    tier = neo.NeoTier()
    inflight = {"n": 0, "max": 0}

    async def fake(script: str) -> str:
        inflight["n"] += 1
        inflight["max"] = max(inflight["max"], inflight["n"])
        await asyncio.sleep(0.05)
        inflight["n"] -= 1
        return _payload()

    tier._run = fake  # type: ignore[method-assign]
    p = match("https://example.com/x")
    r1, r2 = await asyncio.gather(
        tier.fetch("https://a.example/", p),
        tier.fetch("https://b.example/", p),
    )
    assert inflight["max"] == 1 and r1.engine == "neo" and r2.engine == "neo"

asyncio.run(t_singleflight())
print("OK single-flight")

# ---------------------------------------------------------------------------
# Host header + policy
# ---------------------------------------------------------------------------

assert neo._host_header("http://127.0.0.1:9010/mcp") == {}
assert neo._host_header("http://localhost:9010/mcp") == {}
assert neo._host_header("http://host.docker.internal:9010/mcp") == {"Host": "localhost:9010"}

assert match("https://www.reddit.com/r/x/comments/1").tiers == ("http", "browser", "neo")
assert match("https://www.linkedin.com/jobs/view/1").name == "linkedin"
assert match("https://xdaforums.com/m/user/about").name == "xdaforums"
assert match("https://www.spectrumbusiness.net/").tiers == ("http", "browser", "neo")
# Non-walled domains are unchanged.
assert match("https://www.amazon.com/dp/X").tiers == ("http", "browser", "stealth")
print("OK host header + policy")

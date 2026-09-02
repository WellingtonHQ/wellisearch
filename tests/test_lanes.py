"""Unit tests: two-lane crawl (fast probe vs CF challenge lane).

Pure-logic tests — no real browser, no real DB. The browser tier is driven by a
fake page; the worker routing is driven by a fake db. Run with:
    python tests/test_lanes.py
"""
from __future__ import annotations

import asyncio
import json
import time

from wellisearch.crawl import engine, tiers
from wellisearch.crawl.lane import CF, FAST, get_lane, reset_lane, set_lane
from wellisearch.crawl.policy import Policy, match
import wellisearch.crawl.pool as pool_mod
from wellisearch.crawl.results import ChallengeDetected, Rendered
import wellisearch.crawl.tiers.browser as browser_tier
import wellisearch.crawler as crawler_mod
import wellisearch.queue as queue_mod
import wellisearch.worker as worker_mod

BOTWALL_HTML = '<html><body>Just a moment...<div class="cf-turnstile"></div></body></html>'
CLEAN_HTML = (
    "<html><head><title>Article</title></head><body><article>"
    "<p>" + "This is a clean article body with enough text to be a real page. " * 20 + "</p>"
    "</article></body></html>"
)


# ---------------------------------------------------------------------------
# Fake browser page / pool
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status


class FakeMouse:
    def __init__(self) -> None:
        self.clicks = 0

    async def move(
        self,
        x: int,
        y: int,
    ) -> None:
        pass

    async def click(
        self,
        x: int,
        y: int,
    ) -> None:
        self.clicks += 1


class FakePage:
    """Returns a sequence of html from content(); records turnstile clicks."""

    def __init__(
        self,
        contents: list[str],
        status: int = 200,
    ) -> None:
        self._contents = list(contents)
        self._status = status
        self.mouse = FakeMouse()
        self._last = CLEAN_HTML

    async def goto(
        self,
        url: str,
        wait_until: str | None = None,
        timeout: float | None = None,
    ) -> FakeResp:
        return FakeResp(self._status)

    async def content(self) -> str:
        if self._contents:
            return self._contents.pop(0)
        return self._last

    async def title(self) -> str:
        return "Article"

    async def wait_for_timeout(self, ms: float) -> None:
        pass

    async def wait_for_load_state(
        self,
        state: str,
        timeout: float | None = None,
    ) -> None:
        pass

    async def evaluate(self, code: str) -> str:
        # A valid turnstile widget box so _click_turnstile_checkbox actually clicks.
        return json.dumps([100, 200, 800, 70])

    async def close(self) -> None:
        pass


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def new_page(self) -> FakePage:
        return self._page


class FakePool:
    def __init__(self, page: FakePage) -> None:
        self._ctx = FakeContext(page)
        self.acquired = 0

    async def acquire(self, key: str) -> FakeContext:
        self.acquired += 1
        return self._ctx

    async def release(self, ctx: FakeContext) -> None:
        pass


def _install_fake_pool(page: FakePage) -> FakePool:
    """Point the browser tier at a fake pool (both lanes)."""
    pool = FakePool(page)
    browser_tier.get_pool = lambda: pool
    browser_tier.get_cf_pool = lambda: pool
    return pool


# ---------------------------------------------------------------------------
# 1. lane contextvar
# ---------------------------------------------------------------------------
assert get_lane() == FAST
tok = set_lane(CF)
try:
    assert get_lane() == CF
finally:
    reset_lane(tok)
assert get_lane() == FAST
print("OK lane contextvar")

# ---------------------------------------------------------------------------
# 2. fast-lane browser tier: botwall -> ChallengeDetected (probe, no click)
# ---------------------------------------------------------------------------
page = FakePage([BOTWALL_HTML])
_install_fake_pool(page)
tier = browser_tier.BrowserTier()
p = match("https://example.com/x")
tok = set_lane(FAST)
try:
    try:
        asyncio.run(tier.fetch("https://example.com/x", p))
        raise AssertionError("expected ChallengeDetected in fast lane")
    except ChallengeDetected as e:
        assert e.url == "https://example.com/x"
    assert page.mouse.clicks == 0, "fast lane must not click the turnstile"
finally:
    reset_lane(tok)
print("OK fast-lane probe raises ChallengeDetected")

# ---------------------------------------------------------------------------
# 3. CF-lane browser tier: botwall -> full challenge loop (clicks, resolves)
# ---------------------------------------------------------------------------
page = FakePage([BOTWALL_HTML, CLEAN_HTML])
_install_fake_pool(page)
tok = set_lane(CF)
try:
    res = asyncio.run(tier.fetch("https://example.com/x", p))
    assert res.status == 200
    assert "cf-turnstile" not in res.html, "CF lane should have resolved the challenge"
    assert page.mouse.clicks >= 1, "CF lane should have clicked the turnstile"
finally:
    reset_lane(tok)
print("OK CF-lane runs the challenge loop")

# ---------------------------------------------------------------------------
# 4. fast-lane browser tier: clean page -> success (no raise)
# ---------------------------------------------------------------------------
page = FakePage([CLEAN_HTML])
_install_fake_pool(page)
tok = set_lane(FAST)
try:
    res = asyncio.run(tier.fetch("https://example.com/x", p))
    assert res.status == 200
    assert "cf-turnstile" not in res.html
    assert page.mouse.clicks == 0
finally:
    reset_lane(tok)
print("OK fast-lane clean page succeeds")

# ---------------------------------------------------------------------------
# 5. CF pool is separate from the fast pool (and each is a singleton)
# ---------------------------------------------------------------------------
assert pool_mod.get_pool() is not pool_mod.get_cf_pool()
assert pool_mod.get_pool() is pool_mod.get_pool()
assert pool_mod.get_cf_pool() is pool_mod.get_cf_pool()
print("OK separate pools")

# ---------------------------------------------------------------------------
# 6. CF semaphore is separate from the fast semaphore (and each is a singleton)
# ---------------------------------------------------------------------------
assert crawler_mod.crawl_semaphore() is not crawler_mod.cf_crawl_semaphore()
assert crawler_mod.crawl_semaphore() is crawler_mod.crawl_semaphore()
assert crawler_mod.cf_crawl_semaphore() is crawler_mod.cf_crawl_semaphore()
print("OK separate semaphores")

# ---------------------------------------------------------------------------
# 7. crawl_deduped picks the semaphore by lane
# ---------------------------------------------------------------------------
called = []


class FakeSem:
    def __init__(self, name: str) -> None:
        self.name = name

    async def __aenter__(self) -> FakeSem:
        called.append(self.name)
        return self

    async def __aexit__(self, *a) -> bool:
        return False


async def ok_fn() -> str:
    return "ok"


orig_fast = queue_mod.crawl_semaphore
orig_cf = queue_mod.cf_crawl_semaphore
queue_mod.crawl_semaphore = lambda: FakeSem("fast")
queue_mod.cf_crawl_semaphore = lambda: FakeSem("cf")
try:
    async def run_fast() -> str:
        tok = set_lane(FAST)
        try:
            return await queue_mod.crawl_deduped("https://example.com/a", "t", ok_fn)
        finally:
            reset_lane(tok)

    async def run_cf() -> str:
        tok = set_lane(CF)
        try:
            return await queue_mod.crawl_deduped("https://example.com/b", "t", ok_fn)
        finally:
            reset_lane(tok)

    assert asyncio.run(run_fast()) == "ok"
    assert called == ["fast"], called
    called.clear()
    assert asyncio.run(run_cf()) == "ok"
    assert called == ["cf"], called
finally:
    queue_mod.crawl_semaphore = orig_fast
    queue_mod.cf_crawl_semaphore = orig_cf
print("OK crawl_deduped picks semaphore by lane")

# ---------------------------------------------------------------------------
# 8. engine propagates ChallengeDetected (does not swallow it as a tier error)
# ---------------------------------------------------------------------------
class ChallengeTier:
    name = "http"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        raise ChallengeDetected(url)


orig_registry = dict(tiers._REGISTRY)
tiers._REGISTRY.clear()
tiers.register(ChallengeTier())
try:
    tok = set_lane(FAST)
    try:
        try:
            asyncio.run(engine.crawl("https://example.com/x"))
            raise AssertionError("expected ChallengeDetected to propagate")
        except ChallengeDetected:
            pass
    finally:
        reset_lane(tok)
finally:
    tiers._REGISTRY.clear()
    tiers._REGISTRY.update(orig_registry)
print("OK engine propagates ChallengeDetected")

# ---------------------------------------------------------------------------
# 9. worker routes ChallengeDetected to the CF lane (fake db)
# ---------------------------------------------------------------------------
class FakeDB:
    def __init__(self) -> None:
        self.routed = []
        self.done = []
        self.claimed = []

    async def fetch_all(
        self,
        sql: str,
        params: tuple | None = None,
        timeout_ms: int | None = None,
    ) -> list[dict]:
        if "lane = 'cf'" in sql:
            return []
        return [{"url": "https://example.com/challenge"}]

    async def queue_claim(self, url: str) -> bool:
        self.claimed.append(url)
        return True

    async def queue_done(
        self,
        url: str,
        ok: bool,
        error: str | None = None,
    ) -> None:
        self.done.append((url, ok))

    async def queue_route_to_cf(self, url: str) -> bool:
        self.routed.append(url)
        return True


async def fake_crawl_url(url: str, trigger: str) -> dict:
    raise ChallengeDetected(url)


fake_db = FakeDB()
orig_db = worker_mod.db
orig_crawl_url = worker_mod.crawl_url
worker_mod.db = fake_db
worker_mod.crawl_url = fake_crawl_url
try:
    stats = asyncio.run(worker_mod._drain_queue(deadline=time.monotonic() + 60))
    assert fake_db.routed == ["https://example.com/challenge"], fake_db.routed
    assert fake_db.done == [], "challenge must not be marked done"
    assert stats["processed"] == 1
finally:
    worker_mod.db = orig_db
    worker_mod.crawl_url = orig_crawl_url
print("OK worker routes ChallengeDetected to CF lane")

print("ALL LANE TESTS PASSED")

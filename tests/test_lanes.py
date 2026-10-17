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
import wellisearch.fetch as fetch_mod
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
    def __init__(self: FakeResp, status: int) -> None:
        """Initialize a fake response with a status."""
        self.status = status


class FakeMouse:
    def __init__(self: FakeMouse) -> None:
        """Initialize a fake mouse click counter."""
        self.clicks = 0

    async def move(
        self: FakeMouse,
        x: int,
        y: int,
    ) -> None:
        """Fake mouse move (no-op)."""
        pass

    async def click(
        self: FakeMouse,
        x: int,
        y: int,
    ) -> None:
        """Count a fake mouse click."""
        self.clicks += 1


class FakePage:
    """Returns a sequence of html from content(); records turnstile clicks."""

    def __init__(
        self: FakePage,
        contents: list[str],
        status: int = 200,
    ) -> None:
        """Initialize a fake page with a content sequence."""
        self._contents = list(contents)
        self._status = status
        self.mouse = FakeMouse()
        self._last = CLEAN_HTML

    async def goto(
        self: FakePage,
        url: str,
        wait_until: str | None = None,
        timeout: float | None = None,
    ) -> FakeResp:
        """Return a fake response for the goto."""
        return FakeResp(self._status)

    async def content(self: FakePage) -> str:
        """Pop the next content from the sequence."""
        if self._contents:
            return self._contents.pop(0)
        return self._last

    async def title(self: FakePage) -> str:
        """Return a fixed title."""
        return "Article"

    async def wait_for_timeout(self: FakePage, ms: float) -> None:
        """No-op wait."""
        pass

    async def wait_for_load_state(
        self: FakePage,
        state: str,
        timeout: float | None = None,
    ) -> None:
        """No-op wait."""
        pass

    async def evaluate(self: FakePage, code: str) -> str:
        """Return a fake turnstile widget box."""
        # A valid turnstile widget box so _click_turnstile_checkbox actually clicks.
        return json.dumps([100, 200, 800, 70])

    async def close(self: FakePage) -> None:
        """No-op close."""
        pass


class FakeContext:
    def __init__(self: FakeContext, page: FakePage) -> None:
        """Initialize a fake context wrapping a page."""
        self._page = page

    async def new_page(self: FakeContext) -> FakePage:
        """Return the fake page."""
        return self._page


class FakePool:
    def __init__(self: FakePool, page: FakePage) -> None:
        """Initialize a fake pool wrapping a fake context."""
        self._ctx = FakeContext(page)
        self.contexts = [self._ctx]
        self.acquired = 0
        self.invalidated = []
        self.released = []

    async def acquire(self: FakePool, key: str) -> FakeContext:
        """Count the acquire and return the fake context."""
        ctx = self.contexts[min(self.acquired, len(self.contexts) - 1)]
        self.acquired += 1
        return ctx

    async def invalidate(
        self: FakePool,
        key: str,
        ctx: FakeContext,
        error: Exception,
    ) -> None:
        """Record invalidated contexts."""
        self.invalidated.append((key, ctx, error))

    async def release(self: FakePool, ctx: FakeContext, key: str | None = None) -> None:
        """Record released contexts."""
        self.released.append((ctx, key))


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
# 4a. closed browser transport is invalidated and retried once
# ---------------------------------------------------------------------------


class ClosedContext(FakeContext):
    async def new_page(self: ClosedContext) -> FakePage:
        """Simulate the closed Playwright transport seen in production."""
        raise RuntimeError(
            "BrowserContext.new_page: WriteUnixTransport closed=True; the handler is closed"
        )


page = FakePage([CLEAN_HTML])
fake_pool = _install_fake_pool(page)
fake_pool.contexts = [ClosedContext(page), FakeContext(page)]
tok = set_lane(FAST)
try:
    res = asyncio.run(tier.fetch("https://example.com/x", p))
finally:
    reset_lane(tok)
assert res.status == 200
assert fake_pool.acquired == 2, fake_pool.acquired
assert len(fake_pool.invalidated) == 1, fake_pool.invalidated
assert len(fake_pool.released) == 2, fake_pool.released
print("OK browser tier replaces a closed context and retries once")

# ---------------------------------------------------------------------------
# 4b. ordinary crawl timeouts are not retried as a closed browser
# ---------------------------------------------------------------------------


class TimeoutContext(FakeContext):
    async def new_page(self: TimeoutContext) -> FakePage:
        """Simulate a page timeout unrelated to a closed browser context."""
        raise TimeoutError("navigation timed out")


page = FakePage([CLEAN_HTML])
fake_pool = _install_fake_pool(page)
fake_pool.contexts = [TimeoutContext(page)]
tok = set_lane(FAST)
try:
    try:
        asyncio.run(tier.fetch("https://example.com/x", p))
        raise AssertionError("expected TimeoutError to propagate")
    except TimeoutError:
        pass
finally:
    reset_lane(tok)
assert fake_pool.acquired == 1, fake_pool.acquired
assert fake_pool.invalidated == [], fake_pool.invalidated
assert len(fake_pool.released) == 1, fake_pool.released
print("OK browser tier does not retry ordinary timeouts")

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
    def __init__(self: FakeSem, name: str) -> None:
        """Initialize a fake semaphore with a name."""
        self.name = name

    async def __aenter__(self: FakeSem) -> FakeSem:
        """Record the semaphore name on enter."""
        called.append(self.name)
        return self

    async def __aexit__(self: FakeSem, *a: object) -> bool:
        """No-op exit."""
        return False


async def ok_fn() -> str:
    """Return ok."""
    return "ok"


orig_fast = queue_mod.crawl_semaphore
orig_cf = queue_mod.cf_crawl_semaphore
queue_mod.crawl_semaphore = lambda: FakeSem("fast")
queue_mod.cf_crawl_semaphore = lambda: FakeSem("cf")
try:
    async def run_fast() -> str:
        """Crawl deduped in the fast lane."""
        tok = set_lane(FAST)
        try:
            return await queue_mod.crawl_deduped("https://example.com/a", "t", ok_fn)
        finally:
            reset_lane(tok)

    async def run_cf() -> str:
        """Crawl deduped in the CF lane."""
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
        self: ChallengeTier,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Fake fetch that raises ChallengeDetected."""
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
    def __init__(self: FakeDB) -> None:
        """Initialize a fake db with recorded calls."""
        self.routed = []
        self.done = []
        self.claimed = []

    async def fetch_all(
        self: FakeDB,
        sql: str,
        params: tuple | None = None,
        timeout_ms: int | None = None,
    ) -> list[dict]:
        """Return a fake challenge row, or an empty CF lane."""
        if "lane = 'cf'" in sql:
            return []
        return [{"url": "https://example.com/challenge"}]

    async def queue_claim(self: FakeDB, url: str) -> bool:
        """Record the claim."""
        self.claimed.append(url)
        return True

    async def queue_done(
        self: FakeDB,
        url: str,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Record the done call."""
        self.done.append((url, ok))

    async def queue_route_to_cf(self: FakeDB, url: str) -> bool:
        """Record the CF routing."""
        self.routed.append(url)
        return True


async def fake_crawl_url(url: str, trigger: str) -> dict:
    """Fake crawl that raises ChallengeDetected."""
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

# ---------------------------------------------------------------------------
# 10. watchlist caller owns refresh backoff so it can target the selected row
# ---------------------------------------------------------------------------


class BackoffDB:
    def __init__(self: BackoffDB) -> None:
        """Initialize a fake db recording streak bumps."""
        self.bumped = []

    async def log_crawl(self: BackoffDB, *args: object, **kwargs: object) -> None:
        """No-op crawl-log sink."""

    async def execute(
        self: BackoffDB,
        sql: str,
        params: tuple | None = None,
    ) -> None:
        """No-op statement sink (last_status update)."""

    async def refresh_fail_bump(self: BackoffDB, url: str) -> int | None:
        """Record a streak bump."""
        self.bumped.append(url)
        return 1


async def failing_fit_markdown(url: str) -> tuple[str | None, str]:
    """Fake crawl that fails with a plain CrawlError (e.g. a DNS blip)."""
    raise crawler_mod.CrawlError(url, "temporary name resolution failure")


BACKOFF_URL = "https://example.com/backoff"
orig_db_bk = worker_mod.db
orig_fit_markdown = crawler_mod.fit_markdown

for trigger in ("fetch", "manual", "recrawl", "refresh", "search"):
    backoff_db = BackoffDB()
    worker_mod.db = backoff_db
    crawler_mod.fit_markdown = failing_fit_markdown
    try:

        async def attempt(t: str) -> None:
            """Run one failing crawl+store and expect the CrawlError to propagate."""
            try:
                await worker_mod._crawl_and_store(BACKOFF_URL, t)
                raise AssertionError("expected CrawlError to propagate")
            except crawler_mod.CrawlError:
                pass

        asyncio.run(attempt(trigger))
    finally:
        worker_mod.db = orig_db_bk
        crawler_mod.fit_markdown = orig_fit_markdown
    assert backoff_db.bumped == [], (
        f"trigger={trigger} must leave watchlist backoff to the selecting refresh pass"
    )

print("OK crawl attempt leaves watchlist backoff to its caller")

# ---------------------------------------------------------------------------
# 11. worker refresh: bot-walled probe → CF lane + queued pages skipped (fake db)
# ---------------------------------------------------------------------------

WALLED = "https://example.com/walled"


class FakeRefreshDB:
    def __init__(
        self: FakeRefreshDB,
        enqueue_ok: bool = True,
        url: str = WALLED,
    ) -> None:
        """Initialize a fake db for the watchlist refresh path."""
        self.enqueue_ok = enqueue_ok
        self.url = url
        self.enqueued = []
        self.routed = []
        self.bumped = []
        self.sqls = []

    async def fetch_all(
        self: FakeRefreshDB,
        sql: str,
        params: tuple | None = None,
        timeout_ms: int | None = None,
    ) -> list[dict]:
        """Return one stale watchlist page; record the SQL for assertions."""
        self.sqls.append(sql)
        return [{"url": self.url, "fetch_count": 5}]

    async def queue_enqueue(
        self: FakeRefreshDB,
        url: str,
        source: str,
        lane: str = "fast",
    ) -> bool:
        """Record the enqueue; report whether a row was inserted."""
        self.enqueued.append((url, source, lane))
        return self.enqueue_ok

    async def queue_route_to_cf(self: FakeRefreshDB, url: str) -> bool:
        """Record the CF routing."""
        self.routed.append(url)
        return True

    async def refresh_fail_bump(self: FakeRefreshDB, url: str) -> int | None:
        """Record a streak bump (bot-walled pages back off)."""
        self.bumped.append(url)
        return 1


async def walled_crawl_url(url: str, trigger: str) -> dict:
    """Fake crawl whose fast-lane probe hits a bot-wall."""
    raise ChallengeDetected(url)


# 11a. no pending queue row → enqueued straight onto the CF lane + backed off
fake_db = FakeRefreshDB(enqueue_ok=True)
orig_db = worker_mod.db
orig_crawl_url = worker_mod.crawl_url
worker_mod.db = fake_db
worker_mod.crawl_url = walled_crawl_url
try:
    stats = asyncio.run(worker_mod._refresh_watchlist(deadline=time.monotonic() + 60))
finally:
    worker_mod.db = orig_db
    worker_mod.crawl_url = orig_crawl_url
assert fake_db.enqueued == [(WALLED, "refresh", CF)], fake_db.enqueued
assert fake_db.routed == [], "no existing row — must not route"
assert fake_db.bumped == [WALLED], "a bot-walled page must be backed off"
assert stats["challenged"] == 1 and stats["refreshed"] == 1, stats
assert any("NOT EXISTS" in sql for sql in fake_db.sqls), \
    "watchlist query must skip pages already queued (backoff)"
assert any("refresh_backoff_until" in sql for sql in fake_db.sqls), \
    "watchlist query must skip pages in an active refresh backoff"
print("OK refresh routes ChallengeDetected to CF lane")

# 11b. a pending queue row exists → the existing row is routed, not re-enqueued
fake_db = FakeRefreshDB(enqueue_ok=False)
worker_mod.db = fake_db
worker_mod.crawl_url = walled_crawl_url
try:
    stats = asyncio.run(worker_mod._refresh_watchlist(deadline=time.monotonic() + 60))
finally:
    worker_mod.db = orig_db
    worker_mod.crawl_url = orig_crawl_url
assert fake_db.enqueued == [(WALLED, "refresh", CF)], fake_db.enqueued
assert fake_db.routed == [WALLED], fake_db.routed
assert stats["challenged"] == 1 and stats["unchanged"] == 0, stats
print("OK refresh routes the existing queue row to CF lane")

# 11c. a crawl failure backs off the original selected alias, not its normalized URL

LEGACY_ALIAS = "https://sharebang.com/ser.php?t=AADIV53&f=53&psc="


async def failing_refresh_crawl_url(url: str, trigger: str) -> dict:
    """Fail the selected alias as the crawler would after canonicalization."""
    raise crawler_mod.CrawlError(
        "https://sharebang.com/ser.php?t=AADIV53&f=53",
        "closed browser transport",
    )


fake_db = FakeRefreshDB(url=LEGACY_ALIAS)
orig_db = worker_mod.db
orig_crawl_url = worker_mod.crawl_url
worker_mod.db = fake_db
worker_mod.crawl_url = failing_refresh_crawl_url
try:
    stats = asyncio.run(worker_mod._refresh_watchlist(deadline=time.monotonic() + 60))
finally:
    worker_mod.db = orig_db
    worker_mod.crawl_url = orig_crawl_url
assert fake_db.bumped == [LEGACY_ALIAS], fake_db.bumped
assert stats["refreshed"] == 1 and stats["unchanged"] == 0, stats
print("OK failed refresh backs off the selected legacy URL alias")

# ---------------------------------------------------------------------------
# 12. read-path enqueues kick via queue.enqueue — and only on fresh inserts
# ---------------------------------------------------------------------------


class KickDB:
    def __init__(self: KickDB, insert_ok: bool) -> None:
        """Initialize a fake db that records enqueue/route calls."""
        self.inserted = []
        self.routed = []
        self.ok = insert_ok

    async def queue_enqueue(
        self: KickDB,
        url: str,
        source: str,
        lane: str = "fast",
    ) -> bool:
        """Record the enqueue and report whether a row was inserted."""
        self.inserted.append((url, source, lane))
        return self.ok

    async def queue_route_to_cf(self: KickDB, url: str) -> bool:
        """Record CF routing of an already-pending URL."""
        self.routed.append(url)
        return True


kicks = []
orig_kick_worker = queue_mod.kick_worker
orig_queue_db = queue_mod.db


def record_kick() -> None:
    """Count a kick instead of scheduling the debounced worker tick."""
    kicks.append(1)


queue_mod.kick_worker = record_kick
try:
    # a fresh insert kicks once; a rejected duplicate does not; lane passes through
    queue_mod.db = KickDB(insert_ok=True)
    assert (asyncio.run(queue_mod.enqueue("https://example.com/a", source="fetch"))) is True
    assert len(kicks) == 1, "a fresh insert must kick the worker"

    kicks.clear()
    dup_db = KickDB(insert_ok=False)
    queue_mod.db = dup_db
    assert (asyncio.run(queue_mod.enqueue("https://example.com/a", source="fetch"))) is False
    assert kicks == [], "a rejected (duplicate) enqueue must not kick the worker"

    kicks.clear()
    cf_db = KickDB(insert_ok=True)
    queue_mod.db = cf_db
    assert (asyncio.run(queue_mod.enqueue("https://example.com/a", source="fetch", lane="cf"))) is True
    assert cf_db.inserted == [("https://example.com/a", "fetch", "cf")]
    assert len(kicks) == 1, "a fresh CF-lane enqueue must kick the worker"

    # fetch's bot-wall path enqueues onto the CF lane through queue.enqueue — a
    # fresh insert kicks; a rejected one routes and stays quiet
    kicks.clear()

    async def wall_crawl_url(url: str, trigger: str) -> dict:
        """Fake crawl that hits a bot-wall."""
        raise ChallengeDetected(url)

    orig_fetch_crawl, orig_fetch_db = fetch_mod.crawl_url, fetch_mod.db
    fetch_mod.crawl_url = wall_crawl_url
    try:
        wall_db = KickDB(insert_ok=True)
        queue_mod.db = fetch_mod.db = wall_db
        try:
            asyncio.run(fetch_mod._probe_crawl("https://example.com/walled"))
            raise AssertionError("expected the bot-wall CrawlError to propagate")
        except crawler_mod.CrawlError:
            pass
        assert wall_db.inserted == [("https://example.com/walled", "fetch", "cf")]
        assert len(kicks) == 1, "a fresh CF-lane enqueue from fetch must kick the worker"

        kicks.clear()
        reroute_db = KickDB(insert_ok=False)
        queue_mod.db = fetch_mod.db = reroute_db
        try:
            asyncio.run(fetch_mod._probe_crawl("https://example.com/walled"))
            raise AssertionError("expected the bot-wall CrawlError to propagate")
        except crawler_mod.CrawlError:
            pass
        assert reroute_db.routed == ["https://example.com/walled"]
        assert kicks == [], "a rejected enqueue must not kick the worker"
    finally:
        fetch_mod.crawl_url, fetch_mod.db = orig_fetch_crawl, orig_fetch_db
finally:
    queue_mod.kick_worker = orig_kick_worker
    queue_mod.db = orig_queue_db
print("OK read-path enqueues kick via queue.enqueue on fresh inserts only")

# ---------------------------------------------------------------------------
# 13. grace-cancel of a deduped owner settles its shared future: joiners unblock
#     with a CrawlError instead of hanging on shield(fut) forever
# ---------------------------------------------------------------------------


ORPHAN_URL = "https://example.com/dedup-orphan"


async def _orphan_release() -> str:
    """Cancel the crawl_deduped owner mid-crawl (what _adopt_orphan's timer does); report what a joiner sees."""
    started = asyncio.Event()

    async def owner_fn() -> None:
        started.set()
        await asyncio.sleep(30.0)  # hangs until its task is cancelled

    async def unused_fn() -> None:
        return None  # joiners never run fn — they dedup onto the in-flight future

    owner = asyncio.create_task(queue_mod.crawl_deduped(ORPHAN_URL, "fetch", owner_fn))
    await started.wait()
    joiner = asyncio.create_task(queue_mod.crawl_deduped(ORPHAN_URL, "search", unused_fn))
    await asyncio.sleep(0.1)  # let the joiner park on shield(fut)
    owner.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(joiner), timeout=3.0)
    except crawler_mod.CrawlError as e:
        released = str(e)
    else:
        raise AssertionError("joiner must receive a CrawlError when its owner is cancelled")
    try:
        await owner
    except asyncio.CancelledError:
        pass  # expected: the owner probe was grace-cancelled, not completed
    return released


released = asyncio.run(_orphan_release())
assert (
    ORPHAN_URL in released and "aborted" in released
), f"joiner release must name the URL + reason: {released!r}"
assert (
    ORPHAN_URL not in queue_mod.INFLIGHT.urls()
), f"dedup entry survived owner cancellation: {queue_mod.INFLIGHT.urls()}"
print("OK grace-cancel of a deduped owner releases its joiners with CrawlError")

print("ALL LANE TESTS PASSED")

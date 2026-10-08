"""Regression tests: fetch-path refresh backoff for reddit re-crawls (pure logic).

A reddit post whose stored markdown lacks the ranking heading triggers an inline
browser re-crawl on every fetch; a failed re-crawl must bump the page's
refresh-failure backoff so subsequent fetches serve the stale copy instead of
re-running the crawl. Fakes only: no network, no Postgres."""
from __future__ import annotations

import asyncio
import datetime as dt

from wellisearch import crawler
from wellisearch.config import get_settings as real_get_settings
from wellisearch.crawl.extractors.reddit import _comments_heading, needs_refresh
from wellisearch.crawl.results import ChallengeDetected
import wellisearch.fetch as fetch_mod
import wellisearch.queue as queue_mod


class FakeDB:
    """In-memory stand-in for the db helpers _resolve_page uses."""

    def __init__(self, page: dict | None = None) -> None:
        self.page = page
        self.bumped: list[str] = []
        self.enqueued: list[tuple] = []
        self.challenge_in_flight = False
        self.paused = False

    async def page_get(self, url: str) -> dict | None:
        """Return the stored row, if any."""
        return self.page

    async def worker_paused(self) -> bool:
        """Report whether indexing is paused (set per scenario)."""
        return self.paused

    async def queue_challenge_in_flight(self, url: str) -> bool:
        """Report whether a CF challenge row is already in flight."""
        return self.challenge_in_flight

    async def queue_enqueue(
        self,
        url: str,
        source: str = "fetch",
        lane: str | None = None,
    ) -> bool:
        """Record the enqueue and report success."""
        self.enqueued.append((url, source, lane))
        return True

    async def refresh_fail_bump(self, url: str) -> int | None:
        """Record a refresh-failure bump and return the new streak."""
        self.bumped.append(url)
        return 1


class TwoPhaseDB(FakeDB):
    """page_get returns `first` on the first call and `second` afterwards —
    simulates a crawl that stores fresh content between _resolve_page's reads."""

    def __init__(
        self,
        first: dict | None,
        second: dict | None,
    ) -> None:
        super().__init__(first)
        self._second = second
        self._calls = 0

    async def page_get(self, url: str) -> dict | None:
        """Return `first` on the first call, then `second`."""
        self._calls += 1
        return self.page if self._calls == 1 else self._second


_REAL_SETTINGS = fetch_mod.get_settings()
_S = type(_REAL_SETTINGS)(FETCH_TIMEOUT_S=5.0, FETCH_PROBE_TIMEOUT_S=1.0)

_s = real_get_settings()
URL_POST = "https://www.reddit.com/r/test/comments/abc123/a_post_title/"
STALE_MD = "# A post title\n\nPost body without any comments heading."
FRESH_MD = STALE_MD + "\n" + _comments_heading(
    _s.CRAWL_REDDIT_COMMENT_RANKING, max(1, _s.CRAWL_REDDIT_MAX_COMMENTS)
)

# Preconditions: the fixtures must actually exercise needs_refresh.
assert needs_refresh(URL_POST, STALE_MD) is True, "STALE_MD must lack the ranking heading"
assert needs_refresh(URL_POST, FRESH_MD) is False, "FRESH_MD must carry the ranking heading"


def stale_row(backoff_until: dt.datetime | None) -> dict:
    """A stored reddit post row whose markdown predates the ranking heading."""
    return {
        "url": URL_POST,
        "title": "A post title",
        "fit_markdown": STALE_MD,
        "disabled": False,
        "fetch_count": 3,
        "refresh_backoff_until": backoff_until,
    }


# ---------------------------------------------------------------------------
# Save Module Globals
# ---------------------------------------------------------------------------

_real_db = fetch_mod.db
_real_crawl_url = fetch_mod.crawl_url
_real_get_settings = fetch_mod.get_settings
_real_queue_db = queue_mod.db
_real_kick_worker = queue_mod.kick_worker

kicks: list[bool] = []


def _count_kick() -> None:
    kicks.append(True)


queue_mod.kick_worker = _count_kick
fetch_mod.get_settings = lambda: _S

calls: list[str] = []


async def failing_crawl(url: str, trigger: str = "fetch") -> dict:
    """Record the call, then fail with a plain tier error."""
    calls.append(url)
    raise crawler.CrawlError(url, "simulated tier failure on both tiers")


# ---------------------------------------------------------------------------
# Backoff active + stale markdown -> serve the stored copy, no crawl at all
# ---------------------------------------------------------------------------

db1 = FakeDB(stale_row(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)))
fetch_mod.db = db1
queue_mod.db = db1
fetch_mod.crawl_url = failing_crawl

out1 = asyncio.run(fetch_mod._resolve_page(URL_POST))
assert out1["from_index"] is True, "backed-off stale post must be served from the index"
assert out1["content"] == STALE_MD, f"stored copy must pass through untouched: {out1['content']!r}"
assert calls == [], f"no crawl may run while backoff is active: {calls}"
assert db1.bumped == [], "a served-from-index fetch has nothing to bump"
print("OK backoff active (stale served, no crawl)")

# ---------------------------------------------------------------------------
# Backoff expired + stale markdown -> re-crawl runs; failure bumps the streak
# ---------------------------------------------------------------------------

db2 = FakeDB(stale_row(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)))
fetch_mod.db = db2
queue_mod.db = db2
calls.clear()

err2: Exception | None = None
try:
    asyncio.run(fetch_mod._resolve_page(URL_POST))
except Exception as e:  # noqa: BLE001 - asserting on the exact error type
    err2 = e
assert isinstance(err2, crawler.CrawlError), f"crawl failure must propagate: {type(err2)}: {err2}"
assert calls == [URL_POST], f"expired backoff must re-crawl inline: {calls}"
assert db2.bumped == [URL_POST], f"failed refresh-triggered crawl must bump the streak: {db2.bumped}"
assert db2.enqueued == [], "a plain tier failure is not CF-routed from the fetch path"
print("OK backoff expired (re-crawl ran, failure bumped)")

# ---------------------------------------------------------------------------
# Bot-wall on the re-crawl -> CF lane + bump (same pattern as worker refresh)
# ---------------------------------------------------------------------------


async def challenge_crawl(url: str, trigger: str = "fetch") -> dict:
    """Record the call, then hit a bot-wall."""
    calls.append(url)
    raise ChallengeDetected(url)


db3 = FakeDB(stale_row(None))
fetch_mod.db = db3
queue_mod.db = db3
fetch_mod.crawl_url = challenge_crawl
calls.clear()

err3: Exception | None = None
try:
    asyncio.run(fetch_mod._resolve_page(URL_POST))
except Exception as e:  # noqa: BLE001 - asserting on the exact error type
    err3 = e
assert isinstance(err3, crawler.CrawlError), f"expected CrawlError: {type(err3)}: {err3}"
assert "bot-wall detected" in str(err3), f"bot-wall hint missing: {str(err3)!r}"
assert db3.enqueued == [(URL_POST, "fetch", "cf")], f"challenge must route to the CF lane: {db3.enqueued}"
assert db3.bumped == [URL_POST], f"walled re-crawl must bump the streak: {db3.bumped}"
print("OK bot-wall (CF-routed + bumped)")

# ---------------------------------------------------------------------------
# Unindexed URL failure -> no bump (first-time indexing is not a refresh)
# ---------------------------------------------------------------------------

db4 = FakeDB(None)
fetch_mod.db = db4
queue_mod.db = db4
fetch_mod.crawl_url = failing_crawl
calls.clear()

err4: Exception | None = None
try:
    asyncio.run(fetch_mod._resolve_page(URL_POST))
except Exception as e:  # noqa: BLE001 - asserting on the exact error type
    err4 = e
assert isinstance(err4, crawler.CrawlError), f"crawl failure must propagate: {type(err4)}: {err4}"
assert db4.bumped == [], "a first-time indexing failure must not touch refresh backoff"
print("OK unindexed (no bump on first-time crawl failure)")

# ---------------------------------------------------------------------------
# Success after expiry -> fresh content served, nothing bumped
# ---------------------------------------------------------------------------


async def ok_crawl(url: str, trigger: str = "fetch") -> dict:
    """Record the call and succeed."""
    calls.append(url)
    return {"url": url}


db5 = TwoPhaseDB(
    stale_row(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)),
    {**stale_row(None), "fit_markdown": FRESH_MD},
)
fetch_mod.db = db5
queue_mod.db = db5
fetch_mod.crawl_url = ok_crawl
calls.clear()

out5 = asyncio.run(fetch_mod._resolve_page(URL_POST))
assert out5["from_index"] is False, "a successful re-crawl must not report from the index"
assert out5["content"] == FRESH_MD, f"fresh stored content must be returned: {out5['content']!r}"
assert calls == [URL_POST], f"the re-crawl must have run once: {calls}"
assert db5.bumped == [], "a successful crawl never bumps (worker resets on store)"
print("OK success path (fresh stored content served, no bump)")

# ---------------------------------------------------------------------------
fetch_mod.db = _real_db
queue_mod.db = _real_queue_db
queue_mod.kick_worker = _real_kick_worker
fetch_mod.crawl_url = _real_crawl_url
fetch_mod.get_settings = _real_get_settings
print("ALL FETCH REFRESH BACKOFF TESTS PASSED")

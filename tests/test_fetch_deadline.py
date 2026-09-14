"""Regression tests: fetch._resolve_page deadline + cancel semantics (pure logic).

Fakes only: no network, no Postgres."""
from __future__ import annotations

import asyncio
import logging

from wellisearch import crawler
import wellisearch.fetch as fetch_mod
import wellisearch.queue as queue_mod


class FakeDB:
    """In-memory stand-in for the db helpers _resolve_page uses."""

    def __init__(self):
        self.enqueued = []
        self.challenge_in_flight = False

    async def page_get(self, url: str) -> dict | None:
        return None  # not indexed yet: force the on-demand crawl path

    async def queue_challenge_in_flight(self, url: str) -> bool:
        return self.challenge_in_flight

    async def queue_enqueue(
        self, url: str, source: str = "fetch", lane: str | None = None
    ) -> bool:
        self.enqueued.append((url, source, lane))
        return True


_REAL_SETTINGS = fetch_mod.get_settings()
_DEADLINE_S = type(_REAL_SETTINGS)(FETCH_TIMEOUT_S=0.3, FETCH_PROBE_TIMEOUT_S=0.1)
_CANCEL_S = type(_REAL_SETTINGS)(FETCH_TIMEOUT_S=5.0, FETCH_PROBE_TIMEOUT_S=0.1)


URL = "https://example.com/slow"

# --- save module globals we monkeypatch -------------------------------------
_real_db = fetch_mod.db
_real_crawl_url = fetch_mod.crawl_url
_real_get_settings = fetch_mod.get_settings
_real_queue_db = queue_mod.db
_real_kick_worker = queue_mod.kick_worker

kicks: list[bool] = []


def _count_kick() -> None:
    kicks.append(True)


queue_mod.kick_worker = _count_kick

fetch_log = logging.getLogger("wellisearch.fetch")
_records: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _records.append(record.getMessage())


_capture = _Capture()
fetch_log.addHandler(_capture)
fetch_log.setLevel(logging.INFO)  # capture INFO records too (grace expiry, stored-while-gone)

# ---------------------------------------------------------------------------
# Deadline path: crawl slower than FETCH_TIMEOUT_S -> timeout hint + re-enqueue
# ---------------------------------------------------------------------------


async def failing_slow_crawl(url: str, trigger: str = "fetch") -> dict:
    await asyncio.sleep(0.6)  # longer than the 0.3 s deadline
    raise crawler.CrawlError(url, "simulated tier failure after client gave up")


db1 = FakeDB()
fetch_mod.db = db1
queue_mod.db = db1  # read-path enqueues go through queue.enqueue, not fetch's db
fetch_mod.crawl_url = failing_slow_crawl
fetch_mod.get_settings = lambda: _DEADLINE_S


async def scenario_deadline():
    err: Exception | None = None
    try:
        await fetch_mod._resolve_page(URL)
    except Exception as e:  # noqa: BLE001 - asserting on the exact error type
        err = e
    await asyncio.sleep(0.8)  # background crawl fails; its done-callback fires in-loop
    return err


err = asyncio.run(scenario_deadline())
assert isinstance(err, crawler.CrawlError), f"expected CrawlError, got {type(err)}: {err}"
msg = str(err)
assert "The exception is not set." not in msg, f"InvalidStateError leaked to the client: {msg!r}"
assert "timed out" in msg and "try again in ~" in msg, f"timeout hint missing: {msg!r}"
assert db1.enqueued == [(URL, "fetch", "fast")], f"background retry was not enqueued: {db1.enqueued}"
assert kicks == [True], f"background retry must kick the worker debouncedly: {kicks}"
assert any(
    r.startswith("background fetch crawl failed for") and "simulated tier failure" in r
    for r in _records
), f"done-callback never ran against the completed task; records={_records}"
print("OK deadline path (hint + re-enqueue + kick + background watch)")

# ---------------------------------------------------------------------------
# Cancel path: client disconnect must propagate as CancelledError, not replaced
# ---------------------------------------------------------------------------


async def slow_crawl(url: str, trigger: str = "fetch") -> dict:
    await asyncio.sleep(1.0)  # long enough that cancellation lands mid-wait
    return {"url": url}


db2 = FakeDB()
fetch_mod.db = db2
queue_mod.db = db2
fetch_mod.crawl_url = slow_crawl
fetch_mod.get_settings = lambda: _CANCEL_S  # long deadline: cancel must win, not the clock


async def scenario_cancel():
    fetch_task = asyncio.create_task(fetch_mod._resolve_page(URL))
    await asyncio.sleep(0.05)  # _resolve_page is inside its deadline wait now
    fetch_task.cancel()
    try:
        await fetch_task
    except asyncio.CancelledError:
        result = "cancelled"
    else:
        result = "no error"
    await asyncio.sleep(1.2)  # background crawl completes; callback fires in-loop
    return result


result = asyncio.run(scenario_cancel())
assert result == "cancelled", f"cancellation was swallowed/replaced: {result!r}"
print("OK cancel path (CancelledError preserved)")

# ---------------------------------------------------------------------------
# Grace window: an orphan is stopped after FETCH_ORPHAN_GRACE_S, freeing slot + dedup;
# a probe that finishes inside its grace window stores normally (no cancel)
# ---------------------------------------------------------------------------


URL_G1 = "https://example.com/hang"      # never finishes -> the grace timer must stop it
URL_G2 = "https://example.com/finishes"  # past the deadline but inside grace -> let it store
hang_cancelled: list[bool] = []


async def grace_crawl(url: str, trigger: str = "fetch") -> dict:
    if url == URL_G2:
        await asyncio.sleep(0.5)  # past the 0.3 s deadline but inside the 1.0 s grace
        return {"url": url}
    try:
        await asyncio.sleep(30.0)
    except asyncio.CancelledError:
        hang_cancelled.append(True)
        raise


_GRACE_S = type(_REAL_SETTINGS)(
    FETCH_TIMEOUT_S=0.3, FETCH_PROBE_TIMEOUT_S=0.1, FETCH_ORPHAN_GRACE_S=1.0
)

db3 = FakeDB()
fetch_mod.db = db3
queue_mod.db = db3
fetch_mod.crawl_url = grace_crawl
fetch_mod.get_settings = lambda: _GRACE_S


async def scenario_grace(url: str):
    err: Exception | None = None
    try:
        await fetch_mod._resolve_page(url)
    except Exception as e:  # noqa: BLE001 - asserting on the exact error type
        err = e
    await asyncio.sleep(1.6)  # grace elapses (or crawl finishes); callback fires in-loop
    return err


err_g1 = asyncio.run(scenario_grace(URL_G1))
assert isinstance(err_g1, crawler.CrawlError), f"expected CrawlError for {URL_G1}: {type(err_g1)}: {err_g1}"
assert "timed out" in str(err_g1), f"timeout hint missing: {str(err_g1)!r}"
assert hang_cancelled == [True], "grace window did not cancel the hanging orphan probe"
assert any(
    r.startswith("orphan grace expired") and URL_G1 in r for r in _records
), f"no grace-expiry log; records={_records[-4:]}"
assert queue_mod.INFLIGHT.urls() == [], f"dedup entry survived its grace expiry: {queue_mod.INFLIGHT.urls()}"

err_g2 = asyncio.run(scenario_grace(URL_G2))
assert isinstance(err_g2, crawler.CrawlError), f"deadline must still raise for {URL_G2}: {type(err_g2)}: {err_g2}"
assert "timed out" in str(err_g2), f"timeout hint missing: {str(err_g2)!r}"
assert hang_cancelled == [True], "grace timer cancelled a probe that finished inside its window"
assert any(
    r.startswith("background fetch crawl stored") and URL_G2 in r for r in _records
), f"no stored log for {URL_G2}; records={_records[-4:]}"
assert not any(URL_G2 in r for r in _records if r.startswith("orphan grace expired")), (
    "grace expiry logged for a probe that finished inside its window"
)
assert db3.enqueued == [(URL_G1, "fetch", "fast"), (URL_G2, "fetch", "fast")], f"both deadlines must re-queue: {db3.enqueued}"
print("OK grace window (orphan stopped + dedup freed; in-window finish untouched)")

# ---------------------------------------------------------------------------
fetch_mod.db = _real_db
queue_mod.db = _real_queue_db
queue_mod.kick_worker = _real_kick_worker
fetch_mod.crawl_url = _real_crawl_url
fetch_mod.get_settings = _real_get_settings
print("ALL FETCH DEADLINE TESTS PASSED")

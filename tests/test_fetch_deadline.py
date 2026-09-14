"""Regression tests: fetch._resolve_page deadline + cancel semantics (pure logic).

The backgrounded-crawl watcher must be a done-callback — task.exception() on a
pending task raises InvalidStateError, which used to mask the timeout error.
Fakes only: no network, no Postgres."""
from __future__ import annotations

import asyncio
import logging

from wellisearch import crawler
import wellisearch.fetch as fetch_mod


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
        self, url: str, trigger: str = "fetch", lane: str | None = None
    ) -> bool:
        self.enqueued.append((url, trigger, lane))
        return True


# Grab real settings once, before swapping get_settings.
_REAL_SETTINGS = fetch_mod.get_settings()
_DEADLINE_S = type(_REAL_SETTINGS)(FETCH_TIMEOUT_S=0.3, FETCH_PROBE_TIMEOUT_S=0.1)
_CANCEL_S = type(_REAL_SETTINGS)(FETCH_TIMEOUT_S=5.0, FETCH_PROBE_TIMEOUT_S=0.1)


URL = "https://example.com/slow"

# --- save module globals we monkeypatch -------------------------------------
_real_db = fetch_mod.db
_real_crawl_url = fetch_mod.crawl_url
_real_get_settings = fetch_mod.get_settings
fetch_log = logging.getLogger("wellisearch.fetch")
_records: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _records.append(record.getMessage())


_capture = _Capture()
fetch_log.addHandler(_capture)

# ---------------------------------------------------------------------------
# Deadline path: crawl slower than FETCH_TIMEOUT_S -> timeout hint + re-enqueue
# ---------------------------------------------------------------------------


async def failing_slow_crawl(url: str, trigger: str = "fetch") -> dict:
    await asyncio.sleep(0.6)  # longer than the 0.3 s deadline
    raise crawler.CrawlError(url, "simulated tier failure after client gave up")


db1 = FakeDB()
fetch_mod.db = db1
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
assert db1.enqueued == [(URL, "fetch", None)], f"background retry was not enqueued: {db1.enqueued}"
assert any(
    r.startswith("background fetch crawl failed for") and "simulated tier failure" in r
    for r in _records
), f"done-callback never ran against the completed task; records={_records}"
print("OK deadline path (hint + re-enqueue + background watch)")

# ---------------------------------------------------------------------------
# Cancel path: client disconnect must propagate as CancelledError, not replaced
# ---------------------------------------------------------------------------


async def slow_crawl(url: str, trigger: str = "fetch") -> dict:
    await asyncio.sleep(1.0)  # long enough that cancellation lands mid-wait
    return {"url": url}


db2 = FakeDB()
fetch_mod.db = db2
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
fetch_mod.db = _real_db
fetch_mod.crawl_url = _real_crawl_url
fetch_mod.get_settings = _real_get_settings
print("ALL FETCH DEADLINE TESTS PASSED")

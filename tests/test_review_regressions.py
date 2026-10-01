"""PR #15 regressions: canonical URL state, atomic merges, and off-loop trimming.

Run with python tests/test_review_regressions.py; no network or database required.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import datetime as dt
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from wellisearch import crawler, fetch, merge_dupes, queue, tools, worker
from wellisearch.config import Settings
from wellisearch.crawl.results import ChallengeDetected

CANONICAL = "https://example.com/article"
VARIANT = CANONICAL + "?utm_source=review#section"
STORED = {"title": "Article", "fit_markdown": "Stored article.", "fetch_count": 2}


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    """Exercise variant URLs through the public paths and their shared state."""

    async def test_fetch_counts_and_response_urls(self) -> None:
        """Single and bulk reads bump the stored key while preserving requested URLs."""
        fake_db = Mock(page_get=AsyncMock(return_value=STORED), bump_fetch_count=AsyncMock())
        with patch.object(fetch, "db", fake_db):
            single = await fetch.fetch_page(VARIANT)
            bulk = await fetch.fetch_pages([VARIANT])
        self.assertTrue(single["from_index"])
        self.assertEqual(single["url"], VARIANT)
        self.assertEqual(bulk["pages"][0]["url"], VARIANT)
        self.assertEqual(fake_db.bump_fetch_count.await_count, 2)
        for call in fake_db.bump_fetch_count.await_args_list:
            self.assertEqual(call.args, (CANONICAL,))

    async def test_variant_crawls_share_one_owner(self) -> None:
        """A worker crawl and a variant on-demand crawl share one result and slot."""
        started = asyncio.Event()
        release = asyncio.Event()
        joined = asyncio.Event()
        original = queue.crawl_deduped

        async def crawl(url: str, trigger: str) -> dict:
            """Hold the owner until its variant has joined."""
            started.set()
            await release.wait()
            return {"url": url, "status": "ok"}

        async def deduped(
            url: str,
            trigger: str,
            fn: Callable[[], Awaitable[object]],
        ) -> object:
            """Signal the second caller before it awaits the owner's future."""
            if queue.INFLIGHT.get(url) is not None:
                joined.set()
            return await original(url, trigger, fn)

        attempt = AsyncMock(side_effect=crawl)
        with (
            patch.object(worker, "_crawl_and_store", attempt),
            patch.object(queue, "INFLIGHT", queue.InFlight()),
            patch.object(queue, "crawl_deduped", deduped),
            patch.object(queue, "crawl_semaphore", return_value=asyncio.Semaphore(1)),
        ):
            owner = asyncio.create_task(worker.crawl_url(CANONICAL, "search"))
            await asyncio.wait_for(started.wait(), timeout=1)
            waiter = asyncio.create_task(worker.crawl_url(VARIANT, "fetch"))
            try:
                await asyncio.wait_for(joined.wait(), timeout=1)
            finally:
                release.set()
                results = await asyncio.gather(owner, waiter)
            self.assertEqual(results[0], results[1])
            self.assertEqual(len(queue.INFLIGHT), 0)
        attempt.assert_awaited_once_with(CANONICAL, "search")

    async def test_crawl_updates_canonical_state(self) -> None:
        """Success resets backoff; refresh failure records status and bumps the same row."""
        fake_db = Mock(
            page_get=AsyncMock(return_value={"title": "Old title"}),
            execute=AsyncMock(),
            log_crawl=AsyncMock(),
            refresh_fail_bump=AsyncMock(),
            refresh_success_reset=AsyncMock(),
        )
        with (
            patch.object(worker, "db", fake_db),
            patch.object(worker.crawler, "fit_markdown", AsyncMock(return_value=(None, "Body"))),
            patch.object(worker, "store_page", AsyncMock(return_value=("ok", 1))) as store,
        ):
            await worker._crawl_and_store(VARIANT, "manual")
            store.assert_awaited_once_with(CANONICAL, "Body", title="Old title")
            fake_db.refresh_success_reset.assert_awaited_once_with(CANONICAL)
            error = crawler.CrawlError(CANONICAL, "failed")
            with patch.object(worker.crawler, "fit_markdown", AsyncMock(side_effect=error)):
                with self.assertRaises(crawler.CrawlError):
                    await worker._crawl_and_store(VARIANT, "refresh")
        self.assertEqual(fake_db.execute.await_args.args[1], (error.status_label(), CANONICAL))
        fake_db.refresh_fail_bump.assert_awaited_once_with(CANONICAL)

    async def test_challenge_lookup_and_existing_row_routing(self) -> None:
        """Variants see pending challenges and route existing rows by the canonical key."""
        fake_db = Mock(
            page_get=AsyncMock(return_value=None),
            worker_paused=AsyncMock(return_value=False),
            queue_challenge_in_flight=AsyncMock(return_value=True),
            queue_route_to_cf=AsyncMock(),
            refresh_fail_bump=AsyncMock(),
        )
        with patch.object(fetch, "db", fake_db), patch.object(fetch, "crawl_url", AsyncMock()) as crawl:
            with self.assertRaises(crawler.CrawlError):
                await fetch._resolve_page(VARIANT)
            crawl.assert_not_awaited()
            fake_db.queue_challenge_in_flight.assert_awaited_once_with(CANONICAL)
            crawl.side_effect = ChallengeDetected(VARIANT)
            with patch.object(fetch.queue, "enqueue", AsyncMock(return_value=False)):
                with self.assertRaises(crawler.CrawlError):
                    await fetch._probe_crawl(VARIANT)
            await fetch._record_failed_refresh(VARIANT, True)
        fake_db.queue_route_to_cf.assert_awaited_once_with(CANONICAL)
        fake_db.refresh_fail_bump.assert_awaited_once_with(CANONICAL)

    async def test_seed_reports_canonical_queue_row(self) -> None:
        """A variant seed reports the actual enqueue time and queue position."""
        enqueued_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        row = {"status": "pending", "attempts": 0, "enqueued_at": enqueued_at}
        server = Mock()
        registered = []
        server.tool.return_value = lambda fn: registered.append(fn) or fn
        fake_db = Mock(fetch_one=AsyncMock(side_effect=[row, {"ahead": 4}]))
        with (
            patch.object(tools, "db", fake_db),
            patch.object(tools.queue, "enqueue", AsyncMock(return_value=True)),
        ):
            # Capture the registered callable rather than depending on MCP internals.
            tools._tool_seed_url(server)
            result = await registered[0](VARIANT)
        self.assertEqual(result["queue"]["status"], "pending")
        self.assertEqual(result["ahead_in_queue"], 4)
        self.assertEqual(fake_db.fetch_one.await_args_list[0].args[1], (CANONICAL,))
        self.assertEqual(fake_db.fetch_one.await_args_list[1].args[1], (enqueued_at,))

    async def test_tick_trims_on_another_thread(self) -> None:
        """The awaited trim runs outside the event-loop thread."""
        threads = []
        fake_db = Mock(worker_paused=AsyncMock(return_value=True))
        with (
            patch.object(worker, "db", fake_db),
            patch.object(worker, "get_settings", return_value=Settings(_env_file=None)),
            patch.object(worker, "_tick_lock", asyncio.Lock()),
            patch.object(worker, "_drain_queue", AsyncMock(return_value={})),
            patch.object(worker, "_drain_cf_queue", AsyncMock(return_value={})),
            patch.object(worker, "_log_event", AsyncMock()),
            patch.object(worker, "_retention_sweep", AsyncMock()),
            patch.object(worker, "_trim_memory", lambda: threads.append(threading.get_ident())),
            patch.dict(worker.STATE),
        ):
            await worker.tick()
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    async def test_merge_deletes_before_rename_and_rolls_back(self) -> None:
        """An FK validation failure leaves the entire merge in its transaction."""
        conn = Mock(execute=AsyncMock())
        transaction_outcomes = []

        @asynccontextmanager
        async def transaction() -> AsyncIterator[Mock]:
            """Record whether the shared transaction commits or rolls back."""
            try:
                yield conn
            except Exception:
                transaction_outcomes.append("rollback")
                raise
            else:
                transaction_outcomes.append("commit")

        groups = {CANONICAL: [
            {"url": CANONICAL, "fetch_count": 1, "md_len": 20},
            {"url": VARIANT, "fetch_count": 5, "md_len": 30},
        ]}
        fake_db = Mock(transaction=transaction)
        with patch.object(merge_dupes, "db", fake_db):
            self.assertEqual(await merge_dupes._merge_groups(groups), (1, 1))
            statements = [call.args for call in conn.execute.await_args_list]
            self.assertEqual(statements[1], ("DELETE FROM chunks WHERE url = %s", (CANONICAL,)))
            self.assertEqual(statements[2], ("DELETE FROM pages WHERE url = %s", (CANONICAL,)))
            self.assertEqual(statements[3][1], (CANONICAL, VARIANT))
            self.assertEqual(statements[4][1], (CANONICAL, VARIANT))
            conn.execute.side_effect = [None] * 5 + [RuntimeError("FK validation failed")]
            with self.assertRaisesRegex(RuntimeError, "FK validation failed"):
                await merge_dupes._merge_groups(groups)
        self.assertEqual(transaction_outcomes, ["commit", "rollback"])


if __name__ == "__main__":
    unittest.main()

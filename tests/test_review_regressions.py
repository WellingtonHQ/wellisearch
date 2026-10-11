"""PR #15 regressions: canonical URL state, atomic merges, and off-loop trimming.

Run with python tests/test_review_regressions.py; no network or database required.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import datetime as dt
import json
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from wellisearch import app, crawler, fetch, merge_dupes, queue, search_web, tools, worker
from wellisearch.config import Settings
from wellisearch.crawl.extractors.homedepot import _product_node
from wellisearch.crawl.results import ChallengeDetected

CANONICAL = "https://example.com/article"
VARIANT = CANONICAL + "?utm_source=review#section"
STORED = {"title": "Article", "fit_markdown": "Stored article.", "fetch_count": 2}


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    """Exercise variant URLs through the public paths and their shared state."""

    async def test_fetch_counts_and_response_urls(self) -> None:
        """Single and bulk reads bump the stored key while preserving requested URLs."""
        fake_db = Mock(
            page_get=AsyncMock(return_value=STORED),
            bump_fetch_count=AsyncMock(),
            bump_fetch_counts=AsyncMock(),
        )
        with patch.object(fetch, "db", fake_db):
            single = await fetch.fetch_page(VARIANT)
            bulk = await fetch.fetch_pages([VARIANT])
        self.assertTrue(single["from_index"])
        self.assertEqual(single["url"], VARIANT)
        self.assertEqual(bulk["pages"][0]["url"], VARIANT)
        fake_db.bump_fetch_count.assert_awaited_once_with(CANONICAL)
        fake_db.bump_fetch_counts.assert_awaited_once_with([CANONICAL])

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

    async def test_refresh_reads_back_the_canonical_row(self) -> None:
        """A short-URL refresh reports last_crawled from the row stored under its final URL."""
        short = "https://a.co/xyz3d"
        crawled_at = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)
        result = {"url": CANONICAL, "status": "ok", "ms": 5, "chunks": 2}
        stored = dict(STORED, last_crawled=crawled_at)

        server = Mock()
        registered = []
        server.tool.return_value = lambda fn: registered.append(fn) or fn
        fake_db = Mock(page_get=AsyncMock(return_value=stored))
        with (
            patch.object(tools, "db", fake_db),
            patch.object(tools, "crawl_url", AsyncMock(return_value=result)),
        ):
            tools._tool_refresh_page(server)
            out = await registered[0](short)
        self.assertTrue(out["ok"])
        self.assertEqual(out["last_crawled"], str(crawled_at))
        fake_db.page_get.assert_awaited_once_with(CANONICAL)

    async def test_api_refresh_reads_back_the_canonical_row(self) -> None:
        """The REST refresh endpoint reports last_crawled from the final URL's row."""
        short = "https://a.co/xyz3d"
        crawled_at = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)
        result = {"url": CANONICAL, "status": "ok", "ms": 5, "chunks": 2}
        stored = dict(STORED, last_crawled=crawled_at)
        fake_db = Mock(page_get=AsyncMock(return_value=stored), log_event=AsyncMock())
        with (
            patch.object(app, "db", fake_db),
            patch.object(app, "crawl_url", AsyncMock(return_value=result)),
        ):
            out = await app.api_refresh(app.RefreshBody(url=short))
        self.assertTrue(out["ok"])
        self.assertEqual(out["last_crawled"], crawled_at)
        fake_db.page_get.assert_awaited_once_with(CANONICAL)

    async def test_refresh_error_reports_canonical_status(self) -> None:
        """A failed short-URL refresh reports last_status from the final URL's row."""
        short = "https://a.co/xyz3d"
        stored = {"last_status": "error"}
        server = Mock()
        registered = []
        server.tool.return_value = lambda fn: registered.append(fn) or fn
        fake_db = Mock(page_get=AsyncMock(side_effect=[None, stored]))
        with (
            patch.object(tools, "db", fake_db),
            patch.object(
                tools, "crawl_url", AsyncMock(side_effect=crawler.CrawlError(CANONICAL, "failed"))
            ),
            patch.object(tools, "resolve_short_url", AsyncMock(return_value=CANONICAL)),
        ):
            tools._tool_refresh_page(server)
            out = await registered[0](short)
        self.assertFalse(out["ok"])
        self.assertEqual(out["last_status"], "error")
        self.assertEqual(fake_db.page_get.await_args_list[1].args, (CANONICAL,))

    def test_candidate_head_covers_requested_k(self) -> None:
        """A large k gates every fetched row; a small k keeps the union window."""
        s = Settings(_env_file=None)
        rows = [
            {"url": f"https://example.com/p{i}", "score": 1.0 - i * 0.001, "similarity": 0.4}
            for i in range(200)
        ]
        with patch.object(search_web, "get_settings", return_value=s):
            big = search_web._candidate_rows(rows[: max(s.SEARCH_GATE_MIN_K + s.SEARCH_TOP_BY_SIM, 200)], k=200)
            small = search_web._candidate_rows(rows[: s.SEARCH_GATE_MIN_K + s.SEARCH_TOP_BY_SIM], k=5)
        self.assertEqual(len(big), 200)
        self.assertEqual(len(small), s.SEARCH_GATE_MIN_K + s.SEARCH_TOP_BY_SIM)

    async def test_local_mode_fetches_the_raw_window(self) -> None:
        """Local mode fetches exactly k rows at the default vector leg; auto widens both."""
        s = Settings(_env_file=None)
        fake_db = Mock(fetch_all=AsyncMock(return_value=[]))
        with (
            patch.object(search_web, "db", fake_db),
            patch.object(search_web, "embed_one", lambda q: [0.1] * 384),
            patch.object(search_web, "get_settings", return_value=s),
        ):
            await search_web._search_local_index("q", 5, None)
            sql, params = fake_db.fetch_all.await_args.args[:2]
            self.assertEqual(sql.count("%s"), 3)
            self.assertEqual(params[-1], 5)
            await search_web._search_local_index("q", 5, None, widened=True)
            sql, params = fake_db.fetch_all.await_args.args[:2]
            self.assertEqual(sql.count("%s"), 4)
            self.assertEqual(params[2], max(5, s.SEARCH_GATE_MIN_K + s.SEARCH_TOP_BY_SIM))
            self.assertEqual(params[3], s.SEARCH_VECTOR_LEG_LIMIT)

    def test_job_board_paths_match_case_insensitively(self) -> None:
        """Config entries with mixed-case paths match lowercase live URLs."""
        boards = ("glassdoor.com/Job", "ziprecruiter.com/Jobs")
        self.assertTrue(search_web._is_job_board("https://www.glassdoor.com/job/listings", boards))
        self.assertTrue(search_web._is_job_board("https://ziprecruiter.com/jobs/search?query=engineer", boards))
        self.assertFalse(search_web._is_job_board("https://glassdoor.com/reviews", boards))

    def test_uk_second_level_domains_are_distinct(self) -> None:
        """*.gov.uk / *.ac.uk sites keep their own registrable domain for the cap."""
        self.assertEqual(search_web._registrable_domain("https://example.gov.uk/page"), "example.gov.uk")
        self.assertEqual(search_web._registrable_domain("https://ox.ac.uk/x"), "ox.ac.uk")
        self.assertEqual(search_web._registrable_domain("https://www.gov.uk/government"), "gov.uk")

    def test_homedepot_accepts_a_list_at_type(self) -> None:
        """A Product node whose @type is a list still matches (Walmart parity)."""
        html = (
            '<script type="application/ld+json">'
            + json.dumps({"@graph": [{"@type": ["Product", "WebPage"], "name": "Widget"}]})
            + "</script>"
        )
        node = _product_node(html)
        self.assertIsNotNone(node)
        self.assertEqual(node["name"], "Widget")

    async def test_cap_shrink_flags_degraded(self) -> None:
        """A per-domain cap that shrinks a would-be full set below k sets degraded."""
        s = Settings(_env_file=None)
        same_domain = [
            {"url": f"https://shop.example.com/p{i}", "score": 1.0 - i * 0.001,
             "similarity": 0.6, "coverage": 0.9}
            for i in range(5)
        ]
        diverse = [
            {"url": f"https://site{i}.com/p", "score": 1.0 - i * 0.001,
             "similarity": 0.6, "coverage": 0.9}
            for i in range(5)
        ]
        fake_db = Mock(log_search=AsyncMock(), mark_search_hits=AsyncMock())
        with (
            patch.object(search_web, "db", fake_db),
            patch.object(
                search_web, "_search_local_index", AsyncMock(return_value=(same_domain, 0, None))
            ),
            patch.object(search_web, "get_settings", return_value=s),
        ):
            out = await search_web.search_web("q", num_results=5)
        self.assertEqual(out["source"], "local")
        self.assertTrue(out["degraded"])
        self.assertEqual(len(out["results"]), s.SEARCH_MAX_PER_DOMAIN)

        with (
            patch.object(search_web, "db", fake_db),
            patch.object(
                search_web, "_search_local_index", AsyncMock(return_value=(diverse, 0, None))
            ),
            patch.object(search_web, "get_settings", return_value=s),
        ):
            out = await search_web.search_web("q", num_results=5)
        self.assertFalse(out["degraded"])
        self.assertEqual(len(out["results"]), 5)

    async def test_num_results_clamped_to_max(self) -> None:
        """An oversized num_results is capped at SEARCH_MAX_K before the index leg."""
        s = Settings(_env_file=None)
        seen_k = []

        async def fake_index(query, k, max_age_days, widened=False):
            seen_k.append(k)
            return [], 0, None

        fake_db = Mock(log_search=AsyncMock())
        with (
            patch.object(search_web, "db", fake_db),
            patch.object(search_web, "_search_local_index", fake_index),
            patch.object(search_web, "get_settings", return_value=s),
        ):
            out = await search_web.search_web("q", num_results=10_000, search_mode="local")
        self.assertEqual(seen_k, [s.SEARCH_MAX_K])
        self.assertEqual(out["source"], "error")

    async def test_fetch_pages_bumps_counts_in_one_statement(self) -> None:
        """A bulk read bumps every page's fetch_count with one UPDATE, not N."""
        pages = [
            {"url": u, "title": t, "content": t, "from_index": True,
             "fetch_count": 1, "index_ms": 0}
            for u, t in (
                ("https://example.com/a?utm_source=test", "A"),
                ("https://example.com/b?gclid=x", "B"),
                ("https://example.com/b", "B"),
            )
        ]
        fake_db = Mock(bump_fetch_counts=AsyncMock())
        with (
            patch.object(fetch, "db", fake_db),
            patch.object(fetch, "_resolve_all", AsyncMock(return_value=(pages, []))),
            patch.object(fetch, "get_settings", return_value=Settings(_env_file=None)),
        ):
            out = await fetch.fetch_pages(
                [
                    "https://example.com/a?utm_source=test",
                    "https://example.com/b?gclid=x",
                    "https://example.com/b",
                ]
            )
        self.assertTrue(out["ok"])
        fake_db.bump_fetch_counts.assert_awaited_once_with(
            ["https://example.com/a", "https://example.com/b"]
        )


if __name__ == "__main__":
    unittest.main()

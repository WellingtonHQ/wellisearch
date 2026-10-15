"""Regression tests for bounded browser ownership, cancellation, and worker load."""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from wellisearch import activity_log, embed, fetch, tools, worker
from wellisearch.config import Settings
from wellisearch.crawl import pool as pool_module
from wellisearch.crawl.lane import CF
from wellisearch.crawl.policy import DEFAULT_POLICY
from wellisearch.crawl.pool import BrowserPool
from wellisearch.crawl.results import PayloadRejectedError, Rendered
from wellisearch.crawl.tiers import browser, stealth


class Page:
    """Track page closure and optionally hold cleanup until a test releases it."""

    def __init__(self, blocked: bool = False) -> None:
        """Initialize close synchronization and call counters."""
        self.closing = asyncio.Event()
        self.finish = asyncio.Event()
        self.closed = False
        if not blocked:
            self.finish.set()

    async def close(self) -> None:
        """Wait for permission before marking the page closed."""
        self.closing.set()
        await self.finish.wait()
        self.closed = True


class Context:
    """Represent a persistent context without a real browser."""

    def __init__(self, pages: list[Page] | None = None) -> None:
        """Initialize owned pages and close tracking."""
        self.pages = pages or []
        self.closed = False

    async def new_page(self) -> Page:
        """Return an existing test page."""
        return self.pages[0]

    async def close(self) -> None:
        """Record context retirement."""
        self.closed = True


class WorkerConnection:
    """Model a pipe receive that can finish only when its worker is killed."""

    def __init__(self) -> None:
        """Initialize thread-safe synchronization for a fake worker."""
        self.started = threading.Event()
        self.finish = threading.Event()

    def recv(self) -> Rendered:
        """Block just like a worker that has not sent its browser result yet."""
        self.started.set()
        self.finish.wait()
        return Rendered("body", None, 200, 0, "stealth")

    def close(self) -> None:
        """Unblock pipe readers when the worker has been disposed."""
        self.finish.set()


class ResourceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Exercise failures that previously left active browsers outside their limits."""

    def test_document_embedding_batch_memory_is_bounded(self) -> None:
        """Long page chunks use a small ONNX batch without changing their vectors."""
        settings = Settings(_env_file=None, EMBED_BATCH_SIZE=16)
        model = Mock()
        model.embed.return_value = [[1.0], [2.0]]
        with (
            patch.object(embed, "get_settings", return_value=settings),
            patch.object(embed, "_get_model", return_value=model),
        ):
            self.assertEqual(embed.embed(["first", "second"]), [[1.0], [2.0]])
        model.embed.assert_called_once_with(["first", "second"], batch_size=16)

    async def test_mcp_omitted_bulk_budgets_are_not_unlimited(self) -> None:
        """Omission uses configured budgets while explicit null retains its contract."""
        settings = Settings(_env_file=None, FETCH_MAX_CHARS=40000, FETCH_PER_PAGE_CHARS=12000)
        handlers = []
        server = Mock()
        server.tool.return_value = lambda fn: handlers.append(fn) or fn
        with (
            patch.object(tools, "get_settings", return_value=settings),
            patch.object(tools, "_fetch_pages", AsyncMock(return_value={})) as bulk,
            patch.object(tools, "_fmt", return_value="output"),
        ):
            tools._tool_fetch_pages(server)
            await handlers[0](["https://example.com"])
            self.assertEqual(bulk.await_args.kwargs["max_chars"], 40000)
            self.assertEqual(bulk.await_args.kwargs["per_page_chars"], 12000)
            await handlers[0](["https://example.com"], max_chars=None, per_page_chars=None)
            self.assertIsNone(bulk.await_args.kwargs["max_chars"])
            self.assertIsNone(bulk.await_args.kwargs["per_page_chars"])

    async def test_dashboard_logs_are_limited_before_entering_python(self) -> None:
        """Window totals must not require Python to materialize the entire log day."""
        fake_db = Mock(fetch_one=AsyncMock(return_value={"total": 20000, "logs": [{"kind": "event"}]}))
        with patch.object(activity_log, "db", fake_db):
            result = await activity_log.window_logs(86400, 200, " Example ")
        self.assertEqual(result["total"], 20000)
        self.assertEqual(len(result["logs"]), 1)
        sql, params = fake_db.fetch_one.await_args.args
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params, (86400, 86400, 86400, "example", "example", "example", 200))

    async def test_cancel_during_page_close_returns_lease_once(self) -> None:
        """Repeated cancellation cannot skip a lease release or orphan cleanup."""
        page = Page(blocked=True)
        ctx = Context([page])
        pool = Mock(acquire=AsyncMock(return_value=ctx), release=AsyncMock(), invalidate=AsyncMock())
        tier = browser.BrowserTier()
        with (
            patch.object(browser, "get_pool", return_value=pool),
            patch.object(tier, "_crawl", AsyncMock(return_value=Rendered("body", None, 200, 0, "browser"))),
        ):
            task = asyncio.create_task(tier.fetch("https://example.com", DEFAULT_POLICY))
            await page.closing.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            page.finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        pool.release.assert_awaited_once_with(ctx, "shared")
        self.assertTrue(page.closed)

    async def test_stuck_page_close_retires_context(self) -> None:
        """A page that cannot close never consumes its lease indefinitely."""
        page = Page(blocked=True)
        ctx = Context([page])
        pool = Mock(acquire=AsyncMock(return_value=ctx), release=AsyncMock(), invalidate=AsyncMock())
        tier = browser.BrowserTier()
        settings = Settings(_env_file=None, CRAWL_BROWSER_CLEANUP_TIMEOUT_S=0.01)
        with (
            patch.object(browser, "get_settings", return_value=settings),
            patch.object(browser, "get_pool", return_value=pool),
            patch.object(tier, "_crawl", AsyncMock(return_value=Rendered("body", None, 200, 0, "browser"))),
        ):
            await asyncio.wait_for(tier.fetch("https://example.com", DEFAULT_POLICY), timeout=1)
        pool.invalidate.assert_awaited_once()
        pool.release.assert_awaited_once_with(ctx, "shared")

    async def test_stuck_driver_cleanup_has_hard_deadline_and_reaps_profile(self) -> None:
        """A close call that ignores cancellation cannot hold the pool lock forever."""
        entered = asyncio.Event()
        finish = asyncio.Event()
        ctx = Context()

        async def stuck_close() -> None:
            """Model Patchright awaiting a response after its channel is wedged."""
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish.wait()
                raise

        ctx.close = stuck_close
        pool = BrowserPool(size=1)
        settings = Settings(_env_file=None, CRAWL_BROWSER_CLEANUP_TIMEOUT_S=0.01)
        pool._contexts["shared"] = ctx
        with (
            patch.object(pool_module, "get_settings", return_value=settings),
            patch.object(pool_module, "_reap_orphans") as reap,
        ):
            started = time.monotonic()
            closed = await asyncio.wait_for(pool._close_context("shared", ctx), timeout=0.5)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(closed)
            self.assertTrue(entered.is_set())
            reap.assert_called_once()
            finish.set()
            await asyncio.sleep(0)

    async def test_driver_start_has_deadline_and_stops_unpublished_manager(self) -> None:
        """A stalled Playwright startup is bounded and its owned driver is stopped."""
        started = asyncio.Event()

        async def stalled_start() -> None:
            """Model a driver handshake that never completes on its own."""
            started.set()
            await asyncio.Event().wait()

        process = Mock(returncode=None)
        manager = Mock()
        manager.start = AsyncMock(side_effect=stalled_start)
        manager.__aexit__ = AsyncMock()
        manager._connection = Mock(_transport=Mock(_proc=process))
        pool = BrowserPool(size=1)
        settings = Settings(
            _env_file=None,
            CRAWL_BROWSER_LAUNCH_TIMEOUT_S=0.01,
            CRAWL_BROWSER_CLEANUP_TIMEOUT_S=0.01,
            CRAWL_LAUNCH_RETRY_AFTER_S=0,
        )
        with (
            patch.object(pool_module, "get_settings", return_value=settings),
            patch("patchright.async_api.async_playwright", return_value=manager),
            patch.object(pool_module, "_reap_orphans"),
        ):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(pool.acquire("shared"), timeout=0.5)
        self.assertTrue(started.is_set())
        manager.__aexit__.assert_awaited_once_with(None, None, None)
        process.kill.assert_called_once()
        self.assertIsNone(pool._pw)
        self.assertIsNone(pool._pw_manager)
        await asyncio.wait_for(pool._sem.acquire(), timeout=1)
        pool._sem.release()

    async def test_oversized_browser_dom_is_rejected_before_content_transfer(self) -> None:
        """The browser returns an oversized sentinel without transferring its DOM."""
        settings = Settings(_env_file=None, CRAWL_HTML_MAX_BYTES=64)
        page = Mock(
            evaluate=AsyncMock(return_value=None),
            content=AsyncMock(return_value="oversized"),
        )
        with patch.object(browser, "get_settings", return_value=settings):
            with self.assertRaisesRegex(PayloadRejectedError, "CRAWL_HTML_MAX_BYTES"):
                await browser._page_html(page)
        page.evaluate.assert_awaited_once()
        page.content.assert_not_awaited()

    async def test_browser_html_capture_is_atomic_with_size_check(self) -> None:
        """Later DOM growth cannot replace the bounded snapshot returned by evaluate."""
        settings = Settings(_env_file=None, CRAWL_HTML_MAX_BYTES=64)
        page = Mock(
            evaluate=AsyncMock(return_value="<html>bounded snapshot</html>"),
            content=AsyncMock(return_value="x" * 1000000),
        )
        with patch.object(browser, "get_settings", return_value=settings):
            html = await browser._page_html(page)
        self.assertEqual(html, "<html>bounded snapshot</html>")
        page.evaluate.assert_awaited_once()
        page.content.assert_not_awaited()

    async def test_challenge_budget_bounds_initial_botwall_scan(self) -> None:
        """A stalled HTML scan cannot hold a challenge lane beyond its budget."""
        entered = asyncio.Event()

        async def stalled_scan(*args: object) -> str | None:
            """Model a scanner awaiting its bounded processing worker."""
            entered.set()
            await asyncio.Event().wait()

        tier = browser.BrowserTier()
        started = time.monotonic()
        with patch.object(browser, "_botwall", side_effect=stalled_scan):
            html = await tier._resolve_challenge(
                Mock(), "https://example.com", "wall", 200, budget=0.02
            )
        self.assertTrue(entered.is_set())
        self.assertEqual(html, "wall")
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_challenge_budget_bounds_turnstile_actions(self) -> None:
        """A stalled checkbox action cannot exceed the challenge solve budget."""
        entered = asyncio.Event()

        async def detected_wall(*args: object) -> str:
            """Keep the fixture on the challenge branch until its action stalls."""
            return "cf-challenge"

        async def stalled_click(page: object) -> None:
            """Model a click operation that never returns before cancellation."""
            entered.set()
            await asyncio.Event().wait()

        tier = browser.BrowserTier()
        started = time.monotonic()
        with (
            patch.object(browser, "_botwall", side_effect=detected_wall),
            patch.object(browser, "_click_turnstile_checkbox", side_effect=stalled_click),
        ):
            html = await tier._resolve_challenge(
                Mock(), "https://example.com", "wall", 200, budget=0.02
            )
        self.assertTrue(entered.is_set())
        self.assertEqual(html, "wall")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_stealth_oversized_dom_is_cleared_before_scrapling_serializes(self) -> None:
        """Scrapling must serialize a blank page, not retain an oversized DOM string."""

        class FakePage:
            """Minimal synchronous page that records the browser-side preflight."""

            status = 200
            headers: dict[str, str] = {}

            def evaluate(self, script: str, limit: int) -> int | None:
                """Report an oversized DOM without serializing it."""
                return None

            def title(self) -> str:
                """Return the original page title."""
                return "Original"

            def goto(self, url: str) -> None:
                """Record navigation away before Scrapling creates its response."""
                self.navigated = url

            @property
            def html_content(self) -> str:
                """Fail if the tier tries to retain Scrapling's serialized response."""
                raise AssertionError("oversized HTML must be rejected before access")

        page = FakePage()
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)

        def fetch(url: str, **kwargs: Any) -> FakePage:
            """Run the real page action hook before returning Scrapling's page."""
            kwargs["page_action"](page)
            return page

        session.fetch.side_effect = fetch
        settings = Settings(_env_file=None, CRAWL_HTML_MAX_BYTES=64)
        with (
            patch.object(stealth, "get_settings", return_value=settings),
            patch("scrapling.fetchers.StealthySession", return_value=session),
        ):
            with self.assertRaisesRegex(PayloadRejectedError, "CRAWL_HTML_MAX_BYTES"):
                stealth.StealthTier()._fetch_sync("https://example.com", 1)
        self.assertEqual(page.navigated, "about:blank")

    def test_stealth_uses_bounded_snapshot_not_scrapling_page_content(self) -> None:
        """The original DOM is cleared before Scrapling creates its response."""

        class FakePage:
            """Minimal synchronous page with a bounded snapshot and tracked title."""

            status = 200
            headers: dict[str, str] = {"Content-Type": "text/html"}

            def evaluate(self, script: str, limit: int) -> str:
                """Return the bounded capture from the same operation as its size check."""
                return "<html><title>Saved</title><body>Article</body></html>"

            def title(self) -> str:
                """Return the page title before navigation to the blank document."""
                return "Saved"

            def goto(self, url: str) -> None:
                """Record navigation away before Scrapling creates its response."""
                self.navigated = url

            @property
            def html_content(self) -> str:
                """Fail if the implementation reads Scrapling's second serialization."""
                raise AssertionError("Scrapling's page serialization must not be used")

        page = FakePage()
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)

        def fetch(url: str, **kwargs: Any) -> FakePage:
            """Run the page action before creating Scrapling's response."""
            kwargs["page_action"](page)
            return page

        session.fetch.side_effect = fetch
        settings = Settings(_env_file=None, CRAWL_HTML_MAX_BYTES=64)
        with (
            patch.object(stealth, "get_settings", return_value=settings),
            patch("scrapling.fetchers.StealthySession", return_value=session),
        ):
            rendered = stealth.StealthTier()._fetch_sync("https://example.com", 1)
        self.assertEqual(rendered.html, "<html><title>Saved</title><body>Article</body></html>")
        self.assertEqual(rendered.title, "Saved")
        self.assertEqual(page.navigated, "about:blank")

    async def test_old_lease_release_wakes_waiter_for_retiring_replacement(self) -> None:
        """The last release from an invalidated generation cannot strand a waiter."""
        settings = Settings(_env_file=None, CRAWL_BROWSER_MAX_USES=1)
        pool = BrowserPool(size=2)
        old, replacement = Context(), Context()
        pool._contexts["shared"] = old
        pool._in_use["shared"] = 2
        with (
            patch.object(pool_module, "get_settings", return_value=settings),
            patch.object(pool_module, "_reap_orphans"),
            patch.object(pool, "_launch", AsyncMock(return_value=replacement)),
            patch.object(pool, "_ensure_playwright", AsyncMock(return_value=object())),
        ):
            await pool.invalidate("shared", old, RuntimeError("closed context"))
            await pool.release(old, "shared")
            # One stale old lease remains, but a replacement context may now launch.
            await pool.acquire("shared")
            await pool.release(replacement, "shared")
            waiter = asyncio.create_task(pool.acquire("shared"))
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            await pool.release(old, "shared")
            self.assertIs(await asyncio.wait_for(waiter, timeout=1), replacement)
            await pool.release(replacement, "shared")

    async def test_challenge_solver_uses_challenge_budget_not_navigation_timeout(self) -> None:
        """A 300-second navigation budget must not replace the 40-second solve cap."""
        page = Mock(
            goto=AsyncMock(return_value=Mock(status=200, headers={})),
            evaluate=AsyncMock(return_value="body"),
            content=AsyncMock(return_value="body"),
            title=AsyncMock(return_value="Title"),
        )
        tier = browser.BrowserTier()
        settings = Settings(_env_file=None, CRAWL_CF_TIMEOUT_S=300, CRAWL_CHALLENGE_BUDGET_S=40)
        with (
            patch.object(browser, "get_settings", return_value=settings),
            patch.object(browser, "get_lane", return_value=CF),
            patch.object(browser, "settle", AsyncMock()),
            patch.object(tier, "_resolve_challenge", AsyncMock(return_value="body")) as solve,
        ):
            await tier._crawl(page, "https://example.com", DEFAULT_POLICY)
        self.assertEqual(solve.await_args.kwargs["budget"], 40)

    async def test_idle_cleanup_closes_unreturned_pages_and_popups(self) -> None:
        """The last lease sweeps pages even when new_page never returned a handle."""
        pages = [Page(), Page(), Page()]
        ctx = Context(pages)
        pool = BrowserPool(size=1)
        pool._contexts["shared"] = ctx
        pool._in_use["shared"] = 1
        await pool._sem.acquire()
        await pool.release(ctx, "shared")
        self.assertTrue(all(page.closed for page in pages))
        await asyncio.wait_for(pool._sem.acquire(), timeout=1)
        pool._sem.release()

    async def test_idle_cleanup_preserves_only_owned_blank_anchor(self) -> None:
        """Headed persistent Chrome must retain one blank tab or it exits."""
        anchor, orphan = Page(), Page()
        ctx = Context([anchor, orphan])
        pool = BrowserPool(size=1)
        pool._contexts["shared"] = ctx
        pool._anchors["shared"] = anchor
        pool._in_use["shared"] = 1
        await pool._sem.acquire()
        await pool.release(ctx, "shared")
        self.assertFalse(anchor.closed)
        self.assertTrue(orphan.closed)

    async def test_warm_browser_count_matches_pool_size(self) -> None:
        """Visiting many dedicated profiles cannot retain eight browsers per pool."""
        pool = BrowserPool(size=1)
        contexts = []

        async def launch(key: str, playwright: object) -> Context:
            """Create one fake context for each new profile."""
            ctx = Context()
            contexts.append(ctx)
            return ctx

        with (
            patch.object(pool, "_launch", side_effect=launch),
            patch.object(pool, "_ensure_playwright", AsyncMock(return_value=object())),
            patch.object(pool_module, "_reap_orphans"),
        ):
            for i in range(20):
                key = f"site{i}.example"
                ctx = await pool.acquire(key)
                await pool.release(ctx, key)
                self.assertLessEqual(len(pool._contexts), 1)
                self.assertLessEqual(len(pool._created_at), 1)
                self.assertLessEqual(len(pool._uses), 1)
            self.assertTrue(all(ctx.closed for ctx in contexts[:-1]))
            await pool.close_all()

    async def test_contexts_recycle_after_use_limit(self) -> None:
        """Recycling releases accumulated browser and driver page allocations."""
        settings = Settings(_env_file=None, CRAWL_BROWSER_MAX_USES=2)
        pool = BrowserPool(size=1)
        ctx = Context()
        pool._contexts["shared"] = ctx
        with patch.object(pool_module, "get_settings", return_value=settings), patch.object(
            pool_module, "_reap_orphans"
        ):
            for _ in range(2):
                lease = await pool.acquire("shared")
                await pool.release(lease, "shared")
        self.assertTrue(ctx.closed)
        self.assertFalse(pool._contexts)

    async def test_recycling_drains_busy_context_before_replacement(self) -> None:
        """A hot shared profile stops admitting pages when its use budget expires."""
        settings = Settings(_env_file=None, CRAWL_BROWSER_MAX_USES=1)
        pool = BrowserPool(size=2)
        old, new = Context(), Context()
        pool._contexts["shared"] = old
        with (
            patch.object(pool_module, "get_settings", return_value=settings),
            patch.object(pool_module, "_reap_orphans"),
            patch.object(pool, "_launch", AsyncMock(return_value=new)),
            patch.object(pool, "_ensure_playwright", AsyncMock(return_value=object())),
        ):
            await pool.acquire("shared")
            await pool.acquire("shared")
            await pool.release(old, "shared")
            replacement = asyncio.create_task(pool.acquire("shared"))
            await asyncio.sleep(0)
            self.assertFalse(replacement.done())
            self.assertFalse(old.closed)
            await pool.release(old, "shared")
            self.assertIs(await asyncio.wait_for(replacement, timeout=1), new)
            self.assertTrue(old.closed)
            await pool.release(new, "shared")

    async def test_cancelled_transport_reset_still_closes_all_resources(self) -> None:
        """A reset retains ownership of every context and the old driver."""
        gate = Page(blocked=True)
        first = Context()
        first.close = gate.close
        other = Context()
        driver = Mock(stop=AsyncMock())
        pool = BrowserPool(size=2)
        pool._contexts.update({"one": first, "two": other})
        pool._pw = driver
        with patch.object(pool_module, "_reap_orphans"):
            reset = asyncio.create_task(pool.invalidate("one", first, RuntimeError("transport closed")))
            await gate.closing.wait()
            reset.cancel()
            await asyncio.sleep(0)
            reset.cancel()
            gate.finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await reset
        self.assertTrue(other.closed)
        driver.stop.assert_awaited_once()
        self.assertFalse(pool._contexts)
        self.assertIsNone(pool._pw)

    async def test_failed_launch_reaps_processes_and_returns_capacity(self) -> None:
        """A launch failure cannot leave an already-reaped marker or consumed slot."""
        pool = BrowserPool(size=1)
        pool._reaped.add("shared")
        with (
            patch.object(pool, "_ensure_playwright", AsyncMock(return_value=object())),
            patch.object(pool, "_launch", AsyncMock(side_effect=RuntimeError("launch failed"))),
            patch.object(pool_module, "_reap_orphans") as reap,
        ):
            with self.assertRaises(RuntimeError):
                await pool.acquire("shared")
        self.assertEqual(reap.call_count, 2)
        self.assertNotIn("shared", pool._reaped)
        await asyncio.wait_for(pool._sem.acquire(), timeout=1)
        pool._sem.release()

    async def test_cancelled_launch_joins_original_reaper_before_replacement(self) -> None:
        """A delayed orphan scan must never run after a replacement is launched."""
        started = threading.Event()
        finish = threading.Event()
        launches = []

        def reap(profile: str) -> None:
            """Hold the first profile scan while a caller cancellation arrives."""
            if not started.is_set():
                started.set()
                finish.wait()

        async def launch(key: str, playwright: object) -> Context:
            """Record replacement launch only after orphan cleanup has completed."""
            launches.append(key)
            return Context()

        settings = Settings(_env_file=None, CRAWL_LAUNCH_RETRY_AFTER_S=0)
        pool = BrowserPool(size=1)
        with (
            patch.object(pool_module, "get_settings", return_value=settings),
            patch.object(pool, "_ensure_playwright", AsyncMock(return_value=object())),
            patch.object(pool, "_launch", side_effect=launch),
            patch.object(pool_module, "_reap_orphans", side_effect=reap),
        ):
            cancelled = asyncio.create_task(pool.acquire("shared"))
            await asyncio.to_thread(started.wait)
            cancelled.cancel()
            await asyncio.sleep(0)
            self.assertFalse(cancelled.done())
            self.assertEqual(launches, [])
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            replacement = await asyncio.wait_for(pool.acquire("shared"), timeout=1)
            self.assertEqual(launches, ["shared"])
            await pool.release(replacement, "shared")

    async def test_runner_deadline_cancels_active_work_without_starting_backlog(self) -> None:
        """Queued rows cannot begin crawling after their tick's deadline."""
        started = []
        finished = []

        async def process(item: object) -> None:
            """Keep the first item active until the deadline cancels it."""
            started.append(item)
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(item)

        await worker._run_bounded(list(range(1000)), process, time.monotonic() + 0.02, 1)
        self.assertEqual(started, [0])
        self.assertEqual(finished, [0])

    async def test_worker_deadline_returns_claim_without_recording_failure(self) -> None:
        """A budget timeout leaves durable queue work pending for the next tick."""
        started = asyncio.Event()
        fake_db = Mock(
            fetch_all=AsyncMock(return_value=[{"url": "https://example.com"}]),
            queue_claim=AsyncMock(return_value=True),
            queue_release=AsyncMock(),
            queue_done=AsyncMock(),
        )

        async def crawl(url: str, trigger: str) -> None:
            """Simulate a slow crawl that reaches the tick deadline."""
            started.set()
            await asyncio.Event().wait()

        settings = Settings(_env_file=None, WORKER_BUDGET_PER_RUN=2)
        with (
            patch.object(worker, "db", fake_db),
            patch.object(worker, "crawl_url", crawl),
            patch.object(worker, "get_settings", return_value=settings),
        ):
            await worker._drain_queue(time.monotonic() + 0.2)
        self.assertTrue(started.is_set())
        self.assertEqual(fake_db.fetch_all.await_args.args[1], (2,))
        fake_db.queue_release.assert_awaited_once_with("https://example.com")
        fake_db.queue_done.assert_not_awaited()

    async def test_worker_deadline_releases_claim_when_failure_recording_stalls(self) -> None:
        """Cancellation inside an exception handler returns its in-flight queue row."""
        started = asyncio.Event()

        async def stuck_queue_done(*args: object, **kwargs: object) -> None:
            """Leave the row claimed while its error result waits on a stalled DB."""
            started.set()
            await asyncio.Event().wait()

        fake_db = Mock(
            fetch_all=AsyncMock(return_value=[{"url": "https://example.com"}]),
            queue_claim=AsyncMock(return_value=True),
            queue_release=AsyncMock(),
            queue_done=AsyncMock(side_effect=stuck_queue_done),
        )

        async def failed_crawl(url: str, trigger: str) -> None:
            """Fail once so the worker enters its ordinary error-recording path."""
            raise RuntimeError("fixture failure")

        settings = Settings(_env_file=None, WORKER_BUDGET_PER_RUN=2)
        with (
            patch.object(worker, "db", fake_db),
            patch.object(worker, "crawl_url", failed_crawl),
            patch.object(worker, "get_settings", return_value=settings),
        ):
            await worker._drain_queue(time.monotonic() + 0.2)
        self.assertTrue(started.is_set())
        fake_db.queue_done.assert_awaited_once()
        fake_db.queue_release.assert_awaited_once_with("https://example.com")

    async def test_bulk_fetch_admission_is_bounded(self) -> None:
        """Oversized calls fail before creating tasks or reading any stored bodies."""
        settings = Settings(_env_file=None, FETCH_MAX_URLS=2)
        with (
            patch.object(fetch, "get_settings", return_value=settings),
            patch.object(fetch, "_resolve_all", AsyncMock()) as resolve,
        ):
            out = await fetch.fetch_pages(["https://example.com"] * 3)
        self.assertFalse(out["ok"])
        resolve.assert_not_awaited()

    async def test_bulk_fetch_resolution_concurrency_is_bounded(self) -> None:
        """The database and short-link paths share a fixed number of bulk runners."""
        settings = Settings(_env_file=None, FETCH_MAX_PARALLEL=2)
        active = peak = 0

        async def resolve(url: str) -> dict:
            """Measure simultaneous resolutions before returning a stored page."""
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0)
            active -= 1
            return {"url": url}

        with (
            patch.object(fetch, "get_settings", return_value=settings),
            patch.object(fetch, "_resolve_page", resolve),
        ):
            resolved, failed = await fetch._resolve_all([f"https://example.com/{i}" for i in range(20)])
        self.assertEqual(len(resolved), 20)
        self.assertFalse(failed)
        self.assertEqual(peak, 2)

    async def test_stealth_cancel_kills_worker_before_admitting_another(self) -> None:
        """A timed-out synchronous browser cannot continue outside its semaphore."""
        receivers = []
        processes = []
        active = peak = 0

        def pipe(duplex: bool) -> tuple[WorkerConnection, Mock]:
            """Return a new synchronized pipe for each disposable process."""
            receiver = WorkerConnection()
            receivers.append(receiver)
            return receiver, Mock()

        def process(**kwargs: object) -> Mock:
            """Measure actual worker lifetimes rather than awaiting coroutine counts."""
            receiver = receivers[-1]
            child = Mock(pid=12345)
            child.running = False

            def start() -> None:
                """Record a running synchronous worker."""
                nonlocal active, peak
                child.running = True
                active += 1
                peak = max(peak, active)

            def kill() -> None:
                """End the actual worker before a replacement may start."""
                nonlocal active
                child.running = False
                active -= 1
                receiver.finish.set()

            child.start.side_effect = start
            child.kill.side_effect = kill
            child.is_alive.side_effect = lambda: child.running
            processes.append(child)
            return child

        context = Mock(Pipe=pipe, Process=process)
        settings = Settings(_env_file=None, CRAWL_STEALTH_MAX_PARALLEL=1)
        with (
            patch.object(stealth, "_semaphore", None),
            patch.object(stealth, "get_settings", return_value=settings),
            patch.object(stealth.multiprocessing, "get_context", return_value=context),
            patch.object(stealth.os, "killpg", create=True),
            patch.object(stealth.subprocess, "run"),
        ):
            first = asyncio.create_task(stealth.StealthTier().fetch("https://example.com/one", DEFAULT_POLICY))
            await asyncio.sleep(0)
            await asyncio.to_thread(receivers[0].started.wait)
            second = asyncio.create_task(stealth.StealthTier().fetch("https://example.com/two", DEFAULT_POLICY))
            await asyncio.sleep(0)
            self.assertEqual(len(processes), 1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await asyncio.sleep(0)
            self.assertEqual(len(processes), 2)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
        self.assertEqual(active, 0)
        self.assertEqual(peak, 1)
        self.assertTrue(all(child.join.called and child.close.called for child in processes))


if __name__ == "__main__":
    unittest.main()

"""Offline regressions for bounded HTTP bodies, redirects, and HTML processing.

No network, browser, database, embedding model, or .env file is needed. Run with:
    .venv/Scripts/python.exe tests/test_crawl_memory.py
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
import gzip
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from curl_cffi import CurlECode, CurlOpt
from curl_cffi.requests import Response
from curl_cffi.requests.exceptions import RequestException

from wellisearch.crawl import botwall, engine, shorturl
from wellisearch.crawl.extractors import base
from wellisearch.crawl.policy import DEFAULT_POLICY
from wellisearch.crawl.results import ChallengeDetected, Escalate, Fitted, Rendered
from wellisearch.crawl.tiers import http

SHORT_URL = "https://a.co/example"
FINAL_URL = "https://www.amazon.com/dp/example"
WRITE_ERROR = 0xFFFFFFFF
TEST_WAIT_S = 3.0


# ---------------------------------------------------------------------------
# HTTP Bodies
# ---------------------------------------------------------------------------

class HttpMemoryTests(unittest.IsolatedAsyncioTestCase):
    """Drive real callback code with an in-memory curl_cffi session."""

    async def test_chunked_body_stops_before_retaining_an_oversized_chunk(self) -> None:
        """Missing Content-Length must not bypass the decoded-byte cap."""
        session = _Session({"get": [_Hop(chunks=(b"a" * 32, b"b" * 33, b"never"))]})
        with _settings_patch(http), _session_patch(session):
            with self.assertRaisesRegex(ValueError, "CRAWL_HTML_MAX_BYTES"):
                await http.HttpTier().fetch(FINAL_URL, DEFAULT_POLICY)
        body = session.calls[0][1]["content_callback"].__self__
        self.assertEqual(len(body.body), 32)
        self.assertEqual(session.body_chunks, 2)
        self.assertTrue(session.closed)
        self.assertEqual(session.curl_options[CurlOpt.MAXFILESIZE_LARGE], 64)

    async def test_compressed_body_counts_decompressed_bytes(self) -> None:
        """A tiny compressed Content-Length cannot authorize a large inflated body."""
        compressed = gzip.compress(b"x" * 512)
        decoded = gzip.decompress(compressed)
        chunks = tuple(decoded[i:i + 32] for i in range(0, len(decoded), 32))
        hop = _Hop(
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
                "Content-Type": "text/html",
            },
            chunks=chunks,
        )
        session = _Session({"get": [hop]})
        with _settings_patch(http), _session_patch(session):
            with self.assertRaisesRegex(ValueError, "CRAWL_HTML_MAX_BYTES"):
                await http.HttpTier().fetch(FINAL_URL, DEFAULT_POLICY)
        self.assertEqual(session.body_chunks, 3)
        self.assertTrue(session.closed)

    async def test_libcurl_size_backstop_is_a_terminal_payload_rejection(self) -> None:
        """libcurl may reject inflated data before invoking the body callback."""
        session = _Session({"get": [_Hop(error=RequestException("max file size", CurlECode.FILESIZE_EXCEEDED))]})
        with _settings_patch(http), _session_patch(session):
            with self.assertRaisesRegex(ValueError, "CRAWL_HTML_MAX_BYTES"):
                await http.HttpTier().fetch(FINAL_URL, DEFAULT_POLICY)
        self.assertTrue(session.closed)

    async def test_unsupported_types_and_large_lengths_stop_at_headers(self) -> None:
        """Reject a known non-HTML type or declared oversized body before body delivery."""
        cases = (
            ({"Content-Type": "application/pdf"}, "unsupported content type"),
            ({"Content-Type": "application/zip"}, "unsupported content type"),
            ({"Content-Type": "text/html", "Content-Length": "65"}, "CRAWL_HTML_MAX_BYTES"),
            ({"Content-Length": "9" * 10000}, "CRAWL_HTML_MAX_BYTES"),
        )
        for headers, error in cases:
            with self.subTest(error=error, mime=headers.get("Content-Type")):
                session = _Session({"get": [_Hop(headers=headers, chunks=(b"never",))]})
                with _settings_patch(http), _session_patch(session):
                    with self.assertRaisesRegex(ValueError, error):
                        await http.HttpTier().fetch(FINAL_URL, DEFAULT_POLICY)
                self.assertEqual(session.body_chunks, 0)
                self.assertTrue(session.closed)

    async def test_html_without_headers_and_charset_html_are_preserved(self) -> None:
        """Missing MIME headers remain valid, and explicit character encodings still work."""
        cases = (
            ({}, b"<title>First</title><title>Last</title>", "Last"),
            ({"Content-Type": 'text/html; charset="windows-1252"'}, b"<title>caf\xe9</title>", "café"),
            ({"Content-Type": "application/xhtml+xml"}, b"<title>XHTML</title>", "XHTML"),
            ({"Content-Type": "text/plain"}, b"Source code", None),
        )
        for headers, content, title in cases:
            with self.subTest(title=title):
                session = _Session({"get": [_Hop(headers=headers, chunks=(content,))]})
                with _settings_patch(http), _session_patch(session):
                    result = await http.HttpTier().fetch(FINAL_URL, DEFAULT_POLICY)
                self.assertEqual(result.title, title)
                self.assertEqual(result.status, 200)
                self.assertEqual(result.engine, "http")
                self.assertEqual(result.content_type, headers.get("Content-Type"))
                self.assertTrue(session.closed)

    async def test_redirect_headers_do_not_reject_valid_final_html(self) -> None:
        """Only the final response's type and length govern HTML admission."""
        redirect = _Hop(
            url=SHORT_URL,
            status=302,
            headers={
                "Content-Length": "9999",
                "Content-Type": "application/octet-stream",
                "Location": FINAL_URL,
            },
        )
        final = _Hop(chunks=(b"<title>OK</title>",))
        session = _Session({"get": [redirect, final]})
        with _settings_patch(http), _session_patch(session):
            result = await http.HttpTier().fetch(SHORT_URL, DEFAULT_POLICY)
        self.assertEqual(result.title, "OK")
        self.assertIsNone(result.content_type)
        self.assertEqual(session.calls[0][1]["max_redirects"], 2)

    async def test_cancellation_closes_session_and_propagates(self) -> None:
        """A cancelled transfer cannot escape its session context or become a fit."""
        session = _Session({"get": [_Hop(hang=True)]})
        with _settings_patch(http), _session_patch(session):
            task = asyncio.create_task(http.HttpTier().fetch(FINAL_URL, DEFAULT_POLICY))
            await session.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(session.closed)


# ---------------------------------------------------------------------------
# Short URLs
# ---------------------------------------------------------------------------

class ShortUrlMemoryTests(unittest.IsolatedAsyncioTestCase):
    """Resolve redirects without consuming a final response body."""

    async def test_head_follows_redirects_and_aborts_final_headers(self) -> None:
        """Cookie-bound/multi-hop redirect handling stays delegated to one curl session."""
        session = _Session({"head": [
            _Hop(url=SHORT_URL, status=301, headers={"Location": "/next", "Set-Cookie": "x=y"}),
            _Hop(url="https://a.co/next", status=307, headers={"Location": FINAL_URL}),
            _Hop(chunks=(b"never download this",)),
        ]})
        with _settings_patch(shorturl), _session_patch(session):
            self.assertEqual(await shorturl.resolve_short_url(SHORT_URL), FINAL_URL)
        self.assertEqual([method for method, _ in session.calls], ["head"])
        self.assertEqual(session.body_chunks, 0)
        self.assertTrue(session.closed)

    async def test_refused_or_nonredirecting_head_falls_back_to_get_headers(self) -> None:
        """Shorteners that only implement GET must still resolve without downloading a body."""
        for status in (200, 403, 405, 501):
            with self.subTest(status=status):
                session = _Session({
                    "head": [_Hop(url=SHORT_URL, status=status)],
                    "get": [_Hop(url=SHORT_URL, status=302, headers={"Location": FINAL_URL}),
                            _Hop(chunks=(b"never",), hang=True)],
                })
                with _settings_patch(shorturl), _session_patch(session):
                    self.assertEqual(await shorturl.resolve_short_url(SHORT_URL), FINAL_URL)
                self.assertEqual([method for method, _ in session.calls], ["head", "get"])
                self.assertEqual(session.body_chunks, 0)
                self.assertTrue(session.closed)

    async def test_head_connection_error_can_fall_back(self) -> None:
        """An early HEAD-only connection failure does not suppress a valid GET redirect."""
        session = _Session({
            "head": [_Hop(error=RuntimeError("HEAD connection refused"))],
            "get": [_Hop()],
        })
        with _settings_patch(shorturl), _session_patch(session):
            self.assertEqual(await shorturl.resolve_short_url(SHORT_URL), FINAL_URL)
        self.assertTrue(session.closed)

    async def test_unresolved_and_non_http_destinations_fail(self) -> None:
        """A shortener shell or unsupported redirect cannot be indexed as the real page."""
        for final in (SHORT_URL, "file:///not-an-http-destination"):
            session = _Session({"head": [_Hop(url=final)], "get": [_Hop(url=final)]})
            with _settings_patch(shorturl), _session_patch(session):
                self.assertIsNone(await shorturl.resolve_short_url(SHORT_URL))
            self.assertTrue(session.closed)

    async def test_redirect_limit_and_shared_deadline_close_session(self) -> None:
        """Redirect loops and a server hanging before headers both terminate cleanly."""
        redirects = [_Hop(url=SHORT_URL, status=302, headers={"Location": SHORT_URL})] * 4
        session = _Session({"head": redirects, "get": redirects})
        with _settings_patch(shorturl), _session_patch(session):
            self.assertIsNone(await shorturl.resolve_short_url(SHORT_URL))
        self.assertTrue(session.closed)
        session = _Session({"head": [_Hop(hang_before_headers=True)]})
        with _settings_patch(shorturl, CRAWL_TIMEOUT_S=0.02), _session_patch(session):
            self.assertIsNone(await shorturl.resolve_short_url(SHORT_URL))
        self.assertTrue(session.closed)
        self.assertEqual(len(session.calls), 1)

    async def test_cancellation_is_not_translated_to_unresolved_url(self) -> None:
        """A cancelled caller must stop redirect resolution and close its session."""
        session = _Session({"head": [_Hop(hang_before_headers=True)]})
        with _settings_patch(shorturl), _session_patch(session):
            task = asyncio.create_task(shorturl.resolve_short_url(SHORT_URL))
            await session.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(session.closed)
        self.assertEqual(len(session.calls), 1)


# ---------------------------------------------------------------------------
# Bounded CPU Processing
# ---------------------------------------------------------------------------

class ProcessingMemoryTests(unittest.IsolatedAsyncioTestCase):
    """Verify actual worker concurrency, not just the lifetime of awaiting coroutines."""

    async def asyncSetUp(self) -> None:
        """Use a dedicated single-worker test pool and settings without loading .env."""
        self.pool = engine._ProcessingPool(1)
        self.pool_patch = patch.object(engine, "_processing_pool", self.pool)
        self.pool_patch.start()
        self.settings_patch = _settings_patch(engine)
        self.settings_patch.start()
        self.release = threading.Event()
        self.started = threading.Event()
        self.executed = 0

    async def asyncTearDown(self) -> None:
        """Release any blocked real thread before shutting down the test executor."""
        self.release.set()
        await _wait_until(lambda: self.pool._active == 0)
        self.pool._executor.shutdown(wait=True)
        self.settings_patch.stop()
        self.pool_patch.stop()

    def blocking_work(self) -> str:
        """Occupy a real executor thread until the test explicitly releases it."""
        self.executed += 1
        self.started.set()
        if not self.release.wait(TEST_WAIT_S):
            raise AssertionError("test did not release its processing worker")
        return "done"

    async def test_cancelled_owner_keeps_slot_and_waiters_never_enter_queue(self) -> None:
        """Cancelling an awaiter must not admit replacement work while its thread is busy."""
        owner = asyncio.create_task(engine.run_processing(self.blocking_work))
        await _wait_until(self.started.is_set)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        waiters = [asyncio.create_task(engine.run_processing(self.blocking_work)) for _ in range(8)]
        await _wait_until(lambda: len(self.pool._waiters) == len(waiters))
        self.assertEqual(self.executed, 1)
        self.assertEqual(self.pool._active, 1)
        self.assertEqual(self.pool._executor._work_queue.qsize(), 0)
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        self.assertFalse(self.pool._waiters)
        self.release.set()
        await _wait_until(lambda: self.pool._active == 0)
        self.assertEqual(await engine.run_processing(lambda: "next"), "next")

    async def test_processing_timeout_includes_admission_but_not_slot_release(self) -> None:
        """Timed-out submitted and waiting jobs must not permit unbounded work or queues."""
        with _settings_patch(engine, CRAWL_EXTRACT_TIMEOUT_S=0.02):
            with self.assertRaises(TimeoutError):
                await engine.run_processing(self.blocking_work)
            with self.assertRaises(TimeoutError):
                await engine.run_processing(self.blocking_work)
        self.assertEqual(self.executed, 1)
        self.assertEqual(self.pool._active, 1)
        self.assertEqual(self.pool._executor._work_queue.qsize(), 0)

    async def test_multiple_workers_have_a_shared_actual_execution_limit(self) -> None:
        """A two-worker configuration must admit exactly two jobs, never a third queued job."""
        pool = engine._ProcessingPool(2)
        lock = threading.Lock()
        executions = 0

        def block() -> None:
            """Count admitted executions while holding each worker until explicit release."""
            nonlocal executions
            with lock:
                executions += 1
            self.release.wait(TEST_WAIT_S)

        jobs = [asyncio.create_task(pool.run(block, ())) for _ in range(5)]
        try:
            await _wait_until(lambda: executions == 2 and len(pool._waiters) == 3)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            self.assertEqual(executions, 2)
            self.assertEqual(pool._active, 2)
            self.assertEqual(pool._executor._work_queue.qsize(), 0)
        finally:
            self.release.set()
            await asyncio.gather(*jobs, return_exceptions=True)
            await _wait_until(lambda: pool._active == 0)
            pool._executor.shutdown(wait=True)

    async def test_context_and_worker_exceptions_are_preserved(self) -> None:
        """Off-loop processing inherits request context and preserves escalation signals."""
        context = ContextVar("test_processing_context", default="missing")
        token = context.set("request context")
        try:
            thread, value = await engine.run_processing(lambda: (threading.get_ident(), context.get()))
        finally:
            context.reset(token)
        self.assertNotEqual(thread, threading.get_ident())
        self.assertEqual(value, "request context")
        with self.assertRaises(Escalate):
            await engine.run_processing(Mock(side_effect=Escalate("browser")))

    async def test_detached_exception_is_consumed(self) -> None:
        """A late worker failure after caller cancellation must not trigger a loop warning."""
        warnings: list[dict] = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, detail: warnings.append(detail))

        def fail_later() -> None:
            """Raise only after the caller has left its shielded worker future."""
            self.blocking_work()
            raise RuntimeError("detached extraction failure")

        try:
            task = asyncio.create_task(engine.run_processing(fail_later))
            await _wait_until(self.started.is_set)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.release.set()
            await _wait_until(lambda: self.pool._active == 0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(warnings)
        finally:
            loop.set_exception_handler(previous)

    async def test_engine_scans_fits_and_gates_off_loop(self) -> None:
        """Botwall, extraction, and acceptance all use the shared bounded executor."""
        threads: list[int] = []

        def scan(*args: object) -> None:
            """Record the executing thread of the botwall stage."""
            threads.append(threading.get_ident())

        def fit(r: Rendered) -> Fitted:
            """Record the executing thread of the extractor stage."""
            threads.append(threading.get_ident())
            return Fitted(md="article", title="title")

        def accept(f: Fitted) -> bool:
            """Record the executing thread of the quality gate."""
            threads.append(threading.get_ident())
            return True

        extractor = SimpleNamespace(fit=fit, accept=accept)
        tier = SimpleNamespace(fetch=AsyncMock(return_value=_rendered("article")))
        with patch.object(engine, "resolve_short_url", AsyncMock(return_value=FINAL_URL)), \
             patch.object(engine, "_crawl_plan", return_value=(DEFAULT_POLICY, extractor, FINAL_URL)), \
             patch.object(engine.tiers, "by_name", return_value=tier), \
             patch.object(botwall, "is_botwall", side_effect=scan):
            result = await engine.crawl(FINAL_URL)
        self.assertTrue(result.ok)
        self.assertEqual(len(threads), 3)
        self.assertTrue(all(thread != threading.get_ident() for thread in threads))

    async def test_engine_rejects_oversized_browser_html_before_processing(self) -> None:
        """The engine remains a final input guard even for a tier without its own limit."""
        tier = SimpleNamespace(fetch=AsyncMock(return_value=_rendered("界" * 32)))
        with patch.object(engine, "resolve_short_url", AsyncMock(return_value=FINAL_URL)), \
             patch.object(engine.tiers, "by_name", return_value=tier), \
             patch.object(engine, "run_processing", AsyncMock()) as processing:
            result = await engine.crawl(FINAL_URL)
        self.assertFalse(result.ok)
        self.assertFalse(processing.called)
        self.assertTrue(all("CRAWL_HTML_MAX_BYTES" in attempt["error"] for attempt in result.attempts))

    async def test_engine_keeps_challenge_and_escalation_contracts(self) -> None:
        """A tier challenge propagates; an extractor escalation skips to its target tier."""
        tier = SimpleNamespace(fetch=AsyncMock(side_effect=ChallengeDetected(FINAL_URL)))
        with patch.object(engine, "resolve_short_url", AsyncMock(return_value=FINAL_URL)), \
             patch.object(engine.tiers, "by_name", return_value=tier):
            with self.assertRaises(ChallengeDetected):
                await engine.crawl(FINAL_URL)
        tier.fetch = AsyncMock(return_value=_rendered("article"))
        extractor = SimpleNamespace(
            fit=Mock(side_effect=[Escalate("browser"), Fitted("ok", "title")]),
            accept=Mock(return_value=True),
        )
        with patch.object(engine, "resolve_short_url", AsyncMock(return_value=FINAL_URL)), \
             patch.object(engine, "_crawl_plan", return_value=(DEFAULT_POLICY, extractor, FINAL_URL)), \
             patch.object(engine.tiers, "by_name", return_value=tier):
            result = await engine.crawl(FINAL_URL)
        self.assertTrue(result.ok)
        self.assertEqual(result.tier, "browser")
        self.assertEqual(result.attempts, [{"tier": "http", "error": "escalate: browser"}])

    async def test_engine_processing_deadlines_record_tier_errors(self) -> None:
        """Processing timeouts preserve ladder failover while the original worker keeps its slot."""
        tier = SimpleNamespace(fetch=AsyncMock(return_value=_rendered("article")))
        extractor = SimpleNamespace(fit=Mock(side_effect=lambda _: self.blocking_work()), accept=Mock())
        with patch.object(engine, "resolve_short_url", AsyncMock(return_value=FINAL_URL)), \
             patch.object(engine, "_crawl_plan", return_value=(DEFAULT_POLICY, extractor, FINAL_URL)), \
             patch.object(engine.tiers, "by_name", return_value=tier), \
             _settings_patch(engine, CRAWL_EXTRACT_TIMEOUT_S=0.02):
            result = await engine.crawl(FINAL_URL)
        self.assertFalse(result.ok)
        self.assertEqual(self.executed, 1)
        self.assertEqual(self.pool._active, 1)
        self.assertEqual(len(result.attempts), 2)
        self.assertTrue(all("processing timed out" in attempt["error"] for attempt in result.attempts))


class ProcessingClosedLoopTests(unittest.TestCase):
    """Admission belongs to the actual worker, not an asyncio loop's lifetime."""

    def test_cancelled_owner_in_closed_loop_keeps_global_slot(self) -> None:
        """A replacement event loop must not queue work behind an old cancelled owner."""
        pool = engine._ProcessingPool(1)
        release = threading.Event()
        started = threading.Event()

        def block() -> None:
            """Keep one underlying worker alive across two asyncio.run calls."""
            started.set()
            release.wait(TEST_WAIT_S)

        async def first_loop() -> None:
            """Cancel the first awaiter but deliberately leave its worker alive."""
            task = asyncio.create_task(pool.run(block, ()))
            await _wait_until(started.is_set)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        async def second_loop() -> None:
            """Admission must still wait on the original worker in a new loop."""
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(pool.run(lambda: None, ()), timeout=0.02)
            self.assertEqual(pool._active, 1)
            self.assertEqual(pool._executor._work_queue.qsize(), 0)

        try:
            asyncio.run(first_loop())
            asyncio.run(second_loop())
        finally:
            release.set()
            pool._executor.shutdown(wait=True)
        self.assertEqual(pool._active, 0)


# ---------------------------------------------------------------------------
# Pure HTML Guards
# ---------------------------------------------------------------------------

class HtmlGuardTests(unittest.TestCase):
    """Check markdown caps and linear malformed-markup handling without extraction libraries."""

    def test_generic_fit_honors_markdown_cap_after_loading_stub_detection(self) -> None:
        """Long articles mentioning Loading must not become stubs merely because output is capped."""
        with _settings_patch(base), patch.object(base, "generic_md", return_value="article " * 400):
            fitted = base.GenericExtractor().fit(_rendered("<p>Loading...</p>"))
        self.assertEqual(len(fitted.md), 48)

    def test_html_guard_counts_utf8_and_rejects_large_strings_without_encoding(self) -> None:
        """Bound decoded browser strings by bytes and avoid allocating a giant encoded copy."""
        with _settings_patch(engine):
            engine.check_html_size("x" * 64)
            engine.check_html_size("界" * 21)
            with self.assertRaises(ValueError):
                engine.check_html_size("界" * 22)
            with self.assertRaises(ValueError):
                engine.check_html_size("x" * 1000000)

    def test_malformed_hidden_blocks_are_forward_only(self) -> None:
        """Repeated unmatched opening tags must not cause repeated full-document scans."""
        start = time.monotonic()
        for tag in ("code", "noscript", "pre", "script", "style"):
            html = "<p>Article.</p>" + (f"<{tag}>" * 20000) + "access denied"
            self.assertIsNone(botwall.is_botwall(html, 200))
        self.assertLess(time.monotonic() - start, TEST_WAIT_S)
        self.assertFalse(base._is_loading_stub("", "<script>" * 20000 + "Loading..."))
        self.assertIsNone(botwall.is_botwall("<!--" * 20000 + "access denied", 200))

    def test_malformed_tags_and_quoted_attributes_do_not_expose_wall_copy(self) -> None:
        """Quoted greater-than signs and malformed tag runs remain bounded and invisible."""
        self.assertIsNone(botwall.is_botwall('<p title="> access denied">Article.</p>', 200))
        start = time.monotonic()
        self.assertIsNone(botwall.is_botwall("<" * 100000 + "access denied", 200))
        self.assertLess(time.monotonic() - start, TEST_WAIT_S)
        self.assertEqual(botwall.is_botwall("<p>Access denied</p>", 200), "access denied")

    def test_malformed_titles_do_not_retry_suffixes(self) -> None:
        """Many unclosed title tags cannot make the HTTP tier scan every remaining suffix."""
        start = time.monotonic()
        self.assertIsNone(http._extract_title("<title>" * 30000))
        self.assertLess(time.monotonic() - start, TEST_WAIT_S)
        titles = "<title>First</title><title> </title><title>Last</title>"
        self.assertEqual(http._extract_title(titles), "Last")


# ---------------------------------------------------------------------------
# In-Memory Curl Driver
# ---------------------------------------------------------------------------

@dataclass
class _Hop:
    """One fake redirect/final response, delivered through libcurl-style callbacks."""

    url: str = FINAL_URL
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] = ()
    error: Exception | None = None
    hang: bool = False
    hang_before_headers: bool = False


class _Session:
    """Model callback ordering and intentional libcurl aborts without any I/O."""

    def __init__(self, plans: dict[str, list[_Hop]]) -> None:
        """Initialize per-method redirect plans and resource-observation counters."""
        self.plans = plans
        self.curl_options: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.body_chunks = 0
        self.started = asyncio.Event()

    async def __aenter__(self) -> _Session:
        """Enter an owned request session."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Record deterministic cleanup on success, exception, and cancellation."""
        self.closed = True

    async def get(
        self,
        url: str,
        **kwargs: object,
    ) -> Response:
        """Deliver a fake GET through the caller's header/body callbacks."""
        return await self._request("get", kwargs)

    async def head(
        self,
        url: str,
        **kwargs: object,
    ) -> Response:
        """Deliver a fake HEAD through the caller's header callback."""
        return await self._request("head", kwargs)

    async def _request(
        self,
        method: str,
        kwargs: dict,
    ) -> Response:
        """Simulate already-decompressed callback bytes and redirect limits."""
        self.calls.append((method, kwargs))
        self.started.set()
        assert kwargs["allow_redirects"] is True
        assert not kwargs.get("stream")  # An unbounded curl_cffi stream queue is never used.
        for count, hop in enumerate(self.plans[method]):
            response = Response()
            response.url = hop.url
            response.status_code = hop.status
            if count > kwargs["max_redirects"]:
                raise RequestException("redirect limit", CurlECode.TOO_MANY_REDIRECTS, response)
            if hop.error is not None:
                raise hop.error
            if hop.hang_before_headers:
                await asyncio.Event().wait()
            lines = [f"HTTP/2 {hop.status}\r\n".encode()]
            lines.extend(f"{name}: {value}\r\n".encode() for name, value in hop.headers.items())
            lines.append(b"\r\n")
            for line in lines:
                if self.curl_options[CurlOpt.HEADERFUNCTION](line) == WRITE_ERROR:
                    raise RequestException("header aborted", CurlECode.WRITE_ERROR, response)
            if hop.status in (301, 302, 303, 307, 308) and "Location" in hop.headers:
                continue
            if hop.hang:
                await asyncio.Event().wait()
            for chunk in hop.chunks:
                self.body_chunks += 1
                if kwargs["content_callback"](chunk) == WRITE_ERROR:
                    raise RequestException("body aborted", CurlECode.WRITE_ERROR, response)
            return response
        raise AssertionError("redirect plan ended without a final response")


def _rendered(html: str) -> Rendered:
    """Create a raw tier result without involving any browser or extractor."""
    return Rendered(html=html, title="Title", status=200, ms=1, engine="fake")


def _settings_patch(module: object, **overrides: object) -> object:
    """Provide only test settings, never reading local .env or production configuration."""
    values = {
        "CRAWL_CF_TIMEOUT_S": TEST_WAIT_S,
        "CRAWL_EXTRACT_MAX_PARALLEL": 1,
        "CRAWL_EXTRACT_TIMEOUT_S": TEST_WAIT_S,
        "CRAWL_HTML_MAX_BYTES": 64,
        "CRAWL_HTTP_MAX_REDIRECTS": 2,
        "CRAWL_IGNORE_SSL_ERRORS": True,
        "CRAWL_MD_MAX_CHARS": 48,
        "CRAWL_STEALTH_TIMEOUT_S": TEST_WAIT_S,
        "CRAWL_TIMEOUT_S": TEST_WAIT_S,
        "short_url_hosts": frozenset({"a.co"}),
    }
    values.update(overrides)
    return patch.object(module, "get_settings", return_value=SimpleNamespace(**values))


def _session_patch(session: _Session) -> object:
    """Install one owned fake session while preserving the real curl error/response types."""
    def create(**kwargs: object) -> _Session:
        """Apply constructor curl options exactly as the real session does."""
        session.curl_options = kwargs.get("curl_options", {})
        return session

    return patch("curl_cffi.requests.AsyncSession", side_effect=create)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    """Wait for a thread/async state transition without blocking the event loop."""
    async with asyncio.timeout(TEST_WAIT_S):
        while not predicate():
            await asyncio.sleep(0.001)


if __name__ == "__main__":
    unittest.main(verbosity=2)

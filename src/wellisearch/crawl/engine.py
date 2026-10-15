"""crawl() core loop: policy → tier ladder → extractor → gate (design §5.1).

Escalates on bot-wall, fetch errors, and extractor Escalate; returns the
best partial result (ok=False) when every tier fails. ChallengeDetected
propagates to the caller (it must be routed to the CF lane, not swallowed).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import logging
import threading
import time
from typing import TypeVar

from ..config import get_settings
from . import botwall, extractors, tiers
from .lane import CF, get_lane
from .policy import DEFAULT_POLICY, Policy, match
from .probe import clamp
from .results import ChallengeDetected, CrawlResult, Escalate, Fitted, PayloadRejectedError, Rendered
from .shorturl import resolve_short_url

log = logging.getLogger("wellisearch.crawl.engine")

_T = TypeVar("_T")
_HTML_SIZE_CHUNK_CHARS = 65536
_processing_pool: _ProcessingPool | None = None
_processing_pool_lock = threading.Lock()


async def crawl(url: str) -> CrawlResult:
    """Crawl one URL through its policy's tier ladder.

    Tries each tier in order; a bot-wall, fetch error, or gate failure
    moves to the next tier (an Escalate jumps to the named tier). Returns
    ok=True on the first accepted fit, else the best partial (or empty).
    """
    original = url
    # Shorteners (e.g. a.co) resolve to their final URL so policy/extractor
    # selection sees the real site; short URLs are never crawled as-is.
    url = await resolve_short_url(url)
    if url is None:
        raise RuntimeError(f"could not resolve short url {original} to its final destination")
    p, ex, request_url = _crawl_plan(url)
    attempts: list[dict] = []
    best: Fitted | None = None
    start = time.monotonic()
    log.info("crawl %s (policy=%s tiers=%s)", url, p.name, ",".join(p.tiers))
    i = 0
    while i < len(p.tiers):
        name = p.tiers[i]
        tier = tiers.by_name(name)
        if tier is None:
            attempts.append({"tier": name, "error": "disabled"})
            i += 1
            continue
        try:
            r = await asyncio.wait_for(tier.fetch(request_url, p), timeout=_tier_backstop(tier, name, p))
            check_html_size(r.html)
        except ChallengeDetected:
            # Fast-lane probe hit a bot-wall: route to the CF lane rather than
            # trying the next tier (the challenge needs the CF lane's loop).
            raise
        except PayloadRejectedError as error:
            attempts.append({"tier": name, "error": f"{type(error).__name__}: {error}"})
            break
        except Exception as e:
            attempts.append({"tier": name, "error": f"{type(e).__name__}: {e}"})
            i += 1
            continue
        try:
            marker, f, accepted = await run_processing(_process_rendered, ex, r)
        except Escalate as esc:
            attempts.append({"tier": name, "error": f"escalate: {esc.tier}"})
            i = _next_index_after_escalation(p, i, esc)
            continue
        except TimeoutError:
            attempts.append({"tier": name, "error": "TimeoutError: HTML processing timed out"})
            i += 1
            continue
        if marker is not None:
            attempts.append({"tier": name, "error": f"botwall: {marker}", "status": r.status})
            i += 1
            continue
        assert f is not None  # A clean response always produces a fit or raises.
        if accepted:
            ms = int((time.monotonic() - start) * 1000)
            log.info("crawl %s ok (tier=%s ms=%d)", url, name, ms)
            return CrawlResult(
                ok=True,
                title=f.title or r.title,
                md=f.md,
                tier=name,
                ms=ms,
                attempts=attempts,
                flags=f.flags,
            )
        best = f
        attempts.append({
            "tier": name,
            "error": "gate failed",
            "status": r.status,
            "md_chars": len(f.md),
        })
        i += 1
    ms = int((time.monotonic() - start) * 1000)
    log.info("crawl %s failed (tier=none ms=%d)", url, ms)
    if best is not None:
        return CrawlResult(
            ok=False,
            title=best.title,
            md=best.md,
            tier="none",
            ms=ms,
            attempts=attempts,
            flags=best.flags,
        )
    return CrawlResult(ok=False, title=None, md="", tier="none", ms=ms, attempts=attempts)


# ---------------------------------------------------------------------------
# Bounded Processing
# ---------------------------------------------------------------------------

def check_html_size(html: str) -> None:
    """Reject oversized tier/browser HTML before parsing, without a full encoded copy.

    Browser/stealth callers preflight the DOM before transferring its serialized
    HTML; the engine repeats this exact byte check as the final guard for every tier.
    """
    limit = get_settings().CRAWL_HTML_MAX_BYTES
    if len(html) > limit:
        raise PayloadRejectedError(f"HTML exceeds CRAWL_HTML_MAX_BYTES ({limit} bytes)")
    if html.isascii():
        return
    size = 0
    for start in range(0, len(html), _HTML_SIZE_CHUNK_CHARS):
        size += len(html[start:start + _HTML_SIZE_CHUNK_CHARS].encode("utf-8", errors="replace"))
        if size > limit:
            raise PayloadRejectedError(f"HTML exceeds CRAWL_HTML_MAX_BYTES ({limit} bytes)")


async def run_processing(fn: Callable[..., _T], *args: object) -> _T:
    """Run CPU-heavy HTML work off-loop with bounded admission and a caller deadline.

    A cancelled/timed-out awaiter cannot stop a running thread. Its admission
    slot stays owned until the concurrent future really finishes, so repeated
    cancellation cannot build an unbounded executor queue or exceed the limit.
    Browser botwall checks share this same process-wide pool with extractors.
    """
    global _processing_pool
    with _processing_pool_lock:
        if _processing_pool is None:
            _processing_pool = _ProcessingPool(get_settings().CRAWL_EXTRACT_MAX_PARALLEL)
    async with asyncio.timeout(clamp(get_settings().CRAWL_EXTRACT_TIMEOUT_S)):
        return await _processing_pool.run(fn, args)


class _ProcessingPool:
    """A fixed thread pool with no more admitted jobs than workers, across event loops."""

    def __init__(self, workers: int) -> None:
        """Initialize a process-wide execution bound and thread-safe admission state."""
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawl-extract")
        self._lock = threading.Lock()
        self._active = 0
        self._workers = workers
        self._waiters: set[asyncio.Future] = set()

    async def run(
        self,
        fn: Callable[..., _T],
        args: tuple[object, ...],
    ) -> _T:
        """Admit before submitting; cancellation never reaches the concurrent future."""
        await self._acquire()
        waiter = asyncio.get_running_loop().create_future()
        waiter.add_done_callback(_consume_processing_error)
        try:
            future = self._executor.submit(copy_context().run, fn, *args)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(partial(self._finished, waiter))
        return await waiter

    async def _acquire(self) -> None:
        """Wait without occupying a thread or adding an executor work item."""
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._active < self._workers:
                    self._active += 1
                    return
                waiter = loop.create_future()
                self._waiters.add(waiter)
            try:
                await waiter
            finally:
                with self._lock:
                    self._waiters.discard(waiter)

    def _finished(
        self,
        waiter: asyncio.Future,
        future: Future,
    ) -> None:
        """Release on real completion and deliver a result only to a still-live caller."""
        self._release()
        try:
            waiter.get_loop().call_soon_threadsafe(_finish_processing_result, waiter, future)
        except RuntimeError:
            pass  # Admission was released even if the caller's loop already closed.

    def _release(self) -> None:
        """Wake waiting loops after releasing one actual execution slot."""
        with self._lock:
            self._active -= 1
            waiters = tuple(self._waiters)
        for waiter in waiters:
            try:
                waiter.get_loop().call_soon_threadsafe(_wake_processing_waiter, waiter)
            except RuntimeError:
                pass  # A test/embedding event loop can close before its worker finishes.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _consume_processing_error(future: asyncio.Future) -> None:
    """Retrieve detached worker errors so a cancelled caller leaves no warning."""
    if not future.cancelled():
        future.exception()


def _finish_processing_result(waiter: asyncio.Future, future: Future) -> None:
    """Transfer a completed worker result without linking caller/worker cancellation."""
    if waiter.done():
        return
    try:
        waiter.set_result(future.result())
    except BaseException as e:
        waiter.set_exception(e)


def _wake_processing_waiter(waiter: asyncio.Future) -> None:
    """Notify one admission waiter unless it has already been cancelled/notified."""
    if not waiter.done():
        waiter.set_result(None)


def _process_rendered(
    ex: extractors.Extractor,
    r: Rendered,
) -> tuple[str | None, Fitted | None, bool]:
    """Scan, extract, and gate one bounded response entirely off the event loop."""
    marker = botwall.is_botwall(r.html, r.status, r.content_type)
    if marker is not None:
        return marker, None, False
    f = ex.fit(r)
    return None, f, ex.accept(f)


def _crawl_plan(url: str) -> tuple[Policy, extractors.Extractor, str]:
    """Choose the site's extractor/ladder and any render-specific request URL."""
    p = match(url)
    ex = extractors.for_url(url)
    request_url = url
    if p.name == "reddit":
        # Listings/profiles keep the generic ladder; post comments need hydration.
        if extractors.reddit.is_post_url(url):
            request_url = extractors.reddit.comment_request_url(url)
        else:
            p = DEFAULT_POLICY
            ex = extractors.GenericExtractor()
    if p.name == "homedepot" and not extractors.homedepot.is_product_url(url):
        p = DEFAULT_POLICY
        ex = extractors.GenericExtractor()
    if p.name == "walmart" and not extractors.walmart.is_product_url(url):
        p = DEFAULT_POLICY
        ex = extractors.GenericExtractor()
    return p, ex, request_url


def _next_index_after_escalation(
    p: Policy,
    i: int,
    esc: Escalate,
) -> int:
    """Next tier index after an Escalate: the named tier if it's ahead, else i+1."""
    if esc.tier in p.tiers and p.tiers.index(esc.tier) > i:
        return p.tiers.index(esc.tier)
    return i + 1


def _flat_backstop(name: str) -> float:
    """Fallback per-tier backstop for tiers that don't report a worst case.

    Generous on purpose: each tier manages its own goto/challenge timeouts
    internally; this only guards against a hang. The CF-lane browser tier needs
    goto (CF timeout) + the full challenge loop (CF timeout budget), so it gets
    2× the CF timeout.
    """
    s = get_settings()
    if name == "stealth":
        return float(s.CRAWL_STEALTH_TIMEOUT_S)
    if name == "browser" and get_lane() == CF:
        return float(s.CRAWL_CF_TIMEOUT_S) * 2
    return float(s.CRAWL_TIMEOUT_S)


def _tier_backstop(
    tier: "tiers.Tier",
    name: str,
    p: "Policy",
) -> float:
    """Per-tier asyncio.wait_for backstop, derived from the tier's worst case.

    Prefers the tier's own worst_case_s() (goto + settle + network_idle +
    challenge loop + recovery), which is accurate per tier. Falls back to the
    flat per-name budget for tiers that don't report one (e.g. test fakes).
    """
    fn = getattr(tier, "worst_case_s", None)
    if callable(fn):
        return float(fn(p))
    return _flat_backstop(name)

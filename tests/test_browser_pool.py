"""Unit tests for browser-pool recovery after a closed Playwright session.

Pure-logic tests — no real browser or database. Run with:
    python tests/test_browser_pool.py
"""
from __future__ import annotations

import asyncio

from wellisearch.crawl.pool import (
    BrowserPool,
    is_closed_context_error,
    is_transport_closed_error,
)


class FakeContext:
    def __init__(self) -> None:
        """Initialize a fake context that records close calls."""
        self.closed = False

    async def close(self) -> None:
        """Mark the context closed."""
        self.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        """Initialize a fake Playwright driver that records stop calls."""
        self.stopped = False

    async def stop(self) -> None:
        """Mark the driver stopped."""
        self.stopped = True


async def _transport_failure_resets_pool() -> None:
    """A dead transport clears every cached context and stops its driver."""
    pool = BrowserPool(size=1)
    ctx = FakeContext()
    other = FakeContext()
    playwright = FakePlaywright()
    pool._contexts.update({"one.example": ctx, "two.example": other})
    pool._last_used.update({"one.example": 1.0, "two.example": 2.0})
    pool._in_use["one.example"] = 1
    pool._pw = playwright
    pool._reaped.update({"one.example", "two.example"})
    await pool._sem.acquire()

    await pool.invalidate(
        "one.example",
        ctx,
        RuntimeError("WriteUnixTransport closed=True; the handler is closed"),
    )

    assert pool._contexts == {}
    assert pool._last_used == {}
    assert pool._pw is None
    assert not pool._reaped
    assert pool._in_use == {"one.example": 1}, pool._in_use
    assert ctx.closed and other.closed
    assert playwright.stopped

    await pool.release(ctx, "one.example")
    assert pool._in_use["one.example"] == 0
    await pool._sem.acquire()
    pool._sem.release()


async def _closed_context_only_retires_its_profile() -> None:
    """A single closed context is retired without stopping the shared driver."""
    pool = BrowserPool(size=2)
    ctx = FakeContext()
    other = FakeContext()
    playwright = FakePlaywright()
    pool._contexts.update({"one.example": ctx, "two.example": other})
    pool._last_used.update({"one.example": 1.0, "two.example": 2.0})
    pool._in_use["one.example"] = 1
    pool._pw = playwright
    pool._reaped.add("one.example")

    await pool.invalidate(
        "one.example",
        ctx,
        RuntimeError("Target page, context or browser has been closed"),
    )

    assert "one.example" not in pool._contexts
    assert pool._contexts["two.example"] is other
    assert pool._in_use["one.example"] == 1
    assert "one.example" not in pool._reaped
    assert ctx.closed and not other.closed
    assert not playwright.stopped


async def _stale_error_does_not_evict_replacement() -> None:
    """A delayed error from an old lease cannot remove its replacement."""
    pool = BrowserPool(size=1)
    old = FakeContext()
    replacement = FakeContext()
    playwright = FakePlaywright()
    pool._contexts["one.example"] = replacement
    pool._pw = playwright

    await pool.invalidate(
        "one.example",
        old,
        RuntimeError("WriteUnixTransport closed=True; the handler is closed"),
    )

    assert pool._contexts["one.example"] is replacement
    assert not old.closed and not replacement.closed
    assert pool._pw is playwright
    assert not playwright.stopped


def _closed_error_classification() -> None:
    """Only closure errors trigger recovery; ordinary timeouts do not."""
    transport_error = RuntimeError(
        "WriteUnixTransport closed=True; the handler is closed"
    )
    context_error = RuntimeError("Target page, context or browser has been closed")
    timeout_error = TimeoutError("navigation timed out")

    assert is_closed_context_error(transport_error)
    assert is_transport_closed_error(transport_error)
    assert is_closed_context_error(context_error)
    assert not is_transport_closed_error(context_error)
    assert not is_closed_context_error(timeout_error)


asyncio.run(_transport_failure_resets_pool())
print("OK closed transport resets pool and releases old lease")
asyncio.run(_closed_context_only_retires_its_profile())
print("OK closed context retires only its profile")
asyncio.run(_stale_error_does_not_evict_replacement())
print("OK stale closure error preserves replacement context")
_closed_error_classification()
print("OK browser closure errors are classified separately from timeouts")
print("ALL BROWSER POOL TESTS PASSED")

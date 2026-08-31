"""Browser pool: one warm persistent patchright context per profile key (design §3.4).

Bounded by a semaphore (CRAWL_POOL_SIZE concurrent contexts) and LRU-evicted
at CRAWL_PROFILE_MAX. Orphan/SingletonLock cleanup ported from the proven
nodriver worker.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from typing import TYPE_CHECKING

from ..config import get_settings

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext

log = logging.getLogger("wellisearch.crawl.pool")

# Realistic Chrome 131 user agent (avoids headless/patchright fingerprints).
CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Stealth launch args ported from the proven nodriver worker (worker.py ~460).
STEALTH_ARGS: list[str] = [
    "--disable-session-crashed-bubble",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1366,900",
    "--lang=en-US",
    "--no-sandbox",
]

# Consistent, realistic viewport / locale / timezone fingerprint.
VIEWPORT: dict[str, int] = {"width": 1366, "height": 900}
LOCALE = "en-US"
TIMEZONE = "America/Los_Angeles"


def _sanitize_key(key: str) -> str:
    """Filesystem-safe profile dir name from a profile key."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def _profile_dir(key: str) -> str:
    """Absolute profile dir for a key under CRAWL_PROFILE_DIR."""
    return os.path.join(get_settings().CRAWL_PROFILE_DIR, _sanitize_key(key))


def _remove_singleton_lock(profile_dir: str) -> None:
    """Drop a stale SingletonLock so a fresh launch can acquire the profile."""
    lock = os.path.join(profile_dir, "SingletonLock")
    if os.path.islink(lock) or os.path.exists(lock):
        try:
            os.unlink(lock)
            log.warning("removed stale profile SingletonLock %s", profile_dir)
        except OSError:
            pass


def _reap_orphans(profile_dir: str) -> None:
    """Kill leftover chromium holding the profile + drop its SingletonLock.

    Ported from the proven worker's _reap_orphans (Linux /proc scan, guarded
    for non-Linux). A crashed browser leaves a live chromium with the profile's
    SingletonLock; the next launch then hangs on the locked profile.
    """
    if os.path.isdir("/proc"):
        marker = f"--user-data-dir={profile_dir}"
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmd = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            if marker in cmd and "chrom" in cmd:
                try:
                    os.kill(int(entry), signal.SIGKILL)
                    log.warning("reaped orphaned chromium pid=%s", entry)
                except OSError:
                    pass
    _remove_singleton_lock(profile_dir)


class BrowserPool:
    """Bounded pool of warm persistent browser contexts, keyed by profile."""

    def __init__(self, size: int | None = None) -> None:
        # size defaults to the fast-lane pool size; the CF lane passes its own
        # (smaller) CRAWL_CF_POOL_SIZE so challenge crawls get their own contexts.
        self._sem = asyncio.Semaphore(size if size is not None else get_settings().CRAWL_POOL_SIZE)
        self._contexts: dict[str, BrowserContext] = {}
        self._last_used: dict[str, float] = {}
        self._in_use: dict[str, int] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}
        self._reaped: set[str] = set()
        self._pw = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ acquire

    async def acquire(self, key: str) -> BrowserContext:
        """Acquire a context for key, launching (and LRU-evicting) as needed."""
        await self._sem.acquire()
        self._in_use[key] = self._in_use.get(key, 0) + 1
        try:
            return await self._get_or_launch(key)
        except BaseException:
            # BaseException (not Exception) so a cancellation mid-launch also
            # returns the slot; the caller's finally does not run in this case.
            self._in_use[key] = max(0, self._in_use.get(key, 0) - 1)
            self._sem.release()
            raise

    async def release(self, ctx: BrowserContext) -> None:
        """Release the semaphore slot; the context stays cached for reuse."""
        key = self._key_of(ctx)
        if key is not None:
            self._in_use[key] = max(0, self._in_use.get(key, 0) - 1)
        self._sem.release()

    async def close_all(self) -> None:
        """Close every cached context and stop playwright."""
        for key, ctx in list(self._contexts.items()):
            try:
                await ctx.close()
            except Exception as e:
                log.warning("close context %s failed: %s", key, e)
        self._contexts.clear()
        self._last_used.clear()
        self._in_use.clear()
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception as e:
                log.warning("playwright stop failed: %s", e)
            self._pw = None

    # -------------------------------------------------------------------- internals

    def _key_of(self, ctx: BrowserContext) -> str | None:
        for key, c in self._contexts.items():
            if c is ctx:
                return key
        return None

    def _launch_lock(self, key: str) -> asyncio.Lock:
        lk = self._launch_locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            self._launch_locks[key] = lk
        return lk

    async def _ensure_playwright(self):
        if self._pw is None:
            from patchright.async_api import async_playwright

            self._pw = await async_playwright().start()
        return self._pw

    async def _get_or_launch(self, key: str) -> BrowserContext:
        async with self._lock:
            if key in self._contexts:
                self._last_used[key] = time.monotonic()
                return self._contexts[key]
            pw = await self._ensure_playwright()
        async with self._launch_lock(key):
            async with self._lock:
                if key in self._contexts:
                    self._last_used[key] = time.monotonic()
                    return self._contexts[key]
                await self._evict_lru_if_needed()
                ctx = await self._launch(key, pw)
                self._contexts[key] = ctx
                self._last_used[key] = time.monotonic()
                return ctx

    async def _evict_lru_if_needed(self) -> None:
        """Evict the least recently used idle context when at capacity."""
        max_profiles = get_settings().CRAWL_PROFILE_MAX
        while len(self._contexts) >= max_profiles:
            idle = [k for k in self._contexts if self._in_use.get(k, 0) == 0]
            if not idle:
                break
            lru_key = min(idle, key=lambda k: self._last_used.get(k, 0.0))
            ctx = self._contexts.pop(lru_key)
            self._last_used.pop(lru_key, None)
            self._in_use.pop(lru_key, None)
            try:
                await ctx.close()
                log.info("evicted LRU context %s", lru_key)
            except Exception as e:
                log.warning("evict close %s failed: %s", lru_key, e)

    async def _launch(self, key: str, pw) -> BrowserContext:
        s = get_settings()
        profile_dir = _profile_dir(key)
        os.makedirs(profile_dir, exist_ok=True)
        if key not in self._reaped:
            _reap_orphans(profile_dir)  # full cleanup before first launch for this dir
            self._reaped.add(key)
        else:
            _remove_singleton_lock(profile_dir)  # just the lock on later launches
        log.info("launching browser context %s (headless=%s)", key, s.CRAWL_HEADLESS)
        return await pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=s.CRAWL_HEADLESS,
            args=STEALTH_ARGS,
            viewport=VIEWPORT,
            locale=LOCALE,
            timezone_id=TIMEZONE,
            user_agent=CHROME_UA,
        )


_pool: BrowserPool | None = None
_cf_pool: BrowserPool | None = None


def get_pool() -> BrowserPool:
    """Module-level singleton pool (fast lane, CRAWL_POOL_SIZE contexts)."""
    global _pool
    if _pool is None:
        _pool = BrowserPool()
    return _pool


def get_cf_pool() -> BrowserPool:
    """Module-level singleton pool for the CF lane (CRAWL_CF_POOL_SIZE contexts).

    Kept separate from the fast-lane pool so a challenge crawl (which holds a
    context for the whole turnstile loop) never starves the fast lane's
    contexts.
    """
    global _cf_pool
    if _cf_pool is None:
        _cf_pool = BrowserPool(size=get_settings().CRAWL_CF_POOL_SIZE)
    return _cf_pool

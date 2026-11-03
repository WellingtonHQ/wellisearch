"""Browser pool: one warm persistent patchright context per profile key (design §3.4).

Both concurrent leases and retained browsers are bounded by the pool size.
Contexts are periodically recycled to release browser/driver allocations.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from typing import Any, TYPE_CHECKING

from ..config import get_settings
from .cleanup import bounded_operation, complete_cleanup

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Page, Playwright

log = logging.getLogger("wellisearch.crawl.pool")

# Realistic Chrome 131 user agent (avoids headless/patchright fingerprints).
CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Stealth launch args ported from the proven nodriver worker (worker.py ~460).
STEALTH_ARGS: list[str] = [
    "--disable-dev-shm-usage",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-gpu",
    "--disable-session-crashed-bubble",
    "--lang=en-US",
    "--no-sandbox",
    "--window-size=1366,900",
]

# Consistent, realistic viewport / locale / timezone fingerprint.
VIEWPORT: dict[str, int] = {"width": 1366, "height": 900}
LOCALE = "en-US"
TIMEZONE = "America/Los_Angeles"

# Cooldown after a failed launch (CRAWL_LAUNCH_RETRY_AFTER_S in config.py):
# a broken environment (e.g. missing X server) otherwise spawns one doomed
# chromium per URL in a hot loop, each leaving an orphan behind and wasting
# CPU. Its 30s default mirrors the entrypoint.sh Xvfb self-heal poll interval,
# so a dead X has been restarted by the time relaunch is allowed again.


class LaunchBackoffError(RuntimeError):
    """A recent launch for this profile key failed; backing off."""


class BrowserPool:
    """Bounded pool of warm persistent browser contexts, keyed by profile."""

    def __init__(
        self,
        size: int | None = None,
        prefix: str = "",
    ) -> None:
        """Initialize a bounded pool of warm browser contexts."""
        # size defaults to the fast-lane pool size; the CF lane passes its own
        # (smaller) CRAWL_CF_POOL_SIZE so challenge crawls get their own contexts.
        # prefix namespaces the profile dir so the CF pool never reaps the fast
        # pool's live browser (see _profile_dir).
        self._size = max(1, size if size is not None else get_settings().CRAWL_POOL_SIZE)
        self._sem = asyncio.Semaphore(self._size)
        self._prefix = prefix
        self._contexts: dict[str, BrowserContext] = {}
        self._anchors: dict[str, Page] = {}
        self._last_used: dict[str, float] = {}
        self._in_use: dict[str, int] = {}
        self._created_at: dict[str, float] = {}
        self._uses: dict[str, int] = {}
        self._retiring: set[str] = set()
        self._launch_failed_at: dict[str, float] = {}
        self._reaped: set[str] = set()
        self._pw = None
        self._pw_manager: Any = None
        self._lock = asyncio.Lock()
        self._available = asyncio.Condition(self._lock)

    # ---------------------------------------------------------------------------
    # Acquire
    # ---------------------------------------------------------------------------

    async def acquire(self, key: str) -> BrowserContext:
        """Acquire a context for key, launching (and LRU-evicting) as needed."""
        await self._sem.acquire()
        try:
            return await self._get_or_launch(key)
        except BaseException:
            # BaseException (not Exception) so a cancellation mid-launch also
            # returns the slot; the caller's finally does not run in this case.
            self._sem.release()
            raise

    async def release(
        self,
        ctx: BrowserContext,
        key: str | None = None,
    ) -> None:
        """Clean idle pages and return a lease, even if its caller is cancelled."""
        await complete_cleanup(self._release(ctx, key))

    async def invalidate(
        self,
        key: str,
        ctx: BrowserContext,
        error: Exception,
    ) -> None:
        """Discard a closed context, resetting the pool if its transport died.

        Identity-checking prevents a delayed failure from an old lease from
        evicting a replacement context that another crawl has already launched.
        """
        await complete_cleanup(self._invalidate(key, ctx, error))

    async def close_all(self) -> None:
        """Close every cached context and driver before cancellation can escape."""
        await complete_cleanup(self._close_all())

    # ---------------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------------

    async def _release(
        self,
        ctx: BrowserContext,
        key: str | None,
    ) -> None:
        """Retire leaked pages and aged idle contexts before admitting more work."""
        try:
            async with self._lock:
                key = key or self._key_of(ctx)
                if key is None:
                    return
                self._in_use[key] = max(0, self._in_use.get(key, 0) - 1)
                if self._contexts.get(key) is not ctx:
                    if not self._in_use[key]:
                        current = self._contexts.get(key)
                        if current is not None and key in self._retiring:
                            await self._retire(key, current)
                        self._available.notify_all()
                    return
                self._uses[key] = self._uses.get(key, 0) + 1
                s = get_settings()
                expired = (
                    self._uses[key] >= s.CRAWL_BROWSER_MAX_USES
                    or time.monotonic() - self._created_at.get(key, time.monotonic())
                    >= s.CRAWL_BROWSER_MAX_AGE_S
                )
                if expired:
                    self._retiring.add(key)
                if self._in_use[key]:
                    return
                self._in_use.pop(key, None)
                if key in self._retiring or not await self._close_idle_pages(ctx, self._anchors.get(key)):
                    await self._retire(key, ctx)
                self._available.notify_all()
        finally:
            self._sem.release()

    async def _invalidate(
        self,
        key: str,
        ctx: BrowserContext,
        error: Exception,
    ) -> None:
        """Own all context/transport retirement under the pool lock."""
        async with self._lock:
            if self._contexts.get(key) is not ctx:
                return

            if is_transport_closed_error(error):
                await self._reset_resources()
                return

            await self._retire(key, ctx)

    async def _close_all(self) -> None:
        """Retire tracked resources without losing ownership midway through."""
        async with self._lock:
            await self._reset_resources()

    async def _reset_resources(self) -> None:
        """Retire all resources while the caller owns the pool lock."""
        contexts = list(self._contexts.items())
        self._contexts.clear()
        self._anchors.clear()
        self._last_used.clear()
        self._created_at.clear()
        self._uses.clear()
        self._retiring.clear()
        self._launch_failed_at.clear()
        self._reaped.clear()
        playwright, self._pw = self._pw, None
        manager, self._pw_manager = self._pw_manager, None
        for key, ctx in contexts:
            await self._close_context(key, ctx)
        if playwright is not None or manager is not None:
            await self._stop_playwright(playwright, manager)
        self._available.notify_all()

    def _key_of(self, ctx: BrowserContext) -> str | None:
        """Profile key for a context, or None when unknown."""
        for key, c in self._contexts.items():
            if c is ctx:
                return key
        return None

    async def _ensure_playwright(self) -> Playwright:
        """Start the shared playwright instance once and return it."""
        if self._pw is None:
            await bounded_operation(
                self._start_playwright(),
                timeout=get_settings().CRAWL_BROWSER_LAUNCH_TIMEOUT_S,
            )
        return self._pw

    async def _start_playwright(self) -> None:
        """Track a newly started driver before propagating launch cancellation."""
        from patchright.async_api import async_playwright

        self._pw_manager = async_playwright()
        self._pw = await self._pw_manager.start()

    async def _get_or_launch(self, key: str) -> BrowserContext:
        """Return the cached context for key or launch a new one."""
        async with self._available:
            while key in self._retiring and self._in_use.get(key, 0):
                await self._available.wait()
            if key in self._contexts:
                self._last_used[key] = time.monotonic()
                self._in_use[key] = self._in_use.get(key, 0) + 1
                return self._contexts[key]
            retry_after_s = get_settings().CRAWL_LAUNCH_RETRY_AFTER_S
            failed_at = self._launch_failed_at.get(key)
            if failed_at is not None and time.monotonic() - failed_at < retry_after_s:
                raise LaunchBackoffError(
                    f"browser launch for {key!r} recently failed; "
                    f"backing off {retry_after_s:.0f}s to avoid a relaunch storm"
                )
            await self._evict_lru_if_needed()
            try:
                pw = await self._ensure_playwright()
                # Join the original reaper before starting a cancellable launch.
                # A detached old scan must never race a replacement browser.
                await complete_cleanup(asyncio.to_thread(_reap_orphans, _profile_dir(key, self._prefix)))
                ctx = await bounded_operation(
                    self._launch(key, pw),
                    timeout=get_settings().CRAWL_BROWSER_LAUNCH_TIMEOUT_S,
                )
            except BaseException as error:
                self._anchors.pop(key, None)
                self._reaped.discard(key)
                if (
                    isinstance(error, (TimeoutError, asyncio.CancelledError))
                    or (self._pw_manager is not None and self._pw is None)
                ):
                    await complete_cleanup(self._reset_resources())
                await complete_cleanup(asyncio.to_thread(_reap_orphans, _profile_dir(key, self._prefix)))
                self._launch_failed_at[key] = time.monotonic()
                raise
            self._launch_failed_at.pop(key, None)
            self._contexts[key] = ctx
            self._last_used[key] = time.monotonic()
            self._created_at[key] = time.monotonic()
            self._uses[key] = 0
            self._in_use[key] = self._in_use.get(key, 0) + 1
            return ctx

    async def _close_idle_pages(
        self,
        ctx: BrowserContext,
        anchor: Page | None = None,
    ) -> bool:
        """Close idle pages except the owned blank tab keeping headed Chrome alive."""
        try:
            await bounded_operation(
                self._close_pages(ctx, anchor),
                timeout=get_settings().CRAWL_BROWSER_CLEANUP_TIMEOUT_S,
            )
        except Exception:
            return False
        return True

    async def _close_pages(self, ctx: BrowserContext, anchor: Page | None) -> None:
        """Close all non-anchor tabs as one deadline-bounded operation."""
        for page in list(getattr(ctx, "pages", [])):
            if page is not anchor:
                await page.close()

    async def _retire(self, key: str, ctx: BrowserContext) -> None:
        """Remove one tracked context and finish closing its process tree."""
        self._contexts.pop(key, None)
        self._anchors.pop(key, None)
        self._last_used.pop(key, None)
        self._created_at.pop(key, None)
        self._uses.pop(key, None)
        self._retiring.discard(key)
        self._reaped.discard(key)
        if not await self._close_context(key, ctx):
            await self._reset_resources()
        self._available.notify_all()

    async def _close_context(
        self,
        key: str,
        ctx: BrowserContext,
    ) -> bool:
        """Best-effort close for a context being retired."""
        closed = False
        try:
            await bounded_operation(
                ctx.close(),
                timeout=get_settings().CRAWL_BROWSER_CLEANUP_TIMEOUT_S,
            )
            closed = True
        except TimeoutError:
            log.warning("close retired context %s timed out", key)
        except Exception as e:
            log.debug("close retired context %s failed: %s", key, e)
        finally:
            # A failed close must not leave an untracked live Chromium behind.
            await asyncio.to_thread(_reap_orphans, _profile_dir(key, self._prefix))
        return closed

    async def _stop_playwright(
        self,
        playwright: Playwright | None,
        manager: Any = None,
    ) -> None:
        """Best-effort stop for a Playwright driver being retired."""
        try:
            if playwright is not None:
                stop = playwright.stop()
            elif manager is not None:
                stop_manager = getattr(manager, "__aexit__", None)
                if stop_manager is None:
                    return
                stop = stop_manager(None, None, None)
            else:
                return
            await bounded_operation(
                stop,
                timeout=get_settings().CRAWL_BROWSER_CLEANUP_TIMEOUT_S,
            )
        except TimeoutError:
            log.warning("stop retired playwright driver timed out")
        except Exception as e:
            log.warning("stop retired playwright driver failed: %s", e)
        finally:
            # A wedged Node driver may never acknowledge stop()/__abort__.
            # Only kill the process owned by this exact Playwright transport.
            implementation = getattr(playwright, "_impl_obj", None)
            connection = getattr(implementation, "_connection", None)
            if connection is None:
                connection = getattr(manager, "_connection", None)
            transport = getattr(connection, "_transport", None)
            process = getattr(transport, "_proc", None)
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    async def _evict_lru_if_needed(self) -> None:
        """Evict the least recently used idle context when at capacity."""
        s = get_settings()
        max_profiles = min(max(1, s.CRAWL_PROFILE_MAX), self._size)
        cutoff = time.monotonic() - s.CRAWL_LAUNCH_RETRY_AFTER_S
        self._launch_failed_at = {k: v for k, v in self._launch_failed_at.items() if v > cutoff}
        while len(self._contexts) >= max_profiles:
            idle = [k for k in self._contexts if self._in_use.get(k, 0) == 0]
            if not idle:
                break
            lru_key = min(idle, key=lambda k: self._last_used.get(k, 0.0))
            ctx = self._contexts[lru_key]
            self._in_use.pop(lru_key, None)
            await complete_cleanup(self._retire(lru_key, ctx))
            log.info("evicted LRU context %s", lru_key)

    async def _launch(
        self,
        key: str,
        pw: Playwright,
    ) -> BrowserContext:
        """Launch a persistent context for key under its profile dir."""
        s = get_settings()
        profile_dir = _profile_dir(key, self._prefix)
        os.makedirs(profile_dir, exist_ok=True)
        # _get_or_launch has joined orphan cleanup before starting this task.
        log.info("launching browser context %s (headless=%s)", key, s.CRAWL_HEADLESS)
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=s.CRAWL_HEADLESS,
            args=STEALTH_ARGS,
            viewport=VIEWPORT,
            locale=LOCALE,
            timezone_id=TIMEZONE,
            user_agent=CHROME_UA,
            # The tiers are read-only (see CRAWL_IGNORE_SSL_ERRORS).
            ignore_https_errors=s.CRAWL_IGNORE_SSL_ERRORS,
            ignore_default_args=["--disable-popup-blocking"],
            service_workers="block",
        )
        # Closing a headed persistent context's last tab makes Chrome exit.
        # Keep exactly one known blank anchor, never a restored arbitrary page.
        anchor = next((page for page in ctx.pages if page.url == "about:blank"), None)
        if anchor is None:
            anchor = await ctx.new_page()
            await anchor.goto("about:blank")
        self._anchors[key] = anchor
        if not await self._close_idle_pages(ctx, anchor):
            await complete_cleanup(self._close_context(key, ctx))
            raise RuntimeError("could not close restored browser pages")
        self._reaped.add(key)
        return ctx


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
        _cf_pool = BrowserPool(size=get_settings().CRAWL_CF_POOL_SIZE, prefix="cf")
    return _cf_pool


async def close_pools() -> None:
    """Close existing pools without creating new browser drivers at shutdown."""
    global _pool, _cf_pool
    for pool in (_pool, _cf_pool):
        if pool is not None:
            await pool.close_all()
    _pool = _cf_pool = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_key(key: str) -> str:
    """Filesystem-safe profile dir name from a profile key."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def is_closed_context_error(error: Exception) -> bool:
    """True when Playwright reports that a page, context, browser, or transport closed."""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "transport closed",
            "handler is closed",
            "target page, context or browser has been closed",
            "browser has been closed",
            "browsercontext has been closed",
            "context has been closed",
            "target closed",
            "failed to open a new tab",
        )
    )


def is_transport_closed_error(error: Exception) -> bool:
    """True for errors showing that the shared Playwright command transport died."""
    message = str(error).lower()
    return "transport closed" in message or "handler is closed" in message


def _profile_dir(key: str, prefix: str = "") -> str:
    """Absolute profile dir for a key under CRAWL_PROFILE_DIR.

    ``prefix`` namespaces the dir (e.g. ``cf`` for the CF lane) so two pools
    never target the same user-data-dir — otherwise one pool's _reap_orphans
    would SIGKILL the other pool's live warm browser.
    """
    base = get_settings().CRAWL_PROFILE_DIR
    if prefix:
        base = os.path.join(base, prefix)
    return os.path.join(base, _sanitize_key(key))


def _remove_singleton_lock(profile_dir: str) -> None:
    """Drop a stale SingletonLock so a fresh launch can acquire the profile."""
    lock = os.path.join(profile_dir, "SingletonLock")
    if os.path.islink(lock) or os.path.exists(lock):
        try:
            os.unlink(lock)
            log.warning("removed stale profile SingletonLock %s", profile_dir)
        except OSError:
            pass


def _is_orphan_chromium(pid: str, marker: str) -> bool:
    """True if /proc/<pid>/cmdline is a chromium holding our profile dir."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    return marker in cmd.split("\0") and "chrom" in cmd


def _kill_pid(pid: str) -> None:
    """SIGKILL a pid, logging when the orphan is reaped."""
    try:
        os.kill(int(pid), signal.SIGKILL)
        log.warning("reaped orphaned chromium pid=%s", pid)
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
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
        roots = {p for p in pids if _is_orphan_chromium(p, marker)}
        parents = {}
        for pid in pids:
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                    parents[pid] = f.read().rsplit(")", 1)[1].split()[1]
            except (IndexError, OSError):
                continue
        targets = roots.copy()
        while True:
            children = {pid for pid, parent in parents.items() if parent in targets}
            if children <= targets:
                break
            targets.update(children)
        for pid in targets:
            _kill_pid(pid)
    _remove_singleton_lock(profile_dir)

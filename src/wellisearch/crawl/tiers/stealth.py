"""T2 Stealth tier: Scrapling StealthySession (design §3.1).

Scrapling's synchronous browser runs in a disposable process group. Unlike
to_thread(), cancellation can actually stop it and all its Chrome children.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
from multiprocessing.connection import Connection
import os
import signal
import subprocess
import tempfile
import time
from typing import Any

from ...config import get_settings
from ..cleanup import complete_cleanup
from ..html_size import CAPTURE_BOUNDED_HTML_JS
from ..policy import Policy
from ..pool import _reap_orphans
from ..probe import clamp
from ..results import PayloadRejectedError, Rendered
from . import register

log = logging.getLogger("wellisearch.crawl.tiers.stealth")

WORKER_ERROR_MAX_CHARS = 1000
_semaphore: asyncio.Semaphore | None = None


class StealthTier:
    """Scrapling StealthySession: CF auto-solve, paywalls (last resort)."""

    name = "stealth"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Hold capacity until the synchronous worker and its browsers are dead."""
        global _semaphore
        if _semaphore is None:
            _semaphore = asyncio.Semaphore(max(1, get_settings().CRAWL_STEALTH_MAX_PARALLEL))
        timeout_s = clamp(get_settings().CRAWL_STEALTH_TIMEOUT_S)
        async with _semaphore:
            with tempfile.TemporaryDirectory(prefix="wellisearch-stealth-", ignore_cleanup_errors=True) as profile:
                context = multiprocessing.get_context("spawn")
                receiver, sender = context.Pipe(duplex=False)
                process = context.Process(target=_run_worker, args=(sender, url, timeout_s, profile), daemon=True)
                try:
                    process.start()
                    sender.close()
                    message = await asyncio.to_thread(receiver.recv)
                    if isinstance(message, Rendered):
                        return message
                    raise RuntimeError(f"stealth worker failed: {message}")
                finally:
                    sender.close()
                    await complete_cleanup(asyncio.to_thread(_stop_worker, process, receiver, profile))

    def _fetch_sync(
        self,
        url: str,
        timeout_s: float,
        profile: str | None = None,
    ) -> Rendered:
        """Run one sync StealthySession fetch and return the rendered page."""
        from scrapling.fetchers import StealthySession

        from ..engine import check_html_size

        s = get_settings()
        start = time.monotonic()
        oversized = False
        captured_html: str | None = None
        captured_title: str | None = None
        limit = s.CRAWL_HTML_MAX_BYTES

        def capture_and_clear(page: Any) -> None:
            """Capture only bounded HTML, then navigate away before Scrapling serializes."""
            nonlocal captured_html, captured_title, oversized
            captured_html = page.evaluate(CAPTURE_BOUNDED_HTML_JS, limit)
            if captured_html is None:
                oversized = True
            else:
                captured_title = page.title()
            # Scrapling builds a Response from the page after this hook; replace
            # the source document so its own serialization cannot copy a large DOM.
            page.goto("about:blank")

        with StealthySession(
            headless=s.CRAWL_HEADLESS,
            user_data_dir=profile or "",
            # extra_args are merged into the Playwright browser context options;
            # the tiers are read-only (see CRAWL_IGNORE_SSL_ERRORS).
            additional_args={"ignore_https_errors": bool(s.CRAWL_IGNORE_SSL_ERRORS)},
        ) as session:
            page = session.fetch(
                url,
                network_idle=True,
                timeout=int(timeout_s * 1000),
                page_action=capture_and_clear,
            )
        if oversized:
            raise PayloadRejectedError(f"HTML exceeds CRAWL_HTML_MAX_BYTES ({limit} bytes)")
        if captured_html is None:
            raise RuntimeError("browser HTML capture did not complete")
        html = captured_html
        check_html_size(html)
        ms = int((time.monotonic() - start) * 1000)
        return Rendered(
            html=html,
            title=captured_title,
            status=getattr(page, "status", 200),
            ms=ms,
            engine="stealth",
            content_type=_response_content_type(page),
        )

    def worst_case_s(self, p: Policy) -> float:
        """Worst-case budget: a single StealthySession fetch (clamped CRAWL_STEALTH_TIMEOUT_S)."""
        return clamp(get_settings().CRAWL_STEALTH_TIMEOUT_S)


def _response_content_type(page: object) -> str | None:
    """The response's Content-Type header, or None when unavailable."""
    headers = getattr(page, "headers", None)
    if isinstance(headers, dict):
        return headers.get("Content-Type") or headers.get("content-type")
    return None


def _run_worker(
    sender: Connection,
    url: str,
    timeout_s: float,
    profile: str,
) -> None:
    """Isolate a synchronous browser so the parent can kill its entire tree."""
    if os.name == "posix":
        os.setsid()
    try:
        rendered = StealthTier()._fetch_sync(url, timeout_s, profile)
        sender.send(rendered)
    except Exception as error:
        sender.send(f"{type(error).__name__}: {error}"[:WORKER_ERROR_MAX_CHARS])
    finally:
        sender.close()


def _stop_worker(
    process: multiprocessing.Process,
    receiver: Connection,
    profile: str,
) -> None:
    """Kill the owned process group before releasing its concurrency permit."""
    try:
        if process.pid is None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.is_alive():
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=get_settings().CRAWL_BROWSER_CLEANUP_TIMEOUT_S,
                )
            except (OSError, subprocess.TimeoutExpired):
                log.warning("stealth process-tree termination failed; killing worker directly")
        if process.is_alive():
            process.kill()
        process.join()
        process.close()
    finally:
        # Playwright starts Chromium in its own process group. Reap by this
        # worker's unique profile too, including already-reparented browsers.
        _reap_orphans(profile)
        receiver.close()


def _extract_title(page: object) -> str | None:
    """Best-effort <title> from the Scrapling Response; None on any failure.

    When the page carries multiple <title> tags, the last non-empty one wins —
    matching how browsers resolve document.title.
    """
    try:
        for el in reversed(page.css("title")):
            t = el.get_all_text()
            if t and t.strip():
                return t.strip()
    except Exception:
        pass
    return None


register(StealthTier())

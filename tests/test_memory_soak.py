"""Docker/Linux integration soak using real browsers and a local HTTP fixture.

Run after deploying, preferably while background indexing is paused:
    docker exec -i wellisearch python /tmp/test_memory_soak.py

No providers or database rows are modified. All browser profiles are temporary.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

import httpx

from wellisearch.config import Settings, get_settings
from wellisearch.crawl import pool as pool_module, shorturl
from wellisearch.crawl.policy import DEFAULT_POLICY
from wellisearch.crawl.pool import BrowserPool
from wellisearch.crawl.results import PayloadRejectedError
from wellisearch.crawl.tiers import browser, http, stealth

ROUNDS = 20
PARALLEL = 2
STEALTH_CANCEL_ROUNDS = 3
FIXTURE_TIMEOUT_S = 20
HEALTH_INTERVAL_S = 0.25
ARTICLE = (
    b"<html><title>Memory fixture</title><body><article><p>"
    + b"Useful article text. " * 100
    + b"</p></article></body></html>"
)
OVERSIZED_ARTICLE = b"<html><body>" + b"x" * 70000 + b"</body></html>"
GZIP_BODY = gzip.compress(b"decoded html content " * 20000)
SLOW_STARTED = threading.Event()


class Handler(BaseHTTPRequestHandler):
    """Serve bounded test pages, redirects, and a cancellable slow navigation."""

    def do_HEAD(self) -> None:
        """Support a normal shortener and a shortener requiring GET fallback."""
        if self.path == "/short-get":
            self.send_response(405)
            self.end_headers()
        elif self.path == "/short":
            self._redirect()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(ARTICLE)))
            self.end_headers()

    def do_GET(self) -> None:
        """Supply real libcurl callbacks without depending on any external website."""
        if self.path.startswith("/short"):
            self._redirect()
            return
        if self.path == "/slow":
            SLOW_STARTED.set()
            time.sleep(5)
        if self.path == "/gzip":
            payload = GZIP_BODY
        elif self.path == "/oversized":
            payload = OVERSIZED_ARTICLE
        else:
            payload = ARTICLE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if self.path == "/gzip":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(payload)

    def _redirect(self) -> None:
        """Resolve to a different hostname on the same local fixture server."""
        self.send_response(302)
        self.send_header("Location", f"http://localhost:{self.server.server_port}/article")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Keep soak output aggregate-only and easy to scan."""


def sample() -> dict:
    """Read cgroup memory and process/thread counts without external commands."""
    group = Path("/sys/fs/cgroup")
    return {
        "memory_mib": round(int((group / "memory.current").read_text()) / 1024 ** 2, 1),
        "swap_mib": round(int((group / "memory.swap.current").read_text()) / 1024 ** 2, 1),
        "pids": int((group / "pids.current").read_text()),
        "oom_kill": dict(line.split() for line in (group / "memory.events").read_text().splitlines())["oom_kill"],
    }


def owned_chrome_profiles(root: str) -> list[str]:
    """Find only live Chrome roots holding this soak's temporary profile directory."""
    profiles = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            args = (proc / "cmdline").read_bytes().decode(errors="replace").split(chr(0))
            profiles.extend(arg for arg in args if arg.startswith(f"--user-data-dir={root}"))
        except OSError:
            continue
    return profiles


async def main(health_url: str) -> None:
    """Exercise real downloads, recycling, and stealth cancellation alongside API probes."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    latencies = []
    errors = []
    stop = asyncio.Event()

    async def probe() -> None:
        """Measure API responsiveness while real browser processes turn over."""
        async with httpx.AsyncClient(timeout=5) as client:
            while not stop.is_set():
                started = time.perf_counter()
                try:
                    response = await client.get(health_url)
                    assert response.status_code == 200 and response.json()["status"] == "ok"
                    latencies.append(time.perf_counter() - started)
                except Exception as error:
                    errors.append(type(error).__name__)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=HEALTH_INTERVAL_S)

    baseline = sample()
    snapshots = [baseline]
    heartbeat = asyncio.create_task(probe())
    try:
        with tempfile.TemporaryDirectory(prefix="wellisearch-soak-") as root:
            settings = get_settings().model_copy(update={
                "CRAWL_BROWSER_MAX_USES": 4,
                "CRAWL_HTML_MAX_BYTES": 65536,
                "CRAWL_PROFILE_DIR": str(Path(root) / "browsers"),
                "CRAWL_SETTLE_S": 0,
                "CRAWL_SHORT_URL_HOSTS": "127.0.0.1",
            })
            await _exercise_http(base, settings, server.server_port)
            await _exercise_browser(base, settings, snapshots)
            await _exercise_stealth(base, settings, root)
            assert not owned_chrome_profiles(root), "soak left an owned browser alive"
    finally:
        stop.set()
        await heartbeat
        await asyncio.to_thread(server.shutdown)
        server.server_close()
    final = sample()
    assert baseline["oom_kill"] == final["oom_kill"], "soak caused an OOM kill"
    assert not errors, errors
    assert latencies, "no health probes completed"
    print(json.dumps({
        "result": "passed",
        "browser_fetches": ROUNDS * PARALLEL,
        "stealth_cancellations": STEALTH_CANCEL_ROUNDS,
        "health_checks": len(latencies),
        "health_max_ms": round(max(latencies) * 1000, 1),
        "health_avg_ms": round(sum(latencies) / len(latencies) * 1000, 1),
        "peak_memory_mib": max(row["memory_mib"] for row in snapshots),
        "baseline": baseline,
        "final": final,
    }), flush=True)


# ---------------------------------------------------------------------------
# Exercise Tiers
# ---------------------------------------------------------------------------

async def _exercise_http(base: str, settings: Settings, port: int) -> None:
    """Verify bounded HTTP downloads and header-only short-link resolution."""
    with patch.object(http, "get_settings", return_value=settings):
        rendered = await http.HttpTier().fetch(base + "/article", DEFAULT_POLICY)
        assert rendered.title == "Memory fixture" and rendered.html.encode() == ARTICLE
        try:
            await http.HttpTier().fetch(base + "/gzip", DEFAULT_POLICY)
            raise AssertionError("inflated body bypassed libcurl byte limit")
        except PayloadRejectedError:
            pass
    with patch.object(shorturl, "get_settings", return_value=settings):
        for path in ("/short", "/short-get"):
            final = await shorturl.resolve_short_url(base + path)
            assert final == f"http://localhost:{port}/article", final


async def _exercise_browser(base: str, settings: Settings, snapshots: list[dict]) -> None:
    """Recycle real browser contexts and reject oversized DOMs before capture."""
    pool = BrowserPool(size=PARALLEL)
    with (
        patch.object(pool_module, "get_settings", return_value=settings),
        patch.object(browser, "get_settings", return_value=settings),
        patch.object(browser, "get_pool", return_value=pool),
    ):
        try:
            for round_no in range(ROUNDS):
                results = await asyncio.gather(*(
                    browser.BrowserTier().fetch(base + f"/article?i={round_no}-{i}", DEFAULT_POLICY)
                    for i in range(PARALLEL)
                ))
                assert all(result.status == 200 for result in results)
                assert len(pool._contexts) <= PARALLEL
                assert all(
                    ctx.pages == [pool._anchors[key]]
                    for key, ctx in pool._contexts.items()
                )
                snapshots.append(sample())
                if (round_no + 1) % 5 == 0:
                    print(json.dumps({"browser_round": round_no + 1, **snapshots[-1]}), flush=True)
            try:
                await browser.BrowserTier().fetch(base + "/oversized", DEFAULT_POLICY)
                raise AssertionError("browser transferred an oversized DOM")
            except PayloadRejectedError:
                pass
        finally:
            await pool.close_all()


async def _exercise_stealth(base: str, settings: Settings, root: str) -> None:
    """Reject oversized Scrapling DOMs and reap cancelled stealth process trees."""
    with patch.object(tempfile, "tempdir", root):
        with patch.object(stealth, "get_settings", return_value=settings):
            rendered = await asyncio.to_thread(
                stealth.StealthTier()._fetch_sync,
                base + "/article",
                10,
                str(Path(root) / "stealth-success"),
            )
            assert rendered.title == "Memory fixture"
            assert "Useful article text." in rendered.html
            try:
                await asyncio.to_thread(
                    stealth.StealthTier()._fetch_sync,
                    base + "/oversized",
                    10,
                    str(Path(root) / "stealth-oversized"),
                )
                raise AssertionError("Scrapling transferred an oversized DOM")
            except PayloadRejectedError:
                pass
        assert not owned_chrome_profiles(root), "oversized stealth DOM left a browser alive"
        for i in range(STEALTH_CANCEL_ROUNDS):
            SLOW_STARTED.clear()
            task = asyncio.create_task(stealth.StealthTier().fetch(base + "/slow", DEFAULT_POLICY))
            try:
                started = await asyncio.to_thread(SLOW_STARTED.wait, FIXTURE_TIMEOUT_S)
                assert started, "stealth browser did not reach the slow navigation"
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            assert not owned_chrome_profiles(root), "cancelled stealth left an owned browser alive"
            print(json.dumps({"stealth_cancel": i + 1, **sample()}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-url", default="http://127.0.0.1:8780/health")
    asyncio.run(main(parser.parse_args().health_url))

"""Live-instance stress soak against the deployed wellisearch container.

Runs local fixture traffic only (no provider credits, no external sites) through
the live app's on-demand fetch path, so each request drives one full
crawl + store + embed cycle in the running service:

  - normal article pages accepted by the HTTP tier
  - "SPA" shells whose raw body is a short Loading stub; only a real browser
    (JS execution) sees the rendered article, forcing escalation to the
    browser tier and exercising the pooled Chromium contexts
  - oversized pages rejected at the header stage without transferring a body
  - slow pages exercising settle/timeout behavior under load

Background indexing is paused for the run so resource use is attributable to
this workload, then resumed (unless an abort gate fired). Automatic gates stop
new work before the hard limits: memory above MEM_GATE_GIB, process/thread
count above PID_GATE, any OOM or PID-limit event, or repeated health failures.

Run from the repo root on the host that runs the container:
    .venv/Scripts/python.exe tests/test_live_stress.py
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "http://localhost:8780"
CONTAINER_NAME = "wellisearch"
SAMPLE_INTERVAL_S = 2.0
BASELINE_S = 30
DRAIN_OBSERVE_S = 240
ABORT_OBSERVE_S = 60
MEM_GATE_GIB = 10.0
PID_GATE = 900
PASS_MEM_PEAK_GIB = 8.0
WARN_MEM_PEAK_GIB = 12.0
HEALTH_FAIL_LIMIT = 3
FETCH_TIMEOUT_S = 60
CALL_TIMEOUT_S = 15
ARTICLE_PARAGRAPHS = 40
OVERSIZED_BYTES = 6_000_000
SLOW_DELAY_S = 4.0

# (phase name, fixture kind, url count, concurrency) — order matters.
PHASES: tuple[tuple[str, str, int, int], ...] = (
    ("normal", "article", 48, 6),
    ("spa", "spa", 12, 3),
    ("oversized", "oversized", 8, 4),
    ("slow", "slow", 6, 3),
)

_SENTENCE = (
    "Stress paragraph {i} for page {n}. The quick brown fox jumps over the lazy dog near the river bank, "
    "while the slow train passes through the quiet valley without stopping."
)


# ---------------------------------------------------------------------------
# Fixture Server
# ---------------------------------------------------------------------------

class FixtureServer:
    """A threaded local HTTP server bound to all interfaces (host.docker.internal)."""

    def __init__(self: FixtureServer, run_id: str) -> None:
        """Bind the threading server to a free port and stamp the run id."""
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), _FixtureHandler)
        self._server.daemon_threads = True
        _FixtureHandler.run_id = run_id
        self.port = self._server.server_address[1]

    def start(self: FixtureServer) -> None:
        """Serve in a daemon thread."""
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self: FixtureServer) -> None:
        """Stop serving and close the listener."""
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------
# Metrics Monitor
# ---------------------------------------------------------------------------

class MetricsMonitor:
    """Sample container cgroup metrics + health latency; enforce abort gates.

    Samples every SAMPLE_INTERVAL_S while running. stop_new_work halts further
    request submission (soft gate); hard_abort marks an OOM/PID-limit event and
    skips the auto-resume of indexing at teardown.
    """

    def __init__(
        self: MetricsMonitor,
        base_url: str,
        container_name: str,
    ) -> None:
        """Initialize the sampler state and the gate events."""
        self._base_url = base_url
        self._container = container_name
        self.samples: list[dict] = []
        self.stop_new_work = threading.Event()
        self.hard_abort = threading.Event()
        self.gate_reasons: list[str] = []
        self._health_fails = 0
        self._thread: threading.Thread | None = None

    def start(self: MetricsMonitor) -> None:
        """Begin sampling in a daemon thread."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self: MetricsMonitor) -> None:
        """Wait for the sampler to finish its current sample."""
        if self._thread is None:
            return
        self._thread.join(timeout=SAMPLE_INTERVAL_S * 2)

    def _loop(self: MetricsMonitor) -> None:
        """Sample forever at SAMPLE_INTERVAL_S until the thread is stopped."""
        while True:
            self._sample()
            time.sleep(SAMPLE_INTERVAL_S)

    def _sample(self: MetricsMonitor) -> None:
        """Read cgroup counters and health latency, then check the gates."""
        sample: dict = {"t": round(time.monotonic(), 1)}
        try:
            out = subprocess.run(
                [
                    "docker", "exec", self._container, "sh", "-c",
                    "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/pids.current"
                    " /sys/fs/cgroup/memory.events /sys/fs/cgroup/pids.events",
                ],
                capture_output=True, text=True, timeout=10, check=False,
            )
            lines = out.stdout.splitlines()
            sample["mem_bytes"] = int(lines[0])
            sample["pids"] = int(lines[1])
            events = dict(line.split() for line in lines[2:] if " " in line)
            sample["oom_kill"] = int(events.get("oom_kill", 0))
            sample["pids_max_events"] = int(events.get("max", 0))
        except Exception as e:
            print(f"[monitor] cgroup read failed: {e}", flush=True)
            return
        started = time.monotonic()
        try:
            with urllib.request.urlopen(f"{self._base_url}/health", timeout=10) as resp:
                sample["health_ms"] = int((time.monotonic() - started) * 1000)
                sample["health_ok"] = resp.status == 200
        except Exception:
            sample["health_ms"] = None
            sample["health_ok"] = False
        self.samples.append(sample)

        if sample.get("oom_kill", 0) > 0 or sample.get("pids_max_events", 0) > 0:
            self._trip_hard(
                f"OOM/PID-limit event (oom_kill={sample['oom_kill']}, pids_max={sample['pids_max_events']})"
            )
        elif sample["mem_bytes"] / 2**30 > MEM_GATE_GIB or sample["pids"] > PID_GATE:
            self._trip_soft(
                f"soft gate (memory {sample['mem_bytes'] / 2**30:.1f} GiB, pids {sample['pids']})"
            )
        if not sample.get("health_ok"):
            self._health_fails += 1
            if self._health_fails >= HEALTH_FAIL_LIMIT:
                self._trip_soft(f"{HEALTH_FAIL_LIMIT} consecutive health failures")
        else:
            self._health_fails = 0

    def _trip_soft(self: MetricsMonitor, reason: str) -> None:
        """Record the reason and stop new work (first trip only)."""
        if self.stop_new_work.is_set():
            return
        self.gate_reasons.append(reason)
        print(f"[gate] {reason}", flush=True)
        self.stop_new_work.set()

    def _trip_hard(self: MetricsMonitor, reason: str) -> None:
        """Trip the soft gate and mark the run as a hard abort."""
        self._trip_soft(reason)
        if self.hard_abort.is_set():
            return
        self.gate_reasons.append(f"HARD ABORT: {reason}")
        print(f"[gate] HARD ABORT — {reason}", flush=True)
        self.hard_abort.set()


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def run_phase(
    phase: str,
    kind: str,
    count: int,
    concurrency: int,
    base_url: str,
    fixture_base: str,
    api_key: str | None,
    stop_new_work: threading.Event,
) -> list[dict]:
    """Submit `count` unique fixture URLs at the given concurrency."""
    urls = [f"{fixture_base}/{kind}/{i}" for i in range(1, count + 1)]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_fetch_one, base_url, api_key, url, stop_new_work) for url in urls]
        for future in futures:
            results.append(future.result())
    ok = sum(1 for r in results if r.get("ok"))
    skipped = sum(1 for r in results if r.get("skipped"))
    errors = [r["error"] for r in results if not r.get("ok") and not r.get("skipped")]
    print(
        json.dumps({
            "phase": phase, "requested": count, "ok": ok, "skipped": skipped,
            "errors": len(errors), "sample_errors": errors[:3]
        }),
        flush=True
    )
    return results


# ---------------------------------------------------------------------------
# Teardown & Assessment
# ---------------------------------------------------------------------------

def assess(monitor: MetricsMonitor, phase_results: dict[str, list[dict]]) -> tuple[str, list[str]]:
    """PASS/WARN/FAIL with the reasons a reviewer should check first."""
    reasons: list[str] = []
    if monitor.hard_abort.is_set():
        return "FAIL", ["hard abort gate fired (OOM or PID-limit event)"] + monitor.gate_reasons

    mem_peaks = [s["mem_bytes"] for s in monitor.samples if "mem_bytes" in s]
    pid_peaks = [s["pids"] for s in monitor.samples if "pids" in s]
    peak_mem_gib = max(mem_peaks) / 2**30 if mem_peaks else 0.0
    peak_pids = max(pid_peaks) if pid_peaks else 0
    health_ms = [s["health_ms"] for s in monitor.samples if s.get("health_ms") is not None]
    p95_health = (
        sorted(health_ms)[int(len(health_ms) * 0.95)]
        if len(health_ms) > 20
        else (max(health_ms) if health_ms else 0)
    )

    verdict = "PASS"
    if peak_mem_gib >= WARN_MEM_PEAK_GIB:
        verdict, reasons = "FAIL", [f"peak memory {peak_mem_gib:.1f} GiB reached the fail band"]
    elif peak_mem_gib > PASS_MEM_PEAK_GIB:
        verdict, reasons = "WARN", [f"peak memory {peak_mem_gib:.1f} GiB above the pass band"]
    if peak_pids > PID_GATE:
        verdict = _demote(verdict)
        reasons.append(f"peak process/thread count {peak_pids} crossed the soft gate (limit 1,024)")
    elif peak_pids > 800:
        verdict = _demote(verdict)
        reasons.append(f"peak process/thread count {peak_pids} is close to the 1,024 limit")
    if p95_health > 2000:
        verdict = _demote(verdict)
        reasons.append(f"health p95 latency {p95_health} ms during load")

    for phase, results in phase_results.items():
        attempted = [r for r in results if not r.get("skipped")]
        failed = [r for r in attempted if not r.get("ok")]
        skipped = len(results) - len(attempted)
        fail_reason = _phase_failures(phase, attempted, failed)
        if fail_reason is not None:
            verdict = "FAIL"
            reasons.append(fail_reason)
        if skipped:
            reasons.append(f"{phase}: {skipped} requests skipped by a gate (see gate reasons)")

    if len(monitor.samples) < 5:
        verdict = "FAIL"
        reasons.append("too few metric samples to assess the run")
    return verdict, reasons


def _demote(verdict: str) -> str:
    """Downgrade PASS to WARN; keep WARN/FAIL as-is."""
    return "WARN" if verdict == "PASS" else verdict


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the live stress soak end-to-end and print a machine-readable summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--container", default=CONTAINER_NAME)
    parser.add_argument(
        "--keep-pages",
        action="store_true",
        help="leave the stored fixture pages in the index",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    api_key = _read_api_key(repo_root)
    run_id = time.strftime("%Y%m%d-%H%M%S")

    monitor = MetricsMonitor(args.base_url, args.container)
    server = FixtureServer(run_id)
    server.start()
    fixture_base = f"http://host.docker.internal:{server.port}/stress/{run_id}"
    print(f"[setup] run {run_id}, fixture server on host port {server.port}", flush=True)

    # Preflight: the container must be able to reach the fixture server.
    preflight_url = f"{fixture_base}/article/0"
    status, body = _http_json(
        "POST", f"{args.base_url}/api/fetch", api_key=api_key,
        body={"url": preflight_url, "format": "json"}, timeout=FETCH_TIMEOUT_S,
    )
    if not (body or {}).get("ok"):
        print(f"[setup] preflight fetch failed: {status} {body}", flush=True)
        server.stop()
        raise SystemExit(2)

    monitor.start()
    time.sleep(BASELINE_S)
    baseline = monitor.samples[-1] if monitor.samples else {}
    print(json.dumps({"baseline": baseline}), flush=True)

    # Pause background indexing so the workload is attributable; resume at teardown.
    status, body = _http_json("PATCH", f"{args.base_url}/api/worker", api_key=api_key, body={"paused": True})
    if status != 200 or not body.get("ok"):
        print(f"[setup] could not pause indexing: {status} {body}", flush=True)
        monitor.stop()
        server.stop()
        raise SystemExit(3)
    print(json.dumps({"indexing_paused": True, "queue_pending": body.get("queue_pending")}), flush=True)

    phase_results: dict[str, list[dict]] = {}
    try:
        for name, kind, count, concurrency in PHASES:
            _run_phase_gated(
                phase_results, name, kind, count, concurrency,
                args.base_url, fixture_base, api_key, monitor.stop_new_work,
            )

        observe_s = ABORT_OBSERVE_S if monitor.hard_abort.is_set() else DRAIN_OBSERVE_S
        print(f"[drain] observing for {observe_s}s", flush=True)
        deadline = time.monotonic() + observe_s
        while time.monotonic() < deadline:
            time.sleep(SAMPLE_INTERVAL_S * 2)
    finally:
        monitor.stop()
        server.stop()

    stored_urls = (
        [preflight_url]
        + [r["url"] for results in phase_results.values() for r in results if r.get("ok")]
    )
    deleted = 0 if args.keep_pages else _delete_pages(args.base_url, api_key, stored_urls)

    resumed = False
    if not monitor.hard_abort.is_set():
        status, body = _http_json(
            "PATCH", f"{args.base_url}/api/worker", api_key=api_key, body={"paused": False}
        )
        resumed = status == 200 and bool(body.get("ok"))
        print(json.dumps({"indexing_resumed": resumed}), flush=True)

    final = monitor.samples[-1] if monitor.samples else {}
    verdict, reasons = assess(monitor, phase_results)
    summary = {
        "result": verdict.lower(),
        "run_id": run_id,
        "baseline": baseline,
        "final": final,
        "peak_memory_gib": round(
            max((s["mem_bytes"] for s in monitor.samples if "mem_bytes" in s), default=0) / 2**30,
            2,
        ),
        "peak_pids": max((s["pids"] for s in monitor.samples if "pids" in s), default=0),
        "gate_reasons": monitor.gate_reasons,
        "pages_stored": len(stored_urls),
        "pages_deleted": deleted,
        "indexing_resumed": resumed,
        "reasons": reasons,
    }
    print(json.dumps(summary, indent=2), flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _article_html(n: int) -> str:
    """A plain ~6 KB article page accepted by the HTTP tier."""
    paras = "".join(f"<p>{_SENTENCE.format(i=i, n=n)}</p>" for i in range(1, ARTICLE_PARAGRAPHS + 1))
    return (
        f"<html><head><title>Stress Article {n}</title></head><body>"
        f"<article>{paras}</article></body></html>"
    )


def _spa_html(n: int) -> str:
    """A client-rendered shell: raw body is a Loading stub, JS renders the article.

    The http tier captures only the stub (the generic extractor escalates to
    the browser tier); a real browser executes the inline script and sees the
    full article. Neither the rendered text nor the title contains 'loading'.
    """
    js = (
        f'(function(){{var s={json.dumps(_SENTENCE.format(i="{i}", n=n))};'
        f'var h="";for(var i=0;i<{ARTICLE_PARAGRAPHS};i++){{'
        f'h+="<p>"+s.replace("{{i}}",String(i+1))+"</p>";}}'
        "document.body.innerHTML=h;})();"
    )
    return (
        f"<html><head><title>Stress SPA {n}</title></head>"
        f'<body>Loading…<script>{js}</script></body></html>'
    )


def _oversized_html(n: int) -> bytes:
    """A page whose declared size exceeds CRAWL_HTML_MAX_BYTES (default 5.24 MB)."""
    filler = b"x" * (OVERSIZED_BYTES - 160)
    head = f"<html><head><title>Stress Oversized {n}</title></head><body>".encode()
    return head + filler + b"</body></html>"


def _fixture_bytes(kind: str, n: int) -> bytes:
    """Render one fixture page body for the given kind."""
    if kind == "spa":
        return _spa_html(n).encode("utf-8")
    if kind == "oversized":
        return _oversized_html(n)
    return _article_html(n).encode("utf-8")


class _FixtureHandler(BaseHTTPRequestHandler):
    """Serve /stress/{run}/{kind}/{n} pages to the container's crawler."""

    run_id: str = ""

    def do_GET(self: _FixtureHandler) -> None:  # noqa: N802 — http.server API name
        """Serve one fixture page; 404 on unknown paths or the wrong run id."""
        match = re.fullmatch(r"/stress/([^/]+)/(article|spa|oversized|slow)/(\d+)", self.path)
        if not match or match.group(1) != self.run_id:
            self._respond(404, b"not found")
            return
        kind, n = match.group(2), int(match.group(3))
        if kind == "slow":
            time.sleep(SLOW_DELAY_S)
        body = _fixture_bytes(kind, n)
        print(f"[fixture] {kind} {n} ({len(body)} bytes)", flush=True)
        self._respond(200, body)

    def _respond(
        self: _FixtureHandler,
        status: int,
        body: bytes,
    ) -> None:
        """Send one response with an explicit Content-Length."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(
        self: _FixtureHandler,  # noqa: A002 — http.server API name
        fmt: str,
        *args: object,
    ) -> None:
        """Suppress per-request stderr logging; do_GET prints what matters."""


def _http_json(
    method: str,
    url: str,
    api_key: str | None = None,
    body: dict | None = None,
    timeout: int = CALL_TIMEOUT_S,
) -> tuple[int, dict]:
    """One JSON request; returns (status code, parsed body or error stub)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {"error": raw.decode(errors="replace")}
        except json.JSONDecodeError:
            return e.code, {"error": raw.decode(errors="replace")}


def _read_api_key(repo_root: Path) -> str | None:
    """The live API key from .env (never printed); None when unset."""
    env = repo_root / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("WELLISEARCH_API_KEY="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def _fetch_one(
    base_url: str,
    api_key: str | None,
    fixture_url: str,
    stop_new_work: threading.Event,
) -> dict:
    """One POST /api/fetch; skips the call when a gate has stopped new work."""
    if stop_new_work.is_set():
        return {"url": fixture_url, "skipped": True}
    started = time.monotonic()
    status, body = _http_json(
        "POST", f"{base_url}/api/fetch", api_key=api_key,
        body={"url": fixture_url, "format": "json"}, timeout=FETCH_TIMEOUT_S,
    )
    return {
        "url": fixture_url,
        "status": status,
        "ok": bool(body.get("ok")),
        "error": (body.get("error") or "")[:200],
        "ms": int((time.monotonic() - started) * 1000),
    }


def _delete_pages(
    base_url: str,
    api_key: str | None,
    urls: list[str],
) -> int:
    """Best-effort removal of the pages this run stored; returns deletions made."""
    deleted = 0
    for url in urls:
        encoded = urllib.parse.quote(url, safe="")
        status, _body = _http_json("DELETE", f"{base_url}/api/pages/{encoded}", api_key=api_key)
        if status == 200:
            deleted += 1
    return deleted


def _phase_failures(
    phase: str,
    attempted: list[dict],
    failed: list[dict],
) -> str | None:
    """The FAIL reason for a phase's results, or None when the phase is clean."""
    if phase == "oversized":
        # Every attempted oversized page must be rejected, never stored.
        if len(failed) != len(attempted):
            return f"oversized: only {len(failed)} of {len(attempted)} were rejected as expected"
        return None
    if failed:
        return f"{phase}: {len(failed)} unexpected failures, e.g. {failed[0]['error'][:120]}"
    return None


def _run_phase_gated(
    phase_results: dict[str, list[dict]],
    phase: str,
    kind: str,
    count: int,
    concurrency: int,
    base_url: str,
    fixture_base: str,
    api_key: str | None,
    stop_new_work: threading.Event,
) -> None:
    """Run one phase then settle; print a skip note when a gate stopped new work."""
    if stop_new_work.is_set():
        print(f"[phase] {phase} skipped (gate active)", flush=True)
        return
    phase_results[phase] = run_phase(
        phase, kind, count, concurrency, base_url, fixture_base, api_key, stop_new_work
    )
    time.sleep(SAMPLE_INTERVAL_S * 2)


if __name__ == "__main__":
    main()

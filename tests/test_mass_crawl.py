"""Mass-crawl stress test against a disposable wellisearch instance.

Selects 5000 recently crawled URLs from the live database and seeds them into
a throwaway wellisearch container (its own database on the shared Postgres
server), then watches the worker drain the queue while sampling cgroup memory,
PID count, OOM/PID-limit events, health latency, and queue depth every 5 s.

Automatic gates protect the run: a soft gate (memory above MEM_GATE_GIB) pauses
the test worker until usage settles below the resume thresholds, then resumes;
an OOM kill is an immediate hard stop. PID-limit events are recorded and make
the verdict at least WARN — they only become a hard stop when PIDs stay pinned
at the limit for SUSTAINED_LIMIT_S (a sign of cascading launch failures). The
run ends when the queue stays empty for SETTLE_S after all seeds are in, or at
HARD_CAP_S.

The disposable instance and its database are left running afterwards so the
results can be inspected; tear down with:
    docker compose -f compose.test.yml down --remove-orphans
and drop the wellisearch_test database when no longer needed.

Run from the repo root on the host that runs the containers, after starting
the test instance (docker compose -f compose.test.yml up -d):
    .venv/Scripts/python.exe tests/test_mass_crawl.py
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import threading
import time
import urllib.request

import psycopg
from psycopg import connect

BASE_URL = "http://127.0.0.1:8781"
CONTAINER_NAME = "wellisearch-test"
TEST_DB = "wellisearch_test"
URL_COUNT = 5000
RECENT_WINDOW_HOURS = 72
SEED_RATE_PER_S = 10
SAMPLE_INTERVAL_S = 5.0
PROGRESS_EVERY_S = 60.0
BREAKDOWN_EVERY_SAMPLES = 6   # per-process task breakdown every ~30 s
SETTLE_S = 300.0
HARD_CAP_S = 8 * 3600
MEM_GATE_GIB = 10.0
PIDS_PIN_THRESHOLD = 1000   # pids.current at/above this counts as "pinned" at the limit
SUSTAINED_LIMIT_S = 300.0   # pinned for this long => hard stop (cascading failures)
RESUME_MEM_GIB = 8.0
RESUME_PIDS = 700
SOFT_WAIT_MAX_S = 300.0
MAX_SOFT_INTERVENTIONS = 3
HEALTH_FAIL_LIMIT = 5
CALL_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# Environment & Database
# ---------------------------------------------------------------------------

def select_urls(conn: psycopg.Connection, count: int) -> list[str]:
    """Random recently-crawled URLs from the live index (natural status mix)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT url FROM pages WHERE last_crawled > now() - %s * interval '1 hour' "
        "ORDER BY random() LIMIT %s",
        (RECENT_WINDOW_HOURS, count),
    )
    return [row[0] for row in cur.fetchall()]


def queue_depth(conn: psycopg.Connection) -> tuple[int, int]:
    """(pending, in_flight) row counts in the test instance's crawl queue."""
    cur = conn.cursor()
    cur.execute(
        "SELECT count(*) FILTER (WHERE status = 'pending'), "
        "count(*) FILTER (WHERE status = 'in_flight') FROM crawl_queue"
    )
    pending, in_flight = cur.fetchone()
    return int(pending), int(in_flight)


def final_stats(conn: psycopg.Connection) -> dict:
    """Page and queue outcome breakdowns from the test database."""
    cur = conn.cursor()
    cur.execute("SELECT last_status, count(*) FROM pages GROUP BY 1 ORDER BY 2 DESC")
    pages = {status: n for status, n in cur.fetchall()}
    cur.execute("SELECT status, count(*) FROM crawl_queue GROUP BY 1 ORDER BY 2 DESC")
    queue = {status: n for status, n in cur.fetchall()}
    return {"pages_by_status": pages, "queue_by_status": queue}


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Seed the mass-crawl workload and watch the drain to completion."""
    urls, test_conn = _prepare_run()

    last_progress = time.monotonic()
    start = time.monotonic()

    seed_stats = {"done": 0, "errors": 0, "ok": 0}
    seed_stop = threading.Event()
    seed_done = threading.Event()
    seeder = threading.Thread(
        target=_seed_worker, args=(BASE_URL, urls, seed_stop, seed_done, seed_stats), daemon=True
    )
    print(f"[setup] seeding {len(urls)} urls at {SEED_RATE_PER_S}/s", flush=True)
    seeder.start()
    verdict, reasons, paused, elapsed, gate_events, pids_limit_total, samples = _monitor(
        test_conn, len(urls), seed_done, seed_stats, start, last_progress
    )

    seed_stop.set()
    seeder.join(timeout=10)

    stats = final_stats(test_conn)
    test_conn.close()
    summary = _build_summary(
        verdict, reasons, elapsed, seed_stats, pids_limit_total, samples,
        gate_events, paused, stats,
    )
    print(json.dumps(summary, indent=2), flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prepare_run() -> tuple[list[str], psycopg.Connection]:
    """Wait for the test instance to become healthy, select the workload
    URLs from the live index, and open a connection to the test database."""
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    env = _read_env(repo_root)
    live_db = env.get("POSTGRES_DB", "wellisearch")

    # Wait for the test instance (DB self-create + schema + model load).
    print("[setup] waiting for test instance health...", flush=True)
    deadline = time.monotonic() + 300
    while not _health_ms(BASE_URL)[0]:
        if time.monotonic() > deadline:
            print("[setup] test instance never became healthy", flush=True)
            raise SystemExit(2)
        time.sleep(5)

    live_conn = _db_conn(env, live_db)
    urls = select_urls(live_conn, URL_COUNT)
    live_conn.close()
    if len(urls) < URL_COUNT:
        print(f"[setup] only {len(urls)} recent URLs available (wanted {URL_COUNT})", flush=True)
    test_conn = _db_conn(env, TEST_DB)
    return urls, test_conn


def _monitor(
    test_conn: psycopg.Connection,
    url_count: int,
    seed_done: threading.Event,
    seed_stats: dict[str, int],
    start: float,
    last_progress: float,
) -> tuple[str, list[str], bool, float, list[dict], int, list[dict]]:
    """The drain monitor loop: sample the container + queue every
    SAMPLE_INTERVAL_S and enforce the soft/hard gates. Ends when the queue
    stays empty for SETTLE_S after all seeds are in, or at HARD_CAP_S.
    Returns (verdict, reasons, paused, elapsed, gate_events, pids_limit_total,
    samples)."""
    samples: list[dict] = []
    gate_events: list[str] = []
    interventions = 0
    paused = False
    health_fails = 0
    prev_oom_kill = 0
    prev_pids_max = 0
    pids_limit_total = 0
    pinned_since: float | None = None
    empty_since: float | None = None
    elapsed = 0.0
    verdict, reasons = "PASS", []

    while True:
        now = time.monotonic()
        elapsed = now - start
        if elapsed > HARD_CAP_S:
            verdict, reasons = "FAIL", [f"hard cap reached after {elapsed / 3600:.1f} h"]
            break

        taken = _take_sample(test_conn, elapsed, samples)
        if taken is None:
            time.sleep(SAMPLE_INTERVAL_S)
            continue
        sample, ok, health_latency, pending, in_flight, cg = taken

        # Hard gate 1: any NEW OOM kill stops the run for inspection.
        oom_reason = _oom_gate(cg, prev_oom_kill, elapsed)
        if oom_reason is not None:
            paused = True
            verdict, reasons = "FAIL", [oom_reason] + gate_events
            break

        # PID-limit events are recorded; they only hard-stop when PIDs stay pinned.
        if cg and cg["pids_max_events"] > prev_pids_max:
            pids_limit_total = cg["pids_max_events"]
            detail = (
                f"PID-limit event at t={elapsed:.0f}s "
                f"(pids_limit_events {prev_pids_max} -> {cg['pids_max_events']}, pids {cg['pids']})"
            )
            gate_events.append(detail)
            print(f"[gate] {detail}", flush=True)
        if cg is not None:
            prev_oom_kill = max(prev_oom_kill, cg["oom_kill"])
            prev_pids_max = max(prev_pids_max, cg["pids_max_events"])

        # Hard gate 2: PIDs pinned at the limit for SUSTAINED_LIMIT_S.
        pids_fail, pinned_since = _pids_pin_gate(cg, pinned_since, now)
        if pids_fail is not None:
            paused = True
            verdict, reasons = "FAIL", [pids_fail] + gate_events
            break

        # Soft gate (memory only): pause until usage settles below the resume thresholds.
        soft_fail, paused, interventions = _soft_mem_gate(
            cg, paused, interventions, elapsed, samples, gate_events
        )
        if soft_fail is not None:
            verdict, reasons = "FAIL", [soft_fail] + gate_events
            break

        # Repeated health failures get the same pause-and-recover treatment.
        paused, health_fails, interventions = _health_gate(
            ok, paused, health_fails, interventions, elapsed, gate_events
        )

        # Done once every seed is in and the queue stays empty for SETTLE_S.
        empty_since, done = _queue_settled(seed_done, pending, in_flight, empty_since, now)
        if done:
            break

        last_progress = _print_progress(
            now, last_progress, elapsed, seed_stats, url_count, sample,
            pending, in_flight, health_latency,
        )

        time.sleep(SAMPLE_INTERVAL_S)

    return verdict, reasons, paused, elapsed, gate_events, pids_limit_total, samples


def _take_sample(
    test_conn: psycopg.Connection,
    elapsed: float,
    samples: list[dict],
) -> tuple[dict, bool, int | None, int, int, dict | None] | None:
    """One monitor sample: cgroup read, health probe, queue depth.
    Returns (sample, ok, health_latency, pending, in_flight, cgroup) and
    appends the sample to `samples`; None when the queue-depth read fails."""
    cg = _sample_cgroup(CONTAINER_NAME)
    ok, health_latency = _health_ms(BASE_URL)
    try:
        pending, in_flight = queue_depth(test_conn)
    except Exception as e:
        print(f"[monitor] queue depth read failed: {e}", flush=True)
        return None
    sample: dict = {
        "t": round(elapsed, 1), "pending": pending, "in_flight": in_flight,
        "health_ms": health_latency,
    }
    if cg is not None:
        sample.update(cg)
        _maybe_add_breakdown(sample, samples)
    samples.append(sample)
    return sample, ok, health_latency, pending, in_flight, cg


def _oom_gate(
    cg: dict | None,
    prev_oom_kill: int,
    elapsed: float,
) -> str | None:
    """Hard gate 1: any NEW OOM kill stops the run for inspection.
    Pauses the worker and returns the FAIL reason, else None."""
    if not (cg and cg["oom_kill"] > prev_oom_kill):
        return None
    _set_worker_paused(BASE_URL, True)
    return f"OOM kill at t={elapsed:.0f}s (oom_kill {prev_oom_kill} -> {cg['oom_kill']})"


def _pids_pin_gate(
    cg: dict | None,
    pinned_since: float | None,
    now: float,
) -> tuple[str | None, float | None]:
    """Hard gate 2: PIDs pinned at/above PIDS_PIN_THRESHOLD for
    SUSTAINED_LIMIT_S (a sign of cascading launch failures). Returns
    (FAIL reason, updated pinned_since); the reason is None unless the
    gate trips."""
    if cg is not None and cg["pids"] >= PIDS_PIN_THRESHOLD:
        if pinned_since is None:
            return None, now
        if now - pinned_since >= SUSTAINED_LIMIT_S:
            return (
                f"PIDs pinned at/above {PIDS_PIN_THRESHOLD} for {SUSTAINED_LIMIT_S:.0f}s",
                pinned_since,
            )
        return None, pinned_since
    return None, None


def _soft_mem_gate(
    cg: dict | None,
    paused: bool,
    interventions: int,
    elapsed: float,
    samples: list[dict],
    gate_events: list[str],
) -> tuple[str | None, bool, int]:
    """Soft gate (memory only): pause the worker until usage settles below
    the resume thresholds. Returns (FAIL reason, updated paused, updated
    interventions); the reason is set when the intervention budget is
    exceeded."""
    if not (cg and not paused and cg["mem_bytes"] / 2**30 > MEM_GATE_GIB):
        return None, paused, interventions
    interventions += 1
    detail = (
        f"soft gate at t={elapsed:.0f}s "
        f"(memory {cg['mem_bytes'] / 2**30:.1f} GiB, pids {cg['pids']})"
    )
    if interventions > MAX_SOFT_INTERVENTIONS:
        _set_worker_paused(BASE_URL, True)
        return f"{detail} — too many soft-gate interventions", True, interventions
    gate_events.append(detail)
    print(f"[gate] {detail} — pausing worker until settled", flush=True)
    if _set_worker_paused(BASE_URL, True):
        paused = True
        _wait_settled(CONTAINER_NAME, samples)
        if _set_worker_paused(BASE_URL, False):
            paused = False
            print("[gate] usage settled — worker resumed", flush=True)
    return None, paused, interventions


def _health_gate(
    ok: bool,
    paused: bool,
    health_fails: int,
    interventions: int,
    elapsed: float,
    gate_events: list[str],
) -> tuple[bool, int, int]:
    """Repeated health failures get the same pause-and-recover treatment as
    the soft gate. Returns (updated paused, updated health_fails, updated
    interventions)."""
    if not ok:
        health_fails += 1
        if health_fails >= HEALTH_FAIL_LIMIT and not paused:
            interventions += 1
            detail = f"{HEALTH_FAIL_LIMIT} consecutive health failures at t={elapsed:.0f}s"
            gate_events.append(detail)
            print(f"[gate] {detail} — pausing worker until healthy", flush=True)
            paused = _pause_until_healthy()
    else:
        health_fails = 0
    return paused, health_fails, interventions


def _pause_until_healthy() -> bool:
    """Pause the worker, wait (bounded) until healthy again, then resume.
    Returns the updated paused state: True when the worker is still paused,
    False when it was resumed (or the pause itself failed)."""
    if not _set_worker_paused(BASE_URL, True):
        return False
    healthy_deadline = time.monotonic() + SOFT_WAIT_MAX_S
    while not _health_ms(BASE_URL)[0] and time.monotonic() < healthy_deadline:
        time.sleep(SAMPLE_INTERVAL_S)
    return not _set_worker_paused(BASE_URL, False)


def _queue_settled(
    seed_done: threading.Event,
    pending: int,
    in_flight: int,
    empty_since: float | None,
    now: float,
) -> tuple[float | None, bool]:
    """Done once every seed is in and the queue stays empty for SETTLE_S.
    Returns (updated empty_since, done)."""
    if not seed_done.is_set():
        return empty_since, False
    if pending != 0 or in_flight != 0:
        return None, False
    if empty_since is None:
        return now, False
    if now - empty_since >= SETTLE_S:
        return empty_since, True
    return empty_since, False


def _print_progress(
    now: float,
    last_progress: float,
    elapsed: float,
    seed_stats: dict[str, int],
    url_count: int,
    sample: dict,
    pending: int,
    in_flight: int,
    health_latency: int | None,
) -> float:
    """The periodic progress line (every PROGRESS_EVERY_S).
    Returns the updated last_progress."""
    if now - last_progress < PROGRESS_EVERY_S:
        return last_progress
    mem_gib = sample.get("mem_bytes", 0) / 2**30
    print(
        f"[t={elapsed:6.0f}s] seeded={seed_stats['done']}/{url_count} "
        f"pending={pending} in_flight={in_flight} "
        f"mem={mem_gib:.2f}GiB pids={sample.get('pids', '?')} health={health_latency}ms",
        flush=True,
    )
    return now


def _build_summary(
    verdict: str,
    reasons: list[str],
    elapsed: float,
    seed_stats: dict[str, int],
    pids_limit_total: int,
    samples: list[dict],
    gate_events: list[str],
    paused: bool,
    stats: dict,
) -> dict:
    """The final summary. Upgrades PASS to WARN when PID-limit events were
    recorded (contained); aggregates peaks from the samples."""
    if pids_limit_total > 0 and verdict == "PASS":
        verdict = "WARN"
        reasons.append(f"{pids_limit_total} PID-limit events during the run (contained)")

    peak_mem_gib = max((s["mem_bytes"] for s in samples if "mem_bytes" in s), default=0) / 2**30
    peak_pids = max((s["pids"] for s in samples if "pids" in s), default=0)
    latencies = sorted(s["health_ms"] for s in samples if s.get("health_ms") is not None)
    p95_health = (
        latencies[int(len(latencies) * 0.95)]
        if len(latencies) > 20
        else (latencies[-1] if latencies else None)
    )

    comm_peaks: dict[str, int] = {}
    for s in samples:
        for name, n in s.get("tasks_by_comm", {}).items():
            comm_peaks[name] = max(comm_peaks.get(name, 0), n)

    summary = {
        "result": verdict.lower(),
        "reasons": reasons,
        "elapsed_s": int(elapsed),
        "urls_seeded": seed_stats["done"],
        "seeded_ok": seed_stats["ok"],
        "seed_errors": seed_stats["errors"],
        "pids_limit_events_total": pids_limit_total,
        "peak_memory_gib": round(peak_mem_gib, 2),
        "peak_pids": peak_pids,
        "task_peaks_by_comm": comm_peaks,
        "health_p95_ms": p95_health,
        "gate_events": gate_events,
        "worker_paused_at_end": paused,
        **stats,
    }
    return summary


def _read_env(repo_root: pathlib.Path) -> dict[str, str]:
    """Key=value pairs from .env (values are never printed)."""
    env: dict[str, str] = {}
    for line in (repo_root / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def _db_conn(env: dict[str, str], dbname: str) -> psycopg.Connection:
    """A connection to the shared Postgres server on the host."""
    return connect(
        host="localhost",
        port=int(env.get("POSTGRES_PORT", "5432")),
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        dbname=dbname,
    )


def _sample_cgroup(container: str) -> dict | None:
    """One cgroup v2 read from inside the container; None on failure."""
    try:
        out = subprocess.run(
            [
                "docker", "exec", container, "sh", "-c",
                "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/pids.current"
                " /sys/fs/cgroup/memory.events /sys/fs/cgroup/pids.events",
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        lines = out.stdout.splitlines()
        events = dict(line.split() for line in lines[2:] if " " in line)
        return {
            "mem_bytes": int(lines[0]),
            "pids": int(lines[1]),
            "oom_kill": int(events.get("oom_kill", 0)),
            "pids_max_events": int(events.get("max", 0)),
        }
    except Exception as e:
        print(f"[monitor] cgroup read failed: {e}", flush=True)
        return None


_TASK_BREAKDOWN_SCRIPT = (
    "import glob; rows=[];"
    "[rows.append((open(p + '/comm').read().strip(), len(glob.glob(p + '/task/*'))))"
    " for p in glob.glob('/proc/[0-9]*')];"
    "t={};"
    "[t.update({n: t.get(n, 0) + c}) for n, c in rows];"
    "print(sorted(t.items(), key=lambda kv: -kv[1])[:8])"
)


def _task_breakdown(container: str) -> dict[str, int] | None:
    """Total kernel tasks (processes + threads) per process name, top 8."""
    try:
        out = subprocess.run(
            ["docker", "exec", container, "python3", "-c", _TASK_BREAKDOWN_SCRIPT],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return dict(ast.literal_eval(out.stdout.strip()))
    except Exception as e:
        print(f"[monitor] task breakdown read failed: {e}", flush=True)
        return None


def _maybe_add_breakdown(sample: dict, samples: list[dict]) -> None:
    """Attach a per-process task breakdown to the sample every
    BREAKDOWN_EVERY_SAMPLES (skipped when the breakdown read fails)."""
    if len(samples) % BREAKDOWN_EVERY_SAMPLES != 0:
        return
    breakdown = _task_breakdown(CONTAINER_NAME)
    if breakdown is not None:
        sample["tasks_by_comm"] = breakdown


def _health_ms(base_url: str) -> tuple[bool, int | None]:
    """(ok, latency ms) for one /health probe."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=10) as resp:
            return resp.status == 200, int((time.monotonic() - started) * 1000)
    except Exception:
        return False, None


def _set_worker_paused(base_url: str, paused: bool) -> bool:
    """PATCH /api/worker on the test instance (open; no auth header)."""
    data = json.dumps({"paused": paused}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/worker", data=data, method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:
            return json.load(resp).get("ok") is True
    except Exception as e:
        print(f"[gate] worker pause/resume failed: {e}", flush=True)
        return False


def _wait_settled(container: str, samples: list[dict]) -> bool:
    """Sample until memory/PIDs fall below the resume thresholds (or timeout)."""
    deadline = time.monotonic() + SOFT_WAIT_MAX_S
    while time.monotonic() < deadline:
        s = _sample_cgroup(container)
        if s is not None:
            samples.append(s)
        if s is not None and s["mem_bytes"] / 2**30 < RESUME_MEM_GIB and s["pids"] < RESUME_PIDS:
            return True
        time.sleep(SAMPLE_INTERVAL_S)
    return False


def _seed_request(base_url: str, url: str) -> bool:
    """One POST /api/seed; True when the instance accepted the seed."""
    data = json.dumps({"url": url}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/seed", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:
        return json.load(resp).get("ok") is True


def _seed_attempt(base_url: str, url: str) -> tuple[bool | None, Exception | None]:
    """One seed attempt: (ok, None) when it completed, (None, error) when the
    transport failed. ok=False (not None) means the instance rejected the
    seed — no retry."""
    try:
        return _seed_request(base_url, url), None
    except Exception as e:
        return None, e


def _seed_one(base_url: str, url: str) -> bool:
    """One POST /api/seed; one retry on transport failure."""
    for attempt in (1, 2):
        ok, err = _seed_attempt(base_url, url)
        if ok is not None:
            return ok
        if attempt == 2:
            print(f"[seed] failed for {url}: {err}", flush=True)
            return False
    return False


def _seed_worker(
    base_url: str,
    urls: list[str],
    stop_event: threading.Event,
    done_event: threading.Event,
    stats: dict[str, int],
) -> None:
    """Seed every URL at SEED_RATE_PER_S until stopped or finished."""
    for i, url in enumerate(urls):
        if stop_event.is_set():
            break
        if _seed_one(base_url, url):
            stats["ok"] += 1
        else:
            stats["errors"] += 1
        stats["done"] = i + 1
        if (i + 1) % 500 == 0:
            print(f"[seed] {i + 1}/{len(urls)} queued", flush=True)
        time.sleep(1.0 / SEED_RATE_PER_S)
    done_event.set()


if __name__ == "__main__":
    main()

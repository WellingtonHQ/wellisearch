# Memory exhaustion investigation — 2026-10-15

The live container was repeatedly exhausting memory and spending almost all its
time reclaiming pages rather than serving requests. This was browser/process
accumulation amplified by oversized concurrency, unbounded page processing, and
dashboard retries—not simply a large, useful cache.

## Evidence captured before restart

| Measurement | Observation |
|---|---|
| Container memory | 15.97 GiB / 16 GiB. |
| Swap charged to the container | Approximately 8 GiB. |
| CPU | Approximately 2,094%, with no CPU quota. |
| Chromium processes | 371, plus 40 crash handlers. |
| Threads | 7,587 at the host-side snapshot. |
| Out-of-memory kills | 78 recorded kills. |
| Memory pressure | Approximately 99% for both partial and complete stalls. |
| Main Python process | Approximately 4.55 GiB resident, 1.27 GiB swapped; previous resident peak approximately 9.12 GiB. |
| Live configuration | 24 concurrent fast crawls, browser pools of eight each, and up to eight cached profiles per pool. |
| Indexed page sizes | Maximum stored Markdown length 3,780,675 characters, despite the configured 150,000-character extraction cap. |

Two Chrome trees contained 190 processes between them, well beyond the intended
page concurrency. Removing only those two trees freed approximately 6 GiB without
restarting Python or Postgres. Background indexing was temporarily paused and the
container CPU quota set to four cores. After outstanding work settled, `/health`
responded in 38 ms and CPU fell to approximately 1%; this was containment, not the
permanent fix.

## Confirmed defects and fixes

1. Page cleanup had no deadline, and cancellation during cleanup could skip the
   pool release. Cleanup is now owned through repeated cancellation, bounded,
   and always followed by exactly one lease release.
2. Cancelling pool reset or LRU eviction could remove browser ownership before
   closing its processes. Retirement now completes under owned cleanup and
   reaps the exact profile's entire leftover process tree. Failed launches no
   longer suppress future orphan cleanup.
3. Pool size bounded leases but not retained browsers. Both are now bounded by
   pool size; idle page sweeps catch popups and interrupted page creation, and
   use/age limits drain and recycle busy contexts without interrupting active
   leases. Persistent service workers are blocked and Chrome's popup blocker is
   retained.
4. Timed-out synchronous stealth sessions kept running in executor threads after
   their logical crawl slots were returned. Stealth work now runs in separately
   capped, disposable process groups; cancellation kills the browser tree and
   joins the worker before returning capacity.
5. The HTTP path buffered unrestricted responses, generic extraction bypassed
   the Markdown limit, and synchronous extraction could stall the API event
   loop. The payload/extraction fixes bound input and output and move expensive
   processing off the event loop with bounded admission.
6. Fast queue drains selected twice their configured budget, and a task per row
   checked deadlines before waiting for capacity. Fixed-count runners now check
   deadlines when starting work, cancel active work at expiry, and return
   interrupted queue claims without consuming a failure retry.
7. ONNX used fastembed's default batch of up to 256 document chunks. The batch
   is now configurable and defaults to 16; the model and stored vector format
   are unchanged, so no reindex is required.
8. Bulk reads had unlimited URL/task admission, and omitted MCP bulk budgets
   became unlimited. Bulk calls now cap URL count and resolution concurrency;
   omitted MCP budgets use the configured defaults, while explicit null remains
   an opt-in to unlimited output.
9. Dashboard polling allowed overlapping, unbounded request batches while the
   service was stalled. Polls are now single-flight, coalesced, and time-bounded.
   Log projection, filtering, counting, and pagination now run inside Postgres
   instead of materializing the whole day and full search-result bodies in
   Python. Read-only comparisons against 12,880 live entries returned identical
   totals and page contents for ordinary, filtered, and empty result windows.
   Shutdown also joins kick-driven ticks and adopted probes and closes the
   browser pools before shutting down the database.
10. Challenge solving used the full 300-second navigation timeout instead of
    its configured 40-second solve budget, and ordinary missing pages could
    enter challenge solving. The intended budget is now enforced and permanent
    HTTP errors fail without attempting an interactive solve.

The exact historical contribution of each code path cannot be reconstructed
without a pre-incident heap profile. Browser accumulation and memory-pressure
thrashing were directly observed; the lifecycle defects are independently
reproduced by regression tests.

## Deployment safeguards

The local `.env` browser settings were reduced to eight fast crawls, three
fast browser leases/retained contexts, and one challenge browser. Compose now
limits the service to four CPUs, 16 GiB of RAM with no additional swap, and 1,024
processes/threads. These settings preserve host/DB headroom and prevent a future
oversized crawl from monopolizing the Docker VM.

## Verification

All 11 offline Python test suites pass on Python 3.12; the dashboard polling test
also passes. A real-browser soak completed 40 fetches and three cancelled stealth
sessions inside a 2 GiB validation container. Peak memory was 342 MiB, ending at
76 MiB; swap and OOM kills stayed at zero. While that ran, 212 health checks of
the deployed app averaged 23 ms and peaked at 78 ms.

The app container was rebuilt and recreated without restarting Postgres. Its
initial baseline was 118 MiB and 12 processes/threads, with swap disabled by the
equal 16 GiB memory and memory+swap limits. `/health` responded in 23 ms. Local
search, stored-page fetch, paged logs, and MCP initialization/tool listing all
passed; the MCP surface reported six tools and the configured bulk-fetch default.
Background indexing remained paused during these startup and browser-soak tests.

Indexing was subsequently resumed for a bounded production-load observation.
During 180 seconds of active work, the index grew from 12,347 to 12,452 pages.
Memory peaked at 4,279 MiB of 16 GiB, swap and OOM kills stayed at zero, and
process/thread usage peaked at 735 of 1,024. Health checks had a 17 ms median,
60 ms average, and 407 ms peak; the final sample was 2,857 MiB, 454
processes/threads, and 15 ms health latency, with indexing still active.

A later busy crawl tick briefly raised process/thread usage to 891 of 1,024
(55 Chromium processes) and memory to 4,098 MiB. The cgroup recorded no PID-limit
events, swap use, or OOM kills, and health checks stayed between 15 and 26 ms.
As that tick drained, counts fell to 587 processes/threads (41 Chromium processes)
and 3,221 MiB. The peak leaves 133 process/thread slots of headroom, so this is a
specific metric to keep watching as workload changes. The service remains healthy
with indexing enabled. These short observations establish bounded recovery, but
a multi-day memory and process-count plateau still requires continued monitoring.

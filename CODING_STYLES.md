# Code Styles

Rules for keeping this codebase readable and maintainable. Applies to all `.py`
files in `src/wellisearch/`, `tests/`, and `benchmarks/`.

---

## 1. Function Parameters

When a function has more than 2 parameters, or the signature exceeds the line
limit, put each parameter on its own line with a trailing comma.

**Do:**
```python
async def db_log_crawl(
    url: str,
    trigger: str,
    status: str,
    ms: int,
    detail: str | None = None,
    chunks_written: int | None = None,
) -> None: ...
```

**Don't:**
```python
async def db_log_crawl(url, trigger, status, ms, detail=None, chunks_written=None): ...
```

---

## 2. File Ordering

Structure files from highest order to lowest: public APIs and entry points at
the top, internal helpers (`_prefixed`) below. Separate sections with a dash
divider and a short lowercase label.

**Do:**
```python
# ------------------------------------------------------------------ single URL

async def crawl_url(url: str, trigger: str) -> dict: ...


# --------------------------------------------------------------------- ticks

async def _drain_queue(deadline: float) -> dict: ...
```

---

## 3. Alphabetical Sorting

Sort lists of equal elements alphabetically — imports, enum members, list
literals, provider lists, and comment enumerations — unless business logic
dictates a specific order (e.g. `SEARCH_PROVIDERS = "tavily,brave,searxng"` is
priority order, not alphabetical).

**Do:**
```python
from . import crawler, queue
from .config import get_settings
from .db import db
from .index import store_page
```

**Don't:** interleave local and third-party imports or leave them in arbitrary order.

---

## 4. Function Length

A function should not exceed ~100 lines. If it does, break it into smaller
sub-functions with clear names that describe their purpose.

**Do:**
```python
async def tick() -> dict:
    ...
    stats = {
        "queue": await _drain_queue(deadline),    # delegates to sub-function
        "refresh": await _refresh_watchlist(deadline),  # delegates
    }
```

**Don't:** a single 150-line function that drains the queue, refreshes the
watchlist, sweeps retention, and logs events inline.

---

## 5. Indentation Depth

Keep nesting to a maximum of 3 levels (4 including the `def` line). If logic
requires deeper indentation, extract it into a new function or use early
returns with guard clauses.

**Do:**
```python
async def _log_event(message: str, info: dict | None = None) -> None:
    """Best-effort: event logging must never break the worker."""
    try:
        await db.log_event(message, info)          # level 2
    except Exception as e:                          # level 2
        log.warning("event logging failed: %s", e)  # level 3
```

**Don't:** nesting `if` → `for` → `try` → `if` in a row (5 levels deep).

---

## 6. Line Length Cap

Soft cap at **110 characters**; hard cap at **125**. Docstrings, SQL, and URLs
may approach the hard cap; code should stay under the soft cap — wrap strings
across lines, put function arguments on separate lines, or extract to a variable.

**Do:**
```python
log.info(
    "worker started (interval=%sm budget/run=%d parallel=%d)",
    s.WORKER_INTERVAL_MIN, s.WORKER_BUDGET_PER_RUN, s.CRAWL_MAX_PARALLEL,
)
```

---

## 7. Import Ordering

`from __future__ import annotations` first, then three groups separated by
blank lines: standard library, third-party packages, local modules (relative
`from . import ...` in this package). Sort alphabetically within each group.

**Do:**
```python
"""Background worker (asyncio task in the app; plan §8)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from . import crawler, queue
from .config import get_settings
from .db import db
from .index import store_page
```

**Don't:** skip the `__future__` import, intermix groups, or leave them in
arbitrary order.

---

## 8. Type Hints Everywhere

Require type hints on all function and class signatures, public and `_prefixed`
helpers alike. Use modern syntax (`list[str]`, `dict`, `str | None`) — the
project requires Python 3.12+.

**Do:**
```python
async def crawl_url(url: str, trigger: str) -> dict: ...

async def _crawl_and_store(url: str, trigger: str) -> dict: ...  # private — hints required too
```

---

## 9. Docstrings

Every module gets a docstring stating its purpose (reference the BLUEPRINT
section when applicable). Every function gets one too — a one-liner is fine
for trivial helpers; entry points and multi-step functions get a fuller
description.

**Do:**
```python
"""fetch_page (single) + fetch_pages (bulk, budgeted) - the authoritative
source for page content."""

async def tick() -> dict:
    """One worker tick: drain queue + budgeted refresh, wall-clock bounded.
    Skipped (not queued) if a tick is already running."""
```

---

## 10. Magic Numbers → Settings or Named Constants

Tunable behavior belongs in the `Settings` class in `config.py` (UPPER_CASE
fields, env-overridable) — never as bare literals at the call site. One-off
meaningful numbers that aren't tunables become module-level `UPPER_CASE`
constants.

**Do:**
```python
# config.py
    WORKER_INTERVAL_MIN: int = 30
    CRAWL_MAX_PARALLEL: int = 3
```

```python
# worker.py
    sem = asyncio.Semaphore(s.CRAWL_MAX_PARALLEL)
```

**Don't:** scatter `3`, `30`, `45` throughout the code with no context.

---

## 11. Early Returns over Nested Ifs

Use guard clauses to handle edge cases first, keeping the happy path at the
shallowest indentation. Don't wrap the main logic inside an `if`.

**Do:**
```python
async def tick() -> dict:
    if _tick_lock.locked():
        log.info("tick skipped (previous tick still running)")
        return {"skipped": "tick already running"}
    async with _tick_lock:
        ...  # happy path, unindented
```

**Don't:**
```python
async def tick() -> dict:
    if not _tick_lock.locked():                    # wraps the whole function body
        async with _tick_lock:
            ...
```

---

## 12. String Formatting

Use f-strings for building response bodies, SQL-adjacent strings, and user-facing
text. For **logging calls only**, use lazy `%s`/`%d` formatting with arguments —
never f-strings, so the interpolation is skipped when the level is disabled and
no secrets are formatted on the hot path.

**Do:**
```python
log.info("crawl %s: %s (%d ms, %d chunks)", url, status, ms, chunks_written)
out["database"] = f"error: {e}"
```

**Don't:**
```python
log.info(f"crawl {url}: {status}")                 # f-string in a log call
```

---

## 13. Logging

One logger per module, named `wellisearch.<module>`. Log at `info` for
state transitions (tick start/done, crawl results), `warning` for expected
failures (a page failing, a log write failing), `exception` for crashes.
Never log API keys, passwords, or full request bodies.

**Do:**
```python
log = logging.getLogger("wellisearch.worker")
...
log.warning("queue crawl failed for %s: %s", url, e)
```

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
the top, internal helpers (`_prefixed`) below. Separate sections with a
divider (as in the examples) and a short Title Cased label.

**Do:**
```python
# ---------------------------------------------------------------------------
# Single URL
# ---------------------------------------------------------------------------

async def crawl_url(url: str, trigger: str) -> dict: ...

# ---------------------------------------------------------------------------
# Ticks
# ---------------------------------------------------------------------------
async def _drain_queue(deadline: float) -> dict: ...
```

---

## 3. Alphabetical Sorting

Sort collections of equal elements alphabetically: enum members, list
literals, provider lists, and comment enumerations. The one exception is
when business logic dictates a specific order (e.g. `SEARCH_PROVIDERS =
"tavily,brave,exa,youcom"` is priority order, not alphabetical). Import
ordering is covered by Rule 7.

**Do:**
```python
class CrawlStatus(str, Enum):
    failed = "failed"
    ok = "ok"
    unchanged = "unchanged"
```

**Don't:** leave such collections in arbitrary order.

---

## 4. Function Length

A function shouldn't exceed ~100 lines. If it does, break it into smaller
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
may approach the hard cap. Code should stay under the soft cap. Wrap strings
across lines, put function arguments on separate lines, or extract to a variable.

**Do:**
```python
log.info(
    "worker started (interval=%sm budget/run=%d parallel=%d)",
    s.WORKER_INTERVAL_MIN, s.WORKER_BUDGET_PER_RUN, s.CRAWL_MAX_PARALLEL,
)
```

---

## 7. Imports

`from __future__ import annotations` first, then three groups separated by
blank lines: standard library, third-party packages, local modules (relative
imports in this package). Sort alphabetically **within each group**, by the
full import path, so parent-package (`..`) imports come before sibling (`.`)
imports. Business logic may dictate a specific order (e.g. a provider priority
list); that overrides alphabetical.

**Do:**
```python
"""Provider gateway: ordered failover + monthly quota ledger."""
from __future__ import annotations

import datetime as dt
import logging

import httpx

from ..config import get_settings
from ..db import db
from .base import Provider, ProviderError, Result
from .brave import Brave
from .exa import Exa
```

**Don't:** skip the `__future__` import, intermix groups, interleave local and
third-party imports, or leave any group in arbitrary order (e.g. sandwiching
`..config`/`..db` between `.brave` and `.exa`).

---

## 8. Type Hints Everywhere

Require type hints on all function and class signatures, public and `_prefixed`
helpers alike. Use modern syntax (`list[str]`, `dict`, `str | None`). The
project requires Python 3.12+.

**Do:**
```python
async def crawl_url(url: str, trigger: str) -> dict: ...

async def _crawl_and_store(url: str, trigger: str) -> dict: ...  # private: hints required too
```

---

## 9. Docstrings

Every module gets a docstring stating its purpose (reference the BLUEPRINT
section when applicable). Every function gets one too. A one-liner is fine
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
fields, env-overridable), never as bare literals at the call site. One-off
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
text. For **logging calls only**, use lazy `%s`/`%d` formatting with arguments,
never f-strings. The interpolation is then skipped when the level is disabled,
and no secrets are formatted on the hot path.

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

---

## 14. Section Separators

Separate top-level sections with a three-line divider: a line of dashes, a short
Title Cased label, and a closing line of dashes (see Rule 2). This is the one
way to do separators/headings in code. Never use a single-line `# ----...----
label`. Leave exactly **one empty line** between the divider's closing dashes
and the following element (function, class, variable, etc.).

**Do:**
```python
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _helper() -> None: ...
```

**Don't:**
```python
# --------------------------------------------------------------------- main
def main() -> None: ...
```

**Don't:** more than one empty line after the divider.
```python
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _helper() -> None: ...
```

---

## 15. Multi-line Call Parameters

When a call spans multiple lines, break immediately after the opening paren.
No parameter may sit on the same line as `(`. Indent the parameters with a
4-space hanging indent (not aligned to the opening paren) and put the closing
paren on its own line.

The one exception is a literal (dict or list) that opens inline right after
the opening paren (`name([` / `name({`): it is not a parameter for this rule.
It is never dropped below the `(`, and it stays bound per Rule 17, so
its closing bracket shares the call's closing-paren line (`})` / `])`).

**Do:**
```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
)

sections.append(
    "\n".join([
        f"URL: {p['url']}",
        "Status: failed",
    ])
)
```

**Don't:**
```python
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
```

**Don't:** first parameter left on the call line while the rest hang below.
Break right after `(`:
```python
print(f"  {done}/{total} (ok={stats['ok']} unchanged={stats['unchanged']} "
    f"failed={stats['failed']}) eta={eta_min:.0f}m", flush=True
)
```

**Don't:** dropping an inline literal below the `(` (a Rule 17 violation):
```python
subprocess.run(
    [
        "ffmpeg",
        "-y",
    ],
    capture_output=True,
)
```

---

## 16. Spacing Between Elements

When defining elements in a file, don't use more than two consecutive empty lines.

**Do:**
```python
CONSTANT_1=6
CONSTANT_2="Cheese"

def main() 

def run()
```

**Don't:**
```python
def main()


def run()
```

---

## 17. Inline Literals Stay with Their Call

When a literal (dict or list) is the first argument of a call, its opening
bracket sits inline on the call line (`name({` / `name([`) and the pair stays
bound at both ends: the closing bracket and the call's closing paren share
one line (`})` / `])` at the enclosing indent), never two separate lines.
Dropping the opening bracket onto its own line under a bare `(` is a
violation (it takes precedence over Rule 15's "closing paren on its own
line" for exactly this shape). A literal that is not the first argument
cannot open inline: its bracket starts its own parameter line.

When a bound literal spans multiple lines, every element gets its own line
with a trailing comma — never two elements on one line.

An empty literal (`[]` / `{}`) is atomic: it may sit on its own parameter
line in a multi-line call.

**Do:**
```python
thing.addObject({
    "field1": "value1",
    "field2": "value2",
})

lines = "\n".join([
    f"title: {title}",
    body,
])

subprocess.run([
    "ffmpeg",
    "-y",
    str(staged_path),
],
capture_output=True,
)
```

**Don't:**
```python
thing.addObject(
    {
        "field1": "value1",
        "field2": "value2",
    }
)

lines = "\n".join(
    [
        f"title: {title}",
        body,
    ]
)

subprocess.run([
    "ffmpeg", "-y",
    "-f", "concat",
    str(staged_path),
],
capture_output=True,
)
```

---

## 18. No Nested or Over-Packed Expressions

One expression does one thing. If you have to read a line twice, split it
into separate statements with named intermediates, one operation per line. A
nested expression is a violation even when the line is short. A long line
that does one simple thing is not (that is Rule 6's territory).

Check every comprehension and generator line against the two classes below.
When in doubt, split it. A named intermediate is never wrong.

**A. Nested comprehension or generator. Always a violation.** A comprehension
or generator is nested when any of these is true:
- its iterable, element expression, or filter is itself a comprehension or
  generator. The common `next(...)` idiom counts:
  `next((t for t in (f(x) for x in xs) if t is not None), None)`. So does a
  list comprehension written over a generator:
  `[line for line in (f(k) for k in fields) if line is not None]`;
- it has more than one `for` clause, flattening a nested structure:
  `[z for x in A for y in B if pred]`;
- it star-unpacks a generator in its element expression:
  `[max(a, *(g(x) for x in xs)) for ...]`.

**B. Over-packed single expression. A violation.** One expression doing more
than one thing on a line:
- a comprehension combined with another operation on the same line (a slice,
  `sorted(...)`, a `+`): `[x for x in xs if pred][: n]`;
- an `and`/`or` side that chains several distinct transforms: a nested
  `.get`, a conversion, a casefold, a comparison. Example:
  `isinstance(item, dict) and str(item.get("@type", "")).lower() == "jobposting"`.
  An `and`/`or` whose sides are each one clear operation is fine. The
  `isinstance(x, str) and x.strip()` idiom is not a violation.

**Break them up, one operation per line:**
```python
# next((t for t in (f(x) for x in xs) if pred), None)  ->  loop with early return.
for x in xs:
    t = f(x)
    if t is not None:
        return t
return None

# Map, then filter: two readable steps.
addresses = [_location_address(loc) for loc in locs]
addresses = [a for a in addresses if a is not None]

# Comprehension + slice: name the filtered list, then slice on its own line.
fresh = [r for r in similar if r.get("url") not in seen]
extra = fresh[: s.SEARCH_TOP_BY_SIM]

# Per-column widths: compute the inner values in a named step first.
widths = []
for i in range(len(cols)):
    col_widths = [len(r[i]) for r in data]
    widths.append(max(len(cols[i]), *col_widths))

# Two-for flattening: plain nested loop, same order.
urls = []
for results in phase_results.values():
    for r in results:
        if r.get("ok"):
            urls.append(r["url"])

# Guard clause + named intermediate for a multi-step condition.
if not isinstance(item, dict):
    continue
job_type = str(item.get("@type", "")).lower()
if job_type == "jobposting":
    return item
```

**Not violations. Don't reformat these:**
- a single-level comprehension or generator with one `for` clause, with or
  without an `if`: `[k for k in keys if k in d]`,
  `next((p for p in rows if p.get("url") == url), None)`;
- a `for` statement (not a comprehension) iterating a single-level generator:
  `for task in (t for t in tasks if not t.done()):`;
- regular nested `for` loops (statements, not expressions);
- a short chain that reads as one operation: `open(f).read().split()`.
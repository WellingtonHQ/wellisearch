# Adaptive per-URL refresh and freshness-aware ranking (design)

Status: **proposed; not implemented**. This design extends the existing
watchlist refresh in `worker.py` and the local ranking function in
`schema.sql`. It does not add another crawler, worker, or queue.

## Goals

- Re-crawl changing URLs more often and stable URLs less often, with a
  configurable 14-day maximum **target interval** between checks; failures
  and worker backlog can delay an actual successful verification.
- Treat an unchanged but successfully re-crawled page as **recently verified**.
  The age of the content is not the age of our evidence that it is still current.
- Give all pages a 30-day ranking grace period after their last successful
  verification; strongly demote pages that have not been verified for a year.
- Let manual refreshes bypass the schedule while retaining the existing
  failure retries, worker budget, and explicit `search_web(max_age_days=...)`
  freshness filter.

## Existing behavior and constraints

`worker._refresh_watchlist()` currently considers every enabled page once
`last_crawled` is at least `REFRESH_MIN_AGE_HOURS` old (72 hours by default).
It selects by `fetch_count DESC, last_crawled ASC`, subject to a per-tick limit,
pending queue work, and `refresh_backoff_until` after failures. A successful
`store_page()` compares a SHA-256 hash of the extracted markdown; unchanged
pages still advance `last_crawled` without re-embedding.

Today `fn_search_local` multiplies a page's relevance-plus-prominence score by
`exp(-age_days / 14)`, where age comes from `last_crawled`. At 30 days this
leaves only about 12% of the score, even though a page checked 30 days ago
could still be accurate. This is a ranking adjustment, not the optional
`max_age_days` hard filter in `search_web.py`.

There are two traps in the current storage path:

- `store_page()` returns `ok` when the embedding model changes even if the
  markdown hash is identical. `ok` is therefore not a content-change signal.
- `reindex.py` calls `store_page()` on stored markdown without visiting the
  URL; this currently updates `last_crawled`. Re-embedding must not count as
  a verification or change observation. Preserve the last real crawl time
  during offline reindexing before relying on it for ranking or scheduling.

## Per-URL state

Add these columns to `pages` via the idempotent `schema.sql` migration:

| Column | Meaning |
|---|---|
| `refresh_interval_hours` | Current target interval; initially 72 hours. |
| `next_refresh_at` | Earliest time the watchlist may check the URL again. |
| `unchanged_streak` | Consecutive successful, real recrawls with the same markdown hash. |
| `last_content_change_at` | Time a real crawl last detected a different hash; NULL until the first observed change. |
| `fast_probe_until` | End of a temporary 6-hour sampling period; NULL otherwise. |

Keep `last_crawled` as the **last successful network verification**, whether
or not content changed. Keep `refresh_fail_streak` and
`refresh_backoff_until` for **unsuccessful** attempts. Do not infer change
history from `crawl_log`: its 90-day retention is for diagnostics, not durable
scheduling state. The timestamp of the previous detected change is sufficient
to decide whether two observations were close together; compare it before
updating `last_content_change_at`.

On migration, initialize interval to the 72-hour base and backfill
`next_refresh_at` from the best available real-crawl timestamp. Existing
`last_crawled` values may include offline reindexes; use the latest successful
network `crawl_log` entry where available, otherwise use `last_crawled` as a
best-effort fallback. Start streaks at zero rather than guessing from logs.
Apply the normal worker budget to initially overdue rows rather than enqueueing
the entire index at once.

## Scheduling policy

All durations and thresholds below live in `config.py` and are
environment-overridable. Default policy:

| Observation after a successful real crawl | Next target interval |
|---|---:|
| First crawl, or a stable page without enough evidence yet | 72 hours |
| Seven consecutive unchanged recrawls **and** at least seven days since the last detected change (or first crawl) | 7 days |
| Another unchanged recrawl after the weekly check | 14 days (maximum) |
| A change detected at 72 hours, 7 days, or 14 days | 24 hours |
| Two changed daily checks within 48 hours | Try 6-hour checks for 48 hours. |
| During that trial, two changes detected at most 12 hours apart | Keep 6-hour checks, extending the trial while that evidence continues. |
| Trial expires without that evidence | Return to 24 hours. |
| No change for three days at the 24-hour cadence | Return to 72 hours, then follow the stable-page rule. |

At the 6-hour cadence, seven unchanged observations cover less than two days:
do **not** jump directly to weekly checks. Let the fast trial expire, then
step down through 24 and 72 hours; require both the unchanged streak and
elapsed-time evidence for the weekly tier. A single change after a 14-day
interval likewise does not prove six-hour volatility: first sample daily.

These are per-**URL** observations, not per-domain guesses. A news homepage
may change daily while an individual article on the same site never changes.
The first crawl establishes a baseline hash, not a detected change. Successes
from manual refresh, `fetch`, queue work, and the one-shot `recrawl.py` all
count as real observations; offline `reindex.py` does not. A fixed per-URL
interval override, already contemplated in `features-backlog.md`, can be
added later while still collecting change observations underneath it.

Compare the previous and new `content_hash` independently of the embedding
model and `store_page()` status. Only valid extracted page content should
teach the policy: a transient login screen, error page, or crawler artifact
must not masquerade as a content change. Start with the current fit-markdown
hash; if dynamic counters or extractor formatting cause noisy changes, define
an explicit, versioned *change-detection* fingerprint without changing the
content stored or embedded.

Compute and persist the new interval, `next_refresh_at`, and streak/change
state in the same transaction as a successful page update. The current
unchanged fast path uses a separate UPDATE and must be brought into that
transaction. Serialize updates for a URL so two crawls cannot both derive a
schedule from the same old hash. Failures do not advance `last_crawled` or
`next_refresh_at`; the existing refresh-failure backoff determines when the
overdue URL becomes eligible again. Manual refresh bypasses the due time, but
its success should recalculate the next due time.

### Worker selection and capacity

Replace the global age cutoff in `_refresh_watchlist()` with a due-time query:

```sql
SELECT p.url
FROM pages p
WHERE p.disabled = false
  AND p.next_refresh_at <= now()
  AND (p.refresh_backoff_until IS NULL
       OR p.refresh_backoff_until <= now())
  AND NOT EXISTS (
    SELECT 1 FROM crawl_queue q
    WHERE q.url = p.url AND q.status IN ('pending', 'in_flight')
  )
ORDER BY p.next_refresh_at ASC, p.fetch_count DESC
LIMIT %s;
```

Add a partial `pages(next_refresh_at)` index for enabled pages. Keep the
queue-first tick, in-flight deduplication, pause behavior, CF lane, and
per-tick deadline. Due time is a target, not a promise under backlog; oldest
due first prevents popular fast-changing URLs from starving old checks. Track
due count and due lag (for example, p95 hours overdue) so the six-hour tier
cannot silently exceed crawl capacity. A 6-hour URL requires about four
successful checks per day; the current 30-minute worker tick and its budget
set the practical upper bound.

The old `REFRESH_MIN_AGE_HOURS` name implies a hard floor incompatible with
6-hour checks. Introduce a base-interval setting with a migration path for
existing env values. Keep failure-backoff settings separate, including its
current cap of one 72-hour base cycle; the failure policy must not inherit a
14-day success interval by accident. Define the active, fast, weekly, and
maximum intervals plus their evidence windows in `config.py`, not as SQL or
worker literals.

Suggested settings (hours unless noted):

| Setting | Default | Purpose |
|---|---:|---|
| `REFRESH_BASE_HOURS` | 72 | Initial and ordinary interval; migrate existing `REFRESH_MIN_AGE_HOURS` values. |
| `REFRESH_ACTIVE_HOURS` / `REFRESH_FAST_HOURS` | 24 / 6 | Daily cadence and short exploratory cadence. |
| `REFRESH_WEEKLY_HOURS` / `REFRESH_MAX_HOURS` | 168 / 336 | Stable tiers and maximum target interval. |
| `REFRESH_STABLE_STREAK` / `REFRESH_STABLE_WINDOW_DAYS` | 7 / 7 | Both conditions required before weekly checks. |
| `REFRESH_FAST_PROBE_WINDOW_HOURS` / `REFRESH_RAPID_CHANGE_WINDOW_HOURS` | 48 / 12 | Exploratory sampling and evidence to continue it. |
| `REFRESH_ACTIVE_COOLDOWN_HOURS` | 72 | No-change period before dropping from daily to base. |
| `REFRESH_FAILURE_BACKOFF_MAX_HOURS` | 72 | Independent cap for the existing failure-backoff formula. |

## Ranking: verification age, not content age

Replace the current immediate 14-day exponential penalty in
`fn_search_local` with a **30-day grace period** and a **90-day half-life
after the grace period**. Both are configurable. For a page successfully
checked at `last_crawled`:

```text
age_days      = max(0, now - last_crawled) in days
overdue_days  = max(0, age_days - grace_days)
freshness     = 2 ^ (-overdue_days / half_life_days)
final_score   = (RRF_page_score + prominence_bonus) * freshness

defaults: grace_days = 30, half_life_days = 90
```

| Time since the last successful real crawl | Freshness multiplier |
|---|---:|
| 14 days or 30 days | 1.00 |
| 60 days | ~0.79 |
| 120 days | 0.50 |
| 365 days | ~0.076 |

An unchanged recrawl advances `last_crawled` and resets the multiplier to
1.00: the page was just verified, even if its text has been identical for a
year. A page not checked for a year is heavily penalized, **even if its last
known content was stable**. The 14-day maximum is a recrawl target, not a
ranking cap: persistent failures, a paused worker, or backlog can still leave
a URL unverified for months. Do not treat a NULL verification time as fresh.

Keep topical relevance and the existing local-hit gate unchanged; the
freshness multiplier only affects ordering. Keep an explicit caller's
`max_age_days` filter strict: a request for pages checked within seven days
must not be relaxed by the 30-day ranking grace period. Likewise, stored
`fetch_page` content is not automatically recrawled just because this ranking
grace exists.

Place `RANK_FRESHNESS_GRACE_DAYS` and `RANK_FRESHNESS_HALF_LIFE_DAYS` in
`config.py`, and pass them from `search_web.py` into `fn_search_local` as
arguments instead of hard-coding them inside `schema.sql`. Changing its
signature requires dropping the old three-argument function at startup and
updating direct SQL callers/tests as well as the production call. The
existing ranking documentation and its worked example must be updated when
the behavior is implemented.

## Verification and rollout

1. Add the schema migration, schedule calculation, and tests for first
   crawl, seven unchanged recrawls, six-hour sampling, the 14-day cap,
   a change after a long quiet period, a failure, and a manual success.
2. Ensure offline reindexing never advances verification time or the schedule;
   test same-hash/model-change re-embedding separately from a real crawl.
3. Test the watchlist's due-time ordering, queue exclusion, failure backoff,
   worker pause, and restart persistence. Expose interval, next due time,
   changed/unchanged observations, and overdue counts in the pages/dashboard
   views so the policy is inspectable.
4. Change the SQL ranker and run the database and search regression tests.
   Verify the 30-, 120-, and 365-day multipliers and confirm that a successful
   unchanged recrawl removes an old-age penalty. Compare local result order
   and provider fallbacks before and after rollout: a large number of newly
   unpenalized old pages may move into the top-k.

The worker continues to crawl asynchronously. Neither search nor ranking
waits for a scheduled refresh.

# Problematic crawls

Working list of crawl problems found in the index. Fixed items are kept as
short notes with their verification; open items stay at the bottom.

## Resolved

### Crawl failure flood — worker refresh retry loop (reported 2026-09-13, fixed 2026-09-17/18)

~2,150 failures in 12h because dead/unindexable watchlist pages were re-crawled
every tick with no backoff or attempt cap. Fixed:

- **Refresh-path backoff** — `pages.refresh_fail_streak` / `refresh_backoff_until`:
  consecutive refresh failures push the page's next refresh out on an exponential
  schedule (6h → 12h → 24h …, capped ~1 week); a success resets it. Only the
  refresh path bumps backoff — search/manual/recrawl failures don't.
- **Per-tier failure detail** in `crawl_log.detail` (`crawler.failure_detail`) —
  e.g. `http: botwall: cf-turnstile (http 403); browser: ...`, so root cause is
  one query instead of a re-diagnosis.
- **CF-lane routing** — watchlist refreshes that hit a bot-wall are routed to the
  challenge lane instead of failing in the fast lane.

Verified after deploy (2026-09-18): refresh-trigger errors dropped from ~2,156/12h
to **4 in 18h**. The overture3d wiki pair and `search.brave.com/` now refresh as
`unchanged`; the open-webui key-features page and the opencve CVE page still fail
(site-side TLS / 502) but sit in backoff instead of hammering.

### gpupoet "Loading..." stub (fixed 2026-09-18)

`https://gpupoet.com/gpu/shop/nvidia-geforce-rtx-3090-ti` stored a 411–743-char
client-rendered shell ending in `Loading...` — the http tier captured the page
before JS rendered the listings, and the generic gate (≥100 chars) accepted it.

Fixed: `GenericExtractor.fit` now raises `Escalate("browser")` when the extracted
markdown is short (`LOADING_STUB_MAX_CHARS=1500`) **and** the raw HTML (scripts/
styles stripped) still shows a `Loading...` placeholder — so the browser tier,
which waits for settle/network-idle, renders the real content. Long pages that
merely mention "loading" are unaffected.

Verified live: http tier escalates → browser tier stores 2,383 chars with actual
listings (e.g. Zotac 3090 Ti $1,700 used), no `Loading...` in the output.

### Bot-wall false positive on non-HTML content (fixed 2026-09-18)

`is_botwall()` text-scanned every response body for challenge markers, so
non-HTML pages containing marker-like strings were misclassified as walls —
confirmed live on `raw.githubusercontent.com/onyx-dot-app/.../connector.py` (a
crawler library whose source contains "javascript is disabled"), which failed
both tiers repeatedly while holding real content.

Fixed: the marker scan now only runs on HTML responses (`text/html`,
`application/xhtml+xml`). Tiers record the response `Content-Type` on
`Rendered`; an absent/unknown type still gets scanned (safe default), and
`status >= 400` always wins regardless of content type.

Verified live: the raw file now crawls `ok`/`unchanged` with its full source
stored; unit tests cover text/plain, JSON, charset suffixes, and the status
override.

### Amazon extractor gaps (fixed 2026-09-18)

The CORSAIR case example was missing seller and review info. `AmazonExtractor`
now also captures:

- **Rating** — `#acrPopover` title attr (fallback `.a-icon-alt`) + the
  `#acrCustomerReviewText` count, rendered as one line
  (`**Rating:** 4.6 out of 5 stars 12,345 ratings`).
- **Sold by** — buy-box `#sellerProfileTriggerId`.

Product details (tech-spec table) were already extracted when present on the
page; individual review texts are still not captured (count only).

### Reddit post extraction (fixed 2026-09-27)

Reddit threads were bot-walled on both tiers for anonymous crawls, so indexed
posts held only a login-wall stub. Fixed by the reddit extractor PR: a
browser-first policy with the persistent profile fetches `?sort=top/best`, and
`RedditExtractor` stores the post body plus highest-ranked comments under a
`## Reddit comments (...)` heading that also records ranking mode + limit.
NSFW-gated posts (wall text "this post contains mature content") are exempt so
fetches never loop on them.

Verified live (2026-09-27): recrawled all 232 indexed post URLs — 229 stored
with ranked comments, 3 NSFW-gated and marker-exempt, **0** left in a state
where `fetch_page` would re-trigger a browser crawl.

### Fetch-path reddit re-crawl backoff (fixed 2026-09-27)

Latent since the reddit extractor PR: when an indexed post's stored markdown
lacks the ranking heading, `fetch_page` runs a full browser re-crawl inline; if
that crawl fails nothing is stored and the next fetch repeats it — unbounded.
The refresh backoff (`refresh_fail_streak` / `refresh_backoff_until`) only
applied to the worker refresh path; fetch-triggered failures never bumped it
(see `fetch.py:_resolve_page`, `worker.py:167`).

Fixed in `fetch.py`: while a page's refresh backoff is active, `_resolve_page`
serves the stored markdown instead of re-crawling, and a failed
refresh-triggered re-crawl (plain tier failure or bot-wall → CF routing) now
bumps the streak via `db.refresh_fail_bump`, mirroring the worker's refresh
handling. First-time indexing failures are not bumped; successes reset it in
`worker._crawl_and_store`. Regression tests: `tests/test_fetch_refresh_backoff.py`.

## Open

### LinkedIn job descriptions

Need full job descriptions from public LinkedIn postings (no authentication).
Not implemented — no extractor, and LinkedIn aggressively bot-walls anonymous
browser sessions.

### microsoft.eightfold.ai job page

`https://microsoft.eightfold.ai/careers/job?domain=microsoft.com&profile_type=candidate&pid=1970393556941428&location=United+States&filter_include_remote=1`
— client-rendered; needs a dedicated extractor or longer browser wait.

### Genuine bot-walled pages (bounded, no action)

These came back `challenge detected` on the index-wide recrawl and are real
walls, not false positives. They now sit in refresh backoff instead of failing
every tick:

- `https://s2f.kytta.dev/?text=https%3A%2F%2Fdev` (last error 09-10)
- `https://www.spectrumbusiness.net/` — cookie/JS wall, browser tier also failed (09-08)
- `https://xdaforums.com/m/wellingtonhq.9307974/about` — challenge on both tiers (09-08)

### Queue-path (`search`) crawl errors

Separate from the refresh flood: search-triggered queue crawls still fail at a
meaningful rate (~345 in 18h as of 2026-09-18), averaging long runtimes — the
timeout pain noted in the original flood report. Needs its own diagnosis
(per-tier detail is now captured, so `crawl_log` should make this tractable).

# Problematic crawls

Working list of crawl problems found in the index. Fixed items are kept as
short notes with their verification; open items stay at the bottom.

## Open

### Genuine bot-walled pages (bounded, no action)

These came back `challenge detected` on the index-wide recrawl and are real
walls, not false positives. They now sit in refresh backoff instead of failing
every tick:

- `https://s2f.kytta.dev/?text=https%3A%2F%2Fdev` (last error 09-10)
- `https://www.spectrumbusiness.net/` — cookie/JS wall, browser tier also failed (09-08)
- `https://xdaforums.com/m/wellingtonhq.9307974/about` — challenge on both tiers (09-08)

Re-checked 2026-09-28: all three now refresh as `unchanged` at sub-second
runtimes with `refresh_fail_streak = 0` — the wall content is stable, so it
hashes unchanged and costs nothing per tick. Harmless operationally; the only
caveat is that `fetch_page` on these URLs still returns the wall stub (250 /
237 / 1,403 chars) until a tier ever gets through.

### Queue-path (`search`) crawl errors — CF-lane timeout burn (diagnosed 2026-09-28, fix deferred)

**Status: deferred (decided 2026-09-28).** The `is_botwall`/CF-routing change is
held until the browseros neo layer lands. Direction agreed in the meantime: a
404 is intended — the page does not exist and should be marked as such, not
crawled; a 403 may be login-gated content (legitimately unviewable anonymously),
so it shouldn't be treated as a solvable challenge either. The diagnosis below
is kept for when this is picked up.

Search-triggered queue crawls of hard-error pages (403/404/"request blocked")
each burn ~303–305s. As of 2026-09-28: 37 errors in 18h across only 14 distinct
URLs, heavily repeated (bakersplus ×6, several ×3) — a small set of stubborn
pages re-enqueued by recurring searches, not a broad failure rate.

Root cause (traced through `crawl/`): `is_botwall()` returns `"http_<status>"`
for **any** status ≥ 400 (`botwall.py:76-77`). The fast-lane browser tier raises
`ChallengeDetected` for *any* non-None result — including a bare 403/404 with no
challenge content (`tiers/browser.py:107`) — so the worker routes the row to the
CF lane and **resets `attempts = 0`** (`db.queue_route_to_cf`, `db.py:569-584`).
In the CF lane, `_resolve_challenge`'s turnstile loop exits only when
`is_botwall(...) is None` (`tiers/browser.py:150-158`) — which can never hold for
status ≥ 400 (the captured status is immutable inside the loop), so it clicks a
nonexistent widget until the full `CRAWL_CF_TIMEOUT_S = 300` budget expires. The
loop's click result is discarded (`browser.py:156`), and `CRAWL_CHALLENGE_BUDGET_S`
(40s) is dead on this path because the CF call site always passes 300.

Aggravators: no backoff exists on the queue path (refresh backoff only fires for
`trigger == "refresh"`, `worker.py:164-168`); each fast→CF routing grants a fresh
3-attempt budget; and once a row is `failed`, the next search enqueues a brand-new
row (`queue_enqueue` dedupes only pending/in-flight rows) — restarting the full
~15-min cycle. With `CRAWL_CF_POOL_SIZE = 1`, each burn also serializes genuine
challenge work behind it.

Recommended fix: treat "hard HTTP error with no challenge content" as
not-a-challenge at both decision points — (1) raise `ChallengeDetected` in the
fast lane only when a real phrase/structural marker is present in the body, so a
plain 403/404 fails fast (~5s) through normal retry handling; (2) skip or
short-circuit the turnstile loop when no marker is detected (and/or break after N
consecutive "no widget found" clicks). Keep `is_botwall`'s status ≥ 400 behavior
for tier escalation in the engine — that part is cheap and correct. Genuine CF
managed challenges virtually always render marker text, so regression risk is low;
an empty-bodied 403 burns 300s today and fails anyway, so failing fast is strictly
better. Follow-up hardening: queue-path backoff (mirror `refresh_fail_bump` onto
`crawl_queue` failures) and stop resetting `attempts = 0` in `queue_route_to_cf`.
Tests: `tests/test_lanes.py` (404 must not raise `ChallengeDetected`; CF loop with
no marker returns without burning budget; guard case where a real challenge is
served with 403 + "just a moment" still loops) and `tests/test_crawl_core.py` if a
marker-only helper lands in `botwall.py`.

### GitHub PR pages false-positive as loading stubs (found 2026-09-28)

`github.com/<org>/<repo>/pull/*` pages fail both tiers with
`http: escalate: browser; browser: escalate: browser` — 81 errors in 7 days,
mostly open-webui/ArchiveBox PRs. The server HTML holds the real content
(~400k chars) but trafilatura extracts only ~1,395 chars from GitHub's complex
DOM (under `LOADING_STUB_MAX_CHARS = 1500`), and the markup contains a lazy-load
placeholder `<include-fragment aria-label="Loading...">` that trips the gpupoet
stub guard (`_is_loading_stub`) — so both tiers raise `Escalate("browser")` and
the crawl errors out on a page that was fine all along.

Fix direction: exclude attribute values (or at least `aria-label`) from the stub
marker scan, or special-case `<include-fragment>`; alternatively a GitHub PR
extractor that targets the rendered conversation/description containers.

### Intermittent reddit walls on new posts (found 2026-09-28)

New post URLs arriving via search intermittently hit `prove your humanity` +
`net::ERR_HTTP_RESPONSE_CODE_REPEATED` in the browser tier, after which the
stealth tier burns its full 120s budget — ~2 min per failed attempt. The wall is
transient: the same URL re-crawls successfully minutes later in ~4s (verified on
several URLs). Indexed posts are unaffected (recrawl verified 2026-09-27); this
only hits fresh search-triggered crawls — 23 such errors on 09-27 alone.

Fix direction: the queue-path backoff from the CF-lane item would bound the
re-burn; separately, consider dropping `stealth` from the reddit policy when the
browser tier already hit a wall (it adds nothing there, only 120s of latency).

### Social login-wall URLs re-enqueued by search (found 2026-09-28)

Provider search results include facebook.com posts/videos and instagram reels;
each enqueue fails fast (~5–6s: `gate failed [0 chars]` or double escalation) but
nothing remembers the failure, so recurring searches re-enqueue the same URLs —
~370 errors in 7 days (same starkhealthdept1920 post crawled 3+ times). These can
never succeed anonymously. Related fast-fail noise: youtube.com `http_429`
rate-limiting (~38 in 7d) and tiktok short links returning empty bodies.

Fix direction: same queue-path backoff as the CF-lane item (one mechanism covers
all three), or a domain-level skip/short-circuit for known login-wall domains at
search-enqueue time.

### rokthejvm.com Title is "RSS"
Title for [https://rockthejvm.com/articles/structured-concurrency-jdk-25] is "RSS". Something is off about how the title is being crawled.

### Amazon product description missing from stored markdown (found 2026-09-30)

[https://www.amazon.com/Playtex-Baby-Anti-Colic-Pre-Sterilized-Breastfeeding/dp/B0CGKY5JM2]
stores 1,371 chars — title, price + stock, rating, seller, "About this item" bullets —
but the page's **Product description** section is absent. The live DOM has a
`#productDescription` block (~966 chars of real product copy), and it is
server-rendered: a plain impersonated GET returns it in the initial HTML (2.1 MB),
so every tier captures it — `AmazonExtractor.fit` simply never reads that field
(`extractors/amazon.py` has no description anchor). The "Product details" table is
correctly absent here: this page genuinely carries none of the four known detail-
section IDs and no matching tables. A+ content (`#aplus`) holds only nav links
("Visit the Store"), nothing worth capturing.

Fix direction: add a `description` field to `AmazonExtractor`, anchored on
`#productDescription` (fallback `#product-description`), rendered as a
`## Product description` section between bullets and details; then recrawl indexed
amazon pages so stored markdown picks it up.

---

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

### LinkedIn job descriptions (resolved 2026-09-28 — no longer reproduces)

Public `/jobs/view/...` postings now crawl fine through the generic path with
no extractor and no authentication. Verified live (2026-09-28): 352 of 353
indexed linkedin pages hold real content, including full job descriptions
(e.g. McAfee "Principal Engineer, Flutter", ~7k chars); refreshes return `ok`
at sub-2s runtimes (fast lane). The only stubs are non-job URLs (search
results, messaging links) that legitimately hit login walls.

### microsoft.eightfold.ai job page (fixed 2026-09-28)

The Microsoft careers posting stored a **477k-char raw JSON config blob** — the
server HTML's body opens with a hidden `<code id="branding-data">` bootstrap
payload that the generic extractor kept as page content; the real description is
fetched client-side (`/api/pcsx/position_details`, needs session cookie +
Referer).

Fixed: browser-first policy entry for `eightfold.ai` in `crawl/policy.py`
(`("browser",), ("settle", "network_idle")`, like `search.brave.com`). After JS
runs the config blob shrinks to ~2k chars and the rendered DOM holds the full
posting, so no dedicated extractor was needed.

Verified live (2026-09-28): manual recrawl after redeploy stored 7,844 chars of
clean markdown — job number, work site, travel, overview, full description
("Principal Software Engineer", Azure Data engineering). All three indexed
eightfold pages were then recrawled under the new policy: the two marketing
pages (`eightfold.ai`, `/responsible-ai/`) came back `unchanged` (5.1s / 2.7s) —
their stored content already matches what the browser tier produces, so no stale
JSON blobs remain in the index. Note: the job page carries
`<meta name="robots" content="noindex">` — irrelevant for a private index.

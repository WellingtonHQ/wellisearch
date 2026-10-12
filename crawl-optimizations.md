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

### Amazon "no cards" review variant (found 2026-09-30)

Amazon A/B-serves the reviews block two ways: most loads render the full
review cards server-side (`div[data-hook="review"]` under
`ul#localTopReviewsList`), but ~1/3 of loads — in both the http tier and a
real browser — serve `#customerReviews` as a ~107KB shell with zero cards.
Pages stored during a bad load carry `## Amazon reviews (up to N)` +
"No reviews available." instead of review entries; `needs_refresh` sees the
heading and treats the stored copy as current, so nothing re-crawls until the
normal watchlist refresh lands a good load. Harmless but means a fraction of
Amazon pages temporarily lack review text.

Fix direction if it ever bites: v2 could fall back to the page's own AJAX
endpoint `/hz/reviews-render/ajax/medley-reviews/get/` (token lives in the
`#cr-state-object` data-state) when the shell has no cards — a browser tier
was measured and does **not** reliably help (~50% of headful loads also miss
the cards).

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

### Amazon product description missing from stored markdown (found 2026-09-30, fixed 2026-09-30)

Amazon product pages stored title/price/rating/seller/bullets but never the
**Product description** section: `#productDescription` is server-rendered in the
initial HTML (every tier captures it), yet `AmazonExtractor.fit` had no anchor
for it. The "Product details" table was correctly absent on the example page —
it genuinely carries none of the known detail-section IDs.

Fixed: `_description()` helper in `extractors/amazon.py` anchored on
`#productDescription` (fallback `#product-description`), rendered as a
`## Product description` section between bullets and details, with a
`description` signal. Tests extended in `tests/test_extractors.py` (content,
section ordering, absent-description case, fallback id).

Verified live (2026-09-30): rebuilt + redeployed the image, then recrawled all
103 indexed Amazon pages (`recrawl --domain amazon.com`, new suffix-match flag) —
88 updated, 15 unchanged (no description on those pages), 0 failed. The Playtex
B0CGKY5JM2 page now stores 2,363 chars including the full product copy.

### Amazon review texts missing from stored markdown (found 2026-09-30, fixed 2026-09-30)

Amazon product pages stored the rating line (count only) but never the
individual review texts. The full text is server-rendered in the initial HTML
(measured up to ~6,700 chars per review), so no browser work was needed.

Fixed in `extractors/amazon.py`: up to `CRAWL_AMAZON_MAX_REVIEWS` (default 5,
new config knob — later generalized to the universal `CRAWL_MAX_REVIEWS`)
top reviews per product under a `## Amazon reviews (up to N)`
heading — author, star rating, verified-purchase badge, title, date, and the
full body of each `div[data-hook="review"]` card, plus the reported rating
count as a `reviews_reported` signal. Product pages with no cards store a
"No reviews available." placeholder so `needs_refresh` never loops; the
heading encodes the limit, so raising the knob self-refreshes affected pages
on the next pass (same pattern as the reddit comments heading). `fetch.py`
OR's the new `amazon.needs_refresh` into the fetch-path refresh check.

Verified live (2026-09-30): rebuilt + redeployed the image, then recrawled all
103 indexed Amazon pages — 103 updated, 0 failed (~0.6 min). The Playtex
B0CGKY5JM2 page stores 5 reviews with full bodies; the Apple MacBook Neo
B0GR6F79MT page stores multi-thousand-character reviews. See the open
"no cards" variant item for the known caveat.

### Home Depot product pages stored as thin nav stubs (found 2026-09-30, fixed 2026-10-01)

Home Depot product pages (e.g. the AQUA TRU Carafe AT100,
`/p/…/325993266`) stored only ~179 chars of header/nav text and 1 chunk:
trafilatura extracts a thin slice from the ~780 KB server HTML and the
generic gate accepted the stub. The server HTML carries the full **Product
JSON-LD** — name, `offers.price`, `aggregateRating`, `description`,
model/sku/gtin/dimensions, and a `review` array of up to 10 full review
bodies — so no browser tier was needed.

Fixed: new `HomeDepotExtractor` (`crawl/extractors/homedepot.py`) anchored on
the Product JSON-LD node, rendering title, `**Price:**`, `**Rating:**`,
`**Brand:**`, `## Product description`, `## Product details` (labeled spec
fields), and the top `CRAWL_MAX_REVIEWS` reviews under a
`## Home Depot reviews (up to N)` heading (or a "No reviews available."
placeholder). The gate requires title + price, so Akamai challenge pages and
degraded renders are rejected instead of stored. New `homedepot.com` policy
(`http` first, like amazon); non-product URLs (category `/b/`,
`/p/reviews/`) fall back to the generic extractor in `engine.py`, so those
pages keep working. `CRAWL_AMAZON_MAX_REVIEWS` was generalized to the
universal product-crawler knob `CRAWL_MAX_REVIEWS` (default 5), now used by
both amazon and homedepot extractors. `fetch.py` OR's `homedepot` into the
refresh chain so the stale 179-char stubs self-refresh.

Verified live (2026-10-01): rebuilt + redeployed the image, then recrawled
all 38 indexed homedepot.com pages (two passes — Akamai walls the container
egress intermittently). Both AQUA TRU URL variants now store ~3 KB each:
price ($375.00), rating (4.6 / 2,6xx ratings), full description, spec table,
and 5 full review bodies. 5 of 11 product pages passed through; the rest
came back `challenge detected` on every attempt (the gate correctly rejects
the challenge page, so nothing bad is stored — they'll fill in as watchlist
refreshes land a pass or the CF lane solves one, as it did for the canonical
AQUA TRU URL).

### walmart.ca product pages stored as thin price stubs (found 2026-10-04, fixed 2026-10-04)

walmart.ca product pages (e.g. the Mainstays office chair,
`/en/ip/mainstays-bonded-leather-mid-back-managers-office-chair-black/6000199102326`)
stored only ~441 chars of price/delivery text: the extractor registry's
"walmart.com" suffix does not match "walmart.ca", so every walmart.ca URL fell
back to `GenericExtractor`, and trafilatura extracts only a thin slice from
the site's heavy client-rendered HTML. The server HTML carries everything in a
single `application/ld+json` block — a `ProductGroup` node with name,
description, `aggregateRating` (4.1 / 2,704 reviews), a `review` array of up to
10 full review bodies (author, date, stars), and `hasVariant[0]` holding
sku/gtin/model/brand/color plus the CAD offer — so no browser tier is needed.

Fixed: `WalmartExtractor` (`crawl/extractors/walmart.py`) now anchors on that
JSON-LD node when present (walmart.ca) and renders title, `**Price:**`,
`**Rating:**`, `**Brand:**`, `## Product description`, `## Product details`
(model/sku/gtin/color), and the top `CRAWL_MAX_REVIEWS` reviews under a
`## Walmart reviews (up to N)` heading (or a "No reviews available."
placeholder). walmart.com pages carry no Product JSON-LD, so they keep the
legacy generic fit + hero-price gate unchanged; `needs_refresh` is scoped to
walmart.ca hosts only, so stored .com pages never re-crawl in a loop. New
`walmart.ca` policy entry (http/browser, like the default) and an engine.py
fallback that sends non-product walmart URLs (search/category) back to the
generic extractor. `fetch.py` OR's `walmart.needs_refresh` into the refresh
chain so stale stubs self-heal on fetch. Tests extended in
`tests/test_extractors.py` (JSON-LD content, section ordering, review fields,
limit, no-reviews placeholder, gate failures, legacy .com path, needs_refresh
host scoping).

Verified live (2026-10-04): rebuilt + redeployed the image; `fetch_page` on
the chair URL triggered the inline refresh re-crawl (~4s, http tier) and now
stores 3,555 chars — price ($88.00), rating (4.1 / 2,704 ratings), full
description, spec table, and 5 full review bodies with author/stars/date; the
second fetch serves it from the index in ~11ms. It was the only walmart.ca
page in the index, so no bulk recrawl was needed; the 13 stored walmart.com
pages are unaffected (legacy path + needs_refresh host scoping).

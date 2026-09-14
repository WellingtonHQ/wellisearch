# Crawl failure flood — 2026-09-13 (worker refresh retry loop)

**TL;DR:** the ~2,150 crawl failures in the last 12h are not a site-wide outage. A small set of dead/unindexable pages gets re-crawled and fails on nearly every worker tick, with no backoff or attempt cap — so each one logs an error forever. It's been chronic since ~Sept 4 (65k errors in the retention window; up to 23k in a single day on Sept 4).

## The numbers

- Last 12h: **2,156 of 2,320 crawls failed (93%)**; all `status='error'`, detail `all tiers failed or empty markdown`; 2,155/2,156 with trigger=`refresh` (watchlist refresh) and just 1 `search`.
- Hourly rate is flat ~175–200 failures/hour — no spike, a constant drip.
- The whole index has only **44 stale pages** (> REFRESH_MIN_AGE_HOURS=72h); **33 of those are already `last_status='error'`** and most hold real stored content from an earlier successful crawl. Because failures never age the page out, the pool can't drain and the same ~6–8 URLs dominate every refresh batch:

| URL | errors (full log history) | first failure | live probe on 2026-09-13 |
|---|---|---|---|
| open-webui.com/key-features-of-open-webui | 5,727 | Sep 5 | TLS handshake rejected by server (`tlsv1 alert internal error`) — site-side regression; it crawled fine on Sep 2 |
| ictar.mcast.edu.mt/…/lovetami-onlyfans-leaks | 5,699 | Sep 5 | connection timeout — dead/parked domain. Stored content is a 207-char "Adblock detected" stub anyway |
| search.brave.com/ (homepage) | 3,539 | Sep 4 | HTTP 200 but fully client-rendered → extraction yields empty markdown; the one "success" on Sep 6 stored a 233-char near-empty stub |
| wiki.overture3d.com/en/TSG/Clogging | 1,640 | Sep 11 | **SSL certificate expired** (curl: `certificate has expired`) |
| wiki.overture3d.com/en/AdvancedGuide/SupportStructures | 1,640 | Sep 11 | same — expired cert |
| opencve.alliance.unm.edu/cve/CVE-2026-65905 | 101 (Sep 12 only) | Sep 12 | currently **502 Bad Gateway** from the origin; real CVE page, so retrying later is legitimate — it fell out of rotation on its own at ~3:54 AM |
| hormuzstraitmonitor.com/crisis-timeline/ | 1 (Sep 12) | Sep 12 | transient; next attempt succeeded minutes later. No action needed |

- Cost, last 7 days, refresh trigger only: **~286M ms of crawl time** (~3,000 core-minutes across parallel slots) burned on failing re-crawls (avg ~11.7s per failed attempt). Queue-path (`search`) errors are a separate, smaller problem: 397 in 7d averaging **214s each** — that's where the real timeout pain is.
- Cross-check: for every poisoned URL, first failure timestamp = last successful crawl + exactly 72h (e.g. overturewiki success Sep 8 04:23 → first error Sep 11 04:24). The loop starts precisely when the page crosses REFRESH_MIN_AGE_HOURS.

## Why it never stops (mechanism)

- `worker._refresh_watchlist` re-selects stale pages every tick (`last_crawled IS NULL OR < now()-72h`, LIMIT WORKER_BUDGET_PER_RUN).
- On a failed crawl, `_crawl_and_store` updates only `pages.last_status` — **never `last_crawled`** (worker.py:156), so the page stays "stale" forever and is re-picked on every tick.
- The queue path caps retries at QUEUE_MAX_ATTEMPTS=3 (`db.queue_done`); **the refresh path has no attempt cap, backoff, or disable trigger.** A dead URL that once succeeded gets retried indefinitely — one error per worker tick, ~every few minutes, forever.
- Secondary issue: `crawler.fit_markdown` collapses every failure into the single message `all tiers failed or empty markdown` (crawler.py:80), so crawl_log can't distinguish TLS errors from HTTP 5xx from empty-gate rejects — each root cause above had to be re-diagnosed by hand.
- The stale pool also contains pages that were *originally* stored as junk/error stubs and can never refresh cleanly (theditch.st ×2 store "This site can't be reached" text, docs.google.com stores a sign-in wall, youtube/instagram channel pages) — they keep the 44-page pool permanently non-draining.

## Recommended fixes

1. **Backoff on the refresh path** (the actual bug): track consecutive failed refreshes per page and stop hammering — e.g. after N failures set `last_crawled := now()` (re-enters in 72h), or flip a dashboard-visible "failed-refresh" state with exponential backoff / eventual auto-disable. The queue path already has the right shape (`attempts` + QUEUE_MAX_ATTEMPTS); refresh needs its own version since it doesn't go through crawl_queue.
2. **Capture per-tier failure detail in `crawl_log.detail`** — which tier ran, HTTP status if any, TLS/socket error text, or "empty markdown" gate rejection. One extra column/field would have made this report a one-query job.
3. **Data hygiene for the current 44-page pool:** candidates to disable (or delete) outright: search.brave.com/ homepage (client-side JS, no content), ictar.mcast.edu.mt onlyfans-leak page (dead + stored stub was adblock junk), theditch.st ×2 (stored "site can't be reached" pages). The overturewiki pair and opencve CVE page have real stored content — keep them indexed, just stop re-crawling them every tick until the site fixes its cert / 502s.

---

This one request below. Looks like it did not wait for the content to load fully before crawling it.

URL: https://gpupoet.com/gpu/shop/nvidia-geforce-rtx-3090-ti
From Index: false
Chars: 411
Truncated: false
Time: 1224 ms (index: 5 ms, crawl: 1212 ms)

I'd love to hear what you think! Please drop me a line and let me know what you like and what could be better. 🙏

# NVIDIA GeForce RTX 3090 Ti Listings

Below are active listings for the NVIDIA GeForce RTX 3090 Ti GPU. These listings are available and in-stock and you can buy them now. To learn more about the NVIDIA GeForce RTX 3090 Ti GPU visit the NVIDIA GeForce RTX 3090 Ti specifications page.

Loading...


---

# Amazon

It gets some information, like "About this item", but it doesn't capture other tings product details, seller, or customer reviews (even a summary).

```md
Title: CORSAIR 7000D Airflow Full-Tower ATX PC Case – High-Airflow Front Panel – Spacious Interior – Easy Cable Management – 3X 140mm AirGuide Fans with PWM Repeater Included – Black
URL: https://www.amazon.com/dp/B094442NL5?niid=nl_cl_lst_a_0_1&nrid=ZSX2RWQA0TWAJP13F3XS
From Index: true
Chars: 1186
Truncated: false
Time: 8 ms (index: 4 ms)

# CORSAIR 7000D Airflow Full-Tower ATX PC Case – High-Airflow Front Panel – Spacious Interior – Easy Cable Management – 3X 140mm AirGuide Fans with PWM Repeater Included – Black

**Price:** $149.99 — Only 1 left in stock - order soon.

## About this item

- Build your legacy with the 7000D AIRFLOW, a full-tower case for your most ambitious PC builds – offering easy cable management, a spacious interior, and massive cooling potential with room for up to three simultaneous 360mm radiators.

- A high-airflow optimized steel front panel delivers massive airflow to your system for maximum cooling.

- The CORSAIR RapidRoute cable management system makes it simple and fast to route your major cables through a single hidden channel, with an easy-access hinged door and a roomy 30mm of space behind the motherboard for all of your cables.

- Includes three CORSAIR 140mm AirGuide fans and PWM fan repeater, utilizing anti-vortex vanes to concentrate airflow and enhance cooling.

- A massive interior accommodates up to 12x 120mm or 7x 140mm cooling fans, and makes it possible to install multiple radiators including 3x simultaneous 360mm or 2x simultaneous 420mm for extreme cooling.
```

---

# LinkedIn

- We need the ability to full job descriptions from jobs posted on linked.
- Access publicly available info that does not require customer to be authenticated. 


---
# Reddit

- https://www.reddit.com/r/MacOS/comments/1g9hel4/is_it_possible_to_make_screenshots_immediately_go/
- Other reddit links

---

# Bot-wall recrawl leftovers (index-wide scan)

Scanned the whole index for stored pages whose saved markdown/title contains bot-wall / challenge wording — same issue as the Greenhouse fix above. Found 27 of ~9,570 pages; force-recrawled all 27. 23 now hold real content; these 4 came back `challenge detected` (the improved detector correctly refused to store junk), so their old stub is still in place:

- https://raw.githubusercontent.com/onyx-dot-app/onyx/main/backend/onyx/connectors/web/connector.py — **suspected false positive.** It's a plain source file, but the detector text-scans *every* response body, and this one (a crawler library) plausibly contains marker-like strings ("access denied", "request blocked", …). If confirmed: consider skipping or tightening the marker scan for non-HTML content types (`text/plain`, JSON APIs), so code files can't trip it.
- https://s2f.kytta.dev/?text=https%3A%2F%2Fdev — likely a genuine wall this time; re-attempt later.
- https://www.spectrumbusiness.net/ — cookie/JS wall served to us; re-attempt later (a browser-tier pass also failed).
- https://xdaforums.com/m/wellingtonhq.9307974/about — challenge on both http and browser tiers this run; re-attempt later.

Next step: diagnose which exact marker fires on each URL with surrounding context, confirm the GitHub raw-file false positive, then decide on a content-type guard for `is_botwall`.


---
# Job Descriptions

- https://microsoft.eightfold.ai/careers/job?domain=microsoft.com&profile_type=candidate&pid=1970393556941428&location=United+States&filter_include_remote=1
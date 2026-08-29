# Cloudflare anti-bot (2025–26) & Crawl4AI capability research

Research-only report. No code. Every claim carries a URL (repo file paths count for
Crawl4AI). Vendor claims are marked **(vendor)**; independent findings are marked
**(independent)**. Dates of sources are given where known.

Stack under test: wellisearch → Crawl4AI fork (`WellingtonHQ/crawl4ai@wellington`,
base 0.9.2) container, 3 gunicorn workers, patchright/playwright engine,
`CRAWL_MAX_PARALLEL=12`, 120s timeout, 16GB cap on a 16GB macOS host, egress via
the host's ISP IP (MacBook + Tailscale).

---

## 0. TL;DR — the single biggest finding

The deployed Crawl4AI config launches Chromium **with JavaScript disabled**.

- `deploy/docker/config.yml` sets `crawler.browser.kwargs: {headless: true, text_mode: true}`.
- In the fork (and upstream 0.9.x), `text_mode: true` appends launch flags
  `--disable-javascript --disable-images --disable-remote-fonts
  --blink-settings=imagesEnabled=false --disable-software-rasterizer
  --disable-dev-shm-usage` — see
  `fork: crawl4ai/browser_manager.py:108-113` and `:1095-1104`
  (verified identical in upstream:
  https://raw.githubusercontent.com/unclecode/crawl4ai/main/crawl4ai/browser_manager.py).
- The same config also forces `--disable-gpu --disable-software-rasterizer`
  (`config.yml` extra_args), which degrades WebGL to the SwiftShader software
  renderer — a classic headless tell — unless `enable_stealth` is set
  (`fork: crawl4ai/browser_manager.py:94-98`).
- `base_config: {simulate_user: true}` (`config.yml`) injects a
  `navigator_overrider` init script (`fork: crawl4ai/js_snippet/navigator_overrider.js`,
  766 bytes) — but with `--disable-javascript` an init script can do nothing.

Consequences:

1. Cloudflare's JS-dependent stack — the JavaScript Detections engine
   (https://developers.cloudflare.com/bots/concepts/bot-score/), Turnstile
   (https://developers.cloudflare.com/turnstile/), the Challenge Platform, and
   Precursor (https://developers.cloudflare.com/cloudflare-challenges/precursor) —
   is **unsolvable** by a JS-disabled browser. A JS challenge page just renders
   its static shell and times out.
2. JS-dependent SPAs return empty/incomplete HTML.
3. The browser presents as maximally anomalous (no subresources loaded, no JS
   environment) — exactly the profile anti-bot ML is trained on
   (https://developers.cloudflare.com/bots/concepts/bot-score/ — "ML … output
   variable: the predicted probability that a client is human (such as the
   probability of successfully solving a Challenge)").

**Fix cost: one config line (`text_mode: false`). This is the highest-ROI change
available and should be validated before any new engine work.**

---

## 1. What Cloudflare actually checks (2025–26 signal map)

### 1.1 Network / protocol layer (before any content)

| Signal | What it is | Source |
|---|---|---|
| TLS fingerprint (JA3/JA4) | ClientHello hash: ciphers, extensions, ALPN. JA4 sorts extensions to survive Chrome's ClientHello permutation. Used for rules + ML features. | https://developers.cloudflare.com/bots/additional-configurations/ja3-ja4-fingerprint (upd. 2026-05-06); https://blog.cloudflare.com/ja4-signals |
| JA4 **Signals** (inter-request) | Per-fingerprint aggregates over the last hour **of global traffic**: `browser_ratio_1h`, `h2h3_ratio_1h`, `cache_ratio_1h`, `reqs_quantile_1h`, `ips_rank_1h`, `ips_quantile_1h`. CF: "Fingerprints can be easily spoofed … traffic patterns and behaviors are constantly evolving." 15M unique JA4s/day, 500M UAs, billions of IPs. | https://blog.cloudflare.com/ja4-signals (upd. 2026-07-15) |
| HTTP/2 fingerprint | SETTINGS frame params+order, WINDOW_UPDATE value, PRIORITY presence, pseudo-header order (`m,a,s,p` vs `m,p,a,s`…). First presented at Black Hat USA 2017 (Akamai). "Cloudflare Bot Management matches observed TLS + HTTP/2 fingerprints against a database of known browser profiles." | https://scrapfly.io/blog/posts/http2-http3-fingerprinting-guide (2026-04, vendor but technically consistent with CF docs) |
| Cross-layer consistency | TLS says Chrome (BoringSSL) but HTTP/2 says hyper-h2 → "the strongest detection signal available." | same as above |
| HTTP/3 / QUIC params | `initial_max_data`, `max_udp_payload_size`, 0-RTT behavior, Alt-Svc upgrade willingness. "A client that never upgrades stands out immediately." (maturating) | same as above |
| IP reputation / range behavior | Datacenter ASN vs residential; request rate per IP; CF even detects CGNAT ranges to reduce collateral damage. | https://evomi.com/blog/cloudflare-bypass-2025 (2025-03, vendor); https://blog.cloudflare.com/detecting-cgnat-to-reduce-collateral-damage; https://dev.to/double_chen_70da460344c73/selenium-keeps-getting-blocked-by-cloudflare-heres-what-the-fingerprint-actually-catches-and-how-4fn9 (2026-04) |

### 1.2 Client / browser layer

| Signal | What it is | Source |
|---|---|---|
| Bot Score engines | **Heuristics** (known-malicious fingerprint DB → deterministic score 1), **ML** (supervised, "probability of successfully solving a Challenge", most scores 2–99), **Anomaly Detection** (deprecated), **JavaScript Detections (JSD)** ("catches headless browsers… lightweight, invisible JavaScript injection… enabled by default"). Missing `User-Agent` → score 1 immediately. `__cf_bm` cookie smooths per-session score. | https://developers.cloudflare.com/bots/concepts/bot-score (upd. 2026-08-26) |
| Turnstile / managed challenge | "small non-interactive JavaScript challenges… proof-of-work (computational puzzles), proof-of-space, probing for web APIs, and various other challenges for detecting browser-quirks and human behavior… fine-tune the difficulty of the challenge to the specific request." Widgets: Managed / Non-interactive / Invisible. ~3B runs/day. | https://developers.cloudflare.com/turnstile (upd. 2026-08-14); https://blog.cloudflare.com/introducing-precursor |
| Precursor (new, 2026) | Session-level behavioral verification: CDN-injected obfuscated script collecting interaction signals continuously; results update `cf_clearance` session state. Signals include mouse-movement physics (wrist-pivot arcs, cognitive-load delay, physiological tremor vs "linear interpolations or mathematically ideal Bézier curves"), click precision, session rhythm. 206M eval events/24h across 73,438 zones. Modes: Minimize Friction / Maximize Security; per-path Precursor Rules; under Maximize Security, `curl`/non-browser clients on API paths are affected. | https://blog.cloudflare.com/introducing-precursor (2026-07); https://developers.cloudflare.com/cloudflare-challenges/precursor (upd. 2026-08-20); https://blog.cloudflare.com/good-and-bad-agentic-behaviors (2026-08-07) |
| Coming (announced 2026-08) | "Adaptive Intelligence" — self-updating detection engine for all Bot Management customers; **AI Labyrinth** (Maze/Summary/Poison responses); BotBase verified-bot registry (declare honestly + don't abuse). | https://blog.cloudflare.com/good-and-bad-agentic-behaviors |

### 1.3 Implication for our stack

- Our egress is a residential ISP IP (MacBook; Tailscale does not change public
  egress without an exit node) — an **advantage** vs datacenter peers
  (https://dev.to/…4fn9: "If your IP is a datacenter ASN, nothing in the
  browser layer saves you").
- But `CRAWL_MAX_PARALLEL=12` from one IP is itself a signal ("high request
  volumes per minute from a single IP are a strong indicator of automation",
  https://evomi.com/blog/cloudflare-bypass-2025), and JA4 Signals'
  `reqs_quantile_1h` / `ips_quantile_1h` make single-IP volume visible
  (https://blog.cloudflare.com/ja4-signals).
- Since 2026, **session behavior** (Precursor) matters more than any single
  request's fingerprint: a fresh context that loads one URL and dies is
  precisely the "short burst" pattern CF says it targets
  (https://blog.cloudflare.com/introducing-precursor).

---

## 2. OSS effectiveness ranking (2025–26)

Key independent comparison (2026-04):
https://scrapewise.ai/blogs/playwright-stealth-2026 (ScrapeWise blog — vendor
adjacent, but cites concrete per-target results; treat pass rates as directional).

| Tool | Approach | Cloudflare 2025–26 | Evidence | Vendor-claim? | Notes |
|---|---|---|---|---|---|
| **Camoufox** | Firefox fork, C++-level stealth patches, binary fingerprint injection, humanize | CF Enterprise/Turnstile: **passes** (matrix); 0% headless detection in standard tests; **but** ~42.5s avg to solve a CF challenge, 200MB+ per context | (independent) https://scrapewise.ai/blogs/playwright-stealth-2026; (vendor) https://github.com/daijro/camoufox README "Undetectable by design"; **counter-evidence**: https://github.com/daijro/camoufox/issues/574 (Turnstile **silently fails inside Docker** on aarch64/Xvfb, works on host), https://github.com/daijro/camoufox/issues/311 (CF detects camoufox in Docker), https://github.com/daijro/camoufox/issues/170 (Jan 2025, blocked on moneysupermarket.com) | Mixed — strongest independent pass evidence, **but documented Docker failures** | Best raw stealth of the OSS set; our deployment is Docker — the #574 class of failures is directly relevant; slow on challenges (42s) |
| **Patchright** (our engine) | Patched Chromium via Playwright: removes `Runtime.enable` CDP leak, HeadlessChrome UA, WebDriver exposure | CF BotFight (free tier): **passes**; CF Enterprise/Turnstile: **variable**; passes nowsecure; behavioral analysis still catches it on hardest configs | (independent) https://scrapewise.ai/blogs/playwright-stealth-2026; (vendor) https://github.com/Kaliiiiiiiiii-Vinyzu/patchright README "patched and undetected… Chromium only" | Mixed | Our fork's default engine (`use_undetected=True`). Good ceiling for free-tier CF; variable on enterprise |
| **Scrapling** `StealthyFetcher` | Framework w/ stealth fetchers + adaptive parsing | "bypass anti-bot systems like Cloudflare Turnstile out of the box" | (vendor) https://github.com/D4Vinci/Scrapling README (76.8k stars, active 2026-08) | **Yes — vendor claim**, no independent 2025–26 matrix found | Attractive as a replacement *framework* (spiders, AutoThrottle, blocked-request detection) but the CF claim is unaudited here |
| **undetected-chromedriver** | Selenium + patched WebDriver flag/CDP strings | "Cloudflare pushes updates that re-detect it every few months" | (independent) https://dev.to/double_chen_70da460344c73/…4fn9; repo stalled: last push 2025-07-05, 1141 open issues — https://api.github.com/repos/ultrafunkamsterdam/undetected-chromedriver | Mixed | **Effectively unmaintained** — avoid for new work |
| **SeleniumBase UC mode** | CDP-direct, skips ChromeDriver binary | "Works on most CF sites" but flaky: user reports of overnight CF breakage on Ubuntu | (independent) https://github.com/seleniumbase/SeleniumBase/discussions/3933 (Aug 2025); (independent) dev.to article above | Mixed | Same Selenium/CDP risk class as UC-driver; not better than patchright |
| **nodriver** | CDP-direct, no Playwright bridge | CF Enterprise/Turnstile: **blocked**; basic detection: passes | (independent) https://scrapewise.ai/blogs/playwright-stealth-2026; https://github.com/ultrafunkamsterdam/nodriver | Mixed | Lighter (~80–120MB) but no behavioral faking; blocked on the targets that matter |
| **FlareSolverr** | Proxy service wrapping Selenium+undetected-chromedriver, returns cookies | Active (v3.5.0, 2026-05); known gaps: https://github.com/FlareSolverr/Flaresolverr/issues/1636 (missed a TradingView challenge) | https://github.com/FlareSolverr/Flaresolverr README (independent: architecture = Selenium+UC inside) | Partial | Adds an HTTP service + cookie reuse; engine is the same UC class — no independent evidence it beats patchright on CF in 2025–26 |
| **playwright-stealth / playwright-extra** (JS plugins) | JS-layer patches (`navigator.webdriver`, plugins, WebGL string…) | CF Enterprise: **blocked**; mid-tier targets: often passes | (independent) https://scrapewise.ai/blogs/playwright-stealth-2026 ("JS-patch tools … don't stop TLS fingerprinting") | Mixed | Our fork has `enable_stealth` (playwright-stealth) available but it only activates when `use_undetected=False` — i.e. it *replaces* patchright, doesn't stack |
| **curl_cffi** (HTTP-only) | TLS + HTTP/2 browser impersonation, no JS | Basic CF tiers: sometimes; Turnstile/enterprise: **no** (no JS execution) | (vendor) https://curl-cffi.readthedocs.io/en/v0.8.0/faq.html ("TLS and http2 fingerprints are just one of the many factors… for higher levels, you may need … browser automation"); (independent) https://www.reddit.com/r/webscraping/comments/1tqtqct/curl_cffis_tlsspoofing_detected_by_cloudflare, https://www.reddit.com/r/webscraping/comments/1qf8fvt/blocked_by_cloudflare_despite_using_curl_cffi | Mixed | Right tool for the cheap tier on non-CF sites; cannot solve challenges by construction |

**Ranking for our use case (CF-protected targets, Docker, one residential IP):**
1. Patchright with JS enabled + clean flags (already in the fork — currently
   neutered by `text_mode`), 
2. Camoufox (best raw stealth; Docker caveats), 
3. Scrapling (if we want the framework features; CF claim unverified), 
4. FlareSolverr (service layer, same engine class as UC), 
5. SeleniumBase UC / undetected-chromedriver / nodriver (stale or blocked), 
6. curl_cffi (cheap tier only, never for challenges).

---

## 3. IP reputation: does our setup hurt us?

**Verdict: our IP is an advantage, not the problem — but single-IP parallelism is
a real signal.**

- Our egress IP is the host's residential ISP IP (MacBook; Tailscale is a mesh —
  it does not change public egress unless an exit node is used). CF's ML and
  JA4 Signals are trained on traffic where datacenter ASNs dominate the
  bot-positive class: "Cloudflare examines the IP address reputation (is it
  known for malicious activity? Does it belong to a datacenter often associated
  with bots?)" (https://evomi.com/blog/cloudflare-bypass-2025); "If your IP is a
  datacenter ASN, nothing in the browser layer saves you"
  (https://dev.to/…4fn9); curl_cffi's own FAQ lists "IP quality" first among
  CF's factors (https://curl-cffi.readthedocs.io/en/v0.8.0/faq.html).
- **However**: (a) `reqs_quantile_1h` / `ips_quantile_1h` in JA4 Signals
  (https://blog.cloudflare.com/ja4-signals) mean one IP issuing thousands of
  requests stands out against the global baseline of that fingerprint;
  (b) "high request volumes per minute from a single IP are a strong indicator
  of automation" (https://evomi.com/blog/cloudflare-bypass-2025);
  (c) 12 parallel Chrome contexts from one IP (CRAWL_MAX_PARALLEL=12) amplifies
  both.
- **Actionable**: verify the actual egress from inside the container
  (`curl ifconfig.me`) to rule out an exit-node/proxy surprise; consider a
  lower per-domain concurrency for CF-protected hosts; keep residential egress.

---

## 4. Block-detection heuristics wellisearch can implement

### 4.1 What Crawl4AI actually returns for a blocked URL

- `/md` (the endpoint wellisearch uses) returns **only**
  `{"markdown", "title", "success"}` — **no status_code, no headers**
  (`fork: deploy/docker/api.py:324-420`; `handle_markdown_request` returns
  `(markdown, title)`). Navigation errors → HTTP 400/500 + `detail` string.
  A Cloudflare challenge interstitial is frequently served with HTTP 200 and
  *renders successfully* as a page → wellisearch sees `success: true` and
  markdown like "Just a moment…" with **no way to tell it apart from content**.
- `page.goto()` does not raise on 403/503 — the status is captured and the
  (challenge) HTML is extracted (`fork: crawl4ai/async_crawler_strategy.py:788-849`).
  So blocked-by-403 also arrives as `success: true` on `/md`.
- **`/crawl` returns the full `CrawlResult`** including `status_code`,
  `response_headers`, `redirected_url`, `error_message`
  (`fork: crawl4ai/models.py:130-163`) — far richer signals. The retry tier
  should use `/crawl` (it also accepts `browser_config`/`crawler_config`, see §5).

### 4.2 Ready-made heuristic: Crawl4AI's own `antibot_detector.py`

The fork ships `crawl4ai/antibot_detector.py` (281 lines, pure Python, no deps)
with `is_blocked(status_code, html, error_message) -> (bool, reason)`. It is
**not imported anywhere** in the fork (dead module) but is importable:

- **Tier 1** (any page size): Akamai `Reference #`, "Pardon Our Interruption";
  Cloudflare `challenge-form` + `__cf_chl_f_tk`, `<span class="cf-error-code">NNNN</span>`,
  `/cdn-cgi/challenge-platform/…orchestrate`; PerimeterX `window._pxAppId`,
  `captcha.px-cdn.net`; DataDome `captcha-delivery.com`; Imperva `_Incapsula_Resource`,
  "Incapsula incident ID"; Sucuri; Kasada `KPSDK.scriptStart`;
  "blocked by network security" (Reddit SPA shells).
- **Tier 2** (short pages <10KB): "Access Denied", "Checking your browser",
  `<title>Just a moment`, g-recaptcha/h-captcha classes, "Access to This Page
  Has Been Blocked" (PerimeterX), "Request unsuccessful" (Imperva).
- **Tier 3** (structural, <50KB): no `<body>`, <50 chars visible text, zero
  semantic content elements, script-heavy empty shell.
- Status rules: 429 always; 403/503 + HTML always; 4xx/5xx + short page +
  pattern; 200 + near-empty (<100B) = JS-blocked render.

### 4.3 Wellisearch-side heuristics for the `/md` response (content-only)

Apply to `(markdown, title)`:

1. **Title markers**: `Just a moment…`, `Checking your browser`,
   `Access Denied`, `Attention Required`, `Request unsuccessful`,
   `Access to This Page Has Been Blocked`, `Pardon Our Interruption`.
2. **Body markers** (regex on markdown, case-insensitive): `challenge-form`,
   `cf_chl_`, `challenge-platform`, `cf-error-code` / CF error numbers
   (1009, 1010, 1012, 1015, 1020, 1025, 1032 — see
   https://evomi.com/blog/cloudflare-bypass-2025), `px-cdn`,
   `captcha-delivery.com`, `Incapsula`, `KPSDK`, `Reference #18.`
3. **Length heuristic**: challenge interstitials are tiny (<~1–2KB markdown).
   `success: true` + very short markdown + none of our content heuristics
   matching a known-good shape → suspect.
4. **Repeatability**: same URL blocked N times in a window → mark domain as
   CF-hard, escalate to a stealthier tier (or give up with a clear error).
5. **Upgrade path**: switch the retry call from `/md` to `/crawl` to get
   `status_code`/`response_headers` and feed the full
   `antibot_detector.is_blocked()` (which also works on HTML).

---

## 5. Crawl4AI built-in options (our fork, 0.9.2-based)

### 5.1 Engine selection — there is **no `BROWSER_MODE` env var**

- `BROWSER_MODE` does not exist anywhere in the fork (grep across
  `browser_adapter.py`, `async_crawler_strategy.py`, `browser_manager.py`,
  `server.py`, `api.py`).
- Engine is chosen in code: `BrowserConfig.use_undetected` (default **True**) →
  `UndetectedAdapter` (patchright) when patchright is importable, else
  `PlaywrightAdapter` (`fork: crawl4ai/async_crawler_strategy.py:62-71`,
  `fork: crawl4ai/browser_adapter.py:58-274`). `requirements.txt` installs
  `playwright>=1.49.0`, `patchright>=1.49.0`, `playwright-stealth>=2.0.0`.
- `browser_mode` (builtin|dedicated|cdp|docker) is a **browser lifecycle**
  mode, not an engine choice (`fork: crawl4ai/async_configs.py` BrowserConfig).
- To switch engine: `BrowserConfig(use_undetected=False, enable_stealth=True)`
  (stealth only activates when not undetected — see the flag block at
  `fork: crawl4ai/browser_manager.py:94-98`).

### 5.2 "Curl mode" / "trafilatura mode" — **do not exist**

- `crawl4ai/crawlers/` contains only `amazon_product` and `google_search`
  (GitHub tree, branch `wellington`). No curl/crawlee strategy.
- No `curl_cffi`, `trafilatura`, or HTTP-client impersonation in
  `requirements.txt` or `deploy/docker/requirements.txt`.
- `HTTPCrawlerConfig` (`fork: crawl4ai/async_configs.py:1257`: method, headers,
  data, json, follow_redirects, verify_ssl) exists for specific sub-features,
  not as a general fetch tier.
- So the "cheap HTTP tier" must be added by wellisearch itself (e.g.
  `curl_cffi` in the wellisearch service) or by extending the fork.

### 5.3 What the endpoints accept

| | `/md` (`api.py:324`) | `/crawl` (`api.py:655`) |
|---|---|---|
| Request fields | `url, f (fit\|raw\|bm25\|llm), q, c, provider, temperature, base_url` | + `browser_config` dict, `crawler_config` dict (allowlist-gated, untrusted) |
| Browser config source | `config.yml` `crawler.browser.{kwargs,extra_args}` only | `config.yml` + caller dicts (allowlisted fields) |
| Response | `markdown, title, success` | full `CrawlResult`: `status_code, response_headers, redirected_url, markdown, error_message, …` |
| `simulate_user` | **no** (not part of /md's CrawlerRunConfig) | yes — merged from `base_config {simulate_user:true}` |

Allowlisted untrusted `BrowserConfig` fields include: `browser_type, headless,
browser_mode, viewport_*, text_mode, light_mode, enable_stealth, avoid_ads,
avoid_css, user_agent, user_agent_mode, user_agent_generator_config, verbose,
memory_saving_mode, max_pages_before_recycle`
(`fork: crawl4ai/async_configs.py:226`); `use_undetected` is **not**
allowlisted for untrusted callers (silently dropped; default True anyway).
`simulate_user` is **forbidden** for untrusted `CrawlerRunConfig`.

### 5.4 Fork env vars actually in use (compose.yml)

`GUNICORN_BIND, CRAWL4AI_API_TOKEN, LLM_PROVIDER, OPENAI_BASE_URL,
OPENAI_API_KEY, LLM_TEMPERATURE, LLM_REASONING_EFFORT` + documented
`CRAWL4AI_ALLOW_INSECURE_TLS, REDIS_TASK_TTL, CRAWL4AI_HOOKS_ENABLED`.
**None of these change the browser engine or stealth.**

### 5.5 Effort estimate: adding a new browser mode (e.g. Camoufox)

- `BrowserAdapter` is a thin ABC (console capture, `evaluate`, imports —
  `fork: crawl4ai/browser_adapter.py:24-57`). The **launch** logic lives in
  `BrowserManager` (90KB, `fork: crawl4ai/browser_manager.py`), which owns
  flag construction (§0) and pool/recycle.
- Camoufox is Playwright-compatible (it *is* a Playwright-driven Firefox
  fork). Two viable wirings:
  (a) **CDP bridge**: launch camoufox with a CDP endpoint, connect Playwright
  via `connect_over_cdp` — reuses `browser_mode: cdp` plumbing;
  (b) **new launch path** in `BrowserManager` selected by a new
  `BrowserConfig` field (e.g. `engine: camoufox`) + `requirements` install of
  `camoufox`.
- Estimate: **~1–3 days** for a working mode + tests, dominated by pool
  recycling, memory limits (200MB+ per context vs our 16GB cap) and the Docker
  caveats in §2 (issues #574/#311). Not a config change.

---

## 6. Implications for a tiered pipeline

Proposed tiers (cheapest first), mapped to the evidence above:

1. **Tier 0 — plain HTTP impersonation** (new, in wellisearch):
   `curl_cffi` with `impersonate="chrome"` for the majority of targets that
   don't run CF challenges. Cheap, fast, no browser. Vendor + independent
   sources agree it's fine for basic tiers and useless for Turnstile
   (https://curl-cffi.readthedocs.io/en/v0.8.0/faq.html, reddit threads §2).
   Slots in *before* any browser spend.
2. **Tier 1 — patchright with JS ON** (our existing engine, fixed):
   `text_mode: false` + drop `--disable-gpu`/`--disable-software-rasterizer`
   (or set `enable_stealth: true`), real UA, `simulate_user: true`. Independent
   2026 matrix says this passes CF free-tier BotFight
   (https://scrapewise.ai/blogs/playwright-stealth-2026). **This is the fix
   for today's failures; do it before anything else.**
3. **Tier 2 — Camoufox** for the CF Enterprise/Turnstile residue:
   best independent pass evidence, but ~42s/challenge and 200MB+ per context;
   and the documented Docker failures (#574, #311) mean either host-level
   launch or a fixed image. Use sparingly (per-domain escalation, low volume).
4. **Cross-cutting**:
   - Keep the residential egress; cap per-domain parallelism for CF-protected
     hosts (12-way from one IP is a JA4-Signals-level signal).
   - Adopt `antibot_detector.py` patterns (§4) and move retries to `/crawl`
     for status/headers.
   - Since 2026, **session continuity** is a first-class signal (Precursor):
     reusing a warm context per domain beats fresh one-shot contexts — another
     argument for the `browser_mode: dedicated`/pool design over per-request
     browsers.
   - Expect drift: CF ships "Adaptive Intelligence" (self-updating detection)
     and AI Labyrinth later in 2026
     (https://blog.cloudflare.com/good-and-bad-agentic-behaviors) — budget
     for the pass-rate to move under your feet even with a perfect setup.

---

## Appendix — primary sources (2025–2026 unless noted)

Cloudflare (primary):
- https://developers.cloudflare.com/bots/concepts/bot-score (upd. 2026-08-26)
- https://developers.cloudflare.com/bots/additional-configurations/ja3-ja4-fingerprint (upd. 2026-05-06)
- https://developers.cloudflare.com/turnstile (upd. 2026-08-14)
- https://developers.cloudflare.com/cloudflare-challenges/precursor (upd. 2026-08-20)
- https://blog.cloudflare.com/introducing-precursor (2026-07)
- https://blog.cloudflare.com/good-and-bad-agentic-behaviors (2026-08-07)
- https://blog.cloudflare.com/ja4-signals (2024, upd. 2026-07)
- https://blog.cloudflare.com/detecting-cgnat-to-reduce-collateral-damage

Independent / community:
- https://scrapewise.ai/blogs/playwright-stealth-2026 (2026-04, tool matrix)
- https://dev.to/double_chen_70da460344c73/selenium-keeps-getting-blocked-by-cloudflare-heres-what-the-fingerprint-actually-catches-and-how-4fn9 (2026-04; author sells "browser-act" — read with that bias)
- https://scrapfly.io/blog/posts/http2-http3-fingerprinting-guide (2026-04, vendor but technically detailed)
- https://evomi.com/blog/cloudflare-bypass-2025 (2025-03, proxy vendor)
- https://www.reddit.com/r/webscraping/comments/1tqtqct/curl_cffis_tlsspoofing_detected_by_cloudflare
- https://www.reddit.com/r/webscraping/comments/1qf8fvt/blocked_by_cloudflare_despite_using_curl_cffi
- https://github.com/daijro/camoufox/issues/574, /311, /170
- https://github.com/seleniumbase/SeleniumBase/discussions/3933 (2025-08)
- https://github.com/FlareSolverr/Flaresolverr/issues/1636

Fork source (branch `wellington`, base 0.9.2):
- `deploy/docker/api.py:324` (`/md`), `:655` (`/crawl`)
- `deploy/docker/config.yml` (browser kwargs: headless, text_mode; extra_args)
- `crawl4ai/browser_manager.py:94-113`, `:1095-1104` (text_mode → `--disable-javascript`; stealth guard for GPU flags)
- `crawl4ai/async_crawler_strategy.py:62-71` (adapter selection), `:788-849` (goto/status)
- `crawl4ai/browser_adapter.py` (Playwright/Stealth/Undetected adapters)
- `crawl4ai/async_configs.py:226` (untrusted allowlist), `:1257` (HTTPCrawlerConfig), BrowserConfig
- `crawl4ai/antibot_detector.py:26-280` (unused block-detection heuristics)
- `crawl4ai/models.py:130-163` (CrawlResult fields)
- `crawl4ai/js_snippet/navigator_overrider.js` (766B, simulate_user payload)

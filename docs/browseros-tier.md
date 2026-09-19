# BrowserOS neo crawl tier (design)

A fourth transport tier that loads pages in **BrowserOS neo** — a real,
persistent-profile desktop browser driven over MCP. It exists for the sites
that defeat both `http` and headless `browser`: DataDome/Cloudflare managed
challenges, login-walled job boards, forums. A human can solve a stubborn
captcha once in the neo cockpit; the persistent profile then whitelists that
site for all future crawls.

Status: **implemented** on branch `exp/browseros-tier` (`crawl/tiers/neo.py`,
8 unit tests in `tests/test_neo_tier.py`). Experiment results against the real
walls are at the bottom of this doc. See `features-backlog.md` (Engine →
"BrowserOS neo crawl tier").

## Why this works where patchright doesn't

- Real, warm desktop browser with a persistent profile: cookies, TLS/JS
  fingerprints, and any human-solved challenge state survive across crawls.
- Headless `browser` tier launches a fresh context per pool; DataDome-class
  walls key on exactly that. neo's profile is indistinguishable from the user's
  own browsing.
- No LLM in the loop: wellisearch calls the MCP server directly with fixed
  scripts. The "agent" surface of neo is just a transport.

## Endpoint (verified 2026-09-18)

neo runs on the **host machine** (the one running Docker). From inside the
wellisearch container it is reached at:

```
http://host.docker.internal:9010/mcp
```

- Streamable HTTP MCP endpoint, **no auth header** (local-only binding;
  confirmed against `~/.config/opencode/opencode.jsonc`, which points opencode
  at `http://127.0.0.1:9010/mcp` with no headers).
- If the neo app is not running/paired, tool calls fail with
  "browser session not connected" — the tier must map that to a clean
  `CrawlError`, not crash the crawl batch.

## Verified protocol facts (live probes against the running instance)

The MCP **client** SDK is already installed (`mcp>=2.1` in `pyproject.toml`;
wellisearch serves its own MCP server, so no new dependency). Installed 2.2.0:

- Transport: `from mcp.client.streamable_http import streamable_http_client` —
  yields `(read_stream, write_stream)`; pass a pre-configured
  `httpx.AsyncClient` for headers/auth (none needed here).
- Session: `mcp.client.session.ClientSession(read, write)`, async context
  manager, `await session.initialize()`, then
  `await session.call_tool("run", {"code": script})`.

The **`run` tool** is the programmatic surface: one call = one async JS body
executed against a `browser` SDK in neo's runtime. Hard cap: **30s wall time
per call**. Verified behaviors:

| Fact | Evidence |
|---|---|
| `browser.pages.newPage(url)` opens in the background (never steals focus) and returns a numeric pageId; `browser.pages.close(pageId)` closes it | SDK docs + probe |
| `browser.wait(pid, {value: ms})` fixed pause; `{for: "selector", value}` waits for a selector | probe |
| `browser.evaluate(pid, {code: "..."})` — `code` is an async body string that may `return` a value. **A bare function or `{func: ...}` object is rejected** ("provide `code` or `func`") in the run-SDK context; use `code` | probe |
| Small evaluate results come back as an envelope `{page, value}` (value = the returned JS value) | probe (`{"page":18,"value":"Example Domain"}`) |
| **Large results are not inline**: when the result exceeds the limit, the envelope is `{contentLength, page, path, writtenToFile: true}` and the full text is written to a local file on neo's host (e.g. `C:\Users\moreno\AppData\Local\BrowserClaw\Application\<id>\tool-output\evaluate-<ts>-<uuid>.txt`) | probe with 819,650-char outerHTML |
| `maxChars` is honored: `{code: ..., maxChars: 200000}` returned a **153,676-char** outerHTML inline as `{page, value}`; the cap is 200,000 UTF-8 bytes (≈200K ASCII chars; fewer for multibyte content) | probe with 150KB body |
| `browser.cdp(method, params?)` raw CDP escape hatch exists if needed | SDK docs |

## Tier design

New file `src/wellisearch/crawl/tiers/neo.py`, registered like the others
(`tiers/__init__.py:52`). Implements the existing `Tier` protocol
(`tiers/__init__.py:15-30`):

```python
class NeoTier:
    name = "neo"

    async def fetch(self, url: str, p: Policy) -> Rendered: ...
    def worst_case_s(self, p: Policy) -> float: ...
```

### Session lifecycle

- **Lazy singleton**, same pattern as the browser pools (`crawl/pool.py`):
  first `fetch()` opens the MCP session; reuse it across crawls. Do not connect
  at worker startup — neo is a desktop process the human starts/stops, and an
  eager connection would fail boot or sit dead.
- **Reconnect on failure**: catch transport/session errors (stale
  `mcp-session-id` → "Session terminated"), tear down, retry once after a short
  backoff (mirror `CRAWL_LAUNCH_RETRY_AFTER_S`, `pool.py:162-171`).
- Client-side read timeout per `run` call must exceed neo's 30s cap so the
  server's own timeout error arrives intact (`CRAWL_NEO_TIMEOUT_S=45`).

### Fetch flow (one crawl = one or a few `run` calls)

Each step is inside a single `call_tool("run", {"code": script})`:

1. `const pid = await browser.pages.newPage(url)` — fresh tab per crawl
   (pageIds go stale across turns; carrying URLs is the documented safe pattern).
2. `await browser.wait(pid, {for: "selector", value: "body"})` then a fixed
   settle (`CRAWL_NEO_SETTLE_MS`, default 2000 — mirrors `CRAWL_SETTLE_S`).
3. Extract via evaluate (see Result size handling below):
   ```js
   return JSON.stringify({
     html: document.documentElement.outerHTML,
     title: document.title,
     status: (performance.getEntriesByType("navigation")[0] || {}).responseStatus || null,
     ct: document.contentType || null,
   })
   ```
   - `status` from navigation timing is only populated when the site sends
     `Timing-Allow-Origin`; fall back to 200 — same precedent as
     `browser.py:92`.
   - `ct` from `document.contentType` (reliable); if absent → `None`, which is
     safe: `is_botwall()` still scans HTML when the type is unknown
     (`botwall.py`).
4. `await browser.pages.close(pid)` in a `finally` — always close "mine" tabs.
5. Parse the envelope, build
   `Rendered(html=..., title=..., status=status or 200, ms=<measured>, engine="neo", content_type=ct)`.

**Challenge poll (manual-solve path):** if `is_botwall(html, status, ct)` is
not None and a budget remains (`CRAWL_NEO_CHALLENGE_BUDGET_S`, default 60s),
loop: sleep `CRAWL_NEO_POLL_MS` (5s) in Python, then re-read the page. Stop
when clean or budget exhausted. This mirrors `_resolve_challenge`'s bounded
loop (`browser.py:137-161`) minus the turnstile clicking — a human clicks, not
us.

*Implementation deviation:* polls **re-read the same open tab in place**
(`_recheck_script`) instead of re-navigating each poll. Re-navigation resets
auto-solving JS challenges and looks bot-like to WAFs; the persistent profile
picks up a human-solved whitelist on any subsequent load, so an in-place
re-read is sufficient (a tab lost mid-poll triggers one fresh navigation).

**Return `Rendered` even if still walled** (do not raise on a wall): the
engine's own botwall check then records `botwall: <marker>` and fails the crawl
with an actionable detail (`engine.py:53-57`). Raising would conflate "page is
walled" (content outcome) with "tier broke" (error).

**Error mapping:** MCP protocol errors / tool errors → named exceptions
(`NeoNotConnectedError`, `NeoRunError`) carrying the server's message; the
engine records them per-tier and `failure_detail()` renders
`neo: NeoNotConnectedError: browser session not connected — start BrowserOS neo`
(`crawler.py:91-116`).

### Result size handling (verified limits)

Primary path: one evaluate with `maxChars: 200000`. Typical indexed pages fit
inline (`{page, value}`). If the envelope comes back `writtenToFile: true`
(page > 200K UTF-8 bytes), fall back to **chunked retrieval**: stash
`window.__ws_html = document.documentElement.outerHTML`, return its length,
then pull slices of ≤150K chars each via repeated small evaluates and
reassemble wellisearch-side. (Alternative optimization: bind-mount neo's
`tool-output` directory into the container and read the file directly — couples
us to neo's internal path layout, so chunking is the default.)

## Concurrency & lane integration

**Minimal change: one `asyncio.Lock` owned by the tier instance**, acquired
around the whole fetch. One shared real browser ⇒ single-flight. No changes to
queue or lane machinery:

- The existing caps keep doing their jobs (fast-lane `CRAWL_MAX_PARALLEL=8`,
  CF-lane `CRAWL_CHALLENGE_PARALLEL=2`, selected per lane in
  `crawl_deduped`, `queue.py:84`). The neo lock only serializes the subset of
  crawls that actually reach the shared browser.
- No pool/profile machinery — that exists for local patchright contexts; neo is
  a remote desktop browser with one profile by design.
- Backstop interaction: the engine wraps each fetch in
  `asyncio.wait_for(..., _tier_backstop(tier, name, p))` (`engine.py:44`). Time
  spent waiting on the lock counts inside that window, so
  `worst_case_s(p)` = `clamp(CRAWL_NEO_TIMEOUT_S) + (challenge budget when
  `get_lane() == CF`)`. Under heavy contention a queued neo attempt may hit the
  backstop and be retried later (`QUEUE_MAX_ATTEMPTS=3`); acceptable since neo
  is last-resort only.

## Policy integration

Neo sits **after browser**, reached only where http + headless both failed:

1. Fast lane on a walled domain: `http` fails → `browser` probes and raises
   `ChallengeDetected` (`browser.py:107-108`) → worker routes the row to the CF
   lane (`worker.py:236-238`). Neo is never reached in the fast lane — correct,
   a challenge needs the high-budget lane.
2. CF lane: `http` fails → `browser` runs its turnstile loop; when it exhausts
   budget it returns the still-walled HTML (`browser.py:159-161`) → engine
   advances to the next tier → **neo** loads the page in the persistent-profile
   browser, where a previously human-solved challenge now passes.

Changes in `policy.py` (table at `policy.py:23-46`):

- Per-domain entries first (the point of neo is targeting known-walled domains):
  ```python
  "reddit.com": Policy("reddit", ("http", "browser", "neo"), ...),
  "linkedin.com": ..., "xdaforums.com": ..., "spectrumbusiness.net": ...
  ```
- Optional global last resort: append `"neo"` to `DEFAULT_POLICY` only when the
  enabled flag is set (requires `match()` to consult config). Ship per-domain
  first; revisit after seeing real hit rates.
- Gating in `by_name()` (`tiers/__init__.py:41-48`):
  `if name == "neo" and not s.CRAWL_NEO_TIER: return None`. When unconfigured,
  the engine records `{"tier": "neo", "error": "disabled"}` — a clean no-op
  identical to disabled http/stealth.

## Manual-captcha story

From the crawler's perspective, "human solves it once in the cockpit" is just
**a wall that disappears between two navigations**:

1. Crawl hits a walled page → tier detects via `is_botwall` (same markers as
   everywhere else).
2. Tier runs its bounded poll loop (60s default, 5s interval, fresh navigation
   each poll). The human sees the challenge in their real browser, solves it;
   neo's persistent profile stores the resulting cookies/whitelist state.
3. Next poll re-navigates → clean HTML → crawl succeeds **within the same
   attempt** (no extra queue round-trip; a 60s wait fits comfortably inside the
   worker tick budget and the CF lane already spends up to
   `CRAWL_CF_TIMEOUT_S=300` per page).
4. Budget exhausted without a solve → walled `Rendered` returned → engine fails
   the crawl with an actionable detail line, e.g.:
   `neo: botwall: access denied (challenge unsolved after 60s — solve once in
   the BrowserOS neo cockpit; the persistent profile whitelists future crawls)`.
5. After failure, normal refresh backoff applies (`worker.py`), so the page is
   not re-probed every tick while waiting on a human.

## Config knobs (following `config.py` conventions)

| Knob | Type / default | Purpose |
|---|---|---|
| `CRAWL_NEO_TIER` | `bool = False` | Master enable; off by default (needs neo running). Precedent: `CRAWL_HTTP_TIER`, `CRAWL_STEALTH_TIER` (`config.py:116,124`) |
| `CRAWL_NEO_ENDPOINT` | `str = "http://host.docker.internal:9010/mcp"` | Streamable HTTP URL of the neo MCP server (no auth) |
| `CRAWL_NEO_TIMEOUT_S` | `int = 45` | Client read timeout per `run` call; must exceed neo's hard 30s cap |
| `CRAWL_NEO_SETTLE_MS` | `int = 2000` | Fixed post-load settle inside the run script (mirrors `CRAWL_SETTLE_S=2.0`) |
| `CRAWL_NEO_CHALLENGE_BUDGET_S` | `int = 60` | Bounded manual-solve poll budget; `0` = fail immediately on wall |
| `CRAWL_NEO_POLL_MS` | `int = 5000` | Interval between challenge polls (fresh navigation each) |

No token knob: the endpoint is unauthenticated and host-local. If neo ever
gains auth, add `CRAWL_NEO_TOKEN` (never logged).

## Test plan

**Seam:** give `NeoTier` an injectable executor —
`async def run_script(code: str) -> dict` returning the parsed envelope of one
`run` call. The default implementation wraps the MCP client (lazy session +
reconnect); tests inject a fake. Mirrors how `test_providers.py` mocks httpx
and `test_lanes.py` monkeypatches pools (`tests/test_lanes.py:145-150`).

New file `tests/test_neo_tier.py`, plain-assert + `asyncio.run` style:

1. **Clean page**: fake executor returns `{page, value}` with JSON payload →
   `Rendered(engine="neo")` fully populated; assert the script contains
   `newPage`, `evaluate`, and `close`.
2. **Gating**: `tiers.by_name("neo") is None` when `CRAWL_NEO_TIER=false`.
3. **Browser down**: executor raises not-connected → engine returns `ok=False`
   with attempt `{"tier": "neo", "error": "NeoNotConnectedError: ..."}`;
   `failure_detail()` contains the message.
4. **Challenge poll**: walled HTML twice then clean → success after 3 run
   calls; budget-exhausted variant → walled `Rendered` returned (not raised).
5. **Large result**: envelope `{writtenToFile: true}` → chunked retrieval path
   reassembles the full HTML from slices.
6. **Single-flight**: two concurrent `fetch()`s against a slow fake executor →
   max observed in-flight == 1.
7. **Policy**: `match("https://www.reddit.com/r/x/comments/1").tiers == ("http", "browser", "neo")`.

Run with the existing convention (`python tests/test_neo_tier.py`, no network,
DB, or browser needed).

## Experiment results (2026-09-18)

Harness: `benchmarks/neo_vs_cf.py` (full ladder, both phases),
`benchmarks/neo_first_test.py` (tier-patched ladders), and
`benchmarks/production_ladder_test.py` (unpatched production ladder). Test
walls: xdaforums.com (DataDome-class), spectrumbusiness.net, reddit.com
(reCAPTCHA interstitial).

### Headline finding: the "hard walls" were our own detector

Two `is_botwall()` bugs made every tier look like it was failing on these
sites; both are fixed in this branch (`crawl/botwall.py`):

1. **False positive — `<noscript>` text.** Legitimate pages put
   "JavaScript is disabled" warnings inside `<noscript>` blocks for non-JS
   clients (XenForo does exactly this). The marker scan matched the warning on
   *clean* pages, so http/browser/neo all returned real content that the engine
   recorded as `botwall: javascript is disabled` — and neo then polled a clean
   page for its full 60s budget. Fix: strip `<noscript>...</noscript>` before
   scanning (challenge walls render markers as visible text, never in noscript).
2. **False negative — reddit's reCAPTCHA interstitial.** Plain HTTP gets a
   "Reddit - Prove your humanity" page with no existing marker; the engine
   stored ~167KB of challenge garbage as content. Fix: added the
   `"prove your humanity"` marker (title + body text).

### Results after the fix (production ladder, unpatched)

| Site | http tier | browser tier | neo tier |
|---|---|---|---|
| xdaforums.com | **ok** 344ms, md=1403 | not reached | ok alone: 6.2s, md=1261 |
| spectrumbusiness.net | **ok** 132ms, md=237 | not reached | ok alone: 7.2s, md=644 |
| reddit.com | wall detected (`prove your humanity`) → escalate | **ok** 2.9s, md=4371 | not reached |

- xdaforums/spectrumbusiness resolve at tier 1 once the detector is fixed — no
  browser or neo needed. The http tier (curl_cffi Chrome TLS impersonation)
  receives the full noscript page with status 200; content is comparable to the
  JS-rendered version (md=1403 vs 1261 for xdaforums).
- reddit's reCAPTCHA wall defeats http but **not** headless patchright: the
  browser tier resolves it in ~3s. neo was never required by any test site.
- Neo-only ladders confirm the tier itself works when reached: both walls load
  clean real pages in ~6–7s, and an immediate second crawl of the same URL also
  succeeds (no visit-rate walling observed).

### Egress / WAF observations

- The egress IP is a **datacenter address behind a GCP proxy** (`ifconfig.me`
  shows `X-Forwarded-For: <host-ip>, 34.160.x.x`, `via: google`). WAFs treat it
  accordingly and escalate fast under sustained traffic.
- **TLS fingerprint matters more than UA.** Plain httpx (python TLS) to
  xdaforums gets a hard 403 after heavy hammering; the same URL through the
  http tier's curl_cffi Chrome impersonation still returns the full page with
  200. neo's real desktop browser is unaffected by the IP-reputation
  degradation that hits plain HTTP — it loaded clean pages while http was
  403'd.
- Sustained hammering (two ~5min CF-lane cycles per URL plus worker retries)
  degrades the IP's standing for *plain* clients; a quiet period of tens of
  minutes partially recovers it. Keep crawl frequency modest on walled domains.

### Experiment hygiene notes (gotchas hit)

- **The background worker kept re-hammering the test URLs in parallel.** Queue
  entries with retry backoff ran full CF-lane cycles every ~7–10 min through
  both experiment runs, contaminating "clean window" attempts. Clear
  `crawl_queue` for the target domains before a clean-shot measurement.
- **Overlapping test runs look bot-like.** Two concurrent crawls of the same
  URL (a killed run's orphan process + a fresh run) produced walling that a
  single sequential run did not. Run one experiment at a time; check for
  orphans (`/proc/*/cmdline`) before re-running.
- **Run long container tests detached** (`docker exec -d ... > /tmp/out.txt`):
  an attached `docker exec` killed by the shell timeout loses all buffered
  output, and the orphaned process keeps running invisibly.

### Recommendation

Ship the tier as designed (last-resort, per-domain policy entries) — but note
that with the detector fixes, none of the current test walls actually require
neo; its value is for WAFs that defeat headless patchright and for
login-walled / human-solved challenges. The `is_botwall()` fixes benefit all
tiers and are worth merging independently of the neo tier.

## Risks / open questions

- **neo must be running.** If the desktop app is closed, every neo-tier crawl
  fails fast with a clear detail line; http/browser results are unaffected.
  Acceptable for a self-hosted single-user setup (the human who needs these
  pages has the machine on).
- **Profile isolation: none by design.** wellisearch shares one persistent
  profile with the user and other agents — it inherits their logins/cookies,
  its navigations appear in the cockpit/audit under its session name, and it
  must close its own tabs. That sharing is exactly what makes whitelisting work.
- **30s cap vs slow SPAs.** A heavy site may not finish rendering within
  navigation + settle → gate fails → retried next cycle. If common, add longer
  settles per domain via `policy.waits` (currently only the browser tier
  consumes waits, `browser.py:95-96`).
- **Resource cost.** A real headful browser runs for every neo crawl — but
  only on the failure path after http + browser both fail, so it's rare by
  construction. Each crawl visibly opens/closes a background tab in the user's
  actual browser (no focus steal).
- **MCP SDK version drift.** Verified against installed mcp 2.2.0; pin or add a
  transport smoke test if CI resolves a different minor.
- **Why not `browser.read` markdown?** It returns markdown, but the extractor
  pipeline consumes raw HTML and derives signals/flags from structure
  (`extractors/base.py`, price/stock in `signals.py`) — feeding markdown would
  bypass per-site extractors. Raw outerHTML keeps the entire fit/gate pipeline
  unchanged.

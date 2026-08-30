# Engine-gating findings — wellisearch native-crawler decision

- **Date:** 2026-08-30.
- **Purpose:** decide whether we can drop **Crawl4AI** (AGPL-3.0 + its AGPL
  `nodriver` stealth tier) from wellisearch's crawl path and replace it with a
  **permissively-licensed native engine**, without losing the bot-wall results
  we currently rely on.
- **Method:** one-off gating experiment, two runs (retail+CF, then
  news+Amazon-decoy+fit-markdown), same machine, same day. Harness was
  ephemeral (run from `/tmp`, not committed); the method is documented below so
  it can be rebuilt. Results are the consolidated run summaries reported to the
  owner, not per-request raw logs.

---

## TL;DR — Recommendation

**Build the native crawler. Primary engine: `patchright` (Apache-2.0).
Escalation tier: `Scrapling` (BSD-3/Apache-2.0) for hard-CF and metered-paywall
pages. `nodriver`/AGPL is no longer required.**

1. **License-safe and at parity.** On the full retail + Cloudflare/Akamai set,
   `patchright` (Apache-2.0) matched `nodriver` (AGPL) — same targets cleared,
   same content — so we can drop the only AGPL dependency in the crawl path.
2. **Scrapling is a superset, not a replacement.** It is the only engine that
   cleared **Target's search grid** (network-idle wait) and broke **NYT's
   metered paywall** (11.5k chars vs 1.8k) — but it is **2–30× slower** and
   failed Reuters/AP. Use it as a *last-resort escalation tier*, not the default.
3. **patchright beats nodriver for product data.** `nodriver`'s lack of a
   network-idle wait left Amazon's lazy buy-box un-hydrated (7.8–28k chars vs
   30k+ for the others; missed the price on 1 of 4 ASINs). `patchright` is the
   stronger retail engine.
4. **Our fit-markdown wins.** The native trafilatura⊕readability hybrid beat
   Crawl4AI `f=fit` on 3 of 4 retail pages (tie on 1). Crawl4AI `fit` produced
   "session expired" on Amazon, a wrong price on Walmart, and nav/a11y junk on
   The Guardian.
5. **Two engine-independent fixes to ship regardless:** (a) **network-idle
   wait** (product data + Target); (b) **retail extractor rule** — anchor on
   `h1` + buy-box + "About this item" + specs, hard-cut at the first decoy
   ("Frequently bought together" / "protection plan" / "Sponsored" / "See
   buying options"), canonical price = first `$` after the title.

---

## Engines tested

| Label | Engine | License | Notes |
|---|---|---|---|
| **A** | `patchright` (Playwright fork) | **Apache-2.0** | headful, Xvfb, warm per-domain profiles, challenge loop, network-idle wait |
| **B** | `Scrapling` | **BSD-3 / Apache-2.0** | `StealthySession` (patchright under the hood), CF auto-solve, network-idle by default; camoufox dropped in 0.4.15 |
| **C** | `nodriver` | **AGPL-3.0** | current production stealth tier (the dependency we want to drop); no network-idle wait |
| **B0** | Crawl4AI `f=fit` | **AGPL-3.0** | current production path, included as the fit-markdown quality baseline |

All runs were headful under Xvfb (display `:99`) where a browser was used.

---

## Run 1 — retail + bot-walls (3 engines × target matrix)

**Targets:** Walmart item (`Acer ED270RS3`), Amazon items
(`/dp/B08WM3LJQB` Kindle, `/dp/B09LYF2ST7` Echo Dot), Amazon search
(`s?k=usb+c+hub`), Target search (`s?searchTerm=usb+c+hub`), BestBuy search
(`site/search?st=usb+c+hub`), plus the CF/Akamai set we rely on:
**BGG** `geeklist.php?id=1` (Cloudflare), **Stimson** (Akamai),
**xhinker.medium.com** (Cloudflare), **freecodecamp**, **carmax**.

**Result: no engine was hard-blocked anywhere.**

| Outcome | A (patchright) | B (Scrapling) | C (nodriver) |
|---|---|---|---|
| CF/Akamai targets (BGG, Stimson, Medium, FCC, CarMax) | cleared, parity with C | cleared | cleared (baseline) |
| Amazon item + search | **best** — full buy-box, price present | cleared | cleared, **weakest** (lazy buy-box) |
| Walmart item | cleared | cleared | cleared |
| Target search grid | partial | **only engine to clear it** (network-idle) | partial |
| BestBuy search | cleared | cleared | cleared |
| Latency | baseline | **2–30× slower** | baseline |

**Takeaway:** `patchright` (A) matches the AGPL baseline (C) on every
bot-wall target and is *stronger* on Amazon product data. `Scrapling` (B) adds
two wins (Target grid, later NYT) at a 2–30× latency cost.

---

## Run 2 — news + Amazon decoy + fit-markdown quality

### News (NYT / WSJ / Reuters / Guardian / AP)

| Paper | A (patchright) | B (Scrapling) | C (nodriver) |
|---|---|---|---|
| **NYT** (metered) | full article | **broke the metered paywall — 11.5k vs 1.8k chars** | full article |
| **WSJ** | paywalled (content, not engine) | paywalled | paywalled |
| **Reuters** | full | **failed** | full |
| **Guardian** | full | full | full |
| **AP** | full | **failed** | full |

`A ≈ C` on the full news set. `B` is the only paywall breaker but regressed on
Reuters/AP and was 2–30× slower throughout.

### Amazon product — real data, not a decoy

All three engines served the **actual product** (title, price, "About this
item"), not the protection-plan / "Frequently bought together" decoy.

| Signal | A (patchright) | B (Scrapling) | C (nodriver) |
|---|---|---|---|
| Buy-box hydrated | yes | yes | **no — no network-idle → lazy buy-box** |
| Product markdown | **30k+ chars** | 30k+ chars | **7.8–28k chars** |
| Price captured (4 ASINs) | 4/4 | 4/4 | **3/4 (missed 1)** |

**`patchright > nodriver` for product data.** The difference is the network-idle
wait, not the stealth patch.

### Fit-markdown quality: native hybrid (N) vs Crawl4AI `f=fit` (B0)

Same rendered HTML, two extractors. N = native trafilatura⊕readability hybrid
(longer wins) + trim + cap; B0 = Crawl4AI's `f=fit`.

| Page | N (native hybrid) | B0 (Crawl4AI fit) |
|---|---|---|
| Amazon item | **clean buy-box + bullets** | **"session expired" injected** |
| Walmart item | **correct hero price** | **wrong price** |
| Guardian | clean body | **nav / a11y junk** |
| BestBuy | **parity** | parity |

**Score: N wins 3, ties 1, B0 never wins.** The fit-markdown quality gap is an
*extractor* problem, not an *engine* problem — and the native hybrid already
wins it.

---

## Engine-independent lessons (ship in any design)

1. **Network-idle wait is the single biggest product-data lever.** It is what
   separates a hydrated buy-box from a lazy one (Amazon) and a full grid from
   a partial one (Target). Every browser tier must settle on network-idle, not
   just DOM-ready.
2. **Retail extractor rule** (site-specific extractor): anchor on `h1` +
   buy-box + "About this item" + specs; hard-cut at the first decoy marker
   (`Frequently bought together`, `protection plan`, `See buying options`,
   `From the brand`, `Sponsored`, `Customers also viewed`); canonical price =
   the **first `$` after the title**; strip >80-char whitespace runs.
3. **Paywall is content, not engine** (WSJ) — detect and flag it, don't keep
   escalating. NYT metered is the one case where the escalation tier
   (Scrapling) genuinely earns its latency cost.
4. **Bot-wall vs thin-content are different signals.** A challenge page
   (escalate) is not a thin-but-real page (accept + flag). The detector must
   keep them separate so we don't waste a stealth tier on a real short page.

---

## License matrix (the reason this experiment existed)

| Component | License | Usable in MIT wellisearch? |
|---|---|---|
| `patchright` | Apache-2.0 | **Yes** — primary browser engine |
| `Scrapling` | BSD-3 / Apache-2.0 | **Yes** — escalation tier |
| `curl_cffi` | MIT | **Yes** — HTTP fast path |
| `trafilatura` | Apache-2.0 | **Yes** — generic extractor |
| `readability-lxml` | BSD | **Yes** — generic extractor |
| `markdownify` | BSD-3 | **Yes** — HTML→MD |
| `nodriver` | **AGPL-3.0** | **No** — drop (was the blocker) |
| Crawl4AI | **AGPL-3.0** | **No** — drop the service |
| SeleniumBase | MIT umbrella over AGPL-derived CDP code | No — trap |
| undetected-chromedriver | GPL-3.0, stale | No |
| DrissionPage | non-commercial | No |
| rebrowser | no license | No |
| playwright-extra | abandoned | No |
| camoufox | MPL-2.0 (packaging contradiction) | No |

**Net:** a fully permissive (Apache-2.0 / BSD / MIT) engine stack exists that
meets or beats the current AGPL path on every target. That is the decision
this benchmark was gating.

---

## Caveats

- **One-off run, one machine, one day.** Bot-wall countermeasures drift; a
  target that clears today may not next month. Re-run the matrix before a
  release if a target regresses.
- **Results are consolidated run summaries** (as reported), not per-request raw
  logs. The harness was ephemeral and is not committed; rebuild it from the
  method above if you need per-request traces.
- **Latency ratios (2–30×)** are wall-clock on this hardware, not a guarantee;
  they are directionally robust (Scrapling is always the slowest).
- This benchmark decided the **engine** and the **extractor** direction. It did
  **not** measure throughput under concurrent load, memory ceiling, or profile
  persistence — those are Phase 1/2 build concerns (see the design doc).

---

## Decision

- **Adopt:** `patchright` (primary) + `Scrapling` (escalation) + `curl_cffi`
  (HTTP fast path) + `trafilatura`⊕`readability-lxml` (generic extractor).
- **Drop:** Crawl4AI service and the `nodriver` (AGPL) stealth tier.
- **Design:** see `docs/native-crawler-design.md` (general flow + per-site
  extractors, yt-dlp style). Migration is a future branch/PR.

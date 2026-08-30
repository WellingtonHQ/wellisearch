# Engine-gating benchmark (findings)

A one-off **gating experiment** (2026-08-30) that tested three permissive
browser engines plus the current Crawl4AI path against wellisearch's hardest
targets — retail (Amazon / Walmart / Target / BestBuy), bot-walls
(Cloudflare / Akamai: BGG, Stimson, Medium, CarMax), and news (NYT / WSJ /
Reuters / Guardian / AP) — to decide whether we can drop **Crawl4AI (AGPL)**
and its **AGPL `nodriver`** stealth tier in favor of a **permissively-licensed
native crawler**.

This is a *decision* benchmark, not a recurring one: it has no committed
harness (it ran from `/tmp`), so it lives here as **findings only**. The method
is documented in the findings file so it can be rebuilt if a target regresses.

| File | What it is |
|---|---|
| [`ENGINE_GATING_FINDINGS.md`](ENGINE_GATING_FINDINGS.md) | The consolidated results of both runs, the license matrix, and the recommendation. |

**TL;DR:** build the native crawler. Primary engine **`patchright`
(Apache-2.0)** — it matched the AGPL baseline on every bot-wall target and was
*stronger* on Amazon product data. Escalation tier **`Scrapling` (BSD-3)** for
hard-CF / metered-paywall pages (2–30× slower — last resort only). Drop
Crawl4AI + nodriver.

The design that followed (general flow + per-site "extractors", yt-dlp style)
lives in the main docs, not here:

- [`docs/native-crawler-design.md`](../../docs/native-crawler-design.md)
- `docs/images/native-crawler.svg` — the pipeline visual

## Re-running (if a target regresses)

The harness was ephemeral. To rebuild it, you need:

1. The **target matrix** from the findings file (retail + CF/Akamai + news URLs).
2. Three engine harnesses, each headful under Xvfb (display `:99`), warm
   per-domain profile, network-idle wait, and a CF/Akamai challenge loop where
   the engine needs one:
   - **A** — `patchright` (Playwright fork, Apache-2.0)
   - **B** — `Scrapling` `StealthySession` (BSD-3)
   - **C** — `nodriver` (AGPL) — kept only as the reference baseline
3. A **fit-markdown A/B**: render the same HTML once, then extract with
   (N) `trafilatura`⊕`readability-lxml` hybrid + trim + cap vs (B0) Crawl4AI
   `f=fit`, and compare length + key signals (price, title, decoy cut).
4. Record per URL: engine, cleared/challenged, markdown char count, price
   present (retail), and any decoy contamination.

Compare against the tables in `ENGINE_GATING_FINDINGS.md` to detect drift.

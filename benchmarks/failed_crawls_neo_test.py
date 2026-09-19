"""Neo value test on real-world failed crawls (from crawl_log).

Takes URLs that genuinely hit bot-walls in production (not 404/DNS/server
errors) and runs each through a forced ("http", "browser", "neo") ladder with
the fixed is_botwall() detector. Answers: which tier resolves each real
failure, and does neo add value beyond http+browser?

Run inside the app container:

    docker cp benchmarks/failed_crawls_neo_test.py wellisearch:/tmp/ && \
        docker exec -d wellisearch sh -c "python /tmp/failed_crawls_neo_test.py > /tmp/failed_out.txt 2>&1"
"""
from __future__ import annotations

import asyncio
import json
import time

URLS = [
    # CF challenge-platform walls
    "https://saveonphone.com/compare/tmobile-experience-more-vs-beyond/",
    "https://en.ittrip.xyz/category/windows",
    "https://www.ox.security/blog/rce-in-react-server-components/",
    "https://shahjerry33.medium.com/http-request-smuggling-via-content-length-and-transfer-encoding-desync-cl-te-i-am-not-a-83e432f9a926",
    # CF just-a-moment / turnstile
    "https://www.scamadviser.com/check-website/leakshaven.com",
    "https://csnet.co.uk/",
    # "javascript is disabled" on both tiers (suspected noscript false positive)
    "https://f95zone.to/threads/miohonda-onlyfans.184700/",
    "https://s2f.kytta.dev/?text=https%3A%2F%2Fdev",
    # "access denied" walls
    "https://docs.vultr.com/support/platform/billing/how-are-vultr-load-balancers-priced",
    "https://oneuptime.com/blog/post/2026-01-24-fix-privilege-escalation-vulnerabilities/view",
    # hard 403s (Akamai-class)
    "https://www.costco.com/p/-/polymaker-pla-4x1kg-fun-color-175mm-3d-printer-filament-colorful-bundle/4000391995",
    "https://device.report/manual/15897138",
]


def _instrument(timings: list[dict], diag: list[dict]) -> dict:
    from wellisearch.crawl import tiers

    saved = {}
    for name, tier in tiers._REGISTRY.items():
        orig = tier.fetch

        async def timed(url: str, p: object, _name: str = name, _orig=orig) -> object:
            t0 = time.monotonic()
            try:
                r = await _orig(url, p)
            finally:
                timings.append({"tier": _name, "url": url, "ms": int((time.monotonic() - t0) * 1000)})
            diag.append({
                "tier": _name,
                "title": getattr(r, "title", None),
                "status": getattr(r, "status", None),
                "html_head": (getattr(r, "html", "") or "")[:200],
            })
            return r

        saved[name] = orig
        tier.fetch = timed  # type: ignore[method-assign]
    return saved


async def main() -> None:
    from wellisearch.config import get_settings
    from wellisearch.crawl import engine, tiers
    from wellisearch.crawl.lane import CF, set_lane
    from wellisearch.crawl.policy import Policy

    s = get_settings()
    s.CRAWL_NEO_TIER = True
    # Cap CF-lane page-load timeout so walled URLs fail fast at the browser
    # tier (a challenge headless can solve resolves in seconds; 60s is plenty).
    s.CRAWL_CF_TIMEOUT_S = 60

    # Force the full ladder for every domain (production policies lack neo).
    engine.match = lambda url: Policy(  # type: ignore[method-assign]
        "test-neo", ("http", "browser", "neo"), ("settle",), (), "shared"
    )

    timings: list[dict] = []
    diag: list[dict] = []
    saved = _instrument(timings, diag)
    records = []
    try:
        for url in URLS:
            token = set_lane(CF)
            t0 = time.monotonic()
            try:
                r = await engine.crawl(url)
                rec = {
                    "url": url,
                    "ok": r.ok,
                    "tier": r.tier,
                    "ms": int((time.monotonic() - t0) * 1000),
                    "md_chars": len(r.md),
                    "attempts": r.attempts,
                }
            except Exception as e:  # noqa: BLE001 — record and keep going
                rec = {"url": url, "ok": False, "tier": "none", "ms": int((time.monotonic() - t0) * 1000),
                       "md_chars": 0, "attempts": [{"error": f"{type(e).__name__}: {e}"}]}
            finally:
                from wellisearch.crawl.lane import reset_lane

                reset_lane(token)
            records.append(rec)
            print(f"{'ok ' if rec['ok'] else 'FAIL'} tier={rec['tier']} ms={rec['ms']} md={rec['md_chars']} {url}", flush=True)
    finally:
        for name, orig in saved.items():
            tiers._REGISTRY[name].fetch = orig  # type: ignore[union-attr]
        await tiers.aclose_all()

    print("\n=== json ===", flush=True)
    print(json.dumps({"records": records, "timings": timings, "diag": diag}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

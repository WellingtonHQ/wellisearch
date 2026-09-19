"""Production-ladder test on the two hard walls, with the fixed botwall detector.

No tier patching: uses the real policy tiers ("http", "browser", "neo") exactly
as production would with CRAWL_NEO_TIER=True. Answers whether the headless CF
browser tier resolves these sites once is_botwall() no longer false-positives,
or whether neo (persistent-profile browser) is required.

Run inside the app container:

    docker cp benchmarks/production_ladder_test.py wellisearch:/tmp/ && \
        docker exec -d wellisearch sh -c "python /tmp/production_ladder_test.py > /tmp/ladder_out.txt 2>&1"
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

URLS = [
    "https://xdaforums.com/m/wellingtonhq.9307974/about",
    "https://www.spectrumbusiness.net/",
]
if len(sys.argv) > 1:
    URLS = [sys.argv[1]]


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

    s = get_settings()
    s.CRAWL_NEO_TIER = True

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

"""Targeted test: neo-only ladder on the two hard walls.

Proves whether neo resolves them when it gets a clean shot (no preceding CF-tier
hammering of the shared outbound IP, no plain-http request right before). The
ladder is ("neo",) only — the http tier is skipped because a non-JS client
request may itself flag the egress IP.

Pass 1 crawls both URLs; pass 2 immediately re-crawls xdaforums to test whether
a second browser visit within seconds also gets walled (visit-rate walling).

Run inside the app container:

    docker cp benchmarks/neo_first_test.py wellisearch:/tmp/ && \
        docker compose exec wellisearch python /tmp/neo_first_test.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time

URLS = [
    "https://xdaforums.com/m/wellingtonhq.9307974/about",
    "https://www.spectrumbusiness.net/",
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
                "html_head": (getattr(r, "html", "") or "")[:300],
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

    # Neo-only ladder for the two walls (no http, no browser tier).
    orig_match = engine.match

    def patched(url: str):
        p = orig_match(url)
        if "xdaforums.com" in url or "spectrumbusiness.net" in url:
            return dataclasses.replace(p, tiers=("neo",))
        return p

    engine.match = patched  # type: ignore[method-assign]

    timings: list[dict] = []
    diag: list[dict] = []
    saved = _instrument(timings, diag)
    records = []
    try:
        for pass_no in (1, 2):
            urls = URLS if pass_no == 1 else [URLS[0]]
            print(f"=== pass {pass_no} ===", flush=True)
            for url in urls:
                token = set_lane(CF)
                t0 = time.monotonic()
                try:
                    r = await engine.crawl(url)
                    rec = {
                        "pass": pass_no,
                        "url": url,
                        "ok": r.ok,
                        "tier": r.tier,
                        "ms": int((time.monotonic() - t0) * 1000),
                        "md_chars": len(r.md),
                        "attempts": r.attempts,
                    }
                except Exception as e:  # noqa: BLE001 — record and keep going
                    rec = {"pass": pass_no, "url": url, "ok": False, "tier": "none",
                           "ms": int((time.monotonic() - t0) * 1000),
                           "md_chars": 0, "attempts": [{"error": f"{type(e).__name__}: {e}"}]}
                finally:
                    from wellisearch.crawl.lane import reset_lane

                    reset_lane(token)
                records.append(rec)
                print(f"pass={pass_no} {'ok ' if rec['ok'] else 'FAIL'} tier={rec['tier']} "
                      f"ms={rec['ms']} md={rec['md_chars']} {url}", flush=True)
    finally:
        for name, orig in saved.items():
            tiers._REGISTRY[name].fetch = orig  # type: ignore[union-attr]
        await tiers.aclose_all()

    print("\n=== json ===", flush=True)
    print(json.dumps({"records": records, "timings": timings, "diag": diag}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

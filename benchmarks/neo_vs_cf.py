"""Experiment: neo tier vs CF browser tier on bot-walled sites (docs/browseros-tier.md).

Runs the full crawl ladder over a fixed URL set twice — baseline (CRAWL_NEO_TIER
off) and neo-on (on) — in the CF lane, with production settings otherwise.
Records per-URL outcome plus per-tier timing for each phase. Run inside the app
container:

    docker cp benchmarks/neo_vs_cf.py wellisearch:/tmp/ && \
        docker compose exec wellisearch python /tmp/neo_vs_cf.py
"""
from __future__ import annotations

import asyncio
import json
import time

# Hard walls (the neo tier's target) + browser-tier cases (must not regress).
URLS = [
    "https://www.reddit.com/r/Qwen_AI/comments/1vqzl5l/qwen3827b_at_160k_context_on_a_single_rtx_4090/",
    "https://xdaforums.com/m/wellingtonhq.9307974/about",
    "https://www.spectrumbusiness.net/",
    "https://gpupoet.com/gpu/shop/nvidia-geforce-rtx-3090-ti",
    (
        "https://microsoft.eightfold.ai/careers/job?domain=microsoft.com"
        "&profile_type=candidate&pid=1970393556941428&location=United+States"
        "&filter_include_remote=1"
    ),
]


def _instrument(timings: list[dict]) -> dict:
    """Wrap every registered tier's fetch to record per-tier wall time."""
    from wellisearch.crawl import tiers

    saved = {}
    for name, tier in tiers._REGISTRY.items():
        orig = tier.fetch

        async def timed(url: str, p: object, _name: str = name, _orig=orig) -> object:
            t0 = time.monotonic()
            try:
                return await _orig(url, p)
            finally:
                timings.append(
                    {"tier": _name, "url": url, "ms": int((time.monotonic() - t0) * 1000)}
                )

        saved[name] = orig
        tier.fetch = timed  # type: ignore[method-assign]
    return saved


def _restore(saved: dict) -> None:
    from wellisearch.crawl import tiers

    for name, orig in saved.items():
        tiers._REGISTRY[name].fetch = orig  # type: ignore[union-attr]


async def _crawl_all(phase: str) -> list[dict]:
    """Crawl every URL once in the CF lane; return per-URL records."""
    from wellisearch.config import get_settings
    from wellisearch.crawl import engine, tiers
    from wellisearch.crawl.lane import CF, set_lane

    s = get_settings()
    s.CRAWL_NEO_TIER = phase == "neo"
    timings: list[dict] = []
    saved = _instrument(timings)
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
                rec = {
                    "url": url,
                    "ok": False,
                    "tier": "none",
                    "ms": int((time.monotonic() - t0) * 1000),
                    "md_chars": 0,
                    "attempts": [{"error": f"{type(e).__name__}: {e}"}],
                }
            finally:
                from wellisearch.crawl.lane import reset_lane

                reset_lane(token)
            records.append(rec)
            print(
                f"[{phase}] {'ok ' if rec['ok'] else 'FAIL'} "
                f"tier={rec['tier']} ms={rec['ms']} md={rec['md_chars']} {url}",
                flush=True,
            )
    finally:
        _restore(saved)
    return {"records": records, "timings": timings}


async def _run_all_phases() -> dict:
    """Both phases in one event loop (loop-bound pool/lock state must persist)."""
    from wellisearch.crawl.tiers import aclose_all

    out = {}
    try:
        for phase in ("baseline", "neo"):
            print(f"=== phase {phase} ===", flush=True)
            t0 = time.monotonic()
            out[phase] = await _crawl_all(phase)
            out[phase]["wall_ms"] = int((time.monotonic() - t0) * 1000)
    finally:
        await aclose_all()
    return out


def main() -> None:
    out = asyncio.run(_run_all_phases())

    # Side-by-side summary.
    print("\n=== summary ===", flush=True)
    base = {r["url"]: r for r in out["baseline"]["records"]}
    neo = {r["url"]: r for r in out["neo"]["records"]}
    for url in URLS:
        b, n = base[url], neo[url]
        print(
            f"{url}\n  baseline: ok={b['ok']} tier={b['tier']} ms={b['ms']} md={b['md_chars']}"
            f"\n  neo     : ok={n['ok']} tier={n['tier']} ms={n['ms']} md={n['md_chars']}",
            flush=True,
        )

    print("\n=== json ===", flush=True)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

"""crawl() core loop: policy → tier ladder → extractor → gate (design §5.1).

Escalates on bot-wall, fetch errors, and extractor Escalate; returns the
best partial result (ok=False) when every tier fails. Never raises.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import get_settings
from . import botwall, extractors, tiers
from .policy import match
from .results import CrawlResult, Escalate, Fitted

log = logging.getLogger("wellisearch.crawl.engine")


async def crawl(url: str) -> CrawlResult:
    """Crawl one URL through its policy's tier ladder.

    Tries each tier in order; a bot-wall, fetch error, or gate failure
    moves to the next tier (an Escalate jumps to the named tier). Returns
    ok=True on the first accepted fit, else the best partial (or empty).
    """
    p = match(url)
    ex = extractors.for_url(url)
    attempts: list[dict] = []
    best: Fitted | None = None
    start = time.monotonic()
    log.info("crawl %s (policy=%s tiers=%s)", url, p.name, ",".join(p.tiers))
    i = 0
    while i < len(p.tiers):
        name = p.tiers[i]
        tier = tiers.by_name(name)
        if tier is None:
            attempts.append({"tier": name, "error": "disabled"})
            i += 1
            continue
        try:
            timeout = (
                get_settings().CRAWL_STEALTH_TIMEOUT_S
                if name == "stealth"
                else get_settings().CRAWL_TIMEOUT_S
            )
            r = await asyncio.wait_for(tier.fetch(url, p), timeout=timeout)
        except Exception as e:
            attempts.append({"tier": name, "error": f"{type(e).__name__}: {e}"})
            i += 1
            continue
        marker = botwall.is_botwall(r.html, r.status)
        if marker is not None:
            attempts.append({"tier": name, "error": f"botwall: {marker}"})
            i += 1
            continue
        try:
            f = ex.fit(r)
        except Escalate as esc:
            attempts.append({"tier": name, "error": f"escalate: {esc.tier}"})
            if esc.tier in p.tiers and p.tiers.index(esc.tier) > i:
                i = p.tiers.index(esc.tier)
            else:
                i += 1
            continue
        if ex.accept(f):
            ms = int((time.monotonic() - start) * 1000)
            log.info("crawl %s ok (tier=%s ms=%d)", url, name, ms)
            return CrawlResult(
                ok=True,
                title=f.title or r.title,
                md=f.md,
                tier=name,
                ms=ms,
                attempts=attempts,
                flags=f.flags,
            )
        best = f
        attempts.append({"tier": name, "error": "gate failed", "md_chars": len(f.md)})
        i += 1
    ms = int((time.monotonic() - start) * 1000)
    log.info("crawl %s failed (tier=none ms=%d)", url, ms)
    if best is not None:
        return CrawlResult(
            ok=False,
            title=best.title,
            md=best.md,
            tier="none",
            ms=ms,
            attempts=attempts,
            flags=best.flags,
        )
    return CrawlResult(ok=False, title=None, md="", tier="none", ms=ms, attempts=attempts)

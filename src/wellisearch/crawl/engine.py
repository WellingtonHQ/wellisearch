"""crawl() core loop: policy → tier ladder → extractor → gate (design §5.1).

Escalates on bot-wall, fetch errors, and extractor Escalate; returns the
best partial result (ok=False) when every tier fails. ChallengeDetected
propagates to the caller (it must be routed to the CF lane, not swallowed).
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import get_settings
from . import botwall, extractors, tiers
from .lane import CF, get_lane
from .policy import Policy, match
from .results import ChallengeDetected, CrawlResult, Escalate, Fitted

log = logging.getLogger("wellisearch.crawl.engine")


def _flat_backstop(name: str) -> float:
    """Fallback per-tier backstop for tiers that don't report a worst case.

    Generous on purpose: each tier manages its own goto/challenge timeouts
    internally; this only guards against a hang. The CF-lane browser tier needs
    goto (CF timeout) + the full challenge loop (CF timeout budget), so it gets
    2× the CF timeout.
    """
    s = get_settings()
    if name == "stealth":
        return float(s.CRAWL_STEALTH_TIMEOUT_S)
    if name == "browser" and get_lane() == CF:
        return float(s.CRAWL_CF_TIMEOUT_S) * 2
    return float(s.CRAWL_TIMEOUT_S)


def _tier_backstop(
    tier: "tiers.Tier",
    name: str,
    p: "Policy",
) -> float:
    """Per-tier asyncio.wait_for backstop, derived from the tier's worst case.

    Prefers the tier's own worst_case_s() (goto + settle + network_idle +
    challenge loop + recovery), which is accurate per tier. Falls back to the
    flat per-name budget for tiers that don't report one (e.g. test fakes).
    """
    fn = getattr(tier, "worst_case_s", None)
    if callable(fn):
        return float(fn(p))
    return _flat_backstop(name)


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
            r = await asyncio.wait_for(tier.fetch(url, p), timeout=_tier_backstop(tier, name, p))
        except ChallengeDetected:
            # Fast-lane probe hit a bot-wall: route to the CF lane rather than
            # trying the next tier (the challenge needs the CF lane's loop).
            raise
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

"""T1 Browser tier: patchright headful (Xvfb) with a CF challenge loop (design §3.1).

Port of the proven nodriver worker's battle-tested patterns: the turnstile
checkbox click (closed shadow root → computed coordinates), the bounded
challenge poll loop, and the Walmart soft-404 search recovery.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, urlparse

from ...config import get_settings
from ..botwall import is_botwall
from ..lane import CF, get_lane
from ..policy import Policy
from ..pool import get_cf_pool, get_pool
from ..results import ChallengeDetected, Rendered
from ..wait import NETWORK_IDLE_TIMEOUT_S, network_idle, settle
from . import register

if TYPE_CHECKING:
    from patchright.async_api import Page

log = logging.getLogger("wellisearch.crawl.tiers.browser")

# Wait after each turnstile click before re-reading the page (spec: ~2s).
CHALLENGE_POLL_MS = 2000
# Let the walmart search results render before scraping the item links.
WALMART_SEARCH_SETTLE_MS = 4000
# Checkbox offset from the turnstile widget row's left edge (proven worker).
TURNSTILE_OFFSET_X = 24
# Brief pause between the mouse move and the click (proven worker uses 0.4s).
MOUSE_MOVE_PAUSE_MS = 400
# Minimum slug tokens to trust a walmart search recovery.
MIN_SLUG_TOKENS = 3
# Minimum slug-token overlap for a walmart search match to count.
MIN_SLUG_TOKEN_OVERLAP = 3
# Max slug tokens used to build the walmart search query.
MAX_SLUG_QUERY_TOKENS = 8

# Walmart serves missing items as a 200 soft-404 shell. Anchor to the visible
# <h1>: the strings also appear in walmart's site-wide JS bundle on good pages.
_WALMART_404_RE = re.compile(r">we couldn.{0,2}t find this page</h1>", re.IGNORECASE)


class BrowserTier:
    """Patchright headful browser: warm profile, challenge loop, network-idle."""

    name = "browser"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Fetch one URL in a pooled browser; always closes the page + releases.

        The CF lane uses its own pool (get_cf_pool) so a challenge crawl holding
        a context for the whole turnstile loop never starves the fast lane.
        """
        key = _profile_key(url, p)
        pool = get_cf_pool() if get_lane() == CF else get_pool()
        ctx = await pool.acquire(key)
        try:
            page = await ctx.new_page()
            try:
                return await self._crawl(page, url, p)
            finally:
                await _safe_close(page)
        finally:
            await pool.release(ctx)

    async def _crawl(
        self,
        page: Page,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Drive the page: goto, settle, challenge handling, walmart recovery."""
        s = get_settings()
        is_cf = get_lane() == CF
        timeout_s = s.CRAWL_CF_TIMEOUT_S if is_cf else s.CRAWL_TIMEOUT_S
        start = time.monotonic()
        resp = await page.goto(
            url, wait_until="domcontentloaded", timeout=timeout_s * 1000
        )
        status = resp.status if resp is not None else 200
        await settle(page)
        if "network_idle" in p.waits:
            await network_idle(page)
        html = await page.content()

        if is_cf:
            # CF lane: run the full turnstile loop with the high budget.
            html = await self._resolve_challenge(page, url, html, status, budget=timeout_s)
        else:
            # Fast lane: probe only — a bot-wall means route to the CF lane
            # instead of spending the fast lane's time on the challenge loop.
            if is_botwall(html, status) is not None:
                raise ChallengeDetected(url)

        notes: str | None = None
        if _is_walmart_item_404(url, html):
            log.info("walmart item page is a 404 shell — recovering via search: %s", url)
            recovered = await _walmart_recover_item_url(page, url)
            if recovered is not None:
                log.info("recovered walmart item: %s", recovered)
                resp2 = await page.goto(
                    recovered, wait_until="domcontentloaded", timeout=timeout_s * 1000
                )
                status = resp2.status if resp2 is not None else status
                await settle(page)
                html = await page.content()
            if _WALMART_404_RE.search(html) is not None:
                notes = "walmart 404 shell"

        title = await _page_title(page)
        ms = int((time.monotonic() - start) * 1000)
        return Rendered(html=html, title=title, status=status, ms=ms, engine="browser", notes=notes)

    async def _resolve_challenge(
        self,
        page: Page,
        url: str,
        html: str,
        status: int,
        budget: int | None = None,
    ) -> str:
        """Click the turnstile checkbox until clean or the budget is exhausted."""
        budget = budget if budget is not None else get_settings().CRAWL_CHALLENGE_BUDGET_S
        start = time.monotonic()
        round_no = 0
        while is_botwall(html, status) is not None and (time.monotonic() - start) < budget:
            round_no += 1
            log.info("challenge present (poll %d, budget %ds) — clicking turnstile", round_no, budget)
            await _click_turnstile_checkbox(page)
            await page.wait_for_timeout(CHALLENGE_POLL_MS)
            html = await page.content()
        if is_botwall(html, status) is not None:
            log.warning("challenge still present after %ds budget for %s", budget, url)
        return html

    def worst_case_s(self, p: Policy) -> float:
        """Worst-case budget for the engine's wait_for backstop.

        Covers the full work the tier may do: goto + settle (+ network_idle),
        the CF challenge loop (CF lane only), and the walmart soft-404 search
        recovery (search goto + settle + item goto + settle).
        """
        s = get_settings()
        is_cf = get_lane() == CF
        timeout_s = s.CRAWL_CF_TIMEOUT_S if is_cf else s.CRAWL_TIMEOUT_S
        budget = timeout_s + s.CRAWL_SETTLE_S
        if "network_idle" in p.waits:
            budget += NETWORK_IDLE_TIMEOUT_S
        if is_cf:
            budget += timeout_s  # full challenge loop budget
        # Walmart soft-404 search recovery (worst case): search goto + settle +
        # item goto + settle.
        budget += timeout_s + WALMART_SEARCH_SETTLE_MS / 1000 + timeout_s + s.CRAWL_SETTLE_S
        return budget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _safe_close(page: Page) -> None:
    """Close the page; never raises (runs in a finally, even on cancellation)."""
    try:
        await page.close()
    except Exception:
        pass


async def _page_title(page: Page) -> str | None:
    """Best-effort page title; None on any failure."""
    try:
        t = await page.title()
        if t and t.strip():
            return t.strip()
    except Exception:
        pass
    return None


def _profile_key(url: str, p: Policy) -> str:
    """Profile key: the URL's host (sans www.) when dedicated, else 'shared'."""
    if p.profile != "dedicated":
        return "shared"
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "shared"


async def _click_turnstile_checkbox(page: Page) -> bool:
    """Best-effort click on the Cloudflare turnstile checkbox.

    The widget renders inside a CLOSED shadow root: the checkbox is invisible
    to document.querySelectorAll. We locate the widget's container row (a wide,
    medium-height div near mid-page) and click the checkbox at its fixed offset
    (left edge, vertical centre). Ported from the proven worker.
    """
    try:
        raw = await page.evaluate(
            """(() => {
              const els = Array.from(document.querySelectorAll('div'));
              for (const el of els) {
                const r = el.getBoundingClientRect();
                if (r.width > 600 && r.width < 1000 && r.height > 55 && r.height < 85 && r.y > 100) {
                  return JSON.stringify([Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]);
                }
              }
              return null;
            })()"""
        )
    except Exception as e:
        log.warning("turnstile box lookup failed: %s", e)
        return False
    box = None
    if isinstance(raw, str):
        try:
            box = json.loads(raw)
        except (ValueError, TypeError):
            box = None
    if not isinstance(box, list) or len(box) != 4:
        return False
    x = box[0] + TURNSTILE_OFFSET_X
    y = box[1] + box[3] // 2
    try:
        await page.mouse.move(x, y)
        await page.wait_for_timeout(MOUSE_MOVE_PAUSE_MS)
        await page.mouse.click(x, y)
        log.info("clicked turnstile checkbox at (%d, %d) [row=%s]", x, y, box)
        return True
    except Exception as e:
        log.warning("turnstile click failed: %s", e)
        return False


def _is_walmart_item_url(url: str) -> bool:
    """True when the URL is a walmart.com item page (/ip/...)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.netloc.lower() in ("walmart.com", "www.walmart.com")
        and parsed.path.startswith("/ip/")
    )


def _is_walmart_item_404(url: str, html: str) -> bool:
    """True when a walmart item URL served the 200 soft-404 shell."""
    if not _is_walmart_item_url(url):
        return False
    return _WALMART_404_RE.search(html) is not None


def _slug_tokens(slug: str) -> set[str]:
    """Slug split into lowercase tokens longer than one character."""
    return {t for t in re.split(r"[-_]+", slug.lower()) if len(t) > 1}


async def _walmart_recover_item_url(page: Page, url: str) -> str | None:
    """Find the canonical walmart item URL for a soft-404 item page.

    Searches walmart with the slug's keywords and returns the best-matching
    item link whose slug shares enough tokens with the original slug. Ported
    from the proven worker. Returns None when no plausible match is found.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return None
    orig_tokens = _slug_tokens(parts[1])
    if len(orig_tokens) < MIN_SLUG_TOKENS:
        return None
    query = " ".join(parts[1].split("-")[:MAX_SLUG_QUERY_TOKENS])
    search_url = "https://www.walmart.com/search?q=" + quote_plus(query)
    await page.goto(search_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(WALMART_SEARCH_SETTLE_MS)
    raw = await page.evaluate(
        """(() => {
          const seen = new Set();
          const out = [];
          document.querySelectorAll('a[href*="/ip/"]').forEach((a) => {
            const m = (a.getAttribute('href') || '').match(/\\/ip\\/([^/?#]+)\\/([0-9]+)/);
            if (m && !seen.has(m[1] + '/' + m[2])) { seen.add(m[1] + '/' + m[2]); out.push(m[1] + '/' + m[2]); }
          });
          return JSON.stringify(out);
        })()"""
    )
    if not isinstance(raw, str):
        return None
    try:
        candidates = json.loads(raw)
    except ValueError:
        return None
    best: str | None = None
    best_score = 0.0
    for cand in candidates:
        cand_tokens = _slug_tokens(cand.split("/")[0])
        if not cand_tokens:
            continue
        overlap = len(orig_tokens & cand_tokens)
        if overlap < MIN_SLUG_TOKEN_OVERLAP:
            continue
        score = overlap / min(len(orig_tokens), len(cand_tokens))
        if score > best_score:
            best, best_score = cand, score
    return f"https://www.walmart.com/ip/{best}" if best else None


register(BrowserTier())

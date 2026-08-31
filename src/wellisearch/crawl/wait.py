"""Wait helpers for the browser tier: settle + network-idle (design §3.1)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..config import get_settings

if TYPE_CHECKING:
    from patchright.async_api import Page

log = logging.getLogger("wellisearch.crawl.wait")

# Default network-idle window; a page that never goes idle just logs a warning.
NETWORK_IDLE_TIMEOUT_MS = 15000


async def settle(page: Page) -> None:
    """Wait CRAWL_SETTLE_S for post-load JS to render."""
    await page.wait_for_timeout(int(get_settings().CRAWL_SETTLE_S * 1000))


async def network_idle(page: Page, timeout_ms: int = NETWORK_IDLE_TIMEOUT_MS) -> None:
    """Best-effort network-idle wait; logs a warning on timeout, never raises."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception as e:
        log.warning("network_idle wait timed out: %s", e)

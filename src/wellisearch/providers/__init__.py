"""Provider gateway: ordered failover + monthly quota ledger + normalization.

Order comes from SEARCH_PROVIDERS (default tavily → brave → searxng).
First provider that (a) is enabled, (b) is configured, (c) is not quota-
exhausted, and (d) returns a non-empty result set — serves. Every failure
is captured per provider (last_error in provider_state, visible in the
dashboard + /api/providers). A 200 with zero results counts as a soft
failure so the gateway keeps failing over to something that returns pages.
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx

from ..config import get_settings
from ..db import db
from .base import Provider, ProviderError, Result
from .brave import Brave
from .searxng import SearxNG
from .tavily import Tavily

log = logging.getLogger("wellisearch.providers")

REGISTRY: dict[str, type[Provider]] = {
    "tavily": Tavily,
    "brave": Brave,
    "searxng": SearxNG,
}


class GatewayExhausted(Exception):
    """All providers failed. Carries the per-provider error chain."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        summary = "; ".join(f"{e['provider']}: {e['error']}" for e in errors) or "no providers configured"
        super().__init__(summary)


class Gateway:
    def __init__(self) -> None:
        self.s = get_settings()
        self._client = httpx.AsyncClient(timeout=self.s.PROVIDER_TIMEOUT_S)
        self._providers: list[Provider] = []
        for name in self.s.provider_order:
            cls = REGISTRY.get(name)
            if cls is None:
                log.warning("unknown provider %r in SEARCH_PROVIDERS — skipped", name)
                continue
            self._providers.append(cls(self.s, self._client))

    # ------------------------------------------------------------ queries

    @property
    def providers(self) -> list[Provider]:
        return list(self._providers)

    async def search(self, query: str, num: int) -> tuple[list[Result], str, list[dict]]:
        """Try providers in order. Returns (results, provider_name, error_chain).

        Raises GatewayExhausted if none can serve.
        """
        errors: list[dict] = []
        for p in self._providers:
            if not await self._provider_available(p, errors):
                continue
            import time

            t0 = time.monotonic()
            try:
                results = await p.search(query, num)
            except ProviderError as e:
                log.warning("provider %s failed: %s", p.name, e)
                await db.set_provider_state(p.name, last_error=str(e))
                errors.append({"provider": p.name, "error": str(e), "status": e.status})
                continue
            except Exception as e:  # defensive: never leak a provider bug to the LLM
                log.exception("provider %s crashed", p.name)
                await db.set_provider_state(p.name, last_error=f"crash: {e!r}"[:500])
                errors.append({"provider": p.name, "error": f"crash: {e!r}"[:200]})
                continue

            ms = int((time.monotonic() - t0) * 1000)
            if not results:
                await db.set_provider_state(p.name, last_error="empty result set")
                errors.append({"provider": p.name, "error": "empty result set", "ms": ms})
                continue

            # success: quota ledger + state
            await db.quota_bump(p.name)
            await db.set_provider_state(p.name, last_served=dt.datetime.now(dt.timezone.utc), last_error=None)
            log.info("provider %s served %r in %d ms (%d results)", p.name, query, ms, len(results))
            return results, p.name, errors

        raise GatewayExhausted(errors)

    async def _provider_available(self, p: Provider, errors: list[dict]) -> bool:
        state = await db.get_provider_state(p.name)
        if state and not state["enabled"]:
            errors.append({"provider": p.name, "error": "disabled (runtime toggle)"})
            return False
        if not p.configured:
            errors.append({"provider": p.name, "error": "not configured (missing key/url)"})
            return False
        used, limit = await db.quota_used_limit(p.name)
        if limit is not None and used >= limit:
            await db.set_provider_state(p.name, last_error=f"quota exhausted ({used}/{limit})")
            errors.append({"provider": p.name, "error": f"quota exhausted ({used}/{limit})"})
            return False
        return True

    async def close(self) -> None:
        await self._client.aclose()


_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        _gateway = Gateway()
    return _gateway


async def shutdown_gateway() -> None:
    global _gateway
    if _gateway is not None:
        await _gateway.close()
        _gateway = None

"""Provider gateway: ordered failover + monthly quota ledger + normalization.

Failover order is resolved per search: the runtime override
(provider_state.sort_order, set from the dashboard or via PUT /api/providers/order)
when present, else the env default (SEARCH_PROVIDERS). First provider that
(a) is enabled, (b) is configured, (c) is not quota-exhausted, and (d)
returns a non-empty result set — serves. Every failure is captured per
provider (last_error in provider_state, visible in the dashboard +
/api/providers). A 200 with zero results counts as a soft failure so the
gateway keeps failing over to something that returns pages.
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx

from ..config import get_settings
from ..db import db
from .base import Provider, ProviderError, Result
from .brave import Brave
from .exa import Exa
from .tavily import Tavily
from .youcom import YouCom

log = logging.getLogger("wellisearch.providers")

REGISTRY: dict[str, type[Provider]] = {
    "tavily": Tavily,
    "brave": Brave,
    "exa": Exa,
    "youcom": YouCom,
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
        self._by_name: dict[str, Provider] = {}
        self._env_order: list[str] = []  # pool + default order from SEARCH_PROVIDERS
        for name in self.s.provider_order:
            cls = REGISTRY.get(name)
            if cls is None:
                log.warning("unknown provider %r in SEARCH_PROVIDERS — skipped", name)
                continue
            p = cls(self.s, self._client)
            self._by_name[p.name] = p
            self._env_order.append(p.name)

    async def ordered_providers(self) -> list[Provider]:
        """The provider pool in the current failover order.

        The runtime override (provider_state.sort_order, dashboard /
        PUT /api/providers/order) wins when set; otherwise the env default
        order (SEARCH_PROVIDERS) applies. Unknown names in the override are
        dropped, and env providers missing from it keep their env position.
        """
        order = await db.get_provider_order()
        if order is None:
            return [self._by_name[n] for n in self._env_order]
        out: list[Provider] = []
        for name in order:
            p = self._by_name.get(name)
            if p is not None:
                out.append(p)
        # defensive: anything in the pool but not the override keeps its env position
        for name in self._env_order:
            if name not in {q.name for q in out}:
                out.append(self._by_name[name])
        return out

    async def order_names(self) -> tuple[list[str], str]:
        """(order, source) where source is "runtime" or "env"."""
        order = await db.get_provider_order()
        if order is None:
            return list(self._env_order), "env"
        return [n for n in order if n in self._by_name] or list(self._env_order), "runtime"

    async def _ev(
        self,
        message: str,
        info: dict | None = None,
    ) -> None:
        """Best-effort event logging (dashboard log view)."""
        try:
            await db.log_event(message, info)
        except Exception as e:
            log.warning("event logging failed: %s", e)

    # ------------------------------------------------------------ queries

    async def search(
        self,
        query: str,
        num: int,
    ) -> tuple[list[Result], str, list[dict]]:
        """Try providers in the current failover order (see ordered_providers).

        Returns (results, provider_name, error_chain).
        Raises GatewayExhausted if none can serve.
        """
        errors: list[dict] = []
        for p in await self.ordered_providers():
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
                await self._ev(f"provider {p.name} failed", {"error": str(e)[:300], "status": e.status})
                continue
            except Exception as e:  # defensive: never leak a provider bug to the LLM
                log.exception("provider %s crashed", p.name)
                await db.set_provider_state(p.name, last_error=f"crash: {e!r}"[:500])
                errors.append({"provider": p.name, "error": f"crash: {e!r}"[:200]})
                await self._ev(f"provider {p.name} crashed", {"error": repr(e)[:300]})
                continue

            ms = int((time.monotonic() - t0) * 1000)
            if not results:
                await db.set_provider_state(p.name, last_error="empty result set")
                errors.append({"provider": p.name, "error": "empty result set", "ms": ms})
                await self._ev(f"provider {p.name} returned no results", {"query": query[:200], "ms": ms})
                continue

            # success: quota ledger + state
            await db.quota_bump(p.name)
            await db.set_provider_state(p.name, last_served=dt.datetime.now(dt.timezone.utc), last_error=None)
            log.info("provider %s served %r in %d ms (%d results)", p.name, query, ms, len(results))
            await self._ev(
                f"provider {p.name} served search",
                {"query": query[:200], "ms": ms, "results": len(results),
                 "skipped": [e["provider"] for e in errors] or None},
            )
            return results, p.name, errors

        await self._ev("search failed — all providers exhausted", {"query": query[:200], "errors": errors})
        raise GatewayExhausted(errors)

    async def _provider_available(
        self,
        p: Provider,
        errors: list[dict],
    ) -> bool:
        state = await db.get_provider_state(p.name)
        if state and not state["enabled"]:
            errors.append({"provider": p.name, "error": "disabled (runtime toggle)"})
            return False
        if not p.configured:
            errors.append({"provider": p.name, "error": "not configured (missing key/url)"})
            return False
        used, limit = await db.quota_used_limit(p.name)
        if limit and used >= limit:
            await db.set_provider_state(p.name, last_error="quota exhausted")
            log.info("%s quota exhausted (%d/%d) — skipping", p.name, used, limit)
            await self._ev(f"provider {p.name} quota exhausted", {"used": used, "limit": limit})
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

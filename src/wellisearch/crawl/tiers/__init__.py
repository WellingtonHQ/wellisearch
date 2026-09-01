"""Tier registry: transport tiers — how to get the HTML (design §3.1).

Tiers register themselves by name; the engine walks the policy's tier
ladder via by_name(). Config can disable a tier (http / stealth).
"""
from __future__ import annotations

from typing import Protocol

from ...config import get_settings
from ..policy import Policy
from ..results import Rendered


class Tier(Protocol):
    """A transport tier: fetch one URL, return rendered HTML."""

    name: str

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered: ...

    def worst_case_s(self, p: Policy) -> float:
        """Worst-case budget (seconds) the engine uses as its wait_for backstop."""
        ...


_REGISTRY: dict[str, Tier] = {}


def register(tier: Tier) -> None:
    """Register a tier instance under its name."""
    _REGISTRY[tier.name] = tier


def by_name(name: str) -> Tier | None:
    """Tier by name; None when disabled by config or not registered."""
    s = get_settings()
    if name == "http" and not s.CRAWL_HTTP_TIER:
        return None
    if name == "stealth" and not s.CRAWL_STEALTH_TIER:
        return None
    return _REGISTRY.get(name)


# Side-effect imports: each tier module registers itself on import.
from . import browser, http, stealth  # noqa: E402,F401

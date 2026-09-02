"""Per-domain policy table: the single place site knowledge lives (design §3.3).

match() binds a URL to a Policy (tiers, waits, signals, profile class) by
longest domain-suffix match; unknown domains get DEFAULT_POLICY.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Policy:
    """One domain's crawl policy."""

    name: str
    tiers: tuple[str, ...]
    waits: tuple[str, ...]
    signals: tuple[str, ...]
    profile: str  # "dedicated" | "shared"


POLICY: dict[str, Policy] = {
    "amazon.com": Policy(
        "amazon", ("http", "browser", "stealth"), ("settle", "network_idle"), ("price", "stock"), "dedicated",
    ),
    "apnews.com": Policy("ap", ("http", "browser", "stealth"), ("settle",), (), "shared"),
    "bestbuy.com": Policy(
        "bestbuy", ("http", "browser", "stealth"), ("settle",), ("price", "stock"), "dedicated",
    ),
    "boardgamegeek.com": Policy("bgg", ("http", "browser", "stealth"), ("settle",), (), "dedicated"),
    "nytimes.com": Policy("nytimes", ("http", "browser", "stealth"), ("settle",), (), "shared"),
    "reuters.com": Policy("reuters", ("http", "browser", "stealth"), ("settle",), (), "shared"),
    "target.com": Policy(
        "target", ("http", "browser", "stealth"), ("settle", "network_idle"), ("price", "stock"), "dedicated",
    ),
    "theguardian.com": Policy("guardian", ("http", "browser", "stealth"), ("settle",), (), "shared"),
    "walmart.com": Policy(
        "walmart", ("http", "browser", "stealth"), ("settle",), ("price", "stock"), "dedicated",
    ),
    "wsj.com": Policy("wsj", ("http", "browser", "stealth"), ("settle",), (), "shared"),
}

DEFAULT_POLICY = Policy("default", ("http", "browser"), ("settle",), (), "shared")


def match(url: str) -> Policy:
    """Policy for a URL: longest POLICY key that is a host suffix; else default."""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    best_key: str | None = None
    for key in POLICY:
        if host == key or host.endswith("." + key):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    if best_key is not None:
        return POLICY[best_key]
    return DEFAULT_POLICY

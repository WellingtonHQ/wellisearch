"""Result dataclasses shared by the native crawl engine (design §3).

Rendered is a tier's raw output; Fitted is an extractor's output;
CrawlResult is what crawl() returns. Escalate is the extractor→engine
signal to jump to a higher tier.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Rendered:
    """Raw fetch output from one tier."""

    html: str
    title: str | None
    status: int
    ms: int
    engine: str
    notes: str | None = None


@dataclass
class Fitted:
    """Extractor output: fit-markdown + quality signals + flags."""

    md: str
    title: str | None
    signals: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)


@dataclass
class CrawlResult:
    """Final result of one crawl() call (ok or best-partial)."""

    ok: bool
    title: str | None
    md: str
    tier: str
    ms: int
    attempts: list[dict] = field(default_factory=list)
    flags: dict = field(default_factory=dict)


class Escalate(Exception):
    """Raised by an extractor to ask the engine for a higher tier."""

    def __init__(self, tier: str) -> None:
        super().__init__(f"escalate to {tier}")
        self.tier = tier


class ChallengeDetected(Exception):
    """Raised by the browser tier (fast lane) when a bot-wall is present and the
    page should be routed to the CF challenge lane instead of retried here.

    The engine re-raises it (it is not a tier failure to escalate past), and the
    worker catches it to move the queue row onto the CF lane. Carries the URL
    for logging.
    """

    def __init__(self, url: str) -> None:
        super().__init__(f"challenge detected: {url}")
        self.url = url

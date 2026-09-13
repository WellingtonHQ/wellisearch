"""Unit tests: native crawl core (policy, botwall, signals, extractor, engine loop, probe budget)."""
from __future__ import annotations

import asyncio

from wellisearch.config import get_settings
from wellisearch.crawl import engine, probe, tiers
from wellisearch.crawl.botwall import is_botwall
from wellisearch.crawl.extractors.base import GenericExtractor
from wellisearch.crawl.policy import Policy, match
from wellisearch.crawl.results import Rendered
from wellisearch.crawl.signals import find_price, find_stock
import wellisearch.crawl.tiers.http as http_tier
import wellisearch.crawl.tiers.stealth as stealth_tier

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

p = match("https://www.amazon.com/dp/B08WM3LJQB")
assert p.name == "amazon"
assert "stealth" in p.tiers
assert match("https://www.walmart.com/ip/123").name == "walmart"
assert match("https://boardgamegeek.com/geeklist.php?id=1").name == "bgg"
assert match("https://www.nytimes.com/2026/01/01/tech/x.html").name == "nytimes"
assert match("https://example.com/x").name == "default"
assert match("https://notamazon.com/x").name == "default"  # suffix match must not false-positive
print("OK policy")

# ---------------------------------------------------------------------------
# Botwall
# ---------------------------------------------------------------------------

wall = '<html><body>Just a moment...<div class="cf-turnstile"></div></body></html>'
assert is_botwall(wall, 200) is not None
assert is_botwall("hello world article text", 200) is None
assert is_botwall("anything", 403) == "http_403"
assert is_botwall("superturnstile", 200) is None  # word-boundary: no marker inside a longer word
js_wall = (
    "<html><head><title>JavaScript is disabled</title></head>"
    "<body>In order to continue, we need to verify that you're not a robot. "
    "This requires JavaScript. Enable JavaScript and then reload the page.</body></html>"
)
assert is_botwall(js_wall, 200) is not None  # JS-disabled bot-wall must escalate, not store
print("OK botwall")

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

assert find_price('<span>$1,234.56</span>') == "$1,234.56"
assert find_price("no prices here") is None
assert find_stock("Currently In Stock") == "in stock"
assert find_stock("Out of Stock") == "out of stock"
assert find_stock("Only 3 left") == "only 3 left"
assert find_stock("call for pricing") is None
print("OK signals")

# ---------------------------------------------------------------------------
# Generic Extractor
# ---------------------------------------------------------------------------

GOOD_HTML = (
    "<html><head><title>Test Article</title></head><body><article>"
    "<p>" + "The native crawl engine replaces the external REST path with an in-process "
    "tier ladder that escalates from plain HTTP to a rendered browser when a site "
    "raises a bot-wall challenge. " * 2 + "</p>"
    "<p>" + "Each domain policy binds the tier order, the wait strategy, and the quality "
    "signals the extractor must find before the result is accepted by the gate. " * 2 + "</p>"
    "<p>" + "This paragraph exists so the extracted markdown comfortably clears the "
    "minimum length gate that the generic extractor applies to every page. " * 2 + "</p>"
    "</article></body></html>"
)
ex = GenericExtractor()
fitted = ex.fit(Rendered(html=GOOD_HTML, title="Test Article", status=200, ms=1, engine="fake"))
assert len(fitted.md) >= 200, fitted.md[:120]
assert ex.accept(fitted)
tiny = ex.fit(Rendered(html="<html><body>tiny</body></html>", title=None, status=200, ms=1, engine="fake"))
assert not ex.accept(tiny)
garbage = ex.fit(Rendered(html="<<<not html>>>", title=None, status=200, ms=1, engine="fake"))
assert isinstance(garbage.md, str)  # fit() never raises on garbage html
print("OK generic extractor")

# ---------------------------------------------------------------------------
# Engine Loop (Fake Tiers)
# ---------------------------------------------------------------------------

BOTWALL_HTML = '<html><body>Just a moment...<div class="cf-turnstile"></div></body></html>'


class FakeTier:
    name = "http"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Fake fetch for testing."""
        return Rendered(html=GOOD_HTML, title="Test Article", status=200, ms=1, engine="fake")


tiers._REGISTRY.clear()
tiers.register(FakeTier())
res = asyncio.run(engine.crawl("https://example.com/x"))
assert res.ok is True
assert res.tier == "http"
assert res.md


class BotwallHttpTier:
    name = "http"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Fake fetch returning a botwall page."""
        return Rendered(html=BOTWALL_HTML, title=None, status=200, ms=1, engine="fake")


class FakeBotwallTier:
    name = "browser"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Fake fetch returning a botwall page."""
        return Rendered(html=BOTWALL_HTML, title=None, status=200, ms=1, engine="fake")


tiers._REGISTRY.clear()
tiers.register(BotwallHttpTier())
tiers.register(FakeBotwallTier())
res = asyncio.run(engine.crawl("https://example.com/x"))
assert res.ok is False
assert any("botwall" in a.get("error", "") for a in res.attempts), res.attempts
print("OK engine loop")

# ---------------------------------------------------------------------------
# Probe Budget (read-path fast detection)
# ---------------------------------------------------------------------------

s = get_settings()
p_default = match("https://example.com/x")

assert probe.get_probe_s() is None            # no budget set by default (worker path)
assert probe.clamp(45.0) == 45.0              # ...so clamping is a no-op there

tok = probe.set_probe_budget(15)
try:
    assert probe.get_probe_s() == 15
    assert probe.clamp(90.0) == 15            # capped to the budget
    assert probe.clamp(5.0) == 5              # values already below stay

    # Tier worst_case (what drives the engine's wait_for backstop) honors it too.
    http_cap = min(float(s.CRAWL_TIMEOUT_S), 15)
    stealth_cap = min(float(s.CRAWL_STEALTH_TIMEOUT_S), 15)
    msg_http = "http worst_case must honor the probe budget"
    assert http_tier.HttpTier().worst_case_s(p_default) == http_cap, msg_http
    msg_stealth = "stealth worst_case must honor the probe budget"
    assert stealth_tier.StealthTier().worst_case_s(p_default) == stealth_cap, msg_stealth

    # Context isolation: a child task inherits the active budget (what _resolve_page's
    # create_task relies on); a budget set inside one task never leaks to its sibling.
    async def with_budget() -> float:
        t2 = probe.set_probe_budget(7)
        try:
            return await asyncio.sleep(0, result=probe.clamp(90.0))
        finally:
            probe.reset_probe_budget(t2)

    async def sibling() -> float:
        return await asyncio.sleep(0, result=probe.clamp(90.0))

    async def iso() -> list[float]:
        return await asyncio.gather(sibling(), with_budget())

    got = asyncio.run(iso())
    assert got == [15.0, 7.0], f"task-context isolation broken: {got}"
finally:
    probe.reset_probe_budget(tok)

assert probe.get_probe_s() is None            # reset restores the worker no-cap path
assert http_tier.HttpTier().worst_case_s(p_default) == float(s.CRAWL_TIMEOUT_S)
print("OK probe budget")

# ---------------------------------------------------------------------------
print("ALL CRAWL CORE TESTS PASSED")

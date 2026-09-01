"""Unit tests: native crawl core (policy, botwall, signals, extractor, engine loop)."""
import asyncio

from wellisearch.crawl import engine, tiers
from wellisearch.crawl.botwall import is_botwall
from wellisearch.crawl.extractors.base import GenericExtractor
from wellisearch.crawl.policy import match
from wellisearch.crawl.results import Rendered
from wellisearch.crawl.signals import find_price, find_stock, gate

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
assert gate({"price": "$1.00", "stock": "in stock"}, ("price", "stock")) is True
assert gate({"price": None}, ("price",)) is False
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

    async def fetch(self, url, p):
        return Rendered(html=GOOD_HTML, title="Test Article", status=200, ms=1, engine="fake")


tiers._REGISTRY.clear()
tiers.register(FakeTier())
res = asyncio.run(engine.crawl("https://example.com/x"))
assert res.ok is True
assert res.tier == "http"
assert res.md


class BotwallHttpTier:
    name = "http"

    async def fetch(self, url, p):
        return Rendered(html=BOTWALL_HTML, title=None, status=200, ms=1, engine="fake")


class FakeBotwallTier:
    name = "browser"

    async def fetch(self, url, p):
        return Rendered(html=BOTWALL_HTML, title=None, status=200, ms=1, engine="fake")


tiers._REGISTRY.clear()
tiers.register(BotwallHttpTier())
tiers.register(FakeBotwallTier())
res = asyncio.run(engine.crawl("https://example.com/x"))
assert res.ok is False
assert any("botwall" in a.get("error", "") for a in res.attempts), res.attempts
print("OK engine loop")

print("ALL CRAWL CORE TESTS PASSED")

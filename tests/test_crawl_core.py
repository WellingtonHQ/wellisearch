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
# non-HTML content types skip the marker scan (code files contain marker-like strings)
raw_py = 'def check(body):\n    if "access denied" in body:\n        raise Blocked("request blocked")\n'
assert is_botwall(raw_py, 200, "text/plain; charset=utf-8") is None
assert is_botwall('{"error": "unusual traffic"}', 200, "application/json") is None
# absent/unknown content type still gets scanned (safe default)
assert is_botwall("access denied", 200) is not None
assert is_botwall("access denied", 200, None) is not None
assert is_botwall("access denied", 200, "text/html; charset=utf-8") is not None
# status >= 400 wins even for non-HTML bodies
assert is_botwall("", 403, "application/json") == "http_403"
# <noscript> warnings on legitimate pages are not walls (XenForo et al. put
# "JavaScript is disabled" there for non-JS clients)
clean_noscript = (
    '<html><head><title>WellingtonHQ | XDA Forums</title></head>'
    '<body><div class="blockMessage">Forum content here</div>'
    "<noscript><div class=\"u-noJsOnly\">JavaScript is disabled. For a better "
    "experience, please enable JavaScript.</div></noscript></body></html>"
)
assert is_botwall(clean_noscript, 200) is None
# the same marker as visible text still escalates
assert is_botwall("<html><body>JavaScript is disabled. Enable it to continue.</body></html>", 200) is not None
# reddit's reCAPTCHA interstitial must escalate, not be stored as content
reddit_wall = (
    '<!DOCTYPE html><html lang="en"><head><title>Reddit - Prove your humanity</title>'
    "<script src=\"https://www.google.com/recaptcha/api.js\"></script></head>"
    "<body><form action=\"/r/programming/\" method=\"post\"><input type=\"submit\" value=\"Continue\"></form></body></html>"
)
assert is_botwall(reddit_wall, 200) == "prove your humanity"
# marker phrases in code samples are content, not walls (security articles show
# 403 examples); the same phrase as a short page's visible text still escalates
article_code = (
    '<html><head><title>Fix Privilege Escalation Vulnerabilities</title></head>'
    "<body><h1>Privilege escalation</h1>" + "<p>Article text. " * 200 + "</p>"
    "<pre><code>if (!req.user.isAdmin) { return res.status(403).json({ error: 'Access denied' }); }</code></pre>"
    "</body></html>"
)
assert is_botwall(article_code, 200) is None
denied_wall = (
    '<html><head><title>Access Denied</title></head>'
    "<body><h1>Access Denied</h1><p>You do not have permission to view this page.</p></body></html>"
)
assert is_botwall(denied_wall, 200) == "access denied"
# marker phrases in <script> data blobs (nav JSON etc.) are not walls
page_script_blob = (
    '<html><head><title>Vultr Docs</title></head>'
    "<body>" + "<p>Documentation content. " * 200 + "</p>"
    '<script>window.__NAV__={"label":"Fix MySQL Access Denied Errors","href":"/x"};</script>'
    "</body></html>"
)
assert is_botwall(page_script_blob, 200) is None
# embedded CF assets on a content-rich page are not walls (Turnstile form
# widgets, jsd bootstrap scripts); structural markers only count on
# interstitial-sized pages
clean_cf_assets = (
    '<html><head><title>Website Design | Computer Scene</title></head>'
    "<body>" + "<p>Real content. " * 200 + "</p>"
    '<div class="cf7-cf-turnstile"><div id="cf-turnstile-cf7-1" class="cf-turnstile"'
    ' data-sitekey="0x4AAAAA"></div></div>'
    "<script>var a=document.createElement('script');"
    "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>"
    "</body></html>"
)
assert is_botwall(clean_cf_assets, 200) is None
# a real CF interstitial (short visible text + structural assets) still escalates
cf_interstitial = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>'
    "<body><div id=\"content\"><h1>Just a moment...</h1>"
    "Checking your browser before accessing example.com</div>"
    "<script src=\"/cdn-cgi/challenge-platform/scripts/jsd/main.js\"></script></body></html>"
)
assert is_botwall(cf_interstitial, 200) == "just a moment"
# structural markers alone still catch an interstitial with no recognizable copy
cf_bare = (
    '<html><head><title>Checking...</title></head>'
    "<body><div class=\"cf-turnstile\" data-sitekey=\"0x4AAAAA\"></div>"
    "<script src=\"/cdn-cgi/challenge-platform/scripts/jsd/main.js\"></script></body></html>"
)
assert is_botwall(cf_bare, 200) == "challenge-platform"
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

    # Tier worst_case_s honors it too — that value drives the engine's backstop.
    http_cap = min(float(s.CRAWL_TIMEOUT_S), 15)
    stealth_cap = min(float(s.CRAWL_STEALTH_TIMEOUT_S), 15)
    assert http_tier.HttpTier().worst_case_s(p_default) == http_cap
    assert stealth_tier.StealthTier().worst_case_s(p_default) == stealth_cap

    # A child task inherits the active budget; siblings never see each other's.
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

assert probe.get_probe_s() is None            # reset clears the budget
assert http_tier.HttpTier().worst_case_s(p_default) == float(s.CRAWL_TIMEOUT_S)
print("OK probe budget")

# ---------------------------------------------------------------------------
print("ALL CRAWL CORE TESTS PASSED")

"""Unit tests: per-site extractors (fixture HTML, no network)."""
from __future__ import annotations

from wellisearch.crawl.extractors import for_url
from wellisearch.crawl.extractors.amazon import AmazonExtractor
from wellisearch.crawl.extractors.ap import APExtractor
from wellisearch.crawl.extractors.base import generic_md
from wellisearch.crawl.extractors.bestbuy import BestBuyExtractor
from wellisearch.crawl.extractors.guardian import GuardianExtractor
from wellisearch.crawl.extractors.nytimes import NYTimesExtractor
from wellisearch.crawl.extractors.reuters import ReutersExtractor
from wellisearch.crawl.extractors.target import TargetExtractor
from wellisearch.crawl.extractors.walmart import WalmartExtractor
from wellisearch.crawl.extractors.wsj import WSJExtractor
from wellisearch.crawl.results import Escalate, Rendered


def rendered(html: str, title: str | None = None) -> Rendered:
    """Rendered fixture for extractor tests (no network)."""
    return Rendered(html=html, title=title, status=200, ms=1, engine="fake")


# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------
AMAZON_HTML = (
    "<html><head><title>Kindle (10th generation) : Amazon.com</title></head><body>"
    "<span id=\"productTitle\">Kindle (10th generation)</span>"
    "<div data-asin=\"B08WM3LJQB\"><span class=\"a-price\">"
    "<span class=\"a-offscreen\">$129.99</span></span></div>"
    "<div id=\"availability\">In Stock</div>"
    "<ul id=\"feature-bullets\">"
    "<li>Kindle (10th generation) is the perfect device for reading, with a crisp 300 ppi "
    "display, 16 GB of storage, and weeks of battery life on a single charge.</li>"
    "<li>The glare-free display looks like paper in any light, so you can read comfortably "
    "in bright sun or a dim room.</li>"
    "<li>With 16 GB of storage you can carry thousands of books, magazines, and comics, and "
    "the built-in Wi-Fi keeps your library up to date without a computer.</li>"
    "<li>The adjustable warm light lets you read comfortably day or night, and the "
    "lightweight, pocketable design makes it easy to take anywhere.</li>"
    "<li>Water resistance means it can handle a splash in the rain or a dip in the pool, so "
    "your reading never has to stop.</li>"
    "</ul>"
    "<div id=\"hub\">Frequently bought together: add a case, a screen protector, and a "
    "reading light to complete the bundle and save on shipping at checkout.</div>"
    "</body></html>"
)
ex = AmazonExtractor()
fitted = ex.fit(rendered(AMAZON_HTML))
assert "129.99" in fitted.md, fitted.md[:200]
assert "About this item" in fitted.md, fitted.md[:200]
assert "Frequently bought together" not in fitted.md, fitted.md[:200]  # decoy excluded
assert "screen protector" not in fitted.md, fitted.md[:200]
assert fitted.signals["price"] == "$129.99", fitted.signals
assert fitted.signals["stock"] == "In Stock", fitted.signals
assert fitted.title == "Kindle (10th generation)", fitted.title
assert ex.accept(fitted)
# no price element -> gate fails
no_price = ex.fit(rendered(
    AMAZON_HTML.replace(
        "<div data-asin=\"B08WM3LJQB\"><span class=\"a-price\">"
        "<span class=\"a-offscreen\">$129.99</span></span></div>", ""
    )
))
assert not ex.accept(no_price)
# no feature bullets (decoy-only page) -> gate fails
no_bullets = ex.fit(rendered(
    "<html><body><span id=\"productTitle\">Kindle</span>"
    "<div data-asin=\"x\"><span class=\"a-price\">"
    "<span class=\"a-offscreen\">$129.99</span></span></div>"
    "<div>Frequently bought together: add a case and a screen protector.</div>"
    "</body></html>"
))
assert not ex.accept(no_bullets)
print("OK amazon")

# ---------------------------------------------------------------------------
# Walmart
# ---------------------------------------------------------------------------
_WALM = (
    "Key item features: a 27-inch full HD IPS display with a 100Hz refresh rate, AMD "
    "FreeSync, and three-sided slim bezels for a clean desk setup. The 100Hz refresh rate "
    "keeps fast motion smooth, whether you are gaming, scrolling, or editing video, and "
    "AMD FreeSync eliminates tearing and stuttering for a fluid experience. The IPS panel "
    "delivers wide 178-degree viewing angles and accurate colors, so the image looks sharp "
    "from the side as well as head-on. Three-sided slim bezels make it easy to line up "
    "multiple displays for a seamless workspace, and the adjustable stand lets you tilt, "
    "swivel, and height-adjust the screen to your perfect viewing position. "
) * 8
WALMART_HTML = (
    "<html><head><title>Acer ED270RS3 27-inch Full HD Monitor - Walmart.com</title></head><body>"
    "<h1>Acer ED270RS3 27-inch Full HD Monitor</h1>"
    "<p>$199.00</p>"
    f"<p>{_WALM}</p>"
    "</body></html>"
)
ex = WalmartExtractor()
fitted = ex.fit(rendered(WALMART_HTML))
assert fitted.signals["price"] == "$199.00", fitted.signals
assert ex.accept(fitted)
assert not ex.accept(ex.fit(rendered(WALMART_HTML.replace("$199.00", ""))))
print("OK walmart")

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------
_TARG = (
    "This 7-in-1 USB-C hub adds HDMI, three USB 3.0 ports, an SD card slot, a microSD "
    "card slot, and a 100W power pass-through to any laptop. The HDMI port outputs "
    "4K at 30Hz, so you can connect an external monitor or TV and mirror or extend "
    "your display in crisp detail. The three USB 3.0 ports charge devices and transfer "
    "files at up to 5Gbps, and the SD and microSD slots read cards directly without a "
    "separate reader. The 100W power pass-through keeps your laptop charged while you "
    "use every other port, so a single cable handles power and peripherals at once. "
) * 8
TARGET_HTML = (
    "<html><head><title>USB-C Hub 7-in-1 - Target</title></head><body>"
    "<h1>USB-C Hub 7-in-1</h1>"
    "<p>$49.99</p>"
    f"<p>{_TARG}</p>"
    "</body></html>"
)
ex = TargetExtractor()
fitted = ex.fit(rendered(TARGET_HTML))
assert fitted.signals["price"] == "$49.99", fitted.signals
assert ex.accept(fitted)
print("OK target")

# ---------------------------------------------------------------------------
# BestBuy
# ---------------------------------------------------------------------------
_BB = (
    "The thinnest and lightest MacBook ever, with the M3 chip for fast performance, up "
    "to 18 hours of battery life, and a gorgeous 13.6-inch Liquid Retina display. The M3 "
    "chip brings a huge leap in performance and efficiency, so demanding tasks like video "
    "editing, code compilation, and large spreadsheets feel instant while sipping power. "
    "The Liquid Retina display is bright, vivid, and easy on the eyes, with support for "
    "1 billion colors and the full sRGB gamut. The fanless design runs completely silent, "
    "and the all-day battery means you can work, stream, and create without hunting for an "
    "outlet. macOS integrates seamlessly with the rest of your Apple devices, so files, "
    "messages, and calls follow you from phone to tablet to laptop. "
) * 8
BESTBUY_HTML = (
    "<html><head><title>MacBook Air M3 13-inch - Best Buy</title></head><body>"
    "<script type=\"application/ld+json\">"
    "{\"@context\": \"https://schema.org\", \"@type\": \"Product\", "
    "\"name\": \"MacBook Air M3 13-inch\", "
    "\"offers\": {\"@type\": \"Offer\", \"price\": \"899.99\", \"priceCurrency\": \"USD\"}}"
    "</script>"
    "<h1>MacBook Air M3 13-inch</h1>"
    f"<p>{_BB}</p>"
    "</body></html>"
)
ex = BestBuyExtractor()
fitted = ex.fit(rendered(BESTBUY_HTML))
assert fitted.signals["price"] == "899.99", fitted.signals  # JSON-LD fallback (no visible $)
assert ex.accept(fitted)
print("OK bestbuy")

# ---------------------------------------------------------------------------
# NYTimes
# ---------------------------------------------------------------------------
NYT_STUB_HTML = (
    "<html><head><title>Sample Paywall Stub | The New York Times</title></head><body>"
    "<h1>Sample Paywall Stub</h1>"
    "<p>By STAFF WRITER</p>"
    "<p>This is a short placeholder body used to exercise the paywall-stub path. "
    "It is intentionally brief so the extractor treats it as a metered-paywall stub "
    "and escalates to the stealth tier. No real reporting is included.</p>"
    "<p>Subscribe to read all of The New York Times.</p>"
    "</body></html>"
)
NYT_LONG_HTML = (
    "<html><head><title>Sample Long Article | The New York Times</title></head><body>"
    "<h1>Sample Long Article</h1>"
    "<p>" + "This is a long placeholder article body used to exercise the full-article path. "
    "It is intentionally verbose filler text with no real reporting, included only so the "
    "extractor clears its minimum-length gate and accepts the page as a complete article. " * 4 + "</p>"
    "<p>" + "The paragraphs repeat a neutral, generic statement about testing and fixtures. "
    "Nothing here is drawn from any publication; it exists solely to give the extractor "
    "enough characters to treat the page as a full article rather than a paywall stub. " * 4 + "</p>"
    "<p>" + "Additional filler continues here to push the total length well past the "
    "threshold, ensuring the accept gate and the stealth-escalation branch are both "
    "exercised by the test suite without relying on any real-world content at all. " * 4 + "</p>"
    "</body></html>"
)
ex = NYTimesExtractor()
try:
    ex.fit(rendered(NYT_STUB_HTML, "stub"))
    raise AssertionError("expected Escalate for a paywall stub")
except Escalate as e:
    assert e.tier == "stealth", e.tier
fitted = ex.fit(rendered(NYT_LONG_HTML, "full article"))
assert ex.accept(fitted)
assert len(fitted.md.strip()) >= 2000, len(fitted.md)
print("OK nytimes")

# ---------------------------------------------------------------------------
# WSJ
# ---------------------------------------------------------------------------
WSJ_STUB_HTML = (
    "<html><head><title>Sample Paywall Stub | The Wall Street Journal</title></head><body>"
    "<h1>Sample Paywall Stub</h1>"
    "<p>By STAFF WRITER</p>"
    "<p>This is a placeholder lead paragraph used to exercise the paywall-stub path. "
    "It is generic filler text with no real reporting, included only so the extractor "
    "has enough characters to accept the page while the paywall marker is present. "
    "The content is intentionally neutral and drawn from no actual publication. "
    "It exists to give the test a realistic-looking stub without relying on any "
    "copyrighted material from a real outlet, and it repeats a simple statement "
    "about fixtures and testing to reach a comfortable length for the gate. "
    "Nothing here reflects any actual market data, filing, or event.</p>"
    "<p>Subscribe to read all of The Wall Street Journal.</p>"
    "</body></html>"
)
ex = WSJExtractor()
fitted = ex.fit(rendered(WSJ_STUB_HTML, "paywalled lead"))
assert fitted.flags["paywall"] is True, fitted.flags
assert ex.accept(fitted)
print("OK wsj")

# ---------------------------------------------------------------------------
# Reuters
# ---------------------------------------------------------------------------
REUTERS_HTML = (
    "<html><head><title>Sample Market Wrap | Reuters</title></head><body>"
    "<h1>Sample Market Wrap</h1>"
    "<p>" + "This is a placeholder market-wrap body used to exercise the links-section trim. "
    "It is generic filler text with no real reporting, included only so the extractor "
    "clears its minimum-length gate before the marker section is cut away. The text "
    "repeats a neutral statement about testing and fixtures, drawn from no publication. " * 3 + "</p>"
    "<p>" + "Additional filler continues here to push the body length comfortably past the "
    "one-thousand-character threshold, ensuring the accept gate passes while the "
    "marker line below is still exercised by the hard-cut logic. " * 3 + "</p>"
    "<p>Related: a sample headline that should be trimmed from the output.</p>"
    "</body></html>"
)
ex = ReutersExtractor()
md = generic_md(REUTERS_HTML)
assert "Related" in md, md[:200]  # related section present pre-cut, so the cut is exercised
fitted = ex.fit(rendered(REUTERS_HTML))
assert "should be trimmed from the output" not in fitted.md, fitted.md[-200:]
assert ex.accept(fitted)
print("OK reuters")

# ---------------------------------------------------------------------------
# Guardian
# ---------------------------------------------------------------------------
GUARDIAN_HTML = (
    "<html><head><title>Sample Climate Piece | theguardian.com</title></head><body>"
    "<h1>Sample Climate Piece</h1>"
    "<p>" + "This is a placeholder climate body used to exercise the stories-section trim. "
    "It is generic filler text with no real reporting, included only so the extractor "
    "clears its minimum-length gate before the marker section is cut away. The text "
    "repeats a neutral statement about testing and fixtures, drawn from no publication. " * 3 + "</p>"
    "<p>" + "Additional filler continues here to push the body length comfortably past the "
    "one-thousand-character threshold, ensuring the accept gate passes while the "
    "marker line below is still exercised by the hard-cut logic. " * 3 + "</p>"
    "<p>Explore more: a sample headline that should be trimmed from the output.</p>"
    "</body></html>"
)
ex = GuardianExtractor()
md = generic_md(GUARDIAN_HTML)
assert "Explore more" in md, md[:200]  # related section present pre-cut, so the cut is exercised
fitted = ex.fit(rendered(GUARDIAN_HTML))
assert "should be trimmed from the output" not in fitted.md, fitted.md[-200:]
assert ex.accept(fitted)
print("OK guardian")

# ---------------------------------------------------------------------------
# AP
# ---------------------------------------------------------------------------
AP_HTML = (
    "<html><head><title>Sample Senate Story | AP News</title></head><body>"
    "<p>AP News | Most Popular | Newsletters | Sign up</p>"
    "<h1>Sample Senate Story</h1>"
    "<p>" + "This is the real article body for the test, written as generic filler with "
    "no real reporting. It is included so the extractor clears its minimum-length gate "
    "after the head-decoy block above is dropped. The text repeats a neutral statement "
    "about testing and fixtures, drawn from no publication whatsoever. " * 3 + "</p>"
    "<p>" + "Additional filler continues here to push the body length comfortably past the "
    "one-thousand-character threshold, ensuring the accept gate passes while the "
    "head-decoy marker is still exercised by the trim logic on this fixture. " * 3 + "</p>"
    "</body></html>"
)
ex = APExtractor()
md = generic_md(AP_HTML)
assert "Most Popular" in md, md[:200]  # head decoy present pre-cut, so the trim is exercised
fitted = ex.fit(rendered(AP_HTML))
assert "Most Popular" not in fitted.md, fitted.md[:200]
assert "real article body for the test" in fitted.md, fitted.md[:200]
assert ex.accept(fitted)
print("OK ap")

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
assert for_url("https://www.amazon.com/dp/B08WM3LJQB").name == "amazon"
assert for_url("https://www.nytimes.com/2026/x.html").name == "nytimes"
assert for_url("https://example.com/x").name == "generic"
print("OK registry")

print("ALL EXTRACTOR TESTS PASSED")

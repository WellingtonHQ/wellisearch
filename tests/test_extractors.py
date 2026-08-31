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
    "<html><head><title>AI Models Are Getting Smarter. Your Privacy Is Not. | The New York Times</title></head><body>"
    "<h1>AI Models Are Getting Smarter. Your Privacy Is Not.</h1>"
    "<p>By DANIEL M. BRIGGS</p>"
    "<p>Large language models now retain and reuse far more of what users share with them "
    "than earlier systems did, raising fresh questions about who can see that data.</p>"
    "<p>Subscribe to read all of The New York Times.</p>"
    "</body></html>"
)
NYT_LONG_HTML = (
    "<html><head><title>Chipmakers Race to Build the Next Generation of AI Data Centers | The New York Times</title></head><body>"
    "<h1>Chipmakers Race to Build the Next Generation of AI Data Centers</h1>"
    "<p>" + "The race to build the next generation of artificial-intelligence data centers has "
    "turned into a global contest of engineering, capital, and supply chains, with chipmakers, "
    "cloud providers, and governments all betting that the demand for compute will keep "
    "compounding for years. " * 3 + "</p>"
    "<p>" + "Industry executives describe a buildout that spans continents: new fabs in Arizona "
    "and Germany, submarine cables in the Atlantic, and cooling plants in the desert that "
    "draw more power than small cities. The economics are driven by a simple assumption "
    "that every model generation will need more compute than the last. " * 3 + "</p>"
    "<p>" + "Analysts caution that the pace of deployment depends on power availability, "
    "interconnection capacity, and the willingness of utilities to sign long-term contracts "
    "for loads that were unimaginable a decade ago. " * 3 + "</p>"
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
    "<html><head><title>The Quiet Collapse of the Regional Bank | The Wall Street Journal</title></head><body>"
    "<h1>The Quiet Collapse of the Regional Bank</h1>"
    "<p>By OUR STAFF</p>"
    "<p>Regional banks have been quietly shedding deposits all year, and the latest filings "
    "suggest the outflow is accelerating as customers move money into money-market funds "
    "offering higher yields. The trend has forced several mid-sized lenders to raise interest "
    "on savings products, to sell securities portfolios at a loss, and in at least two cases "
    "to seek emergency funding from the Federal Reserve's discount window. Analysts say the "
    "pressure is unlikely to ease until short-term rates come down.</p>"
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
    "<html><head><title>Global markets rally as inflation cools - Reuters</title></head><body>"
    "<h1>Global markets rally as inflation cools</h1>"
    "<p>" + "Stocks around the world climbed on Friday after new data showed inflation slowing "
    "faster than expected in the United States and the euro zone, lifting hopes that central "
    "banks will begin cutting interest rates earlier than markets had priced in. The S&P 500 "
    "rose 1.4 percent to close near record levels, while European and Asian indexes posted "
    "their best session in three months. " * 2 + "</p>"
    "<p>" + "Bond yields fell across the curve, with the two-year Treasury note down 12 basis points "
    "and the ten-year yield slipping below 4 percent for the first time since early last "
    "year. The dollar weakened against a basket of currencies, and oil prices eased on "
    "renewed concerns about demand. " * 2 + "</p>"
    "<p>Related: central banks signal patience on rate cuts as data improves, analysts "
    "say, as the euro zone holds its benchmark rate steady for a fourth consecutive month.</p>"
    "<p>Oil slides as traders weigh demand outlook and supply risks across the major "
    "producing regions of the world.</p>"
    "</body></html>"
)
ex = ReutersExtractor()
md = generic_md(REUTERS_HTML)
assert "Related" in md, md[:200]  # related section present pre-cut, so the cut is exercised
fitted = ex.fit(rendered(REUTERS_HTML))
assert "Central banks signal patience" not in fitted.md, fitted.md[-200:]
assert ex.accept(fitted)
print("OK reuters")

# ---------------------------------------------------------------------------
# Guardian
# ---------------------------------------------------------------------------
GUARDIAN_HTML = (
    "<html><head><title>Climate change: the year the world's heat records fell - theguardian.com</title></head><body>"
    "<h1>Climate change: the year the world's heat records fell</h1>"
    "<p>" + "The past year was the warmest on record for the planet, with average surface "
    "temperatures running more than 1.5 degrees Celsius above pre-industrial levels for "
    "the first full calendar year. Scientists say the milestone, long predicted by climate "
    "models, is a warning rather than a one-off anomaly, and that the coming years will "
    "likely be warmer still. " * 2 + "</p>"
    "<p>" + "Extreme heat drove wildfires across the Mediterranean, drought across southern Europe, "
    "and record flooding in parts of South Asia. Insurance losses reached an all-time high, "
    "and several governments announced emergency funding for the hardest-hit regions. " * 2 + "</p>"
    "<p>Explore more: an analysis of what the new temperature record means for the "
    "Paris Agreement and the next round of climate negotiations in Geneva.</p>"
    "</body></html>"
)
ex = GuardianExtractor()
md = generic_md(GUARDIAN_HTML)
assert "Explore more" in md, md[:200]  # related section present pre-cut, so the cut is exercised
fitted = ex.fit(rendered(GUARDIAN_HTML))
assert "Paris Agreement" not in fitted.md, fitted.md[-200:]
assert ex.accept(fitted)
print("OK guardian")

# ---------------------------------------------------------------------------
# AP
# ---------------------------------------------------------------------------
AP_HTML = (
    "<html><head><title>Senate advances AI safety bill in rare bipartisan vote - AP News</title></head><body>"
    "<p>AP News | Most Popular | Newsletters | Sign up</p>"
    "<h1>Senate advances AI safety bill in rare bipartisan vote</h1>"
    "<p>" + "WASHINGTON — The Senate on Wednesday advanced a sweeping artificial-intelligence "
    "safety bill in a rare bipartisan vote, setting up a final House fight over the first "
    "major federal rules for how large AI models are developed, tested, and deployed. The "
    "measure would require companies to report serious safety incidents to the government "
    "and would create a new federal registry for the most capable models. " * 2 + "</p>"
    "<p>" + "The bill drew support from members of both parties who say the current patchwork of "
    "state laws is unworkable for a technology that crosses state lines by design. Industry "
    "groups warned the reporting requirements would force companies to disclose competitive "
    "secrets, while safety advocates said the rules go far too far. " * 2 + "</p>"
    "</body></html>"
)
ex = APExtractor()
md = generic_md(AP_HTML)
assert "Most Popular" in md, md[:200]  # head decoy present pre-cut, so the trim is exercised
fitted = ex.fit(rendered(AP_HTML))
assert "Most Popular" not in fitted.md, fitted.md[:200]
assert "bipartisan vote" in fitted.md, fitted.md[:200]
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

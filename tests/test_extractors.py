"""Unit tests: per-site extractors (fixture HTML, no network)."""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

from wellisearch.crawl.extractors import for_url
from wellisearch.crawl.extractors.amazon import (
    AmazonExtractor,
    is_product_url,
    needs_refresh as amazon_needs_refresh,
)
from wellisearch.crawl.extractors.ap import APExtractor
from wellisearch.crawl.extractors.base import (
    MIN_MD_CHARS,
    TITLE_MAX_LEN,
    generic_md,
    title_from_markdown,
)
from wellisearch.crawl.extractors.bestbuy import BestBuyExtractor
from wellisearch.crawl.extractors.brave import (
    BraveExtractor,
    _visible_text_markdown,
)
from wellisearch.crawl.extractors.greenhouse import GreenhouseExtractor
from wellisearch.crawl.extractors.guardian import GuardianExtractor
from wellisearch.crawl.extractors.nytimes import NYTimesExtractor
from wellisearch.crawl.extractors.reddit import RedditExtractor, comment_request_url, needs_refresh
from wellisearch.crawl.extractors.reuters import ReutersExtractor
from wellisearch.crawl.extractors.target import TargetExtractor
from wellisearch.crawl.extractors.walmart import WalmartExtractor
from wellisearch.crawl.extractors.wsj import WSJExtractor
from wellisearch.crawl.policy import match
from wellisearch.crawl.results import Escalate, Rendered


def rendered(html: str, title: str | None = None) -> Rendered:
    """Rendered fixture for extractor tests (no network)."""
    return Rendered(html=html, title=title, status=200, ms=1, engine="fake")


# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------

AMAZON_DESC = (
    "<div id=\"productDescription\">"
    "<p>Kindle (10th generation) pairs a crisp 300 ppi display with weeks of battery "
    "life, so you can read anywhere without hunting for an outlet.</p>"
    "<p>The lightweight, pocketable design makes it easy to take anywhere, and the "
    "adjustable warm light lets you read comfortably day or night.</p>"
    "</div>"
)
AMAZON_DETAILS = (
    "<div id=\"detailBullets_feature_div\">"
    "<table><tr><th>Item model number</th><td>B08WM3LJQB</td></tr></table>"
    "</div>"
)
AMAZON_REVIEWS = (
    "<div id=\"customerReviews\"><ul id=\"localTopReviewsList\">"
    '<li><div data-hook="review" id="R1">'
    "<span class=\"a-profile-name\">Angela B</span>"
    "<i class=\"a-icon a-icon-star a-star-5\" data-hook=\"review-star-rating\">"
    "<span class=\"a-icon-alt\">5 out of 5 stars</span></i>"
    "<h5 data-hook=\"reviewTitle\">Perfect starter kit</h5>"
    "<span data-hook=\"review-date\">Reviewed in the United States on September 22, 2026</span>"
    "<span data-hook=\"avp-badge\">Verified Purchase</span>"
    "<div data-hook=\"reviewRichContentContainer\"><p>Love this set.</p>"
    "<p>The bottles reduced colic from the first week.</p></div>"
    "</div></li>"
    '<li><div data-hook="review" id="R2">'
    "<span class=\"a-profile-name\">Marcus T</span>"
    "<i class=\"a-icon a-icon-star a-star-4\" data-hook=\"review-star-rating\">"
    "<span class=\"a-icon-alt\">4 out of 5 stars</span></i>"
    "<h5 data-hook=\"reviewTitle\">Good value</h5>"
    "<span data-hook=\"review-date\">Reviewed in the United States on June 14, 2026</span>"
    "<div data-hook=\"reviewRichContentContainer\"><p>Good value for the price.</p></div>"
    "</div></li>"
    "</ul></div>"
)
AMAZON_HTML = (
    "<html><head><title>Kindle (10th generation) : Amazon.com</title></head><body>"
    "<span id=\"productTitle\">Kindle (10th generation)</span>"
    "<div data-asin=\"B08WM3LJQB\"><span class=\"a-price\">"
    "<span class=\"a-offscreen\">$129.99</span></span></div>"
    "<div id=\"availability\">In Stock</div>"
    "<span id=\"acrPopover\" title=\"4.6 out of 5 stars\">"
    "<i class=\"a-icon a-icon-star\"><span class=\"a-icon-alt\">4.6 out of 5 stars</span></i>"
    "</span>"
    "<span id=\"acrCustomerReviewText\">12,345 ratings</span>"
    "<div id=\"tabular-buybox\"><table><tr><td>Sold by</td>"
    "<td><a id=\"sellerProfileTriggerId\" href=\"/sp?ie=UTF8&amp;seller=ATVPDKIKX0DER\">"
    "Amazon.com</a></td></tr></table></div>"
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
    + AMAZON_DESC
    + AMAZON_DETAILS
    + AMAZON_REVIEWS
    + "<div id=\"hub\">Frequently bought together: add a case, a screen protector, and a "
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
assert "**Rating:** 4.6 out of 5 stars 12,345 ratings" in fitted.md, fitted.md[:300]
assert "**Sold by:** Amazon.com" in fitted.md, fitted.md[:300]
assert fitted.signals["rating"] == "4.6 out of 5 stars", fitted.signals
assert fitted.signals["seller"] == "Amazon.com", fitted.signals
assert fitted.title == "Kindle (10th generation)", fitted.title
assert ex.accept(fitted)
# product description: both paragraphs, ordered between bullets and details
assert "## Product description" in fitted.md, fitted.md[:400]
assert "pairs a crisp 300 ppi display" in fitted.md, fitted.md[:400]
assert "pocketable design makes it easy to take anywhere" in fitted.md, fitted.md[:400]
assert fitted.signals["description"] > 0, fitted.signals
# product details table renders after the description
assert "## Product details" in fitted.md, fitted.md[:400]
assert "Item model number B08WM3LJQB" in fitted.md, fitted.md[:400]
order = [fitted.md.index(h) for h in ("About this item", "Product description", "Product details")]
assert order == sorted(order), fitted.md
# top reviews: heading, per-review fields, displayed order, after details
assert "## Amazon reviews (up to 5)" in fitted.md, fitted.md[:600]
assert "### 1. Angela B (5 out of 5 stars, verified purchase)" in fitted.md, fitted.md[:800]
assert "### 2. Marcus T (4 out of 5 stars)" in fitted.md, fitted.md[:800]
assert "**Perfect starter kit** — Reviewed in the United States on September 22, 2026" in fitted.md
assert "reduced colic from the first week" in fitted.md  # second paragraph kept
assert fitted.md.index("### 1. Angela B") < fitted.md.index("### 2. Marcus T")
assert fitted.signals["reviews"] == 2, fitted.signals
assert fitted.signals["reviews_reported"] == 12345, fitted.signals
rev_order = [fitted.md.index(h) for h in ("Product details", "Amazon reviews")]
assert rev_order == sorted(rev_order), fitted.md
# limit: only the first review is kept
with patch(
    "wellisearch.crawl.extractors.amazon.get_settings",
    return_value=SimpleNamespace(CRAWL_AMAZON_MAX_REVIEWS=1),
):
    limited_fit = ex.fit(rendered(AMAZON_HTML))
assert limited_fit.signals["reviews"] == 1, limited_fit.signals
assert "### 1. Angela B" in limited_fit.md
assert "Marcus T" not in limited_fit.md
# product page with no rendered cards -> heading + placeholder (no empty gap, no loop)
no_reviews = ex.fit(rendered(AMAZON_HTML.replace(AMAZON_REVIEWS, "")))
assert "## Amazon reviews (up to 5)" in no_reviews.md, no_reviews.md[:600]
assert "No reviews available." in no_reviews.md, no_reviews.md[:600]
assert no_reviews.signals["reviews"] == 0, no_reviews.signals
# needs_refresh: stale (pre-feature) markdown re-crawls, current markdown doesn't
amazon_url = "https://www.amazon.com/Kindle-10th-generation/dp/B08WM3LJQB"
assert is_product_url(amazon_url)
assert is_product_url("https://us.amazon.com/Kindle-10th-generation/dp/B08WM3LJQB?th=1")
assert not is_product_url("https://www.amazon.com/gp/bestsellers/electronics/")
assert not is_product_url("https://notamazon.com/dp/B08WM3LJQB")
stale_md = "# Kindle (10th generation)\n\n**Price:** $129.99"
assert amazon_needs_refresh(amazon_url, stale_md)
assert not amazon_needs_refresh(amazon_url, fitted.md)
assert not amazon_needs_refresh(amazon_url, no_reviews.md)  # placeholder heading counts
with patch(
    "wellisearch.crawl.extractors.amazon.get_settings",
    return_value=SimpleNamespace(CRAWL_AMAZON_MAX_REVIEWS=8),
):
    assert amazon_needs_refresh(amazon_url, fitted.md)  # limit changed -> stale
assert not amazon_needs_refresh("https://www.amazon.com/gp/bestsellers/electronics/", stale_md)
# no #productDescription -> no empty Product description section
no_desc = ex.fit(rendered(AMAZON_HTML.replace(AMAZON_DESC, "")))
assert "Product description" not in no_desc.md, no_desc.md[:400]
assert no_desc.signals["description"] == 0, no_desc.signals
# #product-description wrapper (no inner id) still extracts the text
fallback = ex.fit(
    rendered(
        "<html><body>"
        "<span id=\"productTitle\">Kindle</span>"
        "<div data-asin=\"x\"><span class=\"a-price\">"
        "<span class=\"a-offscreen\">$129.99</span></span></div>"
        "<ul id=\"feature-bullets\"><li>One bullet.</li></ul>"
        "<div id=\"product-description\"><p>Fallback wrapper description text.</p></div>"
        "</body></html>"
    )
)
assert "Fallback wrapper description text" in fallback.md, fallback.md[:400]
# no price element -> gate fails
no_price = ex.fit(
    rendered(
        AMAZON_HTML.replace(
            "<div data-asin=\"B08WM3LJQB\"><span class=\"a-price\">"
            "<span class=\"a-offscreen\">$129.99</span></span></div>", ""
        )
    )
)
assert not ex.accept(no_price)
# no feature bullets (decoy-only page) -> gate fails
no_bullets = ex.fit(
    rendered(
        "<html><body><span id=\"productTitle\">Kindle</span>"
        "<div data-asin=\"x\"><span class=\"a-price\">"
        "<span class=\"a-offscreen\">$129.99</span></span></div>"
        "<div>Frequently bought together: add a case and a screen protector.</div>"
        "</body></html>"
    )
)
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
# Brave
# ---------------------------------------------------------------------------

BRAVE_SERP_HTML = (
    '<html><head><title>openwebui - Brave Search</title></head>'
    '<body class="svelte-abc"><div id="__sveltekit_x">'
    '<script type="module">var q=1;</script>'
    '<nav><a href="/">Home</a><span class="tab">Ask</span><span class="tab">Ask</span></nav>'
    '<ol>'
    '<li class="snippet svelte-xyz"><h3 class="stitle">'
    '<a href="https://openwebui.com/x" data-sveltekit-reload>Open WebUI: Self-Hosted AI Platform</a></h3>'
    "<p>Prompts, models, tools, functions, discussions, and reviews, all created by the "
    "community and available to everyone. Browse, install, or self-host today.</p></li>"
    '<li class="snippet svelte-xyz"><h3 class="stitle">'
    '<a href="https://github.com/open-webui/open-webui">Open WebUI on GitHub</a></h3>'
    "<p>Self-hostable AI interface with support for multiple model providers, including "
    "local ones. The repository describes setup, features, and community extensions.</p></li>"
    "</ol>"
    '<footer>Privacy | Feedback | Terms | About Brave Search</footer>'
    "</div></body></html>"
)
ex = BraveExtractor()
fitted = ex.fit(rendered(BRAVE_SERP_HTML))
# note: trafilatura keeps the result snippets but not the h3 titles in this
# list markup — assert on what the generic path actually preserves.
assert "self-host today" in fitted.md, fitted.md[:200]  # snippet body survives
assert "community extensions" in fitted.md, fitted.md[:200]  # second result too
assert fitted.flags.get("extractor") == "brave", fitted.flags
assert "spa_fallback" not in fitted.flags, fitted.flags  # generic path won
assert ex.accept(fitted)

# trafilatura-blind DOM (e.g. a future Svelte build): the visible-text fallback
# must rescue it and flag itself so the failure mode stays diagnosable.
import wellisearch.crawl.extractors.brave as _brave_mod
_orig_generic = _brave_mod.generic_md
_brave_mod.generic_md = lambda html: ""
try:
    fb = ex.fit(rendered(BRAVE_SERP_HTML))
finally:
    _brave_mod.generic_md = _orig_generic
assert "Self-Hosted AI Platform" in fb.md, fb.md[:200]
assert fb.flags.get("spa_fallback") is True, fb.flags
assert ex.accept(fb)

# repeated labels dedupe; scripts never leak into the fallback output
vt = _visible_text_markdown(BRAVE_SERP_HTML)
assert vt.count("Ask") == 1, vt[:200]
assert "var q=1" not in vt
assert len(vt.strip()) >= MIN_MD_CHARS
# a DOM with no visible text degrades to '' (never raises)
assert _visible_text_markdown("<html><body></body></html>") == ""

# nothing but an empty shell -> below the gate, reject rather than store junk
shell = ex.fit(rendered('<html><head><title>x</title></head><body></body></html>'))
assert not ex.accept(shell)
print("OK brave")

# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

REDDIT_COMMENTS = "".join(
    f'<shreddit-comment author="user{i}" depth="{i % 3}" score="{i}">'
    f'<div slot="comment"><p>Comment body {i} with useful advice.</p></div>'
    "</shreddit-comment>"
    for i in range(30)
)
REDDIT_HTML = (
    '<html><body><shreddit-post post-title="Dishwashers at Costco" comment-count="30">'
    '<div property="schema:articleBody"><p>Which dishwasher should I buy?</p></div>'
    "</shreddit-post>"
    '<div id="comments">' + REDDIT_COMMENTS + "</div>"
    '<aside>Unrelated recommendations and navigation</aside></body></html>'
)
reddit = RedditExtractor()
reddit_fit = reddit.fit(rendered(REDDIT_HTML))
assert reddit.accept(reddit_fit)
assert reddit_fit.title == "Dishwashers at Costco"
assert reddit_fit.signals["comments"] == 25
assert "Comment body 29" in reddit_fit.md
assert "Comment body 5" in reddit_fit.md
assert "Comment body 4" not in reddit_fit.md
assert reddit_fit.md.index("Comment body 29") < reddit_fit.md.index("Comment body 28")
assert "Unrelated recommendations" not in reddit_fit.md
assert reddit_fit.md.count("### ") == 25
assert "depth 1" in reddit_fit.md and "depth 2" in reddit_fit.md
with patch(
    "wellisearch.crawl.extractors.reddit.get_settings",
    return_value=SimpleNamespace(
        CRAWL_REDDIT_COMMENT_RANKING="best",
        CRAWL_REDDIT_MAX_COMMENTS=20,
    ),
):
    configured_fit = reddit.fit(rendered(REDDIT_HTML))
    best_url = comment_request_url("https://www.reddit.com/r/x/comments/123/?sort=new")
assert configured_fit.signals["comments"] == 20
assert "Comment body 0" in configured_fit.md
assert "Comment body 19" in configured_fit.md
assert "Comment body 20" not in configured_fit.md
assert "Reddit Best" in configured_fit.md
assert best_url.endswith("?sort=best")
partial_comments = "".join(re.findall(r"<shreddit-comment.*?</shreddit-comment>", REDDIT_COMMENTS)[:3])
partial_fit = reddit.fit(rendered(REDDIT_HTML.replace(REDDIT_COMMENTS, partial_comments)))
assert partial_fit.signals["comments"] == 3
assert partial_fit.signals["comments_reported"] == 30
assert reddit.accept(partial_fit), "the maximum must not become a minimum when Reddit renders fewer comments"
post_only = reddit.fit(rendered(REDDIT_HTML.replace(REDDIT_COMMENTS, "")))
assert not reddit.accept(post_only), "a comment-bearing post must not be stored without its comments"
no_count_html = REDDIT_HTML.replace('comment-count="30"', "").replace(REDDIT_COMMENTS, "")
count_missing = reddit.fit(rendered(no_count_html))
assert not reddit.accept(count_missing), "unknown comment count must not allow a post-only result"
empty_html = REDDIT_HTML.replace('comment-count="30"', 'comment-count="0"').replace(REDDIT_COMMENTS, "")
empty_thread = reddit.fit(rendered(empty_html))
assert reddit.accept(empty_thread), "a real zero-comment post should still be fetchable"
reddit_url = "https://www.reddit.com/r/Appliances/comments/1s8pw99/dishwashers_at_costco/"
assert match(reddit_url).name == "reddit"
assert isinstance(for_url(reddit_url), RedditExtractor)
assert comment_request_url(reddit_url).endswith("?sort=top")
assert comment_request_url(reddit_url + "?sort=new&foo=bar").endswith("?foo=bar&sort=top")
assert (
    comment_request_url("https://www.reddit.com/r/x/comments/123/post/?context=3#t1_abc")
    == "https://www.reddit.com/r/x/comments/123/post/?context=3&sort=top#t1_abc"
)
assert needs_refresh(reddit_url, "# Dishwashers at Costco\n\nWhich dishwasher should I buy?")
assert not needs_refresh(reddit_url, reddit_fit.md)
with patch(
    "wellisearch.crawl.extractors.reddit.get_settings",
    return_value=SimpleNamespace(
        CRAWL_REDDIT_COMMENT_RANKING="best",
        CRAWL_REDDIT_MAX_COMMENTS=25,
    ),
):
    assert needs_refresh(reddit_url, reddit_fit.md)
assert not needs_refresh("https://notreddit.com/r/x/comments/123", "post only")
assert not needs_refresh("https://old.reddit.com/r/x/comments/123", "post only")
assert not needs_refresh("https://www.reddit.com/r/Appliances/", "subreddit listing")
nsfw_gate = "This post contains mature content and may not be appropriate for certain viewers."
assert not needs_refresh(reddit_url, nsfw_gate), "a login wall can never hydrate comments; do not loop"
print("OK reddit")

# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

GREENHOUSE_HTML = (
    "<html><head><title>Sample Platform Engineer - Remote, Nationwide | Careers</title></head><body>"
    '<h1><span class="editor-placeholder">Sample Platform Engineer</span></h1>'
    '<script type="application/ld+json">'
    '{"@context":"http://schema.org","@type":"JobPosting",'
    '"title":"Sample Platform Engineer","employmentType":"FULL_TIME",'
    '"jobLocation":[{"@type":"Place","address":{"@type":"PostalAddress",'
    '"streetAddress":"Remote","addressLocality":"Remote","addressRegion":"Nationwide","addressCountry":"US"}}],'
    '"description":"<p>We are building the platform that keeps thousands of customer teams productive every day. '
    "The team ships small, reviewed changes through a fast continuous delivery pipeline with an emphasis on observability.</p>"
    "<ul><li>Design and build services in Python and Go with a focus on reliability</li>"
    "<li>Own deployment pipelines end to end and improve developer experience</li>"
    "<li>Mentor engineers on architecture, testing, and incident response</li></ul>\""
    "}</script>"
    '<div class="footer-noise">Cookie Notice: we use cookies. Third-party analytics vendors measure traffic. '
    "Performance preferences can be adjusted at any time.</div>"
    "</body></html>"
)
ex = GreenhouseExtractor()
fitted = ex.fit(rendered(GREENHOUSE_HTML))
assert fitted.title == "Sample Platform Engineer", fitted.title
assert "* Design and build services in Python" in fitted.md, fitted.md[:300]
assert "* Mentor engineers on architecture" in fitted.md  # bullets survive extraction
assert "**Location:** Remote, Nationwide, US — **Type:** Full Time" in fitted.md, fitted.md[:200]
assert "Cookie Notice" not in fitted.md  # footer noise outside the job body is excluded
assert ex.accept(fitted)
# fallback: no JSON-LD, only the server-rendered .job-description div (title from <h1>)
fallback = ex.fit(
    rendered(
        "<html><body><h1>Sample Ops Role</h1>"
        "<div class=\"job-description\"><p>We run the platform that keeps customers productive around the clock.</p>"
        "<ul><li>Triage production incidents and drive them to resolution</li></ul></div>"
        "</body></html>"
    )
)
assert fallback.title == "Sample Ops Role", fallback.title
assert "* Triage production incidents" in fallback.md, fallback.md[:200]
# non-job page (board index): no posting, nothing but nav -> gate fails
thin = ex.fit(rendered("<html><body><h1>Careers</h1><p>We hire great people.</p></body></html>"))
assert not ex.accept(thin)
print("OK greenhouse")

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
# Generic (loading-stub escalation)
# ---------------------------------------------------------------------------

from wellisearch.crawl.extractors.base import (
    LOADING_STUB_MAX_CHARS,
    GenericExtractor,
    _is_loading_stub,
)

assert _is_loading_stub(
    "Intro text about the page.", "<html><body>Loading...</div></body></html>"
) is True
assert _is_loading_stub(
    ("x" * 1600), "<html><body>Loading...</body></html>"
) is False  # long body: not a stub
# 'loading...' inside <script> (JS bundle spinner template) must not count
assert _is_loading_stub(
    "Short real content.", '<html><body><p>x</p><script>var t = "Loading...";</script></body></html>'
) is False
gen = GenericExtractor()
stub_html = (
    "<html><head><title>Used NVIDIA GeForce RTX 3090 Ti for Sale</title></head><body>"
    "<h1>Used NVIDIA GeForce RTX 3090 Ti for Sale</h1>"
    "<p>The cheapest listing right now is $1,633. Listings are refreshed every "
    "30 minutes and prices are live asking prices from eBay and Amazon.</p>"
    "<div>Loading...</div>"
    "</body></html>"
)
try:
    gen.fit(rendered(stub_html))
    raise AssertionError("expected Escalate for a loading stub")
except Escalate as e:
    assert e.tier == "browser", e.tier
# a long body that merely mentions 'loading...' is real content -> no escalation
long_body = (
    "<html><head><title>Long Page</title></head><body>"
    "<p>" + "This filler paragraph exists only to push the extracted markdown well past "
    "the loading-stub length threshold, so the extractor treats the page as real content. " * 12 + "</p>"
    "<div>Loading...</div>"
    "</body></html>"
)
fitted = gen.fit(rendered(long_body))
assert len(fitted.md.strip()) > LOADING_STUB_MAX_CHARS, len(fitted.md)
print("OK generic loading stub")

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

assert for_url("https://www.amazon.com/dp/B08WM3LJQB").name == "amazon"
assert for_url("https://boards.greenhouse.io/acme/1234567").name == "greenhouse"
assert for_url(
    "https://careers.ascensus.com/jobs/principal-software-engineer?source=linkedin_posting"
).name == "greenhouse"
assert for_url("https://search.brave.com/search?q=openwebui").name == "brave"
assert for_url("https://www.nytimes.com/2026/x.html").name == "nytimes"
assert for_url("https://example.com/x").name == "generic"
print("OK registry")

# ---------------------------------------------------------------------------
# title_from_markdown
# ---------------------------------------------------------------------------

assert title_from_markdown(
    "Nav junk\n# Real Article Title\nBody text of the article."
) == "Real Article Title", "H1 must win over an earlier plain line"
nav_md = "[Skip to main content](#main)\nActual Headline Here\nSome body copy."
assert title_from_markdown(nav_md) == "Actual Headline Here", "first link line must be skipped"
assert "[Skip" not in (title_from_markdown(nav_md) or ""), "title must never contain the nav link"
assert title_from_markdown(
    "---\nPlain Text Line\nMore body text."
) == "Plain Text Line", "symbol-only first line must be skipped"
long_line = "w" * 200
assert len(title_from_markdown(long_line)) == TITLE_MAX_LEN, "title must cap at TITLE_MAX_LEN"
assert title_from_markdown("") is None, "empty md must yield None"
assert title_from_markdown("   \n") is None, "whitespace-only md must yield None"
assert title_from_markdown("[a](b)\n[c](d)") is None, "all-link md must yield None"
mixed_nav = "Home | [Log in](/login)\nReal Headline Below\nSome body copy."
assert title_from_markdown(mixed_nav) == "Real Headline Below", "mixed nav-junk line with a link must be skipped"
assert title_from_markdown("___\nPlain Title Here\nBody text.") == "Plain Title Here", \
    "underscore HR (symbol-only, word-char-ish) first line must be skipped"
fenced_h1 = (
    "```\n# Fake H1 inside a code block\nx = 1\n```\n"
    "# Real Article Heading\nBody.\n"
)
assert title_from_markdown(fenced_h1) == "Real Article Heading", \
    "H1 matches inside fenced code blocks must be skipped"
fenced_line = (
    "```\nsome code line here\n# not a title either\n```\n"
    "The Real Title Line\nBody text.\n"
)
assert title_from_markdown(fenced_line) == "The Real Title Line", \
    "lines inside fenced code blocks must never become titles"
print("OK title_from_markdown")

print("ALL EXTRACTOR TESTS PASSED")

"""Unit tests: chunker + truncation + renderers (pure logic, no DB)."""
from __future__ import annotations

from wellisearch.chunk import chunk_markdown
from wellisearch.config import get_settings
from wellisearch.crawl.tiers.http import _extract_title as http_extract_title
from wellisearch.crawl.tiers.stealth import _extract_title as stealth_extract_title
from wellisearch.fetch import render_fetch_page_markdown, render_fetch_pages_markdown
from wellisearch.index import _with_title
from wellisearch.search_web import (
    _apply_job_board_penalty,
    _cap_per_domain,
    _candidate_rows,
    _is_job_board,
    _job_intent,
    _registrable_domain,
    render_search_markdown,
)
from wellisearch.serialize import format_timing
from wellisearch.truncation import (
    allocate_budgets,
    boundary_cut_head,
    boundary_cut_tail,
    truncate_page,
)
from wellisearch.url_filter import garbage_reason, is_garbage_url
from wellisearch.urlnorm import normalize_url

# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

md = "Intro paragraph. " * 100
md += "\n\n# Section One\n" + "text " * 300
md += "\n\n" + "```python\n" + "x = 1\n" * 200 + "```\n"
md += "\n\n# Section Two\n" + "more " * 300
md += "\n\n" + "tiny tail"
chunks = chunk_markdown(md, 800)
assert chunks, "no chunks"
assert all(len(c) > 0 for c in chunks)
for c in chunks:
    assert c.count("```") % 2 == 0, "unbalanced fence in: " + c[:80]
print("chunks:", len(chunks))
print("OK chunker")

# ---------------------------------------------------------------------------
# Title Prepend (index._with_title)
# ---------------------------------------------------------------------------

body = "para one\n\npara two"
assert _with_title(body, None) == body, "no title -> unchanged"
assert _with_title(body, "") == body, "empty title -> unchanged"
assert _with_title("", "T") == "", "empty markdown -> unchanged"
out = _with_title(body, "My Title")
assert out == "# My Title\n\n" + body, out[:40]
md_h1 = "# My Title\n\nbody"
assert _with_title(md_h1, "My Title") == md_h1, "extractor-emitted H1 -> no double prepend"
md_ci = "# my  title \n\nbody"
assert _with_title(md_ci, "My Title") == md_ci, "case/whitespace-insensitive match -> no double prepend"
md_other = "# Other Heading\n\nbody"
assert _with_title(md_other, "My Title") == "# My Title\n\n" + md_other, \
    "different H1 -> prepended above it"
print("OK title prepend")

# ---------------------------------------------------------------------------
# URL Normalization (urlnorm.normalize_url)
# ---------------------------------------------------------------------------

assert normalize_url("https://ex.com/a?x=1") == "https://ex.com/a?x=1", \
    "content params kept"
assert normalize_url(
    "https://ex.com/a?utm_source=x&utm_medium=y&refId=abc&trackingId=z&gi=h&mode=location"
) == "https://ex.com/a?mode=location", "tracking params dropped, content kept"
assert normalize_url(
    "https://www.amazon.com/dp/B0HDBD77SD?ref=x&ref_=y&social_share=z&rsd=w&edk=v&psc=1"
) == "https://www.amazon.com/dp/B0HDBD77SD", \
    "amazon a.co redirect tracking params dropped"
assert normalize_url("https://ex.com/a?x=1&amp;y=2") == "https://ex.com/a?x=1&y=2", \
    "&amp; unescaped"
assert normalize_url("https://ex.com/a#frag") == "https://ex.com/a", "fragment dropped"
assert normalize_url("https://EX.com/A") == "https://ex.com/A", "host lowercased"
li = "https://in.linkedin.com/jobs/view/principal-software-engineer-at-gm-4446858972?refId=x&trackingId=y"
assert normalize_url(li) == "https://www.linkedin.com/jobs/view/4446858972", \
    "linkedin slug + locale + tracking -> canonical"
assert normalize_url("https://www.linkedin.com/jobs/view/4446858972/") == \
    "https://www.linkedin.com/jobs/view/4446858972", "trailing slash dropped on linkedin jobs"
assert normalize_url("https://ex.com/jobs/view/123") == "https://ex.com/jobs/view/123", \
    "non-linkedin /jobs/view untouched"
once = normalize_url(li)
assert normalize_url(once) == once, "idempotent"
print("OK url normalization")

# ---------------------------------------------------------------------------
# Tier Title Extraction (last <title> wins, matching browser document.title)
# ---------------------------------------------------------------------------


class _FakeTitleEl:
    def __init__(self, text):
        self._text = text

    def get_all_text(self):
        return self._text


class _FakeStealthPage:
    def __init__(self, texts):
        self._texts = texts

    def css(self, selector):
        assert selector == "title"
        return [_FakeTitleEl(t) for t in self._texts]


assert http_extract_title("<html><head><title>Only</title></head></html>") == "Only", \
    "single title extracted"
two = "<head><title>Medium</title><meta x='1'><title>Real Article | by Author</title></head>"
assert http_extract_title(two) == "Real Article | by Author", "last of two titles wins"
assert http_extract_title("<html><body>no title here</body></html>") is None, \
    "absent -> None"
empty_first = "<head><title>   </title><title>Second</title></head>"
assert http_extract_title(empty_first) == "Second", "blank first skipped"
empty_last = "<head><title>First</title><title>  </title></head>"
assert http_extract_title(empty_last) == "First", "trailing blank falls back to earlier"

assert stealth_extract_title(_FakeStealthPage(["Medium", "Real Article"])) == \
    "Real Article", "stealth: last of two titles wins"
assert stealth_extract_title(_FakeStealthPage([])) is None, "stealth: no title -> None"
assert stealth_extract_title(_FakeStealthPage(["  ", "Second"])) == "Second", \
    "stealth: blank first skipped"
print("OK tier title extraction")

# ---------------------------------------------------------------------------
# Boundary Cuts
# ---------------------------------------------------------------------------

t = "word " * 5000
h = boundary_cut_head(t, 1000)
assert len(h) <= 1000
tt = boundary_cut_tail(t, 1000)
assert len(tt) <= 1000
html = "plain " * 200 + '<div class="x">' + "tail " * 300
h2 = boundary_cut_head(html, 1500)
assert h2.count("<") == h2.count(">"), (h2.count("<"), h2.count(">"))
t2 = "word " * 5000
h3 = boundary_cut_head(t2, 7)  # tiny budget must not crash
assert len(h3) <= 7
print("OK boundary cuts")

# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

b = allocate_budgets("even", [1000, 2000, 3000], [0, 0, 0], 6000, None)
# even split = 2000 each, clamped to page length (page 1 only has 1000)
assert b == [1000, 2000, 2000], b
b = allocate_budgets("even", [5000, 5000], [0, 0], 6000, None)
assert b == [3000, 3000], b
b = allocate_budgets("head", [1000, 2000, 3000], [0, 0, 0], 6000, None)
assert b == [1000, 2000, 3000], b
b = allocate_budgets("priority", [1000, 1000, 1000], [9, 0, 0], 6000, None)
assert b[0] > b[1] == b[2], b
b = allocate_budgets("smart", [1000, 1000], [0, 0], 6000, None)
assert b == [1000, 1000], b  # no prominence -> even, capped by page length
b = allocate_budgets("tail", [500, 500], [0, 0], 800, None)
assert b == [400, 400], b
b = allocate_budgets("priority", [5000, 1000], [9, 1], 6000, 2000)
assert all(x <= 2000 for x in b), b
b = allocate_budgets("smart", [100, 100], [5, 1], 1000, None)
assert b == [100, 100], b  # budget bigger than content: no truncation
print("OK allocation")

# ---------------------------------------------------------------------------
# Per-Page Trim
# ---------------------------------------------------------------------------

text, trunc = truncate_page("x" * 100, 50, "head")
assert trunc and len(text) <= 50
text, trunc = truncate_page("x" * 100, 50, "tail")
assert trunc
text, trunc = truncate_page("x" * 100, 500, "head")
assert not trunc and text == "x" * 100
print("OK per-page trim")

# ---------------------------------------------------------------------------
# Timing Header (feature: response timing)
# ---------------------------------------------------------------------------

# format_timing: None/empty -> no line
assert format_timing(None) is None
assert format_timing({}) is None
# total only
assert format_timing({"total_ms": 120}) == "Time: 120 ms"
# local-only search: index leg only
assert format_timing({"total_ms": 120, "index_ms": 100}) == "Time: 120 ms (index: 100 ms)"
# provider search: index + provider legs
assert format_timing({"total_ms": 1200, "index_ms": 100, "provider_ms": 1050}) == \
    "Time: 1200 ms (index: 100 ms, provider: 1050 ms)"
# fetch crawl: index + crawl legs
assert format_timing({"total_ms": 2300, "index_ms": 10, "crawl_ms": 2250}) == \
    "Time: 2300 ms (index: 10 ms, crawl: 2250 ms)"
print("OK format_timing")

# search renderer: local hit -> Time line with index only, no provider
out = {
    "source": "local", "degraded": False, "count": 1,
    "results": [{"url": "https://x.com/a", "title": "A", "snippet": "s"}],
    "timing": {"total_ms": 120, "index_ms": 100},
}
md = render_search_markdown(out)
assert "Time: 120 ms (index: 100 ms)" in md, md
assert "provider:" not in md, md
# search renderer: provider hit -> Time line with index + provider
out["source"] = "brave"
out["timing"] = {"total_ms": 1200, "index_ms": 100, "provider_ms": 1050}
md = render_search_markdown(out)
assert "Time: 1200 ms (index: 100 ms, provider: 1050 ms)" in md, md
# search renderer: no timing -> no Time line (backward compatible)
del out["timing"]
md = render_search_markdown(out)
assert "Time:" not in md, md
print("OK render_search_markdown timing")

# fetch_page renderer: from index -> Time line with index only
out = {
    "ok": True, "url": "https://x.com/a", "title": "A", "markdown": "body",
    "chars": 4, "truncated": False, "from_index": True,
    "timing": {"total_ms": 45, "index_ms": 40},
}
md = render_fetch_page_markdown(out)
assert "Time: 45 ms (index: 40 ms)" in md, md
assert "crawl:" not in md, md
# fetch_page renderer: crawled -> Time line with index + crawl
out["from_index"] = False
out["timing"] = {"total_ms": 2300, "index_ms": 10, "crawl_ms": 2250}
md = render_fetch_page_markdown(out)
assert "Time: 2300 ms (index: 10 ms, crawl: 2250 ms)" in md, md
# fetch_page renderer: failure still carries a Time line
out = {"ok": False, "url": "https://x.com/bad", "error": "boom", "timing": {"total_ms": 30}}
md = render_fetch_page_markdown(out)
assert "Status: failed" in md and "Time: 30 ms" in md, md
# fetch_page renderer: no timing -> no Time line
out = {
    "ok": True,
    "url": "u",
    "title": "t",
    "markdown": "m",
    "chars": 1,
    "truncated": False,
    "from_index": True,
}
md = render_fetch_page_markdown(out)
assert "Time:" not in md, md
print("OK render_fetch_page_markdown timing")

# fetch_pages renderer: success -> Time line in the global header
out = {
    "ok": True, "pages_fetched": 1, "truncated": False, "total_chars": 10,
    "strategy": "smart", "budget": None,
    "pages": [
        {
            "url": "https://x.com/a",
            "title": "A",
            "content": "body",
            "chars": 4,
            "truncated": False,
            "from_index": True,
        },
    ],
    "timing": {"total_ms": 500, "index_ms": 20, "crawl_ms": 450},
}
md = render_fetch_pages_markdown(out)
assert "Time: 500 ms (index: 20 ms, crawl: 450 ms)" in md, md
# fetch_pages renderer: failure -> Time line in the header
out = {"ok": False, "error": "no valid urls provided", "pages": [], "timing": {"total_ms": 5}}
md = render_fetch_pages_markdown(out)
assert "Status: failed" in md and "Time: 5 ms" in md, md
# fetch_pages renderer: no timing -> no Time line
out = {"ok": True, "pages_fetched": 1, "truncated": False, "total_chars": 10,
       "strategy": "smart", "budget": None,
       "pages": [
           {
               "url": "u",
               "title": "t",
               "content": "c",
               "chars": 1,
               "truncated": False,
               "from_index": True,
           }
       ]}
md = render_fetch_pages_markdown(out)
assert "Time:" not in md, md
print("OK render_fetch_pages_markdown timing")

# ---------------------------------------------------------------------------
# URL Filter (garbage URL rejection)
# ---------------------------------------------------------------------------

# binary / non-page files must be rejected
assert is_garbage_url("https://example.com/video.mp4")
assert is_garbage_url("https://example.com/stream.m3u8")
assert is_garbage_url("https://example.com/seg-123.m4s")
assert is_garbage_url("https://example.com/photo.JPG?w=100")  # uppercase + query
assert is_garbage_url("https://example.com/archive.zip")
assert is_garbage_url("https://example.com/app.exe")
assert is_garbage_url("https://example.com/report.pdf")
assert garbage_reason("https://example.com/video.mp4") == "binary/non-page file (.mp4)"

# HLS .ts segments rejected; TypeScript source must pass
assert is_garbage_url("https://cdn.example.com/hls2/abc/seg-1-v1-a1.ts?t=123")
assert garbage_reason("https://cdn.example.com/hls2/abc/seg-1.ts") == "HLS video segment (.ts)"
assert not is_garbage_url("https://raw.githubusercontent.com/org/repo/main/src/client.ts")
assert garbage_reason("https://raw.githubusercontent.com/org/repo/main/src/client.ts") is None

# "hls"/"seg-" only count as segment markers in full path/filename components,
# so TypeScript source with those substrings must pass (not just not reject)
assert is_garbage_url("https://cdn.example.com/video/seg-12.ts")  # seg-<N> filename alone
assert not is_garbage_url("https://raw.githubusercontent.com/org/repo/main/src/hls-utils.ts")
assert garbage_reason("https://example.com/downloads/legacy-seg-archive.ts") is None

# legitimate pages must pass
assert not is_garbage_url("https://example.com/")
assert not is_garbage_url("https://example.com/blog/post")
assert not is_garbage_url("https://example.com/page.html")
assert not is_garbage_url("https://example.com/docs?version=2")
assert garbage_reason("https://example.com/blog/post") is None
print("OK url_filter")

# ---------------------------------------------------------------------------
# Refresh Backoff Formula
# ---------------------------------------------------------------------------

from wellisearch.config import Settings  # noqa: E402

s = Settings(REFRESH_MIN_AGE_HOURS=72, REFRESH_BACKOFF_BASE_HOURS=6)
assert s.refresh_backoff_hours(0) == 0, "a never-failed page has no backoff"
assert [s.refresh_backoff_hours(k) for k in (1, 2, 3, 4)] == [6, 12, 24, 48]
assert s.refresh_backoff_hours(5) == 72, "backoff caps at the min-age horizon"
assert s.refresh_backoff_hours(99) == 72
small = Settings(REFRESH_MIN_AGE_HOURS=10, REFRESH_BACKOFF_BASE_HOURS=6)
assert small.refresh_backoff_hours(2) == 10, "cap applies even for a small min-age"
print("OK refresh backoff formula")

# ---------------------------------------------------------------------------
# Failure Detail
# ---------------------------------------------------------------------------

from wellisearch.crawl.results import CrawlResult  # noqa: E402
from wellisearch.crawler import failure_detail  # noqa: E402


def _result(attempts):
    return CrawlResult(ok=False, title=None, md="", tier="browser", ms=1, attempts=attempts)


d = failure_detail(
    _result(
        [
            {"tier": "http", "error": "ssl.SSLCertVerificationError: certificate has expired"},
            {"tier": "browser", "error": "botwall: turnstile-challenge", "status": 403},
        ]
    )
)
assert d.startswith("http: ssl.SSLCertVerificationError"), d
assert "(http 403)" in d, d
assert "CRAWL_IGNORE_SSL_ERRORS" in d, "cert error must carry the actionable hint"

d = failure_detail(
    _result([{"tier": "browser", "error": "gate failed", "status": 200, "md_chars": 87}])
)
assert "(http 200)" in d and "[87 chars]" in d, d
assert "CRAWL_IGNORE_SSL_ERRORS" not in d, "non-TLS failures must not get the hint"

d = failure_detail(
    _result([{"tier": "stealth", "error": "httpx.ReadTimeout: request timed out"}])
)
assert "ReadTimeout" in d and "CRAWL_IGNORE_SSL_ERRORS" not in d, d

assert failure_detail(_result([])) == "all tiers failed or empty markdown"
print("OK failure detail")

# ---------------------------------------------------------------------------
# Search Serving Policies (search_web auto-mode helpers)
# ---------------------------------------------------------------------------

# _registrable_domain: www stripping, plain host, two-part TLDs, garbage in
assert _registrable_domain("https://www.linkedin.com/jobs/view/1") == "linkedin.com"
assert _registrable_domain("graphapp.dev/blog/post") == "graphapp.dev"
assert _registrable_domain("HTTPS://WWW.Example.COM/a") == "example.com", \
    "host lowercased"
assert _registrable_domain("https://shop.example.co.uk/page") == "example.co.uk", \
    "two-part TLD -> three labels"
assert _registrable_domain("not a url") == ""
assert _registrable_domain("") == ""

# _is_job_board: host[/path] prefix matching, subdomains, path boundaries
boards = ("linkedin.com/jobs", "indeed.com")
assert _is_job_board("https://www.linkedin.com/jobs/view/123?refId=x", boards)
assert _is_job_board("https://uk.linkedin.com/jobs/search?keywords=a", boards), \
    "subdomain of listed host matches"
assert not _is_job_board("https://www.linkedin.com/company/foo", boards), \
    "path outside the /jobs prefix must not match"
assert not _is_job_board("https://www.linkedin.com/jobsearch", boards), \
    "/jobsearch is not under /jobs/"
assert _is_job_board("https://www.indeed.com/jobs/123", boards)
assert not _is_job_board("https://graphapp.dev/blog/post", boards)

# _job_intent: word-boundary match on the configured terms
terms = ("job", "jobs", "hiring", "open roles", "careers")
assert _job_intent("principal software engineer jobs", terms)
assert not _job_intent("Transitioning from Staff to Principal Software Engineer", terms), \
    "career-advice query is not job intent"
assert _job_intent("Hiring senior engineers in Austin", terms)
assert not _job_intent("career transition advice", terms), \
    "'career' singular must not match 'careers'"

# _apply_job_board_penalty: score+sim halved for boards, coverage untouched
rows = [
    {"url": "https://www.linkedin.com/jobs/view/1", "score": 0.2, "similarity": 0.8, "coverage": 0.9},
    {"url": "https://graphapp.dev/blog/post", "score": 0.15, "similarity": 0.76, "coverage": 0.8},
]
pen = _apply_job_board_penalty(rows, ("linkedin.com/jobs",), 0.5)
assert pen[0]["score"] == 0.1 and pen[0]["similarity"] == 0.4, "board row de-ranked"
assert pen[0]["coverage"] == 0.9, "coverage must stay untouched"
assert pen[1] is rows[1], "non-board row passes through unchanged"

# _cap_per_domain: first N per registrable domain kept in order
rows = [{"url": f"https://www.linkedin.com/jobs/view/{i}"} for i in range(5)] + [
    {"url": f"https://{d}/a"} for d in ("graphapp.dev", "byjlw.com", "reddit.com")
]
capped = _cap_per_domain(rows, 2)
assert len(capped) == 5, [r["url"] for r in capped]
assert [r["url"] for r in capped[:2]] == [u["url"] for u in rows[0:2]], \
    "first two linkedin kept in order"
assert all("linkedin.com" not in r["url"] for r in capped[2:])

# _candidate_rows: a high-similarity row buried by lexical mass is still included
rows = [
    {"url": f"https://row{i}.example.com", "score": 1.0 - i * 0.001, "similarity": None}
    for i in range(130)
]
rows[119]["similarity"] = 0.9  # score rank #120 — beyond the default gate window
cands = _candidate_rows(rows)
assert rows[119] in cands, "high-sim row outside the score window must be admitted"
_s = get_settings()
assert len(cands) <= min(len(rows), _s.SEARCH_GATE_MIN_K + _s.SEARCH_TOP_BY_SIM)
print("OK search serving policies")

# ---------------------------------------------------------------------------
# Version Single-Source-Of-Truth
# ---------------------------------------------------------------------------

# installed package metadata (pyproject, via hatch) must equal the source
# of truth (wellisearch.__version__). Skipped for source-tree dev runs where
# the package is not installed.
import importlib.metadata as _im
import wellisearch as _ws

try:
    _meta_ver = _im.version("wellisearch")
except _im.PackageNotFoundError:
    print("SKIP version consistency (package not installed)")
else:
    assert _meta_ver == _ws.__version__, f"metadata {_meta_ver} != __version__ {_ws.__version__}"
    print("OK version consistency:", _meta_ver)

print("ALL UNIT TESTS PASSED")

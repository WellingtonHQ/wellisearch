"""Unit tests: chunker + truncation + renderers (pure logic, no DB)."""
from wellisearch.chunk import chunk_markdown
from wellisearch.fetch import render_fetch_page_markdown, render_fetch_pages_markdown
from wellisearch.serialize import format_timing
from wellisearch.search_web import render_search_markdown
from wellisearch.truncation import (
    allocate_budgets,
    boundary_cut_head,
    boundary_cut_tail,
    truncate_page,
)

# --- chunker ---------------------------------------------------------------
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

# --- boundary cuts ----------------------------------------------------------
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

# --- allocation -------------------------------------------------------------
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

# --- per-page trim ----------------------------------------------------------
text, trunc = truncate_page("x" * 100, 50, "head")
assert trunc and len(text) <= 50
text, trunc = truncate_page("x" * 100, 50, "tail")
assert trunc
text, trunc = truncate_page("x" * 100, 500, "head")
assert not trunc and text == "x" * 100
print("OK per-page trim")

# --- timing header (feature: response timing) ------------------------------
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
out = {"ok": True, "url": "u", "title": "t", "markdown": "m", "chars": 1, "truncated": False, "from_index": True}
md = render_fetch_page_markdown(out)
assert "Time:" not in md, md
print("OK render_fetch_page_markdown timing")

# fetch_pages renderer: success -> Time line in the global header
out = {
    "ok": True, "pages_fetched": 1, "truncated": False, "total_chars": 10,
    "strategy": "smart", "budget": None,
    "pages": [{"url": "https://x.com/a", "title": "A", "content": "body", "chars": 4, "truncated": False, "from_index": True}],
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
       "pages": [{"url": "u", "title": "t", "content": "c", "chars": 1, "truncated": False, "from_index": True}]}
md = render_fetch_pages_markdown(out)
assert "Time:" not in md, md
print("OK render_fetch_pages_markdown timing")

print("ALL UNIT TESTS PASSED")

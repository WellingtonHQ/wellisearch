"""Unit tests: chunker + truncation (pure logic, no DB)."""
from wellisearch.chunk import chunk_markdown
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

print("ALL UNIT TESTS PASSED")

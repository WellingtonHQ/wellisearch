# PR Review Round 11 — New Findings

Recorded from the opencode-review comment on issue #15 (2026-10-11T16:55Z),
generated against `c51317f`. All 25 issues reported in prior rounds were verified
fixed; three new nits followed.

## 1. Bulk fetches serve duplicate content for variant URLs — FIXED

- **Where:** `fetch.py` `fetch_pages` (was lines 240–255)
- **Why it matters:** `_validate_urls` dedupes exact strings only, so
  `["a?utm_source=x", "a"]` both resolve to the same canonical row and both get a
  budget slice: two identical sections, `total_chars` double-counts one page's body,
  and `pages_fetched` overcounts distinct pages. An LLM caller burns shared char
  budget on repeated text instead of covering more pages.
- **Fix applied:** after `_resolve_all`, collapse `resolved` by
  `normalize_url(p["url"])` (first-seen order) before budget allocation — one page is
  served once per bulk call (`b848fe3`). Regression tests:
  `test_fetch_pages_bumps_counts_in_one_statement` and
  `test_bulk_read_serves_variant_pages_once`.

## 2. Stale DDL comment on `content_hash` — OPEN

- **Where:** `schema.sql:15` (the comment); the digest is computed over
  `_with_title(markdown, title)` at `index.py:66-67`
- **Why it matters:** the comment reads "sha256 of fit_markdown", but this release
  changed the formula to hash the title-prefixed chunk source; a reader maintaining
  the staleness logic will believe body-only changes are detected when title changes
  also flip the hash.
- **Suggested fix:** update the comment to "sha256 of the title-prefixed chunk
  source; unchanged → skip re-embed".

## 3. Overstated "canonical URL" comment in single-fetch response — OPEN

- **Where:** `fetch.py:180-181` (comment + field); normalization is conditional at
  `fetch.py:329-335`
- **Why it matters:** for a non-short variant input (e.g. `a?utm_source=x`),
  `resolve_short_url` returns the URL unchanged and normalization only applies when
  short-URL resolution changed it, so the response echoes the requested variant while
  the comment claims "The canonical URL". Behavior is harmless — counting normalizes
  separately at `fetch.py:163` — but the comment misdescribes the wire contract.
- **Suggested fix:** either normalize unconditionally (`url = normalize_url(resolved)`)
  so the reported URL always keys on the index row, or reword the comment to "the
  requested URL, resolved for short links".

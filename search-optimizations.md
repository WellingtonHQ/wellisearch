# Search optimizations

Read-only audit on 2026-09-27. The aim is to serve the local index when it has
**distinct, relevant answers that satisfy the query**, and use the provider
gateway otherwise. These are observed failure modes and proposed improvements,
not implemented behavior.

## How the current decision works

[`fn_search_local`](src/wellisearch/schema.sql) fuses full-text, bounded
trigram, and vector matches at the chunk level; it orders pages by score before
computing their best-chunk similarity. Coverage counts distinct PostgreSQL
English lexemes in the page's title and body. In auto mode,
[`search_web`](src/wellisearch/search_web.py) fetches at least `SEARCH_GATE_MIN_K`
(default fifty) ranked URLs, filters by age if requested, and serves local when
at least `k` rows (default five) have coverage >= 0.5, distinctive_coverage >=
1.0 (every rare/brand query word present — a word in <1% of chunks), and
similarity >= 0.3. Partial sets now also serve when at least three rows pass
those gates, or when one row additionally clears similarity >= 0.55. A miss
calls the provider; an explicit local request bypasses the gate. The score
ranks rows, but does not qualify them. Among passing rows, serving order is
similarity desc with score as tie-break (2026-09-29), so a semantically strong
page leads even when lexically matching pages hold more RRF mass.

## Confirmed cases

1. **Query constraints disappear or become mere word matches.** PostgreSQL
   parses `Best Costco Dishwasher under $700` as `{best, costco, dishwash,
   700}`: it retains `700`, but drops `under`. `Costco dishwasher under $700`
   and `Costco dishwasher over $700` yield the same lexical query; likewise
   `Best Costco Dishwasher not Bosch` and the version without `not` yield the
   same lexical query. A live Costco catalog variant scores coverage 0.5 and
   similarity about 0.78 while matching neither `best` nor `700`. The literal
   price query had ten passing rows in the first ten, but a passing row does
   not establish a dishwasher below $700, and pages without Costco can pass.
   The previously reported 0.67 coverage for this query was measured on an
   accidentally truncated query without `$700`; it must not be used to
   calibrate the literal query. See
   [`schema.sql:181-200`](src/wellisearch/schema.sql) and
   [`schema.sql:315-321`](src/wellisearch/schema.sql).

2. **Copies count as independent answers.** For `Costco dishwashers`, the
   first five passing URLs are variants of the same `/dishwashers.html` body
   with one content hash. The first ten passing rows contain only two distinct
   hashes; nine indexed catalog URL variants share the first hash. The gate
   counts rows by full URL, so `k=5` can mean five copies of one page. See
   [`schema.sql:278-292`](src/wellisearch/schema.sql) and
   [`search_web.py:102-127`](src/wellisearch/search_web.py).

3. **Page-wide words and an unrelated best chunk can pass together.** For
   `bees make honey`, the first ten include six passing rows, among them a
   wasp-removal article about an air conditioner (coverage 1.0, similarity
   0.417) and a Vitex plant page. None of the wasp article's chunks contains
   all three search terms. Coverage is calculated across the whole page while
   similarity is the best of any chunk, so passing both is not proof of a
   coherent answer. The probed word-list pages failed the similarity gate. See
   [`schema.sql:301-348`](src/wellisearch/schema.sql).

4. **Filtering after the small result window misses eligible pages.** With
   `Costco dishwashers` and a one-day age cutoff, four qualified fresh rows
   occur in the first ten versus nineteen in the first fifty. The partial
   gate now serves four, but still misses many eligible results outside its
   window. Rows with no crawl timestamp are currently retained by the age
   filter. See [`search_web.py:205-224`](src/wellisearch/search_web.py).
   (Partially addressed 2026-09-29: the default gate window widened from ten
   to fifty rows and passing rows serve in similarity order; applying the age
   filter before the window remains open.)

5. **The regression corpus does not cover all default-k quality failures.**
   It now tests `k=5` strong and moderate partial serving, but the shopping
   case uses `k=2`; it cannot detect duplicate-only `k=5` serving, price or
   negation inversions, off-topic pages mixed among five answers, or the
   age-window miss. Its fixture coverage values, 0.5 and 0.75, for the literal
   `$700` query do not prove an under-$700 product. See
   [`tests/test_search_regression.py`](tests/test_search_regression.py).

6. **Lexical mass outranks semantic closeness.** For `Transitioning from Staff to Principal Software Engineer` (2026-09-29), a Reddit career-advice thread carries the second-highest similarity in the index (0.732, coverage 1.0) yet ranks #53 by score — outside the default gate window of fifty rows. Its score is almost entirely its vector-leg contribution alone (~1/62 ≈ 0.016): no single chunk contains all five query lexemes (zero FTS-AND credit), and it misses the bounded trigram pool, which LinkedIn postings dominate via verbatim "Principal Software Engineer" titles. Those postings collect two to three legs at strong ranks (up to three chunks per page counted per leg) plus a `fetch_count` bonus (+0.007 at fc=4–5 vs 0), so their RRF mass is ~3× the thread's; freshness was equal (all rows crawled the same morning). A single excellent semantic leg cannot beat several strong lexical legs plus prominence. See [Meaning over words](#meaning-over-words) for fix options and the job-board de-rank / per-domain cap shipped alongside.

## Recommended order

1. **Count distinct qualified pages.** Collapse verified-equivalent URL
   variants before serving *and* counting `k`, using conservative URL
   normalization and matching content hashes. Do not drop every query string:
   some filter parameters identify genuinely different pages. Look past
   duplicates for additional candidates. This may increase provider calls
   when fewer than three independent passing pages remain and none is strong.

2. **Respect decisive constraints.** Detect clear exclusions, retailer/site
   intent, and numeric relationships such as a price ceiling or units. Require
   evidence for them in a candidate's relevant content; if the index cannot
   verify a decisive condition, defer. Finding the literal number `700`
   somewhere on a page is not evidence of a product priced under $700. Expect
   more provider calls on constrained queries until this evidence is indexed.

3. **Check coherent evidence.** Match important query terms near a relevant
   title, heading, or chunk rather than combining page-wide mentions with
   similarity from an unrelated chunk. Preserve page-wide retrieval for
   recall, and test multi-section catalog pages before making passage checks
   a hard requirement.

4. **Decide after filtering and deduplication.** Apply requested freshness
   and other eligibility before the final `k`-result window, or progressively
   widen a bounded window. Benchmark SQL latency against the configured
   statement timeout; wider windows are not free.

## Meaning over words

The ranking core fuses three legs with equal rank-based credit (`1/(60+rank)`), so a page's semantic closeness earns at most ~1/61 per leg while lexical pages stack up to six chunk-leg credits. Similarity currently drives only the gate and auto-mode sort order, never the score or candidate selection. Options, cheapest first:

1. **Widen the vector leg.** `fn_search_local` caps the vector leg at fifty rows (schema.sql); a semantically close but lexically thin page can miss the pool entirely. Make the cap configurable (`vec_limit`, default two hundred) — HNSW early-stop keeps it cheap.
2. **Semantic-aware candidate window.** Candidates = top-N by score ∪ top-M by similarity (fetch ~150 rows, union in Python). A sim-0.73 page is then always considered regardless of lexical rank; removes the arbitrary fixed window.
3. **Blend similarity into the score.** `score' = rrf_mass × (w + (1−w)·sim)` with configurable w makes local mode and tie-breaks meaning-aware too. Needs calibration against the regression corpus so exact-phrase queries don't regress.
4. **Cross-encoder rerank of gated candidates.** Rerank passing rows with a local MiniLM passage-reranker (~100–300 ms) — true semantic precision ranking over hybrid recall, Google-style ranker. Adds another model to keep in sync with the EMBED_MODEL invariant.
5. **Query-type-aware weighting.** Informational queries ("how to transition X→Y") weight semantics up; exact-phrase/product queries stay lexical-first. Reuses the job-intent detection machinery (`SEARCH_JOB_INTENT_TERMS`).

## Other ranking-quality improvements

1. **Query-aware snippets.** `fn_search_local` returns the first 400 chars of the best-RRF chunk as the snippet; pick instead the 400-char window inside that chunk with the most query-term hits (SQL in the `bestchunk` CTE). Visible on every result.
2. **Title-match boost.** Small additive score bonus when query content words appear in the page title — titles already ride along via H1 prepend, so this sharpens exact-title matches further. Requires a schema change to `fn_search_local`.
3. **Freshness curve redesign.** The current decay is `exp(-age_days/14)` (schema.sql) — aggressive for evergreen content (~9.7-day half-life). A 30-day grace + 90-day half-life curve is designed in docs/adaptive-refresh.md; implement as a separate task.
4. **Cross-domain duplicate suppression.** Syndicated copies of the same article (e.g., a Medium mirror on a personal domain) rank as independent answers. Same-URL variants are already collapsed by content hash; cross-domain needs content-similarity dedup — design first.
5. **Dead config cleanup.** `SEARCH_MIN_SCORE` and `STALE_HOURS` are unreferenced in src/ — remove or wire up.

## How to evaluate changes

Add a fixed corpus with judged relevant pages and explicit constraint labels.
At the default `k=5`, test the literal under/over-$700 and `not Bosch` pairs,
nine equivalent catalog variants, a five-distinct-page control, the wasp and
word-list cases, and a one-day freshness request. Assert both the source
decision and the distinct, relevant results. Include numbered units, a
multi-section catalog, and navigational/site queries as new cases before
changing retrieval. Compare bad-local-serve rate, constraint violations,
distinct relevant results in the top five, unnecessary provider deferrals,
provider calls, and search latency. Keep the full qualified-set requirement
while improving what qualifies and what counts as a distinct answer.

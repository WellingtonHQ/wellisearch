# Problematic local searches

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

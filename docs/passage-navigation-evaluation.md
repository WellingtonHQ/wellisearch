# Passage navigation: evaluation and expected behavior

This document describes a proposed `fetch_page` enhancement, not a shipped API.
First compare passage-selection methods; do not select a production algorithm or
similarity threshold from intuition or from this small synthetic dataset alone.

## Expected behavior

- Without navigation parameters, existing callers receive the same content and
  truncation behavior as today.
- An offset identifies a zero-based Unicode character position in the extracted
  Markdown, not a byte position or an offset into HTML. A returned range and
  `next_offset` allow sequential reads without gaps or duplicated characters,
  including when a response is cut at a whitespace boundary. An end-of-page
  offset returns an empty final range; negative offsets are rejected.
- A query selects a relevant passage in that same Markdown, even when it occurs
  beyond a long preamble. An exact identifier should distinguish similar error
  codes, model names, versions, and configuration keys. A paraphrase should find
  a relevant passage when it uses different words.
- Query responses identify the selected range and whether there is adequate
  evidence of a match. A missing answer is not silently described as a match.
  If semantic ranking is unavailable and a fallback is used, report that fact.
- When continuing a query-selected passage sequentially, use the returned next
  offset without the query. A query with an offset searches after that offset;
  exact boundary behavior still needs to be specified and tested.
- Offsets refer to a particular content revision. The implementation must detect
  or explicitly report a changed page between requests, and must not map chunks
  from one version onto another version's Markdown.

## Benchmark

Run `python benchmarks/passage_bench.py` from an environment with the installed
wellisearch dependencies and the configured embedding model. It prints JSON to
stdout; redirect it to a file outside source control if desired. The first run
may download the model. The benchmark never reads or writes the live database.

`benchmarks/data/passage/fixtures.json` is a small, synthetic, hand-labeled
development set. Each positive case has a unique answer substring; negative
cases have no answer. It covers a 20,000-plus-character preamble, paraphrases,
literal identifiers, similar models and versions, an oversized fenced code
block, and absent answers. All
content is fictional, not a snapshot or representative sample of the live index.

The benchmark uses the production title-prepending, chunking and embedding
functions, then compares these **experimental baselines**, not final designs:

- Keyword overlap plus an exact-phrase bonus, using Python tokenization.
- Cosine similarity between the production model's query and chunk vectors.
- Reciprocal-rank fusion of those two lists; zero keyword overlap gets no
  lexical credit. Rank ties use page order.

Chunk hit means the highest-ranked chunk contains the labeled answer. Window
hit means the answer occurs in the first 400 characters of that chunk; this
exposes the problem of selecting a correct large chunk but returning the wrong
part. It is not an implementation of mapping chunks to exact Markdown offsets.
The report also includes per-case scores, per-category hit counts, individual
in-process ranking times, and median warm query-embedding latency. Chunk
embedding and model startup are excluded. The Python comparison does not measure
Postgres plans, network/connection-pool latency, load, or concurrent contention.

No method currently abstains. Negative cases are reported separately with their
top scores, **not counted as true negatives**. Before shipping, create labeled
development and held-out sets with enough positive and negative cases to tune
and evaluate a no-match policy, report false positives and false negatives, and
avoid tuning and evaluating on the same examples.

## Initial exploratory run

On 2026-11-11, the harness ran inside the running wellisearch container using
`sentence-transformers/all-MiniLM-L6-v2` and its configured chunk budget of 800
tokens. The synthetic set had 12 positive and three no-answer cases. The
numbers below are counts, not confidence estimates:

| Experimental method | Chunk hits | 400-character window hits |
|---|---:|---:|
| Keyword | 12/12 | 11/12 |
| Semantic | 9/12 | 9/12 |
| Hybrid | 11/12 | 10/12 |

The keyword and hybrid baselines selected the oversized code chunk containing
the answer, but a window from its beginning missed the answer. Semantic ranking
also selected the wrong neighboring error code and a wrong version in this run;
hybrid fixed the former but not the latter. All methods returned a top-ranked
chunk for the three no-answer cases because no abstention policy exists. Median
warm query embedding was approximately 2.5 ms on this container; that is not an
end-to-end or concurrency measurement.

These small, synthetic results show why it is premature to select a winner.
Some labeled answers share chunks with other sections, the positive examples
are easy, and the no-answer set is much too small to calibrate confidence. The
configured 800-token budget also differs from the source default, so results
should be rerun and recorded with the intended deployment configuration.

## Exploratory run on the live index

On 2026-11-11, `passage_live_bench.py` read the Docker-hosted index in a
repeatable-read, read-only transaction. At the time, the index contained 16,825
pages and 4,647 distinct stored domains. The benchmark selected 60 eligible
pages from 60 domains, 15 each from four page-length ranges: 4,000–7,999,
8,000–19,999, 20,000–49,999 and 50,000–149,999 characters. Selection is by URL
hash from the first 300 candidates in each range, with one page per domain.
Pages must have at least three fully embedded chunks and a unique multiword
heading after the first chunk. This is a deliberately balanced, filtered
sample, **not a statistically representative random sample of the full index**.
Eighteen selected headings occur more than 20,000 characters into the page.

There were two separate query sets:

- The 60 automatic queries are the exact text of a selected unique heading,
  with that heading's chunk as the label. They test navigation on real page
  structure but strongly favor exact lexical matches.
- The 19 manually authored questions cover eight separately inspected pages.
  Short answer strings identify acceptable chunks; one question has an
  alternative acceptable passage. This is an exploratory set, not an independently
  reviewed or held-out test. Other valid answers may remain unlabeled.

Unlike the synthetic benchmark, this run uses the **existing stored chunk
vectors** and computes cosine similarity and full-text ranks in PostgreSQL.
The full-text fallback uses an AND query, then OR when all AND ranks are zero.
The two hybrid methods fuse ranks with equal-weight reciprocal-rank fusion;
the FTS hybrid uses the fallback. These are experimental page-local baselines,
not a reproduction of the complete `fn_search_local` ranking function.

The following counts are top-chunk hits, followed by hits where an answer also
occurs in the first 400 characters of that chunk:

| Method | Heading chunk/window (60) | Manual chunk/window (19) |
|---|---:|---:|
| Python keyword overlap | 57 / 56 | 9 / 7 |
| PostgreSQL full-text AND | 58 / 57 | 7 / 6 |
| PostgreSQL full-text AND with OR fallback | 58 / 57 | 11 / 9 |
| Stored-vector cosine similarity | 51 / 50 | 13 / 12 |
| Python keyword + vector fusion | 53 / 52 | 12 / 10 |
| PostgreSQL full-text fallback + vector fusion | 57 / 56 | 16 / 14 |

The heading and manually written sets answer different questions and should
not be combined into a single accuracy figure. In this small manual set, the
PostgreSQL full-text/vector combination selected more labeled passages than
either signal alone, but its heading result was slightly worse than lexical
matching alone. A correct chunk did not always put the answer in the short
window. No no-answer cases or abstention thresholds were evaluated in this run.
Consequently, the results justify further testing of this hybrid, **not** a
production algorithm decision or a claim about false-positive rates.

The final sequential run measured approximately 3.1 ms median per warm query
embedding and 2.1 ms median for a SQL call calculating both FTS variants and
vector distances for one page. The first query embedding took approximately
317 ms in that new process. These are exploratory container measurements, not
full request latency or load-test results; they exclude Python ranking, fetch,
pool acquisition, other traffic, and concurrency. The benchmark intentionally
scans the selected page's chunks, not the global vector index.

To reproduce against a configured live index, run
`python benchmarks/passage_live_bench.py`. It prints a report with hashed page
IDs and no raw page content. Its manual question file contains short labeled
answer fragments, not full pages; labels can become stale after refreshes, and
the report lists skipped cases. Because the database changes over time, save a
report and use a frozen or versioned snapshot for a genuinely repeatable
evaluation. Do not commit raw database exports or private page content.

### Comparing method latency

The live benchmark also reports warm p50 and p95 estimates for each candidate.
Python keyword time covers scoring already-loaded chunk text and ranking it.
PostgreSQL full-text time covers a page-scoped SQL call that calculates both
AND and OR scores, plus ranking. Semantic time includes one query embedding,
a separate page-scoped vector SQL call, and ranking. Python hybrid time adds
keyword scoring and fusion to the semantic components. PostgreSQL hybrid time
includes query embedding, a combined FTS/vector SQL call, fusion, and ranking.

These are sums of individually timed components on each case, not independent
end-to-end requests. Query embeddings and loaded chunk text are shared during
the experiment; the embedding time is included in each semantic method's
estimate. The first case is excluded from the warm percentiles because it may
initialize the model. The numbers exclude page lookup, content transfer,
connection-pool acquisition, HTTP/MCP overhead, and concurrent load. As such,
they compare these prototypes in this environment, not production SLAs.

### Expanded 500-page run

On 2026-11-11, the expanded run used `--per-bucket 125` and the same model and
read-only database workflow. It selected 500 eligible pages from 500 different
domains. The initial goal was 125 pages in each length range; the longest
range had only 74 eligible pages under the sampling rules in this run, so the
remaining slots were filled from another range. The resulting distribution was
176 pages of 4,000–7,999 characters, 125 of 8,000–19,999, 125 of
20,000–49,999, and 74 of 50,000–149,999. Of the selected headings, 89 were
more than 20,000 characters into their pages. This remains a filtered,
length-stratified sample, not a random cross-section of the whole index.

The same 19 hand-written questions were reused; increasing the page sample did
**not** add hand-written questions or no-answer examples. The following counts
are selected-chunk hits, not answers verified by an agent:

| Method | Exact-heading hits (500) | Hand-written hits (19) | Warm p50 | Warm p95 |
|---|---:|---:|---:|---:|
| Python keyword overlap | 490 | 9 | 0.49 ms | 2.90 ms |
| PostgreSQL full-text AND | 469 | 7 | 1.51 ms | 2.89 ms |
| PostgreSQL full-text with OR fallback | 469 | 11 | 1.51 ms | 2.89 ms |
| Stored-vector cosine similarity | 390 | 13 | 3.69 ms | 5.10 ms |
| Python keyword + vector fusion | 450 | 12 | 4.30 ms | 7.28 ms |
| PostgreSQL full-text fallback + vector fusion | 471 | 16 | 3.95 ms | 6.22 ms |

An exact-heading hit means that the query was copied from a page heading and
the selected chunk contained that heading. Such queries naturally favor
lexical matching. A hand-written hit means that the selected chunk contains at
least one of the short answer fragments labeled for that question; alternate
valid answers might not be labeled. A hit does not guarantee that a response
starting at the beginning of the chunk contains the answer. For example, the
PostgreSQL hybrid's 400-character windows contained labeled answers for 464
of the heading cases and 14 of the hand-written cases.

The p50 and p95 columns are the median and 95th percentile of the warm,
per-case component sums described above. Vector methods include query-embedding
time, which had a 2.58 ms warm median; the initial query embedding in this new
process took approximately 316 ms. Results depend on this container, its
configuration, and its load. They do not include the full fetch or network
path and do not measure concurrency. The benchmark uses the stored vectors and
does not re-embed page content.

This broader heading sample strengthens the observation that exact lexical
lookups work well on these pages. It does not strengthen the conclusion about
paraphrased questions, since that set is unchanged. The small manual set still
favors the FTS/vector hybrid, but it is not enough to calibrate confidence or
choose a production method without a larger, independently reviewed set of
natural questions and no-answer cases.

## Before selecting an implementation

Expand the fixture set with independently reviewed, appropriately sanitized
examples representative of real pages and agent queries. Include multiple
acceptable passages, misleading lexical overlap, no-answer and partial-answer
cases, long single lines and fenced blocks, repeated text, Unicode, page
refreshes, and evidence both before and after a requested offset. Label answer
spans independently of the chunker, and measure returned-window coverage as well
as position accuracy. Compare a Postgres full-text baseline and page-scoped
pgvector ranking against the Python prototypes using identical fixtures.
Benchmark warmed and cold latency, large pages, and concurrent requests on the
deployment hardware. Record model, dataset version, configuration, per-category
quality, p50/p95 latency, and no-match results before choosing a method.

The current stored chunks do not include Markdown offsets: chunking normalizes
whitespace and can prepend a title. A production design must establish reliable
spans and a migration/backfill or reindex plan before claiming exact offsets.

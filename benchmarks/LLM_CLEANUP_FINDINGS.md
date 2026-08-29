# LLM fit-markdown Cleanup Findings — wellisearch

Session summary comparing five small local LLMs on the fit-markdown cleanup task —
rewriting a page's stored `fit_markdown` into clean, faithful, noise-free markdown —
with a recommendation.

- **Hardware:** windows desktop (Intel i13700K) and 2019 MacBook Pro (i9-9980HK), both macOS, CPU-only (Ollama).
- **Harness:** `benchmarks/llm_md_cleanup_bench.py` (see `README.md`), run in Docker.
- **Sample:** 20 pages, stratified across length, `fit_markdown` 500–20,000 chars.
- **Judge:** Qwen3.8-27B (OpenAI-compatible, via Tailscale), scored 1–5 on faithfulness, noise removal, and preservation.
- **Date:** 2026-08-27.

---

## TL;DR — Recommendation

**Default to `qwen3:1.7b` for this task.**

1. **`qwen3:1.7b` — the default.** Ties the 4b/8b tier on faithfulness and
   preservation (judge 5/4/5 vs 5/5/5 — only noise removal is a point lower), at
   2–3× the speed of the 4b tier and 3–4× the speed of 8b.
2. **`qwen3:0.6b` — bulk/backlog mode.** ~2× faster than 1.7b but measurably worse
   on all three judge axes (4.5/3.5/4.0); leaves more boilerplate and over-trims on
   some pages. Fine for re-cleaning a backlog, not for the interactive path.
3. **`qwen3:4b-instruct` — quality ceiling.** The only larger model worth
   considering, but its edge over 1.7b is ~1 judge point on noise removal at 2–3×
   the time (16–40 s/doc). Worth it only for offline batch work.
4. **Drop `qwen3:8b`.** No measurable gain over 4b-instruct — in fact the *worst*
   preservation in the group (0.57) — while costing 1.3–1.5× the time.
5. **Drop `gemma3:12b`.** Slowest of all candidates, no quality gain over
   4b-instruct, and it failed to serve on the MacBook (HTTP 500 on all 20 pages).

---

## Models tested

| Model | Role |
|---|---|
| `qwen3:1.7b` | candidate (default) |
| `qwen3:0.6b` | candidate (fast/bulk) |
| `qwen3:4b-instruct` | quality ceiling |
| `qwen3:8b` | baseline (largest qwen3) |
| `gemma3:12b` | non-Qwen control — **excluded** |

## How it was scored

Two complementary lenses per page:

- **Deterministic (unbiased):** `no_addition` (share of output 8-grams already in
  the input — ≈1 = nothing fabricated), `preservation` (share of input content
  words kept — ≈1 = not over-trimmed), `boilerplate_removed` (fraction of
  boilerplate patterns removed), `len_ratio` (output/input length).
- **Judge (Qwen3.8-27B):** 1–5 on faithfulness, noise removal, preservation.

Config: temperature 0, max 2048 output tokens, 20 pages × 5 models per machine.

## Results (medians over 20 pages)

| model | judge f/n/p | no_addition | preservation | len_ratio | tok/s | s/doc |
|---|---|---|---|---|---|---|
| qwen3:1.7b | 5/4/5 (mac 5/4/4) | 0.96–0.98 | 0.69–0.70 | 0.58–0.62 | 24.0 / 10.0 | 6.9 / 14.2 |
| qwen3:0.6b | 4.5/3.5/4.0 | 0.88–0.89 | 0.60–0.64 | 0.60–0.61 | 61.7 / 20.9 | 2.6 / 7.9 |
| qwen3:4b-instruct | 5/5/5 | 0.88 | 0.74 | 0.73 | 12.1 / 4.7 | 16.3 / 40.1 |
| qwen3:8b | 5/5/5 | 1.00 | **0.57** | 0.60 | 5.9 / 3.2 | 21.7 / 60.0 |
| gemma3:12b | 5/5/5 (mac: n/a) | 1.00 | 0.70 | 0.55 | 4.2 / — | 35.5 / HTTP 500 |

`tok/s` and `s/doc` columns are desktop / MacBook.

## Key findings

1. **Quality plateaus at 1.7b.** The only consistent edge of the bigger models is
   noise removal: judge noise 4.0 (1.7b) → 5.0 (4b/8b/gemma). On faithfulness,
   1.7b is in the same band or better — its `no_addition` (0.96–0.98) is *higher*
   than 4b-instruct's (0.88). On preservation, 1.7b (0.69–0.70) is within 0.04 of
   4b (0.74); 8b is the worst in the group (0.57).
2. **8b and gemma add nothing over 4b** while costing 1.3–2.2× the time.
3. **0.6b is measurably worse, not just slower.** Judge drops on all three axes, it
   leaves boilerplate (noise 3.5), and it over-trims on some pages (e.g. the
   Wikipedia edit page: 3% of content words kept, `len_ratio` 0.019).
4. **1.7b beat the big models on the hardest page.**
   `en.wikipedia.org/wiki/Special:EditPage/Actor_model`: 8b and 4b both fabricated
   (`no_addition` ≈ 0, judge faith 1); 1.7b stayed faithful (judge faith 5 on both
   machines). One page of 20 — but a reminder that small ≠ dumb on this task.
5. **The deterministic `boilerplate_removed` metric is noisy** (gemma 0.71 vs 0.0
   for every other model, with identical 5/5/5 judge scores). It is
   pattern-based — trust the judge for noise removal.
6. **Quality is machine-independent; speed is not.** Medians agree within
   ~0.01–0.02 across machines; tok/s scales with hardware (1.7b: 24 vs 10). A model
   chosen here is portable across the tailnet.
7. **Speed is decode-rate dominated.** Output lengths are similar across models
   (105–175 tokens median), so s/doc ratios track tok/s.

## Caveats

- **Judge bias:** Qwen3.8-27B judges Qwen models (family affinity). The unbiased
  deterministic metrics corroborate the small models on faithfulness/preservation,
  so the direction of the conclusion holds — but exact judge gaps may be
  optimistic.
- 20 pages, one sample; medians are stable across machines but this is not a
  statistical estimate of the full corpus.
- Judge scores are integers on a 1–5 scale; a "1 point" gap may be coarser than it
  looks.
- `gemma3:12b` excluded (slowest; failed to serve on the MacBook — root cause
  unverified).
- **No implementation in the app yet** — this is exploration.

## Artifacts

- Harness: `benchmarks/llm_md_cleanup_bench.py`, `benchmarks/Dockerfile.fit-markdown-cleanup`, `benchmarks/docker-compose.yml`, `benchmarks/README.md`
- Runs: 2026-08-27T17:46:28Z (desktop i13700K), 2026-08-27T18:24:10Z (2019 MacBook Pro)
- Per-machine files: `benchmarks/results/llm-cleanup.{sample,results,report}` (gitignored)
- Related: `benchmarks/EMBEDDING_FINDINGS.md` (embedding model findings)

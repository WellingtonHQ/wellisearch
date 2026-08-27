# Embedding Model Findings — wellisearch

Session summary comparing the three candidate embedding models for wellisearch's
CPU-only, self-hosted search, with a recommendation for our use case.

- **Hardware:** Intel i9-9980HK (8c/16t), 64 GB RAM, macOS — CPU only, no GPU.
- **Harness:** `benchmarks/embed_bench.py` (see `README.md`), run in Docker.
- **Date:** 2026-08-26.

---

## TL;DR — Recommendation

**Keep MiniLM as the default, but fix the real bug: our chunks are too big for it.**

1. **Keep `all-MiniLM-L6-v2`.** On CPU it is ~14× faster than nomic and ~390× faster
   than Qwen, and on general retrieval it ties both (10/10). For a real-time,
   CPU-only service, that speed is not optional.
2. **Reduce `MAX_CHUNK_TOKENS` from 800 to ~512** (see "The cheap fix" below). This
   removes the one real quality gap MiniLM has — silently truncating the tail of long
   chunks — at zero model-change cost.
3. **Only consider nomic** if, after #2, query logs still show misses on details buried
   in long, same-topic documents. Accept ~14× slower embedding.
4. **Do not use Qwen on CPU.** ~390× slower is not viable for real-time search.

---

## The three models

| Model | Backend | Dims | Max context | Role |
|---|---|---|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | FastEmbed / ONNX Runtime | 384 | **512 tokens** | current production |
| `nomic-ai/nomic-embed-text-v1.5` | sentence-transformers / PyTorch | 768 | **8192 tokens** | candidate |
| `Qwen/Qwen3-Embedding-0.6B` | sentence-transformers / PyTorch | 1024 | **32768 tokens** | candidate |

The headline difference is **max context**: MiniLM hard-caps at 512 tokens, while
nomic and Qwen embed the full document.

---

## Key finding: MiniLM truncates 24% of our chunks

- Production chunking targets `MAX_CHUNK_TOKENS = 800` (`config.py:60`); the chunker
  estimates tokens at ~4 chars/token (`chunk.py:16`).
- Measured against the live index (591,391 chunks): **24.3% (143,502) exceed 512
  tokens**; p99 ≈ 3,376 chars, max ≈ 3.0M chars.
- Verified MiniLM's behavior is a **hard truncation**, not a soft degrade:
  `cosine(emb(full 800-tok), emb(first 512 tok)) = 1.00000`. Anything past token 512
  is simply dropped from the vector.
- nomic (8192) and Qwen (32768) see the whole chunk.

So for the quarter of our corpus that is long, **MiniLM is embedding a different
(shorter) document than nomic/Qwen.** That is the only place the models can differ.

---

## Test results

Three test sets, all in `benchmarks/data/`:

### 1. `data/` — general (142 real chunks, 10 queries)
Distinct topics; the relevant chunk is identifiable from its head.

| Model | R@1 | R@10 | MRR@10 |
|---|---|---|---|
| MiniLM | 1.00 | 1.00 | 1.000 |
| nomic | 1.00 | 1.00 | 1.000 |
| Qwen | 1.00 | 1.00 | 1.000 |

**All tie.** This is the regime most of our traffic lives in.

### 2. `data/longtail/` — 10 real long chunks (783–1228 tokens), answer in the tail, *distinct* topics

| Model | R@1 | R@10 | MRR@10 |
|---|---|---|---|
| MiniLM | 0.90 | 1.00 | 0.914 |
| nomic | 0.90 | 1.00 | 0.950 |
| Qwen | 1.00 | 1.00 | 1.000 |

**All retrieve the right chunk (R@10 = 1.00).** Because the topics are distinct, the
head is enough to rank the correct chunk even when the exact fact is in the truncated
tail. Marginal differences only.

### 3. `data/needle/` — controlled: 5 chunks sharing a ~875-token head, differing *only* in a tail fact past token 512

This isolates the truncation effect: the head is identical, so only a model that reads
the tail can tell them apart.

| Model | R@1 | MRR@10 | Correct rank per query |
|---|---|---|---|
| MiniLM | **0.20** | 0.457 | #1, #2, #3, #4, #5 (≈ random) |
| nomic | 0.80 | 0.867 | mostly #1 |
| Qwen | **1.00** | **1.000** | all #1 |

**This is where the long-context advantage is real and large.** When the only
distinguishing signal is in the tail of otherwise-similar documents, MiniLM is
effectively guessing; nomic and Qwen nail it.

---

## Speed (default 142-chunk set, idle machine)

| Model | tok/s | ms/doc (median) | Relative |
|---|---|---|---|
| MiniLM | ~12,100 | ~15 | **1×** |
| nomic | ~870 | ~133 | ~14× slower |
| Qwen | ~31 | ~1,600 | ~390× slower |

For 590k chunks, a full reindex on this box is roughly: MiniLM minutes, nomic ~20 min,
Qwen ~4+ hours. And that is *embedding only* — it also slows every live query.

---

## The cheap fix (do this before switching models)

The truncation problem is caused by **chunk size > model window**, not by MiniLM being
a bad model. Two aligned options:

- **Recommended:** set `MAX_CHUNK_TOKENS ≈ 512` (a touch under, ~450–480, to absorb the
  chunker's rough 4-chars/token estimate). Then **no chunk exceeds MiniLM's window**, so
  nothing is ever truncated. Smaller chunks also give finer retrieval granularity.
  Cost: a one-time reindex — far cheaper than a permanent 14× slowdown.
- **Alternative:** keep 800-token chunks and switch to nomic so the window is large
  enough. Cost: ~14× slower on every embed, forever.

Both remove the tail-truncation gap. The first is strictly cheaper for a CPU service.

---

## Recommendation for our use case

wellisearch is a **real-time, CPU-only, self-hosted** search service. That ordering of
constraints drives the call:

1. **MiniLM stays.** Speed is a hard requirement (live queries + reindex time) and
   MiniLM ties the others on general retrieval. nomic/Qwen buy nothing in the regime
   most queries live in (test sets 1 & 2).
2. **Fix the chunk window** (`MAX_CHUNK_TOKENS` → ~512) so MiniLM stops silently
   dropping tails. This closes the only measured quality gap (test set 3) without
   changing the model.
3. **Revisit nomic only with evidence.** If, after #2, query logs show a real rate of
   "detail buried in a long same-topic doc" misses, nomic is a defensible upgrade —
   but budget ~14× slower embedding and a reindex.
4. **Qwen is out on CPU.** Its quality edge over nomic is small (both read the full
   chunk) while its cost is ~450× nomic's. Only worth it on a GPU, which we don't have.

**Decision rule:** the model choice should be driven by how often the "shared-topic +
tail-fact" case (test set 3) occurs in real traffic — not by the needle benchmark
alone. Measure it in the logs first; the cheap chunk fix likely makes the switch
unnecessary.

---

## Caveats

- Quality numbers come from small, hand-built sets (10–5 queries). They show the
  *mechanism* and *direction* clearly, but are not a statistical estimate of real
  traffic. The needle set is deliberately adversarial to MiniLM.
- Speed is machine-specific (this i9-9980HK). Ratios should hold on other CPUs;
  absolute tok/s will not.
- A model change (or chunk-size change) requires a **full reindex** of ~590k chunks and
  a new `EMBED_DIMS` in the vector schema if the model changes.

## Artifacts

- Harness: `benchmarks/embed_bench.py`, `benchmarks/Dockerfile.embed-bench`, `benchmarks/README.md`
- Test sets: `benchmarks/data/` (general), `benchmarks/data/longtail/`, `benchmarks/data/needle/`
- Results: `benchmarks/results/` (gitignored)

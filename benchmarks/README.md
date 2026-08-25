# Embedding-model benchmark

A fixed, reproducible CPU-only benchmark for wellisearch's embedding model.
It runs the **same** retrieval test against several models and reports both
**quality** (hardware-independent) and **speed** (hardware-dependent), so you
can compare *models* and *machines* on one number each.

## What it tests

A snapshot of the real wellisearch index: **10 pages → 142 chunks**, with **10
real user-style queries**. Ground truth for each query is the set of chunks
from the page that query is about. A model "wins" a query if any of that page's
chunks lands in the top-k.

Three models run by default:

| Model | Backend | Notes |
|---|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | **FastEmbed / ONNX Runtime** | the **current production** model, benchmarked through the same backend the app uses |
| `nomic-ai/nomic-embed-text-v1.5` | sentence-transformers / PyTorch | candidate (768-dim) |
| `Qwen/Qwen3-Embedding-0.6B` | sentence-transformers / PyTorch | candidate (1024-dim) |

The baseline is measured through FastEmbed/ONNX (production path); the
candidates run on PyTorch. All models truncate at `min(--max-len, model_max)`
(= 512 tokens here), so the comparison is fair.

## Metrics

- **Quality:** Recall@1, Recall@5, Recall@10, MRR@10 (hardware-independent).
- **Speed:**
  - **throughput** (tokens/s) over the full corpus — the realistic batched workload;
  - **single-doc latency** (median / p95 ms), sampled across short→long docs — the `embed_one` cost;
  - **total wall time** for the whole run.

## Run it

### Docker (recommended — reproducible environment)

```sh
# build once (context = the benchmarks/ directory)
docker build -f benchmarks/Dockerfile -t embedbench benchmarks/

# run the full 3-model comparison; models cache + results persist on the host
docker run --rm \
  -v embedbench-cache:/models \
  -v "$PWD/benchmarks/results:/results" \
  embedbench
```

First run downloads the three models into the `embedbench-cache` volume
(~1.7 GB); later runs reuse it. Results land in `benchmarks/results/`.

### Plain Python

```sh
pip install -r benchmarks/requirements.txt   # torch + sentence-transformers + fastembed
python benchmarks/embed_bench.py
```

### Options

```
--model ID          repeatable; default = MiniLM baseline + nomic + qwen
--batch N           encode batch size (default 16)
--max-len N         max tokens per doc (default 512)
--threads N         inference threads (default 8)
--topk N            retrieval cutoff (default 10)
--latency-n N       single-doc latency samples (default 5)
--no-latency        skip single-doc latency
--data DIR          test data dir (default benchmarks/data)
--results DIR       results dir (default benchmarks/results)
--no-save           don't write JSON result files
```

## Reading the results

Each run writes to `benchmarks/results/`:

- `<model>_<timestamp>.json` — full per-model result (quality + speed + env).
- `summary_<timestamp>.json` — all models side-by-side **plus total wall time**;
  this is the file to compare across machines.

The console report prints the same table and the total run time.

## Comparing across machines

Because quality is hardware-independent, a model that scores 10/10 on one
machine scores 10/10 on all of them. Speed (tok/s, ms/doc, wall time) is what
varies by machine — that's the point of running it on each box. Keep the
`--threads`, `--batch`, and `--max-len` settings the same across machines for a
like-for-like speed comparison (the defaults are a reasonable fair CPU setup).

## Notes & caveats

- **The test set is small (10 queries) and non-discriminating on quality** —
  every model currently scores 10/10, so it separates models by *speed*, not
  quality. To differentiate quality, add harder/more diverse queries to
  `benchmarks/data/queries.json` (each needs a page present in `corpus.jsonl`).
- **Backend matters for speed.** The MiniLM baseline runs on ONNX (very
  optimized for a 22M model); the candidates run on PyTorch. A like-for-like
  model comparison would also need the candidates on ONNX.
- **Dimensions differ** (384 / 768 / 1024). Changing the model invalidates all
  stored vectors — the app requires a full reindex (`python -m wellisearch.reindex`).
- Models download from Hugging Face on first use and are cached locally.

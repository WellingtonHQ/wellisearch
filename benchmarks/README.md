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
candidates run on PyTorch. All models truncate at `min(--max-len, model_max)`,
so the comparison is fair.

## Test sets

Three sets ship under `data/`, each isolating a different question:

| Set | What it is | What it tests | `--max-len` |
|---|---|---|---|
| `data/` (default) | 10 real pages → 142 chunks, 10 queries | general quality + speed; all models capped at 512 | `512` (default) |
| `data/longtail` | 10 real chunks (512+ tokens) whose answer fact sits in the **tail** (past token 512) | retrieval when the answer is in MiniLM's truncated region | `8192` |
| `data/needle` | 5 same-topic chunks that differ **only** in a tail fact | the controlled long-context test — isolates "full chunk vs. head" | `8192` |

The `longtail` and `needle` sets run with `--max-len 8192` so nomic/Qwen embed
the **full** chunk while MiniLM stays capped at its native 512 — that difference
is the variable under test. The default set caps all models at 512 for a fair
speed/quality comparison.

## Metrics

- **Quality:** Recall@1, Recall@5, Recall@10, MRR@10 (hardware-independent).
- **Speed:**
  - **throughput** (tokens/s) over the full corpus — the realistic batched workload;
  - **single-doc latency** (median / p95 ms), sampled across short→long docs — the `embed_one` cost;
  - **total wall time** for the whole run.

## Run it

### Docker (recommended — reproducible environment)

```sh
# build once (context = the benchmarks/ directory). Rebuild after adding test data.
docker build -f benchmarks/Dockerfile -t embedbench benchmarks/

# 1) default set (142 chunks) — the general quality + speed comparison
docker run --rm \
  -v embedbench-cache:/models \
  -v "$PWD/benchmarks/results:/results" \
  embedbench

# 2) longtail set — real long chunks, answer fact in the tail (past token 512)
docker run --rm \
  -v embedbench-cache:/models \
  -v "$PWD/benchmarks/results:/results" \
  embedbench python embed_bench.py --data /bench/data/longtail --max-len 8192 --results /results

# 3) needle set — controlled long-context test (same topic, different tail fact)
docker run --rm \
  -v embedbench-cache:/models \
  -v "$PWD/benchmarks/results:/results" \
  embedbench python embed_bench.py --data /bench/data/needle --max-len 8192 --results /results
```

First run downloads the three models into the `embedbench-cache` **named
volume** (~1.7 GB); later runs reuse it. Results land in `benchmarks/results/`
(bind-mounted from the host).

**`--rm` is safe to keep.** It only removes the *container* after the run — it
does **not** touch the `embedbench-cache` volume (the models) or the
`benchmarks/results/` bind mount, both of which persist across runs. It's good
hygiene: it prevents dead containers from piling up in `docker ps -a`. Don't
remove it.

The `longtail` and `needle` runs pass `--max-len 8192` so nomic/Qwen embed the
full chunk while MiniLM stays capped at 512 — the variable under test. (The
image bundles all three sets under `/bench/data/`; rebuild after adding data.)

### Cleanup

- **Containers:** nothing to clean up — `--rm` removes each container when it
  exits. (If you ever run *without* `--rm`, prune the leftovers with
  `docker container prune` or `docker rm <id>`.)
- **Model cache** (only if you want to force a re-download / free ~1.7 GB):
  `docker volume rm embedbench-cache`
- **Results:** just delete the files in `benchmarks/results/` on the host.

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

- **The default set is small (10 queries) and non-discriminating on quality** —
  every model scores 10/10 there, so it separates models by *speed*, not
  quality. The `needle` set *is* discriminating: it isolates the long-context
  effect (MiniLM ~0.20 R@1 vs. nomic ~0.80 / Qwen ~1.00). To add more quality
  signal, add harder/more diverse queries to a set's `queries.json` (each needs
  a page present in that set's `corpus.jsonl`).
- **Backend matters for speed.** The MiniLM baseline runs on ONNX (very
  optimized for a 22M model); the candidates run on PyTorch. A like-for-like
  model comparison would also need the candidates on ONNX.
- **Dimensions differ** (384 / 768 / 1024). Changing the model invalidates all
  stored vectors — the app requires a full reindex (`python -m wellisearch.reindex`).
- Models download from Hugging Face on first use and are cached locally.

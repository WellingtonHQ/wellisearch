# Benchmarks

Two CPU-only benchmarks for wellisearch, both fixed and reproducible:

| Benchmark | Script | What it measures |
|---|---|---|
| **Embedding model** | `embed_bench.py` | retrieval quality (Recall@k, MRR) + speed (tok/s, ms/doc) |
| **LLM fit-markdown cleanup** | `llm_md_cleanup_bench.py` | how well small LLMs clean stored markdown — quality + TTFT / latency / tok-s |

Both write results to `benchmarks/results/`. The embedding bench needs a Docker
image (or a venv) with PyTorch. The LLM cleanup bench is a thin HTTP client: it
talks to **Ollama** (candidate models) and **LM Studio** (the 27B judge) over
their OpenAI-compatible endpoints — it downloads no model weights itself.

---

## Embedding-model benchmark

A fixed, reproducible CPU-only benchmark for wellisearch's embedding model.
It runs the **same** retrieval test against several models and reports both
**quality** (hardware-independent) and **speed** (hardware-dependent), so you
can compare *models* and *machines* on one number each.

### What it tests

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

### Test sets

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

### Metrics

- **Quality:** Recall@1, Recall@5, Recall@10, MRR@10 (hardware-independent).
- **Speed:**
  - **throughput** (tokens/s) over the full corpus — the realistic batched workload;
  - **single-doc latency** (median / p95 ms), sampled across short→long docs — the `embed_one` cost;
  - **total wall time** for the whole run.

### Run it

#### Docker (recommended — reproducible environment)

```sh
# build once (context = the benchmarks/ directory). Rebuild after adding test data.
docker build -f benchmarks/Dockerfile.embed-bench -t embedbench benchmarks/

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

#### Cleanup

- **Containers:** nothing to clean up — `--rm` removes each container when it
  exits. (If you ever run *without* `--rm`, prune the leftovers with
  `docker container prune` or `docker rm <id>`.)
- **Model cache** (only if you want to force a re-download / free ~1.7 GB):
  `docker volume rm embedbench-cache`
- **Results:** just delete the files in `benchmarks/results/` on the host.

#### Plain Python

```sh
pip install -r benchmarks/requirements.txt   # torch + sentence-transformers + fastembed
python benchmarks/embed_bench.py
```

#### Options

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

### Reading the results

Each run writes to `benchmarks/results/`:

- `<model>_<timestamp>.json` — full per-model result (quality + speed + env).
- `summary_<timestamp>.json` — all models side-by-side **plus total wall time**;
  this is the file to compare across machines.

The console report prints the same table and the total run time.

### Comparing across machines

Because quality is hardware-independent, a model that scores 10/10 on one
machine scores 10/10 on all of them. Speed (tok/s, ms/doc, wall time) is what
varies by machine — that's the point of running it on each box. Keep the
`--threads`, `--batch`, and `--max-len` settings the same across machines for a
like-for-like speed comparison (the defaults are a reasonable fair CPU setup).

### Notes & caveats

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

---

## LLM fit-markdown cleanup benchmark

A fixed, reproducible CPU-only benchmark for the **fit-markdown cleanup** task:
given a page's stored `fit_markdown` (the baseline), a small LLM rewrites it
into clean markdown **without adding, inferring, or embellishing** content. We
compare five local models on **quality** and **performance**.

### What it tests

The input is the page's existing `fit_markdown` (markdown → clean markdown, not
HTML → markdown). A model "wins" a page if it strips boilerplate (nav, cookie,
sign-in, ads, footer) while keeping every substantive fact, heading, list,
table, and code block — and adds nothing new.

Five models run by default (served by Ollama):

| Label | Ollama tag | Params |
|---|---|---|
| `qwen3-8b` | `qwen3:8b` | 8B |
| `qwen3-4b` | `qwen3:4b` | 4B |
| `gemma3-12b` | `gemma3:12b` | 12B |
| `qwen3-1.7b` | `qwen3:1.7b` | 1.7B |
| `qwen3-0.6b` | `qwen3:0.6b` | 0.6B |

The sample is a stratified set of **5 real URLs** (default; override with
`--sample-size`) pulled from the index (Postgres) — spread across domains and
content-length quantiles — and snapshotted to JSON so the run is reproducible
and re-runnable offline.

### Metrics

- **Quality (deterministic, unbiased):**
  - `no_addition` — share of output 8-grams already present in the input (≈1 = nothing fabricated);
  - `preservation` — share of input content-words kept (≈1 = not over-trimmed);
  - `boilerplate_removed` — fraction of boilerplate patterns removed;
  - `structure_preserved` — headings / tables / code-fences / list-items kept;
  - `length_ratio` — output length ÷ input length.
- **Quality (LLM judge, 1–5 rubric):** a 27B model (Qwen3.8-27B in LM Studio)
  scores **faithfulness** (nothing added/changed), **noise_removal** (boilerplate
  gone), and **preservation** (substance kept).
- **Performance:** time-to-first-token (TTFT), total latency, completion tok/s,
  and token counts. A per-model warmup call excludes one-time load latency.

> **Judge-bias caveat:** the judge is Qwen3.8-27B and 4 of the 5 candidates are
> Qwen3 — the judge may favour its own family. Treat the **deterministic**
> metrics as the unbiased core and the judge scores as a secondary signal.

### Run it

The bench is a thin HTTP client — no local weights. It needs:

1. **Ollama** serving the candidate models (any OpenAI-compatible base URL).
2. **LM Studio** (or any OpenAI-compatible server) serving the 27B judge.
3. **Postgres** (only for the `sample` step, to build the input snapshot).

#### Ollama (candidate models)

```sh
# bring up Ollama (see ollama-compose.yml)
docker compose -f benchmarks/ollama-compose.yml up -d
```

That's it for setup. **The bench auto-downloads the models it will use** on
first `run` — it checks Ollama (`GET /api/tags`) and pulls any missing ones
(`POST /api/pull`). So you do **not** have to `ollama pull` them yourself. The
first run is slow (it fetches the weights, ~17 GB for all five into the
`ollama-models` volume); later runs reuse them.

Ollama exposes an OpenAI-compatible API at `http://127.0.0.1:11434/v1` (the
default `--ollama-url`). To pre-warm the cache manually, you can still run
`docker compose -f benchmarks/ollama-compose.yml exec ollama ollama pull <tag>`.

 #### Docker (all-in — run the whole pipeline on any tailnet machine)

Everything runs in containers: the `sample` step reaches Postgres over
Tailscale, the judge is reached over Tailscale, and Ollama does the heavy
inference locally. No bare Python, no host routing tricks — bring up the stack
on any machine on the tailnet that can reach the Postgres + judge Tailscale
addresses. Ollama is local, so **each machine measures its own hardware**;
diff the reports to compare.

The credentials (Postgres Tailscale host, judge URL + key) live in the repo
root `.env`, so pass it with `--env-file .env` (Compose won't auto-find it,
because the compose file sits in `benchmarks/`).

```sh
# From the repo root:
#   - full pipeline (sample + run + report) in one shot:
docker compose --env-file .env -f benchmarks/docker-compose.yml \
  run --build --rm bench python llm_md_cleanup_bench.py all

#   - or just the inference run over the existing sample:
docker compose --env-file .env -f benchmarks/docker-compose.yml up --build
```

`--build` is included on the one-shot command so it always picks up the latest
`llm_md_cleanup_bench.py` (Compose reuses the existing image otherwise). The
rebuild is cheap — it just re-COPYs the script and reuses the pip cache.

`up --build` starts `ollama` (auto-pulls the five models on first run) +
`bench` (runs all five models over the 5-doc sample, with the judge). The
sample goes in and the results come out through the `./results` mount, so
`llm-cleanup.report.md` lands on the host.

Every status line is timestamped with the local wall clock
(e.g. `[2026-08-26 22:31:18] [run] qwen3-8b (qwen3:8b) over 5 pages …`), so
you can see when each step happened. The container's `TZ` is pinned to
`America/Los_Angeles` by default (override with the `TZ` env var) so the
timestamps match the clock you read them against.

Notes:
- **Shared Ollama instead of per-machine pulls:** point the client at an
  existing server and skip the bundled one —
  `OLLAMA_BASE_URL=http://<host>:11434/v1 docker compose --env-file .env -f benchmarks/docker-compose.yml up bench`.
- **Judge / Postgres** must be routable from the machine — both are Tailscale
  addresses in `.env`, so any tailnet member works. Add `--no-judge` to the
  bench command for deterministic-metrics-only runs.
- **Reproducible input:** `sample` writes `results/llm-cleanup.sample.json`;
  that file is the shared input, so every machine scores the *same* 5 docs.
- The image + stack are defined in `Dockerfile.fit-markdown-cleanup` and
  `docker-compose.yml` (separate from the embedding bench's
  `Dockerfile.embed-bench` / `ollama-compose.yml`).

#### Plain Python

```sh
# deps: httpx + psycopg (both already in the app's pyproject.toml)
pip install httpx 'psycopg[binary]'

# 1) build the sample snapshot from the index (default 5 pages; needs Postgres)
python benchmarks/llm_md_cleanup_bench.py sample

# 2) run all five models (+ judge) over the sample
python benchmarks/llm_md_cleanup_bench.py run

# 3) (re)generate the Markdown report from the last run
python benchmarks/llm_md_cleanup_bench.py report

# or do all three at once:
python benchmarks/llm_md_cleanup_bench.py all

# quick sanity check (2 pages x first 2 models):
python benchmarks/llm_md_cleanup_bench.py run --smoke
```

#### Options

```
--models L=T,...    comma list label=ollama_tag (default: the five models above)
--sample-size N     number of pages in the sample (default 5)
--no-judge          skip the 27B LLM judge (deterministic metrics only)
--concurrency N     parallel pages per model (default 1 = fair CPU timing)
--ollama-url URL    Ollama OpenAI-compatible base URL (default http://127.0.0.1:11434/v1)
--judge-url URL     judge OpenAI-compatible base URL (required unless --no-judge; JUDGE_BASE_URL works too)
--out-dir DIR       output dir (default benchmarks/results)
--smoke             2 pages x first 2 models, quick sanity check
```

Environment variables (all optional, sensible defaults): `POSTGRES_*`,
`OLLAMA_BASE_URL`, `OLLAMA_API_KEY`, `JUDGE_BASE_URL` (required when the
judge runs), `JUDGE_MODEL` (default `qwen3.8-27b`), `JUDGE_API_KEY` (required
when the judge runs), `BENCH_TEMPERATURE` (default `0.0`),
`BENCH_MAX_OUTPUT_TOKENS` (default `2048`), `BENCH_SAMPLE_SIZE` (default `5`),
`BENCH_OUT_DIR`.

The judge is configured in the repo-root `.env` (gitignored) — `JUDGE_BASE_URL`,
`JUDGE_MODEL`, `JUDGE_API_KEY` — and the bench loads it automatically (explicit
env vars / flags override). Put your LM Studio key there, not in source. There
is no built-in default endpoint or key: a `run` without the judge configured
exits with a clear "set JUDGE_BASE_URL / JUDGE_API_KEY" error (or pass
`--no-judge`).

### Reading the results

Each run writes to `benchmarks/results/`:

- `llm-cleanup.sample.json` — the stratified input snapshot (reproducible).
- `llm-cleanup.results.json` — full per-page results (metrics + judge + timing).
- `llm-cleanup.report.md` — the side-by-side Markdown report (median / p95 per
  metric, plus a per-page detail table). This is the file to read.

The console prints the same summary table as it runs.

### Notes & caveats

- **Fair CPU timing:** run with `--concurrency 1` (the default) and the same
  `--temperature` / `--max-output_tokens` across models. A per-model warmup call
  is made first so one-time load latency is excluded from the measured runs.
- **Deterministic first:** because the judge shares a family with most
  candidates, trust the deterministic metrics (no-addition, preservation,
  boilerplate-removed) for the quality ranking; use the judge as a tie-breaker.
- **Sample is a snapshot:** once built, `run`/`report` work offline and are
  reproducible. Re-run `sample` to refresh it from the live index.
- **Judge is required for the rubric scores** but optional (`--no-judge`) if you
  only want the deterministic metrics and performance numbers.

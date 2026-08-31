#!/usr/bin/env python3
"""Embedding-model benchmark for wellisearch (CPU only).

Runs a fixed, reproducible retrieval test against one or more embedding models
and reports both QUALITY (hardware-independent) and SPEED (hardware-dependent),
so you can compare models and machines on one number each.

The default run compares three models:

  1. sentence-transformers/all-MiniLM-L6-v2  -- the CURRENT production model,
     served through FastEmbed (ONNX Runtime). This is the baseline.
  2. nomic-ai/nomic-embed-text-v1.5          -- candidate (sentence-transformers/PyTorch).
  3. Qwen/Qwen3-Embedding-0.6B               -- candidate (sentence-transformers/PyTorch).

The baseline is benchmarked through the SAME backend the app uses (FastEmbed/
ONNX), so its speed reflects production; the candidates run on PyTorch.

Test set (shipped in benchmarks/data, snapshotted from the wellisearch
Postgres index): 10 real pages -> 142 chunks, 10 real user-style queries.
Ground truth for each query = the chunks of the page that query is about.
A model "wins" a query if any of that page's chunks lands in the top-k.

Quality metrics:  Recall@1, Recall@5, Recall@10, MRR@10
Speed metrics:    throughput (tokens/s), median/p95 single-doc latency (ms),
                  total wall-clock time for the whole run

Usage:
  # default: production baseline + both candidates
  python benchmarks/embed_bench.py

  # a single model:
  python benchmarks/embed_bench.py --model nomic-ai/nomic-embed-text-v1.5

  # any model(s) you like (repeatable):
  python benchmarks/embed_bench.py \
      --model sentence-transformers/all-MiniLM-L6-v2 \
      --model nomic-ai/nomic-embed-text-v1.5 \
      --model Qwen/Qwen3-Embedding-0.6B

  # tune (defaults match a fair CPU setup):
  python benchmarks/embed_bench.py --batch 16 --max-len 512 --threads 8

Each run writes benchmarks/results/<model>_<timestamp>.json (per model) and a
benchmarks/results/summary_<timestamp>.json (all models + total time). Compare
those files across machines/models.

Backends:
  - all-MiniLM*  -> FastEmbed (ONNX Runtime), matching production.
  - everything else -> sentence-transformers (PyTorch), on CPU.

Docker (reproducible environment, see benchmarks/Dockerfile.embed-bench):
  docker build -f benchmarks/Dockerfile.embed-bench -t embedbench benchmarks/
  docker run --rm -v "$PWD/benchmarks/results:/results" embedbench

Dependencies (not part of the app):  pip install -r benchmarks/requirements.txt
Models are downloaded from Hugging Face on first run and cached locally.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

DATA_DIR = HERE / "data"

RESULTS_DIR = HERE / "results"

# Production baseline first, then the candidates.
DEFAULT_MODELS = [
    "sentence-transformers/all-MiniLM-L6-v2",  # current production (FastEmbed/ONNX)
    "nomic-ai/nomic-embed-text-v1.5",
    "Qwen/Qwen3-Embedding-0.6B",
]

# Models to run through the FastEmbed (ONNX) backend instead of PyTorch.
FASTEMBED_BACKEND = {
    "sentence-transformers/all-MiniLM-L6-v2",
    "all-MiniLM-L6-v2",
}

def detect_cpu() -> str:
    """The CPU model name (macOS sysctl, /proc/cpuinfo, or platform.processor)."""
    try:
        if sys.platform == "darwin":
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
        name = _read_proc_cpuinfo()
        if name is not None:
            return name
    except Exception:
        pass
    return platform.processor() or "unknown"

def model_prefixes(model_id: str) -> tuple[str, str]:
    """(doc_prefix, query_prefix). Models with task-specific prefixes use them;
    otherwise plain text (MiniLM/BGE/e5-style)."""
    m = model_id.lower()
    if "nomic" in m:
        return "search_document: ", "search_query: "
    if "qwen" in m and "embedding" in m:
        return (
            "",
            "Instruct: Given a web search query, retrieve relevant passages "
            "that answer the query\nQuery: ",
        )
    return "", ""

def percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile of a sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)

def load_data(data_dir: Path) -> tuple[list[dict], list[dict], dict[str, set[str]]]:
    """Load the test set: chunks, queries, and per-query ground-truth chunk ids."""
    chunks = [
        json.loads(line)
        for line in (data_dir / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    queries = json.loads((data_dir / "queries.json").read_text(encoding="utf-8"))
    by_url: dict[str, list[str]] = {}
    for c in chunks:
        by_url.setdefault(c["url"], []).append(c["id"])
    for q in queries:
        if not by_url.get(q["url"]):
            raise SystemExit(
                f"Query '{q['q']}' has no page '{q['url']}' in the corpus; "
                "the test set is inconsistent."
            )
    truth = {q["q"]: set(by_url[q["url"]]) for q in queries}
    return chunks, queries, truth

class TorchRunner:
    """sentence-transformers / PyTorch backend (nomic, qwen, ...)."""

    backend = "sentence-transformers (PyTorch)"

    def __init__(
        self,
        model_id: str,
        args: argparse.Namespace,
    ) -> None:
        """Load the sentence-transformers model on CPU and set up the tokenizer,
        dimension, and task prefixes."""
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(args.threads)
        t0 = time.perf_counter()
        self.model = SentenceTransformer(model_id, device="cpu", trust_remote_code=True)
        self.load_s = time.perf_counter() - t0
        self.tok = self.model.tokenizer
        _get_dim = (
            getattr(self.model, "get_embedding_dimension", None)
            or self.model.get_sentence_embedding_dimension
        )
        self.dim = int(_get_dim())
        import sentence_transformers

        self.engine_version = (
            f"sentence-transformers {sentence_transformers.__version__}, "
            f"torch {torch.__version__}"
        )
        self.max_len = _effective_max_len(self.tok, args.max_len)
        self.doc_prefix, self.query_prefix = model_prefixes(model_id)
        self.batch = args.batch

    def truncate(self, text: str) -> str:
        """Truncate text to max_len tokens (via the tokenizer)."""
        ids = self.tok(text, truncation=True, max_length=self.max_len, add_special_tokens=False)["input_ids"]
        return self.tok.decode(ids)

    def tok_count(self, text: str) -> int:
        """Token count of text after truncation to max_len."""
        return _tok_len(self.tok, text, self.max_len)

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode a batch of texts to L2-normalized float32 vectors."""
        X = self.model.encode(
            texts, batch_size=self.batch, normalize_embeddings=True, show_progress_bar=False
        )
        return _l2_normalize(np.asarray(X, dtype=np.float32))

    def encode_one(self, text: str) -> np.ndarray:
        """Encode one text to an L2-normalized float32 vector."""
        X = self.model.encode([text], normalize_embeddings=True, show_progress_bar=False)
        return _l2_normalize(np.asarray(X, dtype=np.float32))[0]

class FastEmbedRunner:
    """FastEmbed / ONNX Runtime backend (the app's production path)."""

    backend = "fastembed (ONNX Runtime)"

    def __init__(
        self,
        model_id: str,
        args: argparse.Namespace,
    ) -> None:
        """Load the FastEmbed (ONNX) model and set up the tokenizer, dimension,
        and batch size."""
        import fastembed
        import onnxruntime
        from fastembed import TextEmbedding
        from transformers import AutoTokenizer

        self.model_name = model_id if "/" in model_id else f"sentence-transformers/{model_id}"
        cache_dir = os.environ.get("FASTEMBED_CACHE_DIR") or str(Path.home() / ".cache" / "fastembed")
        t0 = time.perf_counter()
        self.model = TextEmbedding(
            model_name=self.model_name, cache_dir=cache_dir, threads=args.threads
        )
        self.load_s = time.perf_counter() - t0
        self.tok = AutoTokenizer.from_pretrained(self.model_name)
        self.dim = int(self.model.embedding_size)
        self.engine_version = f"fastembed {fastembed.__version__}, onnxruntime {onnxruntime.__version__}"
        self.max_len = _effective_max_len(self.tok, args.max_len)
        self.doc_prefix = self.query_prefix = ""
        self.batch = args.batch

    def truncate(self, text: str) -> str:
        """Truncate text to max_len tokens (via the tokenizer)."""
        ids = self.tok(text, truncation=True, max_length=self.max_len, add_special_tokens=False)["input_ids"]
        return self.tok.decode(ids)

    def tok_count(self, text: str) -> int:
        """Token count of text after truncation to max_len."""
        return _tok_len(self.tok, text, self.max_len)

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode a batch of texts to L2-normalized float32 vectors."""
        X = np.stack(
            [np.asarray(v, dtype=np.float32) for v in self.model.embed(texts, batch_size=self.batch)]
        )
        return _l2_normalize(X)

    def encode_one(self, text: str) -> np.ndarray:
        """Encode one text to an L2-normalized float32 vector."""
        v = np.asarray(next(iter(self.model.embed([text]))), dtype=np.float32)
        return _l2_normalize(v[None, :])[0]

def make_runner(model_id: str, args: argparse.Namespace) -> TorchRunner | FastEmbedRunner:
    """Pick the backend runner for a model (FastEmbed for MiniLM, else PyTorch)."""
    if model_id in FASTEMBED_BACKEND or "minilm" in model_id.lower():
        return FastEmbedRunner(model_id, args)
    return TorchRunner(model_id, args)

def score_quality(
    S: np.ndarray,
    queries: list[dict],
    truth: dict[str, set[str]],
    chunks: list[dict],
    order: list[int],
    topk: int,
) -> dict:
    """Compute Recall@1/5/10 and MRR@10 from the query×doc score matrix."""
    nq, nd = S.shape
    topk = min(topk, nd)
    recall = {1: 0, 5: 0, 10: 0}
    mrr = 0.0
    per_query = []
    for i, q in enumerate(queries):
        rel = truth[q["q"]]
        idx = np.argsort(-S[i])[:topk]
        best = 0
        for rank, d in enumerate(idx, start=1):
            if chunks[order[d]]["id"] in rel:
                best = rank
                break
        for k in (1, 5, 10):
            recall[k] += 1 if (best and best <= k) else 0
        if best:
            mrr += 1.0 / best
        per_query.append({"query": q["q"], "best_rank": best})
    n = len(queries)
    return {
        "recall@1": round(recall[1] / n, 4),
        "recall@5": round(recall[5] / n, 4),
        "recall@10": round(recall[10] / n, 4),
        "mrr@10": round(mrr / n, 4),
        "per_query": per_query,
    }

def measure_latency(
    encode_one: Callable[[str], np.ndarray],
    tok_count: Callable[[str], int],
    doc_texts: list[str],
    sample_n: int,
) -> dict | None:
    """Measure single-doc encode latency (median/p95) over a sampled subset."""
    n = len(doc_texts)
    if n == 0:
        return None
    sample_n = min(sample_n, n)
    idxs = [min(n - 1, max(0, int(round(n * (i + 0.5) / sample_n)) - 1)) for i in range(sample_n)]
    encode_one(doc_texts[n // 2])  # warmup
    samples = []
    for j in idxs:
        ts = time.perf_counter()
        encode_one(doc_texts[j])
        samples.append(time.perf_counter() - ts)
    samples.sort()
    return {
        "n": len(samples),
        "sampled_lengths": [tok_count(doc_texts[j]) for j in idxs],
        "median_ms": round(1000 * percentile(samples, 0.5), 1),
        "p95_ms": round(1000 * percentile(samples, 0.95), 1),
    }

def run_model(
    model_id: str,
    chunks: list[dict],
    queries: list[dict],
    truth: dict[str, set[str]],
    args: argparse.Namespace,
) -> dict:
    """Run the full benchmark for one model: embed, score, latency, env metadata."""
    runner = make_runner(model_id, args)

    order = sorted(range(len(chunks)), key=lambda i: len(chunks[i]["text"]))
    doc_texts = [runner.truncate(runner.doc_prefix + chunks[i]["text"]) for i in order]
    q_texts = [runner.truncate(runner.query_prefix + q["q"]) for q in queries]

    total_toks = sum(runner.tok_count(t) for t in doc_texts) + sum(runner.tok_count(t) for t in q_texts)

    t0 = time.perf_counter()
    D = runner.encode(doc_texts)
    Q = runner.encode(q_texts)
    embed_s = time.perf_counter() - t0

    S = (Q @ D.T).astype(np.float32)
    quality = score_quality(S, queries, truth, chunks, order, args.topk)
    latency = (
        None if args.no_latency
        else measure_latency(runner.encode_one, runner.tok_count, doc_texts, args.latency_n)
    )

    return {
        "model": model_id,
        "backend": runner.backend,
        "embedding_dim": int(runner.dim),
        "quality": quality,
        "speed": {
            "total_tokens": total_toks,
            "embed_seconds": round(embed_s, 2),
            "tokens_per_sec": round(total_toks / embed_s, 1) if embed_s else None,
            "doc_latency": latency,
        },
        "load_seconds": round(runner.load_s, 2),
        "data": {
            "n_chunks": len(chunks),
            "n_queries": len(queries),
            "n_pages": len({c["url"] for c in chunks}),
        },
        "env": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu": detect_cpu(),
            "cpu_count": os.cpu_count(),
            "engine_version": runner.engine_version,
            "threads": args.threads,
            "batch": args.batch,
            "max_len": args.max_len,
        },
    }

def slug(model_id: str) -> str:
    """A filesystem-safe slug for a model id."""
    return model_id.replace("/", "__").replace(":", "_").lower()

def print_report(results: list[dict], total_wall: float) -> None:
    """Print the comparison table and per-model detail."""
    if not results:
        return
    print()
    print("=" * 100)
    print(f"{'model':<36} {'R@1':>5} {'R@10':>5} {'MRR@10':>7} {'tok/s':>8} {'ms/doc':>9}  backend")
    print("-" * 100)
    for r in results:
        q = r["quality"]
        sp = r["speed"]
        ms = (sp["doc_latency"] or {}).get("median_ms", "-")
        print(
            f"{r['model']:<36} {q['recall@1']:>5.2f} {q['recall@10']:>5.2f} "
            f"{q['mrr@10']:>7.3f} {sp['tokens_per_sec']:>8.0f} {ms:>9}  {r['backend']}"
        )
    print("-" * 100)
    for r in results:
        hits = sum(1 for p in r["quality"]["per_query"] if p["best_rank"])
        print(
            f"  {r['model']}: {hits}/{len(r['quality']['per_query'])} queries hit in top-10, "
            f"dim={r['embedding_dim']}, load={r['load_seconds']}s, "
            f"embed={r['speed']['embed_seconds']}s"
        )
        misses = [p["query"] for p in r["quality"]["per_query"] if not p["best_rank"]]
        if misses:
            print(f"    missed: {'; '.join(misses)}")
    print("=" * 100)
    print(f"Total wall time for {len(results)} model(s): {total_wall:.1f}s")
    print("=" * 100)

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args (models, batch, max-len, threads, topk, latency, dirs)."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--model", action="append", default=None,
        help="Model id (repeatable). Default: MiniLM baseline + nomic + qwen3-embed.",
    )
    p.add_argument("--batch", type=int, default=16, help="encode batch size (default 16)")
    p.add_argument("--max-len", type=int, default=512, help="max tokens per doc (default 512)")
    p.add_argument("--threads", type=int, default=8, help="inference threads (default 8)")
    p.add_argument("--topk", type=int, default=10, help="retrieval cutoff (default 10)")
    p.add_argument("--latency-n", type=int, default=5, help="single-doc latency samples (default 5)")
    p.add_argument("--no-latency", action="store_true", help="skip single-doc latency measurement")
    p.add_argument("--data", type=Path, default=DATA_DIR, help="test data dir")
    p.add_argument("--results", type=Path, default=RESULTS_DIR, help="results dir")
    p.add_argument("--no-save", action="store_true", help="don't write JSON result files")
    a = p.parse_args(argv)
    a.model = a.model or DEFAULT_MODELS
    return a

def main(argv: list[str] | None = None) -> int:
    """Run the benchmark across all models, print the report, write result files."""
    args = parse_args(argv)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    chunks, queries, truth = load_data(args.data)
    print(f"Loaded test set: {len(chunks)} chunks, {len(queries)} queries, "
          f"{len({c['url'] for c in chunks})} pages")
    print(f"Models: {', '.join(args.model)}\n")

    results = []
    run_t0 = time.perf_counter()
    for m in args.model:
        print(f"### {m} " + "-" * max(0, 60 - len(m)))
        t0 = time.perf_counter()
        r = run_model(m, chunks, queries, truth, args)
        wall = time.perf_counter() - t0
        print(
            f"  [{r['backend']}] quality: R@1={r['quality']['recall@1']:.2f} "
            f"R@5={r['quality']['recall@5']:.2f} R@10={r['quality']['recall@10']:.2f} "
            f"MRR@10={r['quality']['mrr@10']:.3f}"
        )
        sp = r["speed"]
        lat = sp["doc_latency"] or {}
        print(
            f"  speed: {sp['total_tokens']:,} tokens in {sp['embed_seconds']}s "
            f"= {sp['tokens_per_sec']} tok/s"
            + (f" | doc latency median {lat.get('median_ms')}ms / p95 {lat.get('p95_ms')}ms" if lat else "")
            + f" | wall {wall:.1f}s"
        )
        results.append(r)
    total_wall = time.perf_counter() - run_t0

    print_report(results, total_wall)

    if not args.no_save:
        args.results.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for r in results:
            out = args.results / f"{slug(r['model'])}_{stamp}.json"
            out.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"wrote {out}")
        summary = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_wall_seconds": round(total_wall, 2),
            "n_models": len(results),
            "n_chunks": len(chunks),
            "n_queries": len(queries),
            "env": {
                "cpu": detect_cpu(),
                "cpu_count": os.cpu_count(),
                "machine": platform.machine(),
                "python": sys.version.split()[0],
                "threads": args.threads,
                "batch": args.batch,
                "max_len": args.max_len,
            },
            "models": [
                {
                    "model": r["model"],
                    "backend": r["backend"],
                    "embedding_dim": r["embedding_dim"],
                    "recall@1": r["quality"]["recall@1"],
                    "recall@10": r["quality"]["recall@10"],
                    "mrr@10": r["quality"]["mrr@10"],
                    "tokens_per_sec": r["speed"]["tokens_per_sec"],
                    "median_doc_ms": (r["speed"]["doc_latency"] or {}).get("median_ms"),
                    "engine_version": r["env"]["engine_version"],
                }
                for r in results
            ],
        }
        sout = args.results / f"summary_{stamp}.json"
        sout.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {sout}")
    return 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _effective_max_len(tok: object, args_max: int) -> int:
    """Cap the requested max length at the model's true limit (if known)."""
    native = getattr(tok, "model_max_length", None)
    try:
        native = int(native)
    except (TypeError, ValueError):
        native = None
    if native is None or native > 100000:  # sentinel / unknown -> trust args
        return args_max
    return min(args_max, native)

def _l2_normalize(X: np.ndarray) -> np.ndarray:
    """L2-normalize along the last axis (zero-safe)."""
    n = np.linalg.norm(X, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return (X / n).astype(np.float32)

def _tok_len(
    tok: object,
    text: str,
    max_len: int,
) -> int:
    """Token count of text after truncation to max_len."""
    return len(tok(text, truncation=True, max_length=max_len, add_special_tokens=False)["input_ids"])

def _read_proc_cpuinfo() -> str | None:
    """Return the CPU model name from /proc/cpuinfo, or None if unavailable."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
            return next(
                (
                    line.split(":", 1)[1].strip()
                    for line in f
                    if line.lower().startswith("model name")
                ),
                None,
            )
    except Exception:
        return None

if __name__ == "__main__":
    raise SystemExit(main())

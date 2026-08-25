#!/usr/bin/env python3
"""Embedding-model benchmark for wellisearch (CPU only).

Runs a fixed, reproducible retrieval test against one or more embedding
models and reports both QUALITY (hardware-independent) and SPEED
(hardware-dependent), so you can compare models and machines on one
number each.

Test set (shipped in benchmarks/data, snapshotted from the wellisearch
Postgres index): 10 real pages -> 142 chunks, 10 real user-style queries.
Ground truth for each query = the chunks of the page that query is about.
A model "wins" a query if any of that page's chunks lands in the top-k.

Quality metrics:  Recall@1, Recall@5, Recall@10, MRR@10
Speed metrics:    throughput (tokens/s), median/p95 single-doc latency (ms)

Usage:
  # run the default comparison (nomic vs qwen3-embed):
  python benchmarks/embed_bench.py

  # a single model:
  python benchmarks/embed_bench.py --model nomic-ai/nomic-embed-text-v1.5

  # any model(s) you like (repeatable):
  python benchmarks/embed_bench.py \
      --model nomic-ai/nomic-embed-text-v1.5 \
      --model Qwen/Qwen3-Embedding-0.6B \
      --model BAAI/bge-base-en-v1.5

  # tune (defaults match a fair CPU setup):
  python benchmarks/embed_bench.py --batch 16 --max-len 512 --threads 8

Each run writes benchmarks/results/<model>_<timestamp>.json with the full
machine-readable result (quality + speed + environment). Compare those files
across machines/models.

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
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"

DEFAULT_MODELS = [
    "nomic-ai/nomic-embed-text-v1.5",
    "Qwen/Qwen3-Embedding-0.6B",
]


def detect_cpu() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
        with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def model_prefixes(model_id: str) -> tuple[str, str]:
    """(doc_prefix, query_prefix). Models with task-specific prefixes use them;
    otherwise plain text (BGE/e5-style)."""
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
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def load_data(data_dir: Path):
    chunks = [
        json.loads(line)
        for line in (data_dir / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    queries = json.loads((data_dir / "queries.json").read_text(encoding="utf-8"))
    by_url: dict[str, list[str]] = {}
    for c in chunks:
        by_url.setdefault(c["url"], []).append(c["id"])
    # sanity: every query must have at least one relevant chunk in the corpus
    for q in queries:
        if not by_url.get(q["url"]):
            raise SystemExit(
                f"Query '{q['q']}' has no page '{q['url']}' in the corpus; "
                "the test set is inconsistent."
            )
    truth = {q["q"]: set(by_url[q["url"]]) for q in queries}
    return chunks, queries, truth


def run_model(model_id, chunks, queries, truth, args) -> dict:
    import torch

    from sentence_transformers import SentenceTransformer

    t_load0 = time.perf_counter()
    model = SentenceTransformer(model_id, device="cpu", trust_remote_code=True)
    load_s = time.perf_counter() - t_load0
    tok = model.tokenizer

    def tok_count(text: str) -> int:
        return len(
            tok(text, truncation=True, max_length=args.max_len, add_special_tokens=False)[
                "input_ids"
            ]
        )

    doc_prefix, query_prefix = model_prefixes(model_id)

    def truncate(text: str) -> str:
        ids = tok(text, truncation=True, max_length=args.max_len, add_special_tokens=False)["input_ids"]
        return tok.decode(ids)

    # sort by length so each batch pads only to its own longest member
    order = sorted(range(len(chunks)), key=lambda i: len(chunks[i]["text"]))
    doc_texts = [truncate(doc_prefix + chunks[i]["text"]) for i in order]
    q_texts = [truncate(query_prefix + q["q"]) for q in queries]

    doc_toks = sum(tok_count(t) for t in doc_texts)
    q_toks = sum(tok_count(t) for t in q_texts)
    total_toks = doc_toks + q_toks

    t0 = time.perf_counter()
    D = torch.as_tensor(
        model.encode(doc_texts, batch_size=args.batch, normalize_embeddings=True, show_progress_bar=False),
        dtype=torch.float32,
    )
    Q = torch.as_tensor(
        model.encode(q_texts, batch_size=args.batch, normalize_embeddings=True, show_progress_bar=False),
        dtype=torch.float32,
    )
    embed_s = time.perf_counter() - t0

    # quality: S[i, j] = sim(query_i, doc_texts[j] = chunks[order[j]])
    S = Q @ D.T
    topk = min(args.topk, S.shape[1])
    recall = {1: 0, 5: 0, 10: 0}
    mrr = 0.0
    per_query = []
    for i, q in enumerate(queries):
        rel = truth[q["q"]]
        idx = S[i].topk(topk).indices.tolist()
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
    quality = {
        "recall@1": round(recall[1] / n, 4),
        "recall@5": round(recall[5] / n, 4),
        "recall@10": round(recall[10] / n, 4),
        "mrr@10": round(mrr / n, 4),
        "per_query": per_query,
    }

    # single-document latency (the real embed_one cost): warm up, then time a few
    latency = None
    if not args.no_latency and doc_texts:
        model.encode([doc_texts[0]], normalize_embeddings=True, show_progress_bar=False)
        sample_n = min(args.latency_n, len(doc_texts))
        samples = []
        for j in range(sample_n):
            ts = time.perf_counter()
            model.encode([doc_texts[j]], normalize_embeddings=True, show_progress_bar=False)
            samples.append(time.perf_counter() - ts)
        samples.sort()
        latency = {
            "n": len(samples),
            "median_ms": round(1000 * percentile(samples, 0.5), 1),
            "p95_ms": round(1000 * percentile(samples, 0.95), 1),
        }

    return {
        "model": model_id,
        "embedding_dim": int(D.shape[1]),
        "quality": quality,
        "speed": {
            "total_tokens": total_toks,
            "embed_seconds": round(embed_s, 2),
            "tokens_per_sec": round(total_toks / embed_s, 1) if embed_s else None,
            "doc_latency": latency,
        },
        "load_seconds": round(load_s, 2),
        "data": {
            "n_chunks": len(chunks),
            "n_queries": n,
            "n_pages": len({c["url"] for c in chunks}),
        },
        "env": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu": detect_cpu(),
            "cpu_count": os.cpu_count(),
            "torch": torch.__version__,
            "threads": args.threads,
            "batch": args.batch,
            "max_len": args.max_len,
        },
    }


def slug(model_id: str) -> str:
    return model_id.replace("/", "__").replace(":", "_").lower()


def print_report(results: list[dict]) -> None:
    if not results:
        return
    print()
    print("=" * 78)
    print(f"{'model':<34} {'R@1':>5} {'R@10':>5} {'MRR@10':>7} {'tok/s':>8} {'ms/doc':>9}")
    print("-" * 78)
    for r in results:
        q = r["quality"]
        sp = r["speed"]
        ms = (sp["doc_latency"] or {}).get("median_ms", "-")
        print(
            f"{r['model']:<34} {q['recall@1']:>5.2f} {q['recall@10']:>5.2f} "
            f"{q['mrr@10']:>7.3f} {sp['tokens_per_sec']:>8.0f} {ms:>9}"
        )
    print("-" * 78)
    for r in results:
        hits = sum(1 for p in r["quality"]["per_query"] if p["best_rank"])
        print(f"  {r['model']}: {hits}/{len(r['quality']['per_query'])} queries hit in top-10, "
              f"dim={r['embedding_dim']}, load={r['load_seconds']}s")
        misses = [p["query"] for p in r["quality"]["per_query"] if not p["best_rank"]]
        if misses:
            print(f"    missed: {'; '.join(misses)}")
    print("=" * 78)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", action="append", default=None,
                   help="Hugging Face model id (repeatable). Default: nomic + qwen3-embed.")
    p.add_argument("--batch", type=int, default=16, help="encode batch size (default 16)")
    p.add_argument("--max-len", type=int, default=512, help="max tokens per doc (default 512)")
    p.add_argument("--threads", type=int, default=8, help="torch CPU threads (default 8)")
    p.add_argument("--topk", type=int, default=10, help="retrieval cutoff (default 10)")
    p.add_argument("--latency-n", type=int, default=5, help="single-doc latency samples (default 5)")
    p.add_argument("--no-latency", action="store_true", help="skip single-doc latency measurement")
    p.add_argument("--data", type=Path, default=DATA_DIR, help="test data dir")
    p.add_argument("--results", type=Path, default=RESULTS_DIR, help="results dir")
    p.add_argument("--no-save", action="store_true", help="don't write JSON result files")
    a = p.parse_args(argv)
    a.model = a.model or DEFAULT_MODELS
    return a


def main(argv=None) -> int:
    args = parse_args(argv)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch

    torch.set_num_threads(args.threads)

    chunks, queries, truth = load_data(args.data)
    print(f"Loaded test set: {len(chunks)} chunks, {len(queries)} queries, "
          f"{len({c['url'] for c in chunks})} pages")
    print(f"Models: {', '.join(args.model)}\n")

    results = []
    for m in args.model:
        print(f"### {m} " + "-" * max(0, 60 - len(m)))
        t0 = time.perf_counter()
        r = run_model(m, chunks, queries, truth, args)
        wall = time.perf_counter() - t0
        print(
            f"  quality: R@1={r['quality']['recall@1']:.2f} R@5={r['quality']['recall@5']:.2f} "
            f"R@10={r['quality']['recall@10']:.2f} MRR@10={r['quality']['mrr@10']:.3f}"
        )
        sp = r["speed"]
        lat = sp["doc_latency"] or {}
        print(
            f"  speed:   {sp['total_tokens']:,} tokens in {sp['embed_seconds']}s "
            f"= {sp['tokens_per_sec']} tok/s"
            + (f" | doc latency median {lat.get('median_ms')}ms / p95 {lat.get('p95_ms')}ms" if lat else "")
            + f" | wall {wall:.1f}s"
        )
        results.append(r)

    print_report(results)

    if not args.no_save:
        args.results.mkdir(parents=True, exist_ok=True)
        for r in results:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out = args.results / f"{slug(r['model'])}_{stamp}.json"
            out.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

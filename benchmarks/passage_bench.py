#!/usr/bin/env python3
"""Compare experimental page-local passage ranking against labeled fixtures.

This is a research harness, not the fetch implementation. Run with the app's
dependencies and model cache: ``python benchmarks/passage_bench.py``.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import statistics
import time

from wellisearch.chunk import chunk_markdown
from wellisearch.config import get_settings
from wellisearch.embed import embed, embed_one
from wellisearch.index import _with_title

DATA = Path(__file__).resolve().parent / "data" / "passage" / "fixtures.json"
WINDOW_CHARS = 400
RRF_K = 60
TOKEN_RE = re.compile(r"[\w]+(?:[.-][\w]+)*", re.UNICODE)
METHODS = ("keyword", "semantic", "hybrid")


def load_fixtures(path: Path) -> dict:
    """Load and validate answer labels before evaluating any ranking method."""
    data = json.loads(path.read_text(encoding="utf-8"))
    pages = {page["id"]: page for page in data["pages"]}
    if len(pages) != len(data["pages"]):
        raise ValueError("duplicate page id")
    ids = set()
    for case in data["queries"]:
        if case["id"] in ids or case["page"] not in pages or not case["query"].strip():
            raise ValueError(f"invalid query: {case['id']}")
        ids.add(case["id"])
        answer = case["answer"]
        if answer is not None and page_markdown(pages[case["page"]]).count(answer) != 1:
            raise ValueError(f"answer must occur exactly once: {case['id']}")
        if (answer is None) != (case["category"] == "no-answer"):
            raise ValueError(f"inconsistent no-answer label: {case['id']}")
    return data


def page_markdown(page: dict) -> str:
    """Build the body, including long boilerplate ahead of relevant sections."""
    parts = [page["preamble"] * page["preamble_repeat"], *page["sections"]]
    if page.get("fence_repeat"):
        fence = "```ini\n" + "GENERIC_SETTING=default\n" * page["fence_repeat"]
        parts.insert(1, fence + "SPECIAL_DEPLOY_KEY=enabled\n```")
    return "\n\n".join(parts)


def keyword_scores(query: str, chunks: list[str]) -> list[float]:
    """Score distinct literal terms with a small exact-phrase bonus.

    This simple baseline is intentionally not Postgres full-text search.
    """
    terms = set(TOKEN_RE.findall(query.casefold()))
    phrase = query.casefold().strip()
    return [
        len(terms & set(TOKEN_RE.findall(chunk.casefold()))) / max(1, len(terms))
        + (1.0 if phrase and phrase in chunk.casefold() else 0.0)
        for chunk in chunks
    ]


def cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity, including a safe zero-vector case."""
    if len(a) != len(b):
        raise ValueError("embedding dimensions differ")
    norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return float(sum(x * y for x, y in zip(a, b)) / norm) if norm else 0.0


def rank_indices(scores: list[float]) -> list[int]:
    """Rank highest score first and break ties by original page order."""
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def hybrid_scores(lexical: list[float], semantic: list[float]) -> list[float]:
    """Fuse ranks; zero-overlap keyword candidates earn no lexical credit."""
    if len(lexical) != len(semantic):
        raise ValueError("score counts differ")
    out = [0.0] * len(lexical)
    for rank, i in enumerate(rank_indices(semantic), 1):
        out[i] += 1 / (RRF_K + rank)
    for rank, i in enumerate(rank_indices(lexical), 1):
        if lexical[i] > 0:
            out[i] += 1 / (RRF_K + rank)
    return out


def evaluate_case(
    case: dict,
    chunks: list[str],
    vectors: list[list[float]],
    query_vector: list[float],
) -> dict:
    """Rank the same page's chunks with all methods and record labeled outcomes."""
    if len(chunks) != len(vectors) or not chunks:
        raise ValueError("chunks and vectors must be nonempty and aligned")
    started = time.perf_counter()
    lexical = keyword_scores(case["query"], chunks)
    keyword_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    semantic = [cosine(query_vector, vector) for vector in vectors]
    semantic_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    hybrid = hybrid_scores(lexical, semantic)
    hybrid_ms = (time.perf_counter() - started) * 1000
    scores = {"keyword": lexical, "semantic": semantic, "hybrid": hybrid}
    result = {}
    for method in METHODS:
        ranking = rank_indices(scores[method])
        top = ranking[0]
        answer = case["answer"]
        result[method] = {
            "top_chunk": top,
            "top_score": scores[method][top],
            "chunk_hit": answer in chunks[top] if answer is not None else None,
            "window_hit": answer in chunks[top][:WINDOW_CHARS] if answer is not None else None,
        }
    return {
        "category": case["category"], "id": case["id"], "page": case["page"],
        "ranking_ms": {"keyword": keyword_ms, "semantic": semantic_ms, "hybrid_fusion": hybrid_ms},
        "results": result,
    }


def summarize(rows: list[dict]) -> dict:
    """Report positive-case recall and unclassified negatives separately."""
    positive = [row for row in rows if row["category"] != "no-answer"]
    negative = [row for row in rows if row["category"] == "no-answer"]
    summary = {"positive_cases": len(positive), "no_answer_cases": len(negative), "methods": {}}
    for method in METHODS:
        by_category = {}
        for category in sorted({row["category"] for row in positive}):
            group = [row for row in positive if row["category"] == category]
            by_category[category] = {
                "count": len(group),
                "chunk_hits": sum(row["results"][method]["chunk_hit"] for row in group),
                "window_hits": sum(row["results"][method]["window_hit"] for row in group),
            }
        summary["methods"][method] = {
            "chunk_hits": sum(row["results"][method]["chunk_hit"] for row in positive),
            "window_hits": sum(row["results"][method]["window_hit"] for row in positive),
            "by_category": by_category,
        }
    return summary


def main() -> None:
    """Embed production-style chunks and print a reproducible JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA)
    args = parser.parse_args()
    data = load_fixtures(args.data)
    pages = {}
    for page in data["pages"]:
        source = _with_title(page_markdown(page), page["title"])
        chunks = chunk_markdown(source, get_settings().MAX_CHUNK_TOKENS)
        pages[page["id"]] = (chunks, embed(chunks))
    rows = []
    embedding_times = []
    for case in data["queries"]:
        started = time.perf_counter()
        query_vector = embed_one(case["query"])
        embedding_times.append((time.perf_counter() - started) * 1000)
        chunks, vectors = pages[case["page"]]
        rows.append(evaluate_case(case, chunks, vectors, query_vector))
    report = {
        "model": get_settings().EMBED_MODEL,
        "chunk_tokens": get_settings().MAX_CHUNK_TOKENS,
        "window_chars": WINDOW_CHARS,
        "pages": len(pages),
        "chunk_counts": {name: len(value[0]) for name, value in pages.items()},
        "query_embedding_median_ms": statistics.median(embedding_times),
        "summary": summarize(rows),
        "cases": rows,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

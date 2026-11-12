#!/usr/bin/env python3
"""Read-only passage ranking experiments on a deterministic live-index sample.

Run in an environment with the wellisearch database settings and dependencies.
The report contains aggregate results and hashed page IDs, not page contents.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import statistics
import time

import numpy as np
from pgvector.psycopg import register_vector
import psycopg
from psycopg.rows import dict_row

from wellisearch.config import get_settings
from wellisearch.embed import embed_one

from passage_bench import hybrid_scores, keyword_scores, rank_indices

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "data" / "passage" / "live_questions.json"
BUCKETS = ((4000, 8000), (8000, 20000), (20000, 50000), (50000, 150000))
CANDIDATES_PER_BUCKET = 2000
DEFAULT_PER_BUCKET = 15
HEADING_RE = re.compile(r"(?m)^#{2,5}\s+([^\n]{8,120})\s*$")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")
WINDOW_CHARS = 400
METHODS = ("keyword", "postgres_fts", "fts_fallback", "semantic", "hybrid", "fts_hybrid")


def sample_pages(conn: psycopg.Connection, per_bucket: int) -> tuple[list[dict], dict]:
    """Select balanced pages, then fill shortfalls from other length ranges."""
    selected = []
    domains: set[str] = set()
    counts = {}
    candidates = {}
    for low, high in BUCKETS:
        bucket = f"{low}-{high - 1}"
        rows = conn.execute(
            "SELECT url, left(md5(url), 10) AS id, domain, embedding_model "
            "FROM pages WHERE NOT disabled AND length(fit_markdown) >= %s "
            "AND length(fit_markdown) < %s AND embedding_model = %s "
            "ORDER BY md5(url) LIMIT %s",
            (low, high, get_settings().EMBED_MODEL, CANDIDATES_PER_BUCKET),
        ).fetchall()
        candidates[bucket] = rows
        counts[bucket] = _select_candidates(conn, rows, bucket, selected, domains, per_bucket)
    for bucket, rows in candidates.items():
        missing = per_bucket * len(BUCKETS) - len(selected)
        if missing <= 0:
            break
        counts[bucket] += _select_candidates(conn, rows, bucket, selected, domains, missing)
    return selected, counts


def _select_candidates(
    conn: psycopg.Connection,
    rows: list[dict],
    bucket: str,
    selected: list[dict],
    domains: set[str],
    limit: int,
) -> int:
    """Add up to limit eligible pages, preserving hash order and unique domains."""
    found = 0
    for page in rows:
        if not page["domain"] or page["domain"] in domains:
            continue
        chunks = load_chunks(conn, page["url"])
        if len(chunks) < 3 or any(chunk["embedding"] is None for chunk in chunks):
            continue
        page["fit_markdown"] = conn.execute(
            "SELECT fit_markdown FROM pages WHERE url = %s", (page["url"],)
        ).fetchone()["fit_markdown"]
        heading = select_heading(page, chunks)
        if heading is None:
            continue
        page["chunks"] = chunks
        page["heading"] = heading
        page["bucket"] = bucket
        selected.append(page)
        domains.add(page["domain"])
        found += 1
        if found == limit:
            break
    return found


def load_chunks(conn: psycopg.Connection, url: str) -> list[dict]:
    """Read existing chunks, including their stored vectors, in page order."""
    return conn.execute(
        "SELECT seq, text, embedding FROM chunks WHERE url = %s ORDER BY seq", (url,)
    ).fetchall()


def select_heading(page: dict, chunks: list[dict]) -> dict | None:
    """Select one unique body heading after the first chunk, independent of ranking."""
    if len(chunks) < 3 or any(chunk["embedding"] is None for chunk in chunks):
        return None
    candidates = []
    body = page["fit_markdown"]
    for chunk in chunks[1:]:
        for match in HEADING_RE.finditer(chunk["text"]):
            heading = match.group(1).strip()
            if len(WORD_RE.findall(heading)) < 3 or body.count(heading) != 1:
                continue
            if any(char in heading for char in "[]`<>|\\"):
                continue
            candidates.append({"heading": heading, "seq": chunk["seq"], "offset": body.index(heading)})
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["offset"])
    return candidates[len(candidates) // 2]


def load_manual_cases(conn: psycopg.Connection, path: Path) -> tuple[list[dict], list[str]]:
    """Resolve hand-authored questions; report stale or ambiguous labels."""
    cases = []
    skipped = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        pages = conn.execute(
            "SELECT url, left(md5(url), 10) AS id, domain, fit_markdown, embedding_model "
            "FROM pages WHERE left(md5(url), 10) = %s AND NOT disabled", (item["page"],)
        ).fetchall()
        if len(pages) != 1 or pages[0]["embedding_model"] != get_settings().EMBED_MODEL:
            skipped.append(item["id"])
            continue
        page = pages[0]
        chunks = load_chunks(conn, page["url"])
        answers = item.get("answers", [item["answer"]])
        matches = [chunk["seq"] for chunk in chunks if any(answer in chunk["text"] for answer in answers)]
        if not matches or not chunks or any(chunk["embedding"] is None for chunk in chunks):
            skipped.append(item["id"])
            continue
        cases.append({"page": page, "chunks": chunks, "query": item["query"], "answer": item["answer"],
                      "answers": answers, "targets": matches, "category": "manual", "id": item["id"]})
    return cases, skipped


def heading_cases(pages: list[dict]) -> list[dict]:
    """Use each chosen real heading as a query; ground truth is its unique span."""
    return [
        {"page": page, "chunks": page["chunks"], "query": page["heading"]["heading"],
         "answer": page["heading"]["heading"], "targets": [page["heading"]["seq"]],
         "category": "heading", "id": page["id"]}
        for page in pages
    ]


def evaluate(conn: psycopg.Connection, case: dict) -> dict:
    """Rank stored chunks and measure the component costs of each option."""
    started = time.perf_counter()
    vector = np.asarray(embed_one(case["query"]), dtype=np.float32)
    embed_ms = (time.perf_counter() - started) * 1000
    sql_tail = " FROM chunks WHERE url = %s ORDER BY seq"
    fts_sql = (
        "ts_rank_cd(tsv, plainto_tsquery('english', %s)) AS lexical, "
        "ts_rank_cd(tsv, to_tsquery('english', "
        "replace(plainto_tsquery('english', %s)::text, ' & ', ' | '))) AS lexical_or"
    )
    started = time.perf_counter()
    fts_rows = conn.execute(
        "SELECT seq, " + fts_sql + sql_tail,
        (case["query"], case["query"], case["page"]["url"]),
    ).fetchall()
    fts_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    vector_rows = conn.execute(
        "SELECT seq, 1 - (embedding <=> %s::vector) AS similarity" + sql_tail,
        (vector, case["page"]["url"]),
    ).fetchall()
    vector_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    rows = conn.execute(
        "SELECT seq, 1 - (embedding <=> %s::vector) AS similarity, "
        + fts_sql + sql_tail,
        (vector, case["query"], case["query"], case["page"]["url"]),
    ).fetchall()
    sql_ms = (time.perf_counter() - started) * 1000
    chunks = case["chunks"]
    sequences = [chunk["seq"] for chunk in chunks]
    if any([row["seq"] for row in group] != sequences for group in (rows, fts_rows, vector_rows)):
        raise RuntimeError("chunk snapshot mismatch")
    started = time.perf_counter()
    lexical = keyword_scores(case["query"], [chunk["text"] for chunk in chunks])
    keyword_ms = (time.perf_counter() - started) * 1000
    semantic = [float(row["similarity"]) for row in vector_rows]
    fts = [float(row["lexical"]) for row in fts_rows]
    fallback = fts if any(fts) else [float(row["lexical_or"]) for row in fts_rows]
    combined_semantic = [float(row["similarity"]) for row in rows]
    combined_fts = [float(row["lexical"]) for row in rows]
    combined_fallback = combined_fts if any(combined_fts) else [float(row["lexical_or"]) for row in rows]
    scores = {"keyword": lexical, "postgres_fts": fts, "fts_fallback": fallback, "semantic": semantic}
    started = time.perf_counter()
    scores["hybrid"] = hybrid_scores(lexical, semantic)
    hybrid_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    scores["fts_hybrid"] = hybrid_scores(combined_fallback, combined_semantic)
    fts_hybrid_ms = (time.perf_counter() - started) * 1000
    results = {}
    answers = case.get("answers", [case["answer"]])
    latencies = {"keyword": keyword_ms, "postgres_fts": fts_ms, "fts_fallback": fts_ms,
                 "semantic": embed_ms + vector_ms, "hybrid": embed_ms + vector_ms + keyword_ms + hybrid_ms,
                 "fts_hybrid": embed_ms + sql_ms + fts_hybrid_ms}
    for method in METHODS:
        started = time.perf_counter()
        top = rank_indices(scores[method])[0]
        latencies[method] += (time.perf_counter() - started) * 1000
        results[method] = {
            "top_seq": chunks[top]["seq"],
            "chunk_hit": chunks[top]["seq"] in case["targets"],
            "window_hit": any(answer in chunks[top]["text"][:WINDOW_CHARS] for answer in answers),
            "top_score": scores[method][top],
        }
    return {"id": case["id"], "page": case["page"]["id"], "category": case["category"],
            "chunks": len(chunks), "embed_ms": embed_ms, "sql_ms": sql_ms,
            "method_latency_ms": latencies, "results": results}


def latency_summary(rows: list[dict]) -> dict:
    """Return warm p50 and nearest-rank p95 of per-case component sums."""
    if len(rows) < 2:
        return {}
    result = {}
    for method in METHODS:
        values = sorted(row["method_latency_ms"][method] for row in rows[1:])
        result[method] = {"p50_ms": statistics.median(values),
                          "p95_ms": values[math.ceil(0.95 * len(values)) - 1]}
    return result


def summarize(rows: list[dict]) -> dict:
    """Summarize each label source separately to avoid mixing different tasks."""
    out = {}
    for category in sorted({row["category"] for row in rows}):
        group = [row for row in rows if row["category"] == category]
        out[category] = {"cases": len(group), "methods": {}}
        for method in METHODS:
            out[category]["methods"][method] = {
                "chunk_hits": sum(row["results"][method]["chunk_hit"] for row in group),
                "window_hits": sum(row["results"][method]["window_hit"] for row in group),
            }
    return out


def main() -> None:
    """Run one repeatable-read, read-only snapshot and print aggregate JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    args = parser.parse_args()
    if args.per_bucket < 1 or args.per_bucket > CANDIDATES_PER_BUCKET:
        parser.error("--per-bucket must be between 1 and the candidate limit")
    settings = get_settings()
    with psycopg.connect(settings.conninfo(), row_factory=dict_row, autocommit=True) as conn:
        register_vector(conn)
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '30s'")
        pages, buckets = sample_pages(conn, args.per_bucket)
        manual, skipped = load_manual_cases(conn, args.questions)
        rows = [evaluate(conn, case) for case in heading_cases(pages) + manual]
    report = {
        "model": settings.EMBED_MODEL, "target_pages": args.per_bucket * len(BUCKETS),
        "bucket_pages": buckets,
        "sample_domains": len({page["domain"] for page in pages}),
        "manual_skipped": skipped, "window_chars": WINDOW_CHARS,
        "sample_chunk_counts": [len(page["chunks"]) for page in pages],
        "heading_offsets_over_20000": sum(page["heading"]["offset"] > 20000 for page in pages),
        "first_query_embedding_ms": rows[0]["embed_ms"] if rows else None,
        "warm_query_embedding_median_ms": statistics.median(row["embed_ms"] for row in rows[1:])
        if len(rows) > 1 else None,
        "sql_combined_median_ms": statistics.median(row["sql_ms"] for row in rows) if rows else None,
        "warm_method_latency_ms": latency_summary(rows), "summary": summarize(rows), "cases": rows,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

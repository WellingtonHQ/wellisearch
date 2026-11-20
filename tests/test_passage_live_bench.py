"""Offline tests for read-only live passage sampling and scoring."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))
SPEC = importlib.util.spec_from_file_location("passage_live_bench", BENCHMARKS / "passage_live_bench.py")
assert SPEC and SPEC.loader
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


class _Rows:
    """Minimal cursor for the in-memory scoring test."""

    def fetchall(self) -> list[dict]:
        """Provide scores where AND fails but OR matches the answer."""
        return [
            {"seq": 0, "similarity": 0.2, "lexical": 0.0, "lexical_or": 0.0},
            {"seq": 1, "similarity": 0.9, "lexical": 0.0, "lexical_or": 0.5},
        ]


class _Connection:
    """Read-only fake connection for scoring tests."""

    def execute(self, query: str, params: tuple) -> _Rows:
        """Return the prearranged query result."""
        return _Rows()


class LivePassageBenchmarkTests(unittest.TestCase):
    """Pin sampling eligibility and scoring independent of live data."""

    def test_heading_must_be_unique_in_body_and_after_first_chunk(self) -> None:
        """Repeated headings do not make reliable ground truth."""
        body = "## First heading here\nX\n## Useful unique heading\nY\n## Repeated heading words\nZ"
        chunks = [
            {"seq": 0, "text": "## First heading here", "embedding": [1]},
            {"seq": 1, "text": "## Useful unique heading", "embedding": [1]},
            {"seq": 2, "text": "## Repeated heading words", "embedding": [1]},
        ]
        body += "\n## Repeated heading words"
        selected = bench.select_heading({"fit_markdown": body}, chunks)
        self.assertEqual(selected["seq"], 1)

    def test_missing_embeddings_exclude_page(self) -> None:
        """Do not compare a different candidate pool for semantic ranking."""
        chunks = [{"seq": i, "text": "## Some heading words", "embedding": None} for i in range(3)]
        self.assertIsNone(bench.select_heading({"fit_markdown": "## Some heading words"}, chunks))

    def test_fts_or_fallback_is_scored_separately(self) -> None:
        """The production-style OR fallback must not be mistaken for AND."""
        chunks = [{"seq": 0, "text": "irrelevant"}, {"seq": 1, "text": "answer"}]
        case = {"id": "x", "category": "manual", "query": "question", "answer": "answer",
                "targets": [1], "page": {"id": "p", "url": "u"}, "chunks": chunks}
        with patch.object(bench, "embed_one", return_value=[1.0]):
            result = bench.evaluate(_Connection(), case)["results"]
        self.assertFalse(result["postgres_fts"]["chunk_hit"])
        self.assertTrue(result["fts_fallback"]["chunk_hit"])
        self.assertTrue(result["fts_hybrid"]["window_hit"])

    def test_alternative_answers_count_in_the_returned_window(self) -> None:
        """An independently labeled alternative passage is a valid answer."""
        chunks = [{"seq": 0, "text": "irrelevant"}, {"seq": 1, "text": "alternative"}]
        case = {"id": "x", "category": "manual", "query": "question", "answer": "primary",
                "answers": ["primary", "alternative"], "targets": [1],
                "page": {"id": "p", "url": "u"}, "chunks": chunks}
        with patch.object(bench, "embed_one", return_value=[1.0]):
            result = bench.evaluate(_Connection(), case)["results"]
        self.assertTrue(result["fts_hybrid"]["window_hit"])

    def test_latency_excludes_first_query_and_reports_p95(self) -> None:
        """Cold model initialization does not distort warm latency percentiles."""
        rows = [{"method_latency_ms": {method: value for method in bench.METHODS}}
                for value in (1000.0, 1.0, 2.0, 3.0)]
        result = bench.latency_summary(rows)["semantic"]
        self.assertEqual(result, {"p50_ms": 2.0, "p95_ms": 3.0})

    def test_short_bucket_is_filled_from_another_bucket(self) -> None:
        """The target sample size survives an underrepresented length range."""
        candidates = {
            1: [{"url": f"u{i}", "domain": f"d{i}"} for i in range(3)],
            2: [{"url": "other", "domain": "other"}],
        }

        class Cursor:
            """Provide candidate rows or one page body."""

            def __init__(self, rows: list[dict]) -> None:
                """Retain the selected fake rows."""
                self.rows = rows

            def fetchall(self) -> list[dict]:
                """Return candidate rows."""
                return self.rows

            def fetchone(self) -> dict:
                """Return a page body."""
                return {"fit_markdown": "body"}

        class Connection:
            """Supply fake rows for the sampling query."""

            def execute(self, query: str, params: tuple) -> Cursor:
                """Select the candidate range or a page body."""
                return Cursor(candidates[params[0]] if "length(fit_markdown)" in query else [])

        chunks = [{"embedding": [1]} for _ in range(3)]
        with (patch.object(bench, "BUCKETS", ((1, 2), (2, 3))),
              patch.object(bench, "get_settings", return_value=SimpleNamespace(EMBED_MODEL="model")),
              patch.object(bench, "load_chunks", return_value=chunks),
              patch.object(bench, "select_heading", return_value={"heading": "valid"})):
            pages, counts = bench.sample_pages(Connection(), 2)
        self.assertEqual(len(pages), 4)
        self.assertEqual(counts, {"1-1": 3, "2-2": 1})
        self.assertEqual(len({page["domain"] for page in pages}), 4)


if __name__ == "__main__":
    unittest.main()

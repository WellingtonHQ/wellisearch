"""Contract tests for the page-passage benchmark; no database or model needed."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest

import numpy as np

PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "passage_bench.py"
SPEC = importlib.util.spec_from_file_location("passage_bench", PATH)
assert SPEC and SPEC.loader
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


class PassageBenchmarkTests(unittest.TestCase):
    """Pin labels, ranking, and the distinction between chunks and windows."""

    def test_fixture_labels_and_long_preamble(self) -> None:
        """Every labeled answer is unique and at least one lies past 20,000 chars."""
        data = bench.load_fixtures(bench.DATA)
        pages = {page["id"]: bench.page_markdown(page) for page in data["pages"]}
        cases = [case for case in data["queries"] if case["answer"]]
        self.assertTrue(any(pages[case["page"]].index(case["answer"]) > 20000 for case in cases))
        self.assertTrue(any(case["answer"] is None for case in data["queries"]))

    def test_exact_identifier_and_stable_ties(self) -> None:
        """Distinct identifiers win over adjacent codes; ties follow page order."""
        scores = bench.keyword_scores("E_CONN_042", ["E_CONN_043", "E_CONN_042"])
        self.assertEqual(bench.rank_indices(scores), [1, 0])
        self.assertEqual(bench.rank_indices([1.0, 1.0]), [0, 1])

    def test_window_is_not_the_same_as_chunk(self) -> None:
        """An answer deep in a selected chunk is not counted as returned."""
        case = {"id": "tail", "page": "p", "category": "keyword", "query": "needle", "answer": "needle"}
        row = bench.evaluate_case(case, ["x" * 500 + " needle"], [[1.0, 0.0]], [1.0, 0.0])
        for result in row["results"].values():
            self.assertTrue(result["chunk_hit"])
            self.assertFalse(result["window_hit"])

    def test_no_answer_is_not_a_false_positive_or_a_success(self) -> None:
        """Negative examples stay unclassified until a threshold is calibrated."""
        case = {"id": "negative", "page": "p", "category": "no-answer", "query": "missing", "answer": None}
        row = bench.evaluate_case(case, ["unrelated"], [[1.0]], [1.0])
        self.assertIsNone(row["results"]["semantic"]["chunk_hit"])
        self.assertEqual(bench.summarize([row])["no_answer_cases"], 1)

    def test_hybrid_has_no_credit_for_zero_keyword_overlap(self) -> None:
        """An arbitrary page-order tie is not treated as lexical evidence."""
        scores = bench.hybrid_scores([0.0, 0.0], [0.1, 0.9])
        self.assertEqual(bench.rank_indices(scores), [1, 0])

    def test_numpy_embeddings_produce_json_serializable_report(self) -> None:
        """Production FastEmbed vectors contain NumPy scalar values."""
        case = {"id": "one", "page": "p", "category": "keyword", "query": "yes", "answer": "yes"}
        row = bench.evaluate_case(case, ["yes"], [[np.float32(1)]], [np.float32(1)])
        json.dumps(row)


if __name__ == "__main__":
    unittest.main()

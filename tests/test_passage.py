"""Passage navigation for fetch_page: find (literal), query (relevance), offset.

Pure-logic + mocked-DB tests (no Postgres, no embedding model, no network):
  python tests/test_passage.py
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from wellisearch import fetch
from wellisearch.fetch import render_fetch_page_markdown
from wellisearch.passage import (
    chunk_body_offset,
    fuse_ranks,
    locate_literal,
    make_window,
    rank_indices,
)

BODY = (
    "Intro line.\n\n"
    "## Setup\n\n"
    "Run the installer with the --force flag.\n\n"
    "## Troubleshooting\n\n"
    "Error E_CONN_042 means the pool is exhausted. Retry after raising the limit.\n\n"
    "## Billing\n\n"
    "Update your card on the billing page.\n"
)


# ---------------------------------------------------------------------------
# passage.py — pure logic
# ---------------------------------------------------------------------------

class PassagePureTests(unittest.TestCase):
    """Pin literal location, ranking fusion, windowing, and chunk mapping."""

    def test_locate_literal_is_case_insensitive_and_exact(self) -> None:
        pos = locate_literal(BODY, "e_conn_042")
        self.assertIsNotNone(pos)
        self.assertEqual(BODY[pos: pos + 10].lower(), "e_conn_042")
        # Exact position, not line-backed: the char before the match is the
        # space after "Error", not the start of the line.
        self.assertEqual(BODY[pos - 1], " ")

    def test_locate_literal_respects_offset(self) -> None:
        first = locate_literal(BODY, "the")
        later = locate_literal(BODY, "the", (first or 0) + 1)
        self.assertIsNotNone(first)
        self.assertIsNotNone(later)
        # Both "the" sit on the installer line: exact positions advance past
        # the first match (line-backing would have collapsed them to one).
        self.assertEqual(BODY[first: first + 3].lower(), "the")
        self.assertEqual(BODY[later: later + 3].lower(), "the")
        self.assertGreater(later, first)

    def test_locate_literal_absent(self) -> None:
        self.assertIsNone(locate_literal(BODY, "ZZZ_NOT_THERE"))

    def test_rank_indices_ties_by_page_order(self) -> None:
        self.assertEqual(rank_indices([1.0, 1.0, 0.5]), [0, 1, 2])

    def test_fuse_ranks_ignores_zero_lexical(self) -> None:
        # semantic says chunk 1 is best; lexical all-zero -> no lexical credit
        self.assertEqual(rank_indices(fuse_ranks([0.0, 0.0], [0.2, 0.9])), [1, 0])
        # lexical overlap on chunk 0 boosts it over a semantic tie
        self.assertEqual(rank_indices(fuse_ranks([0.5, 0.0], [0.5, 0.5]))[0], 0)

    def test_make_window_bounds_and_next(self) -> None:
        w = make_window(BODY, 0, 40)
        self.assertEqual(w.start_offset, 0)
        self.assertLessEqual(w.end_offset, len(BODY))
        self.assertTrue(w.truncated)
        self.assertEqual(w.next_offset, w.end_offset)
        w_end = make_window(BODY, len(BODY), 40)
        self.assertEqual(w_end.window, "")
        self.assertIsNone(w_end.next_offset)
        self.assertFalse(w_end.truncated)

    def test_chunk_body_offset_maps_to_true_body_position(self) -> None:
        text = "Run the installer with the --force flag."
        pos = chunk_body_offset(BODY, None, text)
        self.assertEqual(BODY.find(text), pos)

    def test_chunk_body_offset_with_prepended_title(self) -> None:
        text = "Error E_CONN_042 means the pool is exhausted."
        pos = chunk_body_offset(BODY, "My Title", text)
        self.assertEqual(BODY.find(text), pos)


# ---------------------------------------------------------------------------
# fetch.fetch_page — navigation (db + embed mocked)
# ---------------------------------------------------------------------------

STORED = {
    "url": "https://example.com/a",
    "title": None,
    "fit_markdown": BODY,
    "fetch_count": 1,
}


def _fake_db(ranks: list[dict] | None = None) -> Mock:
    """A mock db serving the stored page and canned chunk ranks."""
    return Mock(
        page_get=AsyncMock(return_value=STORED),
        bump_fetch_count=AsyncMock(),
        page_chunk_ranks=AsyncMock(return_value=ranks or []),
    )


class FetchNavigationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise find/query/offset through the public fetch_page path."""

    async def test_head_mode_defaults(self) -> None:
        with patch.object(fetch, "db", _fake_db()):
            out = await fetch.fetch_page("https://example.com/a", max_chars=50)
        self.assertEqual(out["mode"], "head")
        self.assertEqual(out["start_offset"], 0)
        self.assertEqual(out["total_chars"], len(BODY))
        self.assertLess(out["end_offset"], len(BODY))
        self.assertEqual(out["next_offset"], out["end_offset"])
        self.assertTrue(out["truncated"])

    async def test_find_returns_match_window(self) -> None:
        with patch.object(fetch, "db", _fake_db()):
            out = await fetch.fetch_page("https://example.com/a", find="E_CONN_042", max_chars=200)
        self.assertEqual(out["mode"], "find")
        self.assertTrue(out["found"])
        self.assertIn("E_CONN_042", out["markdown"])
        self.assertGreater(out["start_offset"], 0)
        self.assertGreater(out["end_offset"], out["start_offset"])

    async def test_find_not_found_reports_empty(self) -> None:
        with patch.object(fetch, "db", _fake_db()):
            out = await fetch.fetch_page("https://example.com/a", find="NOPE")
        self.assertEqual(out["mode"], "find")
        self.assertFalse(out["found"])
        self.assertEqual(out["markdown"], "")
        self.assertEqual(out["chars"], 0)

    async def test_find_and_query_mutually_exclusive(self) -> None:
        with patch.object(fetch, "db", _fake_db()):
            out = await fetch.fetch_page("https://example.com/a", find="x", query="y")
        self.assertFalse(out["ok"])
        self.assertIn("mutually exclusive", out["error"])

    async def test_negative_offset_rejected(self) -> None:
        with patch.object(fetch, "db", _fake_db()):
            out = await fetch.fetch_page("https://example.com/a", offset=-5)
        self.assertFalse(out["ok"])
        self.assertIn("offset", out["error"])

    async def test_query_uses_ranks_and_maps_offset(self) -> None:
        ranks = [
            {"seq": 0, "text": "Intro line.", "fts_and": 0.0, "fts_or": 0.0, "similarity": 0.2},
            {
                "seq": 1,
                "text": "Error E_CONN_042 means the pool is exhausted. Retry after raising the limit.",
                "fts_and": 0.5,
                "fts_or": 0.0,
                "similarity": 0.9,
            },
        ]
        with (
            patch.object(fetch, "db", _fake_db(ranks)),
            patch.object(fetch, "embed_one", return_value=[1.0]),
        ):
            out = await fetch.fetch_page("https://example.com/a", query="pool exhausted", max_chars=200)
        self.assertEqual(out["mode"], "query")
        self.assertIn("E_CONN_042", out["markdown"])
        self.assertGreater(out["start_offset"], 0)
        self.assertIsNotNone(out["match_similarity"])

    async def test_query_without_embed_degrades_to_fts(self) -> None:
        # embed raises -> qvec None -> ranking degrades to full-text only
        ranks = [
            {"seq": 0, "text": "Intro line.", "fts_and": 0.0, "fts_or": 0.0, "similarity": None},
            {"seq": 1, "text": "Troubleshooting section text.", "fts_and": 0.4, "fts_or": 0.0, "similarity": None},
        ]
        def boom(text: str) -> list[float]:
            raise RuntimeError("no model")

        with (
            patch.object(fetch, "db", _fake_db(ranks)),
            patch.object(fetch, "embed_one", side_effect=boom),
        ):
            out = await fetch.fetch_page("https://example.com/a", query="troubleshooting", max_chars=200)
        self.assertEqual(out["mode"], "query")
        self.assertIn("Troubleshooting", out["markdown"])
        self.assertIsNone(out["match_similarity"])

    async def test_query_offset_inside_chunk_starts_at_offset(self) -> None:
        # One chunk spans the whole body: a query offset in its middle must
        # start the window at the offset, not read back to the chunk start.
        ranks = [
            {"seq": 0, "text": BODY, "fts_and": 0.5, "fts_or": 0.0, "similarity": 0.9},
        ]
        with (
            patch.object(fetch, "db", _fake_db(ranks)),
            patch.object(fetch, "embed_one", return_value=[1.0]),
        ):
            mid = len(BODY) // 2
            out = await fetch.fetch_page("https://example.com/a", query="pool", offset=mid, max_chars=50)
        self.assertEqual(out["start_offset"], mid)
        self.assertTrue(BODY.startswith(out["markdown"], mid), out["markdown"])

    async def test_query_offset_past_end_is_empty(self) -> None:
        # offset at end-of-content: no chunk extends past it -> empty window.
        ranks = [
            {"seq": 0, "text": BODY, "fts_and": 0.5, "fts_or": 0.0, "similarity": 0.9},
        ]
        with (
            patch.object(fetch, "db", _fake_db(ranks)),
            patch.object(fetch, "embed_one", return_value=[1.0]),
        ):
            out = await fetch.fetch_page("https://example.com/a", query="pool", offset=len(BODY))
        self.assertEqual(out["markdown"], "")
        self.assertIsNone(out["next_offset"])
        self.assertIsNone(out["match_similarity"])

    def test_renderer_includes_navigation_context(self) -> None:
        out = {
            "ok": True,
            "url": "https://example.com/a",
            "title": "T",
            "markdown": "body",
            "chars": 4,
            "truncated": True,
            "from_index": True,
            "mode": "find",
            "found": True,
            "start_offset": 10,
            "end_offset": 14,
            "next_offset": 14,
            "total_chars": 100,
            "timing": {"total_ms": 5, "index_ms": 2},
        }
        md = render_fetch_page_markdown(out)
        self.assertIn("Mode: find", md)
        self.assertIn("Found: true", md)
        self.assertIn("Window: 10-14 of 100, next 14", md)
        self.assertIn("Time: 5 ms (index: 2 ms)", md)


if __name__ == "__main__":
    unittest.main()

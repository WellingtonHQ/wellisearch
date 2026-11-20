"""Passage navigation for fetch_page: locate a section of a page to read from.

Two optional, mutually-exclusive modes sit on top of the default head
truncation:

- find   — literal (case-insensitive) search of the stored body; read from the
           match. Good for exact text (error codes, keys, headings, phrases).
- query  — open-ended relevance over the page's stored chunks (Postgres
           full-text + embedding hybrid, per
           docs/passage-navigation-evaluation.md); read from the top-ranked
           chunk.

Both accept `offset` (start here / search from here) and `max_chars` (window
size) and return exact start/end/next offsets so a caller can read a page
block by block. With no mode the default head-truncation behavior is unchanged.

This module is pure (no DB, no model) so it can be unit-tested in isolation;
the DB + embedding work for `query` lives in fetch.py / db.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from .index import _with_title
from .truncation import boundary_cut_head

RRF_K = 60  # reciprocal-rank-fusion constant (matches the search fusion)

_NEG_INF = float("-inf")


@dataclass(slots=True)
class PassageWindow:
    """A boundary-safe window of the body, with exact navigation offsets."""

    start_offset: int
    end_offset: int
    next_offset: int | None
    window: str
    truncated: bool


# ---------------------------------------------------------------------------
# Ranking / fusion
# ---------------------------------------------------------------------------

def rank_indices(scores: list[float]) -> list[int]:
    """Rank highest score first; break ties by original (page) order."""
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def fuse_ranks(lexical: list[float], semantic: list[float]) -> list[float]:
    """RRF-fuse two per-chunk score lists into one per-chunk score list.

    A chunk with a zero lexical score earns no lexical credit, so an arbitrary
    page-order tie is never treated as lexical evidence (see the benchmark).
    """
    if len(lexical) != len(semantic):
        raise ValueError("lexical and semantic score lists differ in length")
    out = [0.0] * len(lexical)
    for rank, i in enumerate(rank_indices(semantic), 1):
        out[i] += 1 / (RRF_K + rank)
    for rank, i in enumerate(rank_indices(lexical), 1):
        if lexical[i] > 0:
            out[i] += 1 / (RRF_K + rank)
    return out


# ---------------------------------------------------------------------------
# Location + windowing
# ---------------------------------------------------------------------------

def locate_literal(body: str, needle: str, offset: int = 0) -> int | None:
    """Case-insensitive position of needle at/after offset. None when it does
    not occur. Returns the exact match position (not line-backed) so repeated
    matches on one line still advance correctly with `offset`."""
    needle_l = (needle or "").strip().lower()
    if not needle_l:
        return None
    pos = body.lower().find(needle_l, offset)
    if pos < 0:
        return None
    return pos


def make_window(body: str, start_offset: int, max_chars: int | None) -> PassageWindow:
    """Cut a boundary-safe window of at most max_chars from start_offset.

    `truncated` is True when content remains after the window; `next_offset`
    (None at end-of-page) tells the caller exactly where to continue.
    """
    if start_offset >= len(body):
        return PassageWindow(len(body), len(body), None, "", False)
    rest = body[start_offset:]
    if max_chars is not None and max_chars > 0:
        window = boundary_cut_head(rest, max_chars)
    else:
        window = rest
    end_offset = start_offset + len(window)
    return PassageWindow(
        start_offset=start_offset,
        end_offset=end_offset,
        next_offset=end_offset if end_offset < len(body) else None,
        window=window,
        truncated=end_offset < len(body),
    )


def chunk_body_offset(body: str, title: str | None, chunk_text: str) -> int | None:
    """Map a stored chunk to its start offset in the body-only markdown.

    Chunks are cut from _with_title(body, title) with leading/trailing
    whitespace stripped, so each chunk (except a merged trailing stub) is a
    contiguous substring of the title-prepended source. Locate it there and
    translate to a body offset; None when it cannot be mapped (the merged
    stub), so the caller falls back to a boundary-safe head read.
    """
    source = _with_title(body, title)
    body_start = len(source) - len(body)
    pos = source.find(chunk_text)
    if pos < 0:
        # Merged trailing stub: two slices joined with a blank line — not
        # contiguous. Fall back to its leading text, which is a substring.
        pos = source.find(chunk_text[:64])
        if pos < 0:
            return None
    return max(0, pos - body_start)

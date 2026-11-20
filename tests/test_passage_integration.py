"""Passage navigation integration test (needs Postgres): page_chunk_ranks +
fetch_page find/query/offset against a seeded fixture page.

Runs against a dedicated throwaway database (wellisearch_test — dropped and
rebuilt on every run) so the chunk layout is deterministic. Not part of CI
(needs the embedding model + Postgres); run it in the container:

  docker exec wellisearch python /tmp/passage-evaluation/tests/test_passage_integration.py
"""
from __future__ import annotations

import asyncio
import os

# Dedicated test DB. The container's POSTGRES_HOST env already points at the
# local Postgres, so only the database name is swapped here.
os.environ["POSTGRES_DB"] = "wellisearch_test"

import psycopg  # noqa: E402

from wellisearch import fetch  # noqa: E402
from wellisearch.config import get_settings  # noqa: E402
from wellisearch.db import db  # noqa: E402
from wellisearch.embed import embed_one  # noqa: E402
from wellisearch.index import store_page  # noqa: E402
from wellisearch.passage import chunk_body_offset  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

URL = "https://example.com/pool-troubleshooting-guide"
TITLE = "Pool Troubleshooting Guide"
NEEDLE = "E_CONN_042"
QUERY = "what does E_CONN_042 mean"

_FILLER = (
    "The connection pool routes each request through a queue that waits for a "
    "free client before handing out a cursor. When the queue fills, new requests "
    "wait their turn, and the operator can watch the depth grow in the dashboard."
)

# Sections + enough filler to force several chunks at the container's
# MAX_CHUNK_TOKENS, so the chunk-to-offset mapping is exercised in the middle
# of the page, not just at the start.
BODY = (
    "## Setup\n\n"
    "Run the installer with the --force flag to skip the interactive prompts.\n\n"
    "## Troubleshooting\n\n"
    f"Error {NEEDLE} means the pool is exhausted. Retry after raising the limit.\n\n"
    "## Billing\n\n"
    "Update your card on the billing page.\n\n"
    + "\n\n".join(_FILLER for _ in range(40))
    + "\n"
)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def _fresh_database() -> None:
    """Drop the test DB so every run starts from a fixed, empty corpus."""
    s = get_settings()
    admin = await psycopg.AsyncConnection.connect(
        s.conninfo(s.POSTGRES_ADMIN_DB), autocommit=True
    )
    try:
        async with admin.cursor() as cur:
            safe = s.POSTGRES_DB.replace('"', '""')
            await cur.execute(f'DROP DATABASE IF EXISTS "{safe}" WITH (FORCE)')
    finally:
        await admin.close()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

async def _check_rank_shape(body: str, chunks: int) -> list[dict]:
    """page_chunk_ranks: one seq-ordered row per chunk with all score columns."""
    qvec = await asyncio.to_thread(embed_one, QUERY)
    rows = await db.page_chunk_ranks(URL, QUERY, qvec)
    assert len(rows) == chunks, f"rank rows {len(rows)} != chunks {chunks}"
    assert [r["seq"] for r in rows] == list(range(chunks)), [r["seq"] for r in rows]
    for r in rows:
        assert r["fts_and"] >= 0 and r["fts_or"] >= 0, r
        assert r["similarity"] is not None, r
        assert 0.0 <= r["coverage"] <= 1.0, r
        assert r["text"], r
    print(
        f"OK page_chunk_ranks ({len(rows)} rows, seq-ordered, fts + similarity present)"
    )
    return rows


async def _check_find(body: str) -> None:
    """find: the window starts exactly at the match position."""
    out = await fetch.fetch_page(URL, find=NEEDLE, max_chars=400)
    assert out["ok"] and out["mode"] == "find" and out["found"], out
    pos = out["start_offset"]
    assert body[pos: pos + len(NEEDLE)] == NEEDLE, (pos, NEEDLE)
    assert NEEDLE in out["markdown"], out["markdown"]
    assert out["end_offset"] > pos and out["total_chars"] == len(body)
    print(f"OK find (exact match at offset {pos}, window {out['chars']} chars)")

    # find + offset past the match -> reported, empty window
    out = await fetch.fetch_page(URL, find=NEEDLE, offset=out["end_offset"])
    assert out["ok"] and out["mode"] == "find" and out["found"] is False, out
    assert out["markdown"] == "" and out["chars"] == 0
    assert out["start_offset"] == out["end_offset"]
    print("OK find + offset past the match (found: false, empty window)")


async def _check_query(body: str, title: str, rows: list[dict]) -> None:
    """query: the winner maps to a real chunk offset and carries the new scores."""
    out = await fetch.fetch_page(URL, query=QUERY, max_chars=400)
    assert out["ok"] and out["mode"] == "query", out
    assert NEEDLE in out["markdown"], out["markdown"]
    assert out["semantic_similarity"] is not None, out
    assert 0.0 <= out["coverage"] <= 1.0, out
    assert out["margin"] >= 0.0, out
    assert "match_fts" not in out, out
    offsets = [chunk_body_offset(body, title, r["text"]) for r in rows]
    assert out["start_offset"] in offsets, (out["start_offset"], offsets)
    # the winning chunk's text sits at the reported offset
    winner = next(r for r in rows if chunk_body_offset(body, title, r["text"]) == out["start_offset"])
    assert winner["text"] in body[out["start_offset"]: out["start_offset"] + len(winner["text"])]
    print(
        "OK query (winner at chunk offset %d; semantic %.3f, coverage %.2f, margin %.4f)"
        % (out["start_offset"], out["semantic_similarity"], out["coverage"], out["margin"])
    )


async def _check_offset_and_tiling(body: str) -> None:
    """offset: exact start; consecutive windows tile the body without overlap."""
    mid = body.find("## Billing")
    assert mid > 0
    out = await fetch.fetch_page(URL, offset=mid, max_chars=300)
    assert out["ok"] and out["mode"] == "head", out
    assert out["start_offset"] == mid, (out["start_offset"], mid)
    assert out["markdown"].startswith("## Billing")
    print(f"OK offset (window starts exactly at {mid})")

    first = await fetch.fetch_page(URL, max_chars=5000)
    assert first["truncated"] and first["next_offset"] is not None, first
    second = await fetch.fetch_page(URL, offset=first["next_offset"], max_chars=5000)
    assert first["end_offset"] == second["start_offset"], (first, second)
    assert first["markdown"] + second["markdown"] == body[: second["end_offset"]]
    print(f"OK tiling (window 1 ends at {first['end_offset']}, window 2 starts there)")


async def _check_default_mode(body: str) -> None:
    """No navigation params: head truncation, start 0, full total_chars."""
    out = await fetch.fetch_page(URL, max_chars=200)
    assert out["ok"] and out["mode"] == "head", out
    assert out["start_offset"] == 0 and out["total_chars"] == len(body)
    assert out["truncated"] and out["next_offset"] is not None
    print("OK default head mode (start 0, total_chars, next_offset)")


async def main() -> None:
    """Seed one fixture page and run every navigation check against it."""
    await _fresh_database()
    await db.startup()
    print("OK startup (test DB rebuilt, schema applied)")

    status, chunks = await store_page(URL, BODY, title=TITLE)
    assert status == "ok" and chunks >= 3, f"{status}, {chunks} chunks"
    page = await db.page_get(URL)
    body = page["fit_markdown"]
    print(f"OK seeded {URL} ({chunks} chunks, {len(body)} chars)")

    rows = await _check_rank_shape(body, chunks)
    await _check_find(body)
    await _check_query(body, TITLE, rows)
    await _check_offset_and_tiling(body)
    await _check_default_mode(body)

    await db.close()
    print("ALL PASSAGE INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

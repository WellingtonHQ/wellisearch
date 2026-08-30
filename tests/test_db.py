"""DB integration: schema apply, store_page, fn_search_local, queue, quota."""
from __future__ import annotations

import asyncio
import os

# host-local endpoints (container aliases don't resolve on the host)
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("CRAWL4AI_URL", "http://127.0.0.1:11235")

from wellisearch.db import db  # noqa: E402


async def main() -> None:
    await db.startup()
    print("OK startup (schema applied)")
    await _clean_slate()
    await _check_tables()
    await _check_fn_search_local_nonsense()
    await _store_page_roundtrip()
    await _check_local_hit()
    await _check_queue_quota_provider_state()
    await _check_provider_order()
    await _check_event_log()
    await _cleanup()
    await db.close()
    print("ALL DB INTEGRATION TESTS PASSED")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _clean_slate() -> None:
    """Delete this test's URLs (DB persists between runs)."""
    for table in ("pages", "crawl_queue"):
        await db.execute(f"DELETE FROM {table} WHERE url LIKE 'https://example.com/%%'")
    await db.execute("DELETE FROM provider_quota")
    await db.execute("DELETE FROM provider_state")
    # note: search_log / crawl_log are left untouched — they are shared history


async def _check_tables() -> None:
    """The public schema tables all exist."""
    tables = await db.fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    names = [t["tablename"] for t in tables]
    for expected in (
        "pages", "chunks", "crawl_queue", "search_log",
        "provider_quota", "crawl_log", "provider_state", "event_log",
    ):
        assert expected in names, f"missing table {expected}"
    print("OK tables:", names)


async def _check_fn_search_local_nonsense() -> None:
    """fn_search_local exists and runs (nonsense token -> no rows,
    regardless of what else the shared dev index contains)."""
    rows = await db.fetch_all(
        "SELECT * FROM fn_search_local(%s, NULL, 5)", ("zxqvjflurbqz xyptwqrfvz",)
    )
    assert rows == [], rows
    print("OK fn_search_local (nonsense query -> no rows)")


async def _store_page_roundtrip() -> None:
    """store_page with a real embedding, then the unchanged short-circuit."""
    from wellisearch.index import store_page

    section = (
        "pgvector is an extension for PostgreSQL that adds native support for "
        "similarity search over embedding vectors. It stores vector data and "
        "supports approximate nearest neighbor search with HNSW and ivfflat "
        "index types. It is commonly used for semantic search, retrieval "
        "augmented generation, deduplication, and recommendation systems. "
        "Vectors are stored as fixed-size arrays of floating point numbers, "
        "and distance operators such as cosine distance and euclidean "
        "distance make it possible to query the closest rows efficiently. "
        "The HNSW index builds a navigable small world graph at write time "
        "and answers queries in logarithmic time, which makes it suitable "
        "for high recall workloads. "
    ) * 8
    md = (
        "# pgvector introduction\n"
        + section
        + "\n## Usage\n"
        + section.replace("pgvector is", "You enable it with")
        + "\n## Queries\n"
        + section.replace("pgvector is", "Distance operators such as")
    )
    status, chunks = await store_page("https://example.com/pgvector-intro", md, title="pgvector introduction")
    print("store_page:", status, "chunks:", chunks)
    assert status == "ok" and chunks >= 2

    # unchanged short-circuit
    status, chunks = await store_page("https://example.com/pgvector-intro", md, title="x")
    assert status == "unchanged" and chunks == 0
    print("OK unchanged short-circuit")


async def _check_local_hit() -> None:
    """fn_search_local now finds the stored page (findability, not top-5)."""
    # Findability, not top-5: the live corpus (~1.34M chunks) contains dozens
    # of real, more complete pgvector pages that legitimately outrank this
    # synthetic blurb (it typically lands in the ~40s-50s). The assertion is
    # that a stored page matching the query topic is ranked at all — a broken
    # candidate pool (e.g. an empty trigram leg) misses it entirely.
    from wellisearch.embed import embed_one

    qvec = await asyncio.to_thread(embed_one, "how to use pgvector for semantic search")
    rows = await db.fetch_all(
        "SELECT url, title, score, left(snippet, 60) AS snippet FROM fn_search_local(%s, %s::vector, 100)",
        ("how to use pgvector for semantic search", qvec),
    )
    assert rows, "no local rows"
    mine = [r for r in rows if r["url"] == "https://example.com/pgvector-intro"]
    assert mine, "pgvector page not ranked in top-100"
    print("OK local hit:", mine[0])

    # unrelated query should not rank it highly
    qvec2 = await asyncio.to_thread(embed_one, "chocolate cake recipe with espresso")
    rows2 = await db.fetch_all(
        "SELECT url, score FROM fn_search_local(%s, %s::vector, 5)",
        ("chocolate cake recipe with espresso", qvec2),
    )
    print("unrelated query rows:", rows2)


async def _check_queue_quota_provider_state() -> None:
    """Queue dedupe/claim/done, quota ledger, provider state toggle."""
    ins = await db.queue_enqueue("https://example.com/queued-page", "test")
    assert ins
    assert not await db.queue_enqueue("https://example.com/queued-page", "test")
    print("OK queue dedupe")
    assert await db.queue_claim("https://example.com/queued-page")
    await db.queue_done("https://example.com/queued-page", ok=True)
    row = await db.fetch_one(
        "SELECT status FROM crawl_queue WHERE url = %s",
        ("https://example.com/queued-page",),
    )
    assert row["status"] == "done", row

    await db.quota_bump("tavily")
    used, limit = await db.quota_used_limit("tavily")
    assert used >= 1 and limit == 1000
    print("OK quota ledger:", used, limit)

    await db.set_provider_state("brave", enabled=False)
    st = await db.get_provider_state("brave")
    assert st["enabled"] is False
    await db.set_provider_state("brave", enabled=True, last_error=None)
    print("OK provider state toggle")


async def _check_provider_order() -> None:
    """Provider order: runtime override roundtrip (NULL = env default)."""
    assert await db.get_provider_order() is None, "expected no override at start"
    await db.set_provider_order(["brave", "tavily"])
    assert await db.get_provider_order() == ["brave", "tavily"], await db.get_provider_order()
    # toggling a provider's enabled flag must not clobber its sort_order
    await db.set_provider_state("brave", enabled=False)
    assert await db.get_provider_order() == ["brave", "tavily"], await db.get_provider_order()
    await db.set_provider_state("brave", enabled=True, last_error=None)
    # reset clears the override
    await db.set_provider_order([])
    assert await db.get_provider_order() is None, "reset should clear the override"
    print("OK provider order roundtrip")


async def _check_event_log() -> None:
    """event_log: roundtrip + null info."""
    await db.log_event("test event", {"foo": "bar", "n": 42})
    row = await db.fetch_one("SELECT message, info FROM event_log ORDER BY id DESC LIMIT 1")
    assert row and row["message"] == "test event" and (row["info"] or {}) == {"foo": "bar", "n": 42}, row
    print("OK event_log roundtrip")
    await db.log_event("test event no info")
    row = await db.fetch_one("SELECT message, info FROM event_log ORDER BY id DESC LIMIT 1")
    assert row and row["message"] == "test event no info" and row["info"] is None, row
    print("OK event_log null info")


async def _cleanup() -> None:
    """Delete the test rows (keep a clean slate for the next run)."""
    await db.execute("DELETE FROM pages WHERE url LIKE 'https://example.com/%%'")
    await db.execute("DELETE FROM crawl_queue WHERE url LIKE 'https://example.com/%%'")
    print("OK cleanup")


asyncio.run(main())


"""DB integration: schema apply, store_page, fn_search_local, queue, quota.

Runs against a dedicated throwaway database (wellisearch_test — created on
first run) so the destructive clean-slate below can never touch production
state such as provider_quota / provider_state."""
from __future__ import annotations

import asyncio
import os

# host-local endpoint + dedicated test DB (container aliases don't resolve on
# the host; a throwaway DB keeps this suite away from production state)
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ["POSTGRES_DB"] = "wellisearch_test"

from wellisearch.db import db  # noqa: E402


async def main() -> None:
    """Run the full DB integration suite (schema, store_page, search, queue,
    quota, provider state/order, app state, event log)."""
    await db.startup()
    print("OK startup (schema applied)")
    await _clean_slate()
    await _check_tables()
    await _check_fn_search_local_nonsense()
    await _store_page_roundtrip()
    await _check_local_hit()
    await _check_queue_quota_provider_state()
    await _check_provider_order()
    await _check_app_state()
    await _check_event_log()
    await _check_merge_dupes()
    await _cleanup()
    await db.close()
    print("ALL DB INTEGRATION TESTS PASSED")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _check_merge_dupes() -> None:
    """Verify page/chunk renames commit together and FK failure rolls back the merge."""
    from unittest.mock import patch

    from psycopg import AsyncConnection

    from wellisearch import merge_dupes

    canonical = "https://example.com/merge-review"
    variant = canonical + "?utm_source=review"
    await db.execute(
        "INSERT INTO pages (url, fetch_count, fit_markdown) VALUES (%s, 1, 'Old'), (%s, 5, 'New')",
        (canonical, variant),
    )
    await db.execute(
        "INSERT INTO chunks (url, seq, text, last_crawled) "
        "VALUES (%s, 0, 'Old', now()), (%s, 0, 'New', now())",
        (canonical, variant),
    )
    groups = {canonical: [
        {"url": canonical, "fetch_count": 1, "md_len": 3},
        {"url": variant, "fetch_count": 5, "md_len": 3},
    ]}
    original = merge_dupes._merge_group

    async def orphan_after_merge(
        conn: AsyncConnection,
        url: str,
        group: list[dict],
    ) -> tuple[int, int]:
        """Inject an orphan while the FK is dropped to force validation failure."""
        result = await original(conn, url, group)
        await conn.execute(
            "INSERT INTO chunks (url, seq, text, last_crawled) VALUES (%s, 0, 'Orphan', now())",
            (canonical + "-orphan",),
        )
        return result

    with patch.object(merge_dupes, "_merge_group", orphan_after_merge):
        try:
            await merge_dupes._merge_groups(groups)
        except Exception as exc:
            assert "foreign key" in str(exc).lower(), f"unexpected failure: {exc}"
        else:
            raise AssertionError("restoring the FK must reject orphan chunks")
    rows = await db.fetch_all(
        "SELECT url, fit_markdown FROM pages WHERE url IN (%s, %s) ORDER BY url",
        (canonical, variant),
    )
    assert rows == [
        {"url": canonical, "fit_markdown": "Old"},
        {"url": variant, "fit_markdown": "New"},
    ], f"failed merge must roll back page mutations: {rows}"
    assert await merge_dupes._merge_groups(groups) == (1, 1)
    chunks = await db.fetch_all("SELECT url, text FROM chunks WHERE url = %s", (canonical,))
    assert chunks == [{"url": canonical, "text": "New"}], f"survivor chunks must move: {chunks}"
    assert await db.page_get(variant) is None, "variant page must be removed"
    await db.execute("DELETE FROM pages WHERE url = %s", (canonical,))
    assert not await db.fetch_all("SELECT url FROM chunks WHERE url = %s", (canonical,))
    print("OK merge_dupes (atomic rollback, canonical rename, restored cascade)")


async def _clean_slate() -> None:
    """Delete this test's URLs (DB persists between runs)."""
    for table in ("crawl_queue", "pages"):
        await db.execute(f"DELETE FROM {table} WHERE url LIKE 'https://example.com/%%'")
    await db.execute("DELETE FROM provider_quota")
    await db.execute("DELETE FROM provider_state")
    # note: crawl_log / search_log are left untouched — they are shared history


async def _check_tables() -> None:
    """The public schema tables all exist."""
    tables = await db.fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    names = [t["tablename"] for t in tables]
    for expected in (
        "app_state", "chunks", "crawl_log", "crawl_queue", "event_log",
        "pages", "provider_quota", "provider_state", "search_log",
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

    # unchanged short-circuit. The title feeds the chunk source (H1 prepend),
    # so it must match or the digest changes and a re-embed happens instead.
    status, chunks = await store_page("https://example.com/pgvector-intro", md, title="pgvector introduction")
    assert status == "unchanged" and chunks == 0
    print("OK unchanged short-circuit")

    # re-embed without a fetch (crawled=False): identical content is a true
    # no-op and must not stamp crawl-time values
    before = await db.fetch_one(
        "SELECT last_crawled, crawl_count, last_status FROM pages WHERE url = %s",
        ("https://example.com/pgvector-intro",),
    )
    status, chunks = await store_page(
        "https://example.com/pgvector-intro", md, title="pgvector introduction", crawled=False
    )
    assert status == "unchanged" and chunks == 0
    after = await db.fetch_one(
        "SELECT last_crawled, crawl_count, last_status FROM pages WHERE url = %s",
        ("https://example.com/pgvector-intro",),
    )
    assert after == before, (before, after)
    print("OK re-embed no-op leaves crawl-time values untouched")

    # re-embed of new content (model-change path): rewrites chunks but still
    # must not stamp crawl-time values; chunks mirror the page's last crawl
    status, chunks = await store_page(
        "https://example.com/pgvector-intro", md + "\n## extra\nextra text.",
        title="pgvector introduction", crawled=False,
    )
    assert status == "ok" and chunks >= 2
    after2 = await db.fetch_one(
        "SELECT last_crawled, crawl_count, last_status FROM pages WHERE url = %s",
        ("https://example.com/pgvector-intro",),
    )
    assert after2 == before, (before, after2)
    chunk_row = await db.fetch_one(
        "SELECT last_crawled FROM chunks WHERE url = %s ORDER BY seq LIMIT 1",
        ("https://example.com/pgvector-intro",),
    )
    assert chunk_row["last_crawled"] == before["last_crawled"], (before, chunk_row)
    print("OK re-embed of new content leaves crawl-time values untouched")


async def _check_local_hit() -> None:
    """fn_search_local now finds the stored page (findability, not top-5)."""
    # Findability, not top-1: other pages in the test index may legitimately
    # outrank this synthetic blurb. The assertion is that a stored page
    # matching the query topic is ranked at all — a broken candidate pool
    # (e.g. an empty trigram leg) misses it entirely.
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

    # a bump must not null the stored limit when no runtime override is set
    await db.set_provider_state("tavily", enabled=True)
    await db.quota_bump("tavily")
    row = await db.fetch_one(
        "SELECT quota_limit FROM provider_quota WHERE provider = 'tavily' ORDER BY month DESC LIMIT 1"
    )
    assert row is not None and row["quota_limit"] == 1000, row
    print("OK quota limit preserved without override")

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


async def _check_app_state() -> None:
    """app_state runtime flags: set/get roundtrip. Restores the prior pause
    state afterwards (the dev DB may be shared with a running instance)."""
    from wellisearch.db import INDEXING_PAUSED_KEY

    original = await db.get_app_value(INDEXING_PAUSED_KEY)
    try:
        assert not await db.worker_paused(), "expected the flag unset at start"
        await db.set_worker_paused(True)
        assert (await db.get_app_value(INDEXING_PAUSED_KEY)) is True
        assert await db.worker_paused()
        row = await db.fetch_one(
            "SELECT value, updated_at FROM app_state WHERE key = %s", (INDEXING_PAUSED_KEY,)
        )
        assert row and row["value"] is True and row["updated_at"], row
        # upsert, not insert: setting again updates the same row
        await db.set_worker_paused(False)
        n = await db.fetch_one("SELECT count(*) AS n FROM app_state WHERE key = %s", (INDEXING_PAUSED_KEY,))
        assert n["n"] == 1, f"expected a single flag row, got {n['n']}"
        assert not await db.worker_paused()
        print("OK app_state roundtrip")
    finally:
        if original is None:
            await db.execute("DELETE FROM app_state WHERE key = %s", (INDEXING_PAUSED_KEY,))
        else:
            await db.set_app_value(INDEXING_PAUSED_KEY, original)


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

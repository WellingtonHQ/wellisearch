"""Postgres access: pool, startup retry, self-create app DB, DDL apply, helpers.

Startup sequence (§11, cross-project — no depends_on):
  1. retry-connect to the admin DB (default `postgres`) — ~10 × 3 s
  2. idempotently CREATE DATABASE if the app DB does not exist
  3. open the main pool against the app DB
  4. apply schema.sql (idempotent DDL + fn_search_local)
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import contextlib
import datetime as dt
import logging
import pathlib
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Settings, get_settings
from .url_filter import garbage_reason

log = logging.getLogger("wellisearch.db")

# psycopg's async mode needs a selector event loop; Windows defaults to the
# proactor loop. Setting this before any loop runs is safe (no-op on Linux).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# schema.sql ships inside the package (works in dev layout and installed wheel)
SCHEMA_FILE = pathlib.Path(__file__).resolve().parent / "schema.sql"

STARTUP_RETRIES = 10
STARTUP_RETRY_S = 3.0


class Database:
    """Postgres access: pool, startup (self-create app DB + DDL), and helpers
    for pages, quotas, provider state, logs, and the crawl queue."""

    def __init__(self) -> None:
        """Starts with no pool; call startup() before use."""
        self._pool: AsyncConnectionPool | None = None

    # ---------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------

    @property
    def pool(self) -> AsyncConnectionPool:
        """The live connection pool; raises if startup() has not been called."""
        if self._pool is None:
            raise RuntimeError("database not started (call startup() first)")
        return self._pool

    async def startup(self) -> None:
        """Bring the database up: wait for Postgres, create the app DB if
        missing, open the pool, and apply schema.sql."""
        s = get_settings()

        # 1+2. ensure the app DB exists (admin DB is guaranteed to exist)
        last_err: Exception | None = None
        for attempt in range(1, STARTUP_RETRIES + 1):
            try:
                await self._ensure_app_db(s)
                last_err = None
                break
            except (psycopg.OperationalError, psycopg.Error) as e:
                last_err = e
                log.warning(
                    "waiting for postgres… (%d/%d) %s",
                    attempt, STARTUP_RETRIES, e,
                )
                await asyncio.sleep(STARTUP_RETRY_S)
        if last_err is not None:
            raise RuntimeError(f"could not reach Postgres after {STARTUP_RETRIES} attempts") from last_err

        # 3. open the main pool
        self._pool = AsyncConnectionPool(
            conninfo=s.conninfo(),
            min_size=s.DB_POOL_MIN_SIZE,
            max_size=s.DB_POOL_MAX_SIZE,
            open=False,
            kwargs={"row_factory": dict_row},
            configure=_register_vector,
        )
        await self._pool.open(wait=True)

        # 4. apply DDL (idempotent)
        schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
        async with self.pool.connection() as conn:
            await conn.execute(schema_sql)
        log.info("schema applied (extensions, tables, fn_search_local)")

    async def _ensure_app_db(self, s: Settings) -> None:
        """Idempotently create the app DB (via the admin DB) and ensure the
        extensions the pool requires (vector, pg_trgm) exist in it.

        The pool's configure step registers the ``vector`` type, so a freshly
        created DB must already have the extension — otherwise the pool fails
        to open before ``schema.sql`` gets a chance to create it.
        """
        admin = await psycopg.AsyncConnection.connect(
            s.conninfo(s.POSTGRES_ADMIN_DB), autocommit=True
        )
        try:
            async with admin.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (s.POSTGRES_DB,),
                )
                exists = await cur.fetchone()
                if not exists:
                    # identifier must be safe: it comes from our own config
                    safe = s.POSTGRES_DB.replace('"', '""')
                    await cur.execute(f'CREATE DATABASE "{safe}"')
                    log.info("created database %s", s.POSTGRES_DB)
        finally:
            await admin.close()

        # Ensure the extensions the pool needs are present in the app DB, so a
        # fresh database boots without the "vector type not found" pool error.
        app = await psycopg.AsyncConnection.connect(
            s.conninfo(s.POSTGRES_DB), autocommit=True
        )
        try:
            async with app.cursor() as cur:
                await cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        finally:
            await app.close()
        log.info("ensured extensions (vector, pg_trgm) in %s", s.POSTGRES_DB)

    async def close(self) -> None:
        """Close the pool (if open) and clear the reference."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ---------------------------------------------------------------------------
    # Raw API
    # ---------------------------------------------------------------------------

    async def execute(
        self,
        sql: str,
        params: tuple | list | None = None,
    ) -> int:
        """Run one statement; returns the affected row count (0 when unknown)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params or ())
            return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0

    async def fetch_all(
        self,
        sql: str,
        params: tuple | list | None = None,
        timeout_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run a SELECT. `timeout_ms` sets a per-statement backstop (SET LOCAL,
        scoped to one explicit transaction — never leaks into the pool) so a
        slow query can't hold a pooled connection for minutes. Raises
        psycopg.errors.QueryCanceled on expiry."""
        async with self.pool.connection() as conn:
            if timeout_ms is not None:
                # SET does not accept parameter placeholders — inline the
                # (int-coerced) value instead.
                async with conn.transaction():
                    await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    cur = await conn.execute(sql, params or ())
                    return list(await cur.fetchall())
            cur = await conn.execute(sql, params or ())
            return list(await cur.fetchall())

    async def fetch_one(
        self,
        sql: str,
        params: tuple | list | None = None,
    ) -> dict[str, Any] | None:
        """Run a SELECT and return the first row (or None)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params or ())
            row = await cur.fetchone()
            return row

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """Yield a pooled connection inside an explicit transaction (commit on
        success, roll back on exception)."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn

    # ---------------------------------------------------------------------------
    # Pages / Fetch
    # ---------------------------------------------------------------------------

    async def page_get(self, url: str) -> dict[str, Any] | None:
        """The page row for a URL, or None."""
        return await self.fetch_one("SELECT * FROM pages WHERE url = %s", (url,))

    async def bump_fetch_count(
        self,
        url: str,
        n: int = 1,
    ) -> None:
        """Increment a page's fetch_count by n."""
        await self.execute(
            "UPDATE pages SET fetch_count = fetch_count + %s WHERE url = %s",
            (n, url),
        )

    async def mark_search_hits(self, urls: list[str]) -> None:
        """Bump search_hit_count for every served result in one statement
        (one round-trip instead of one per row — k sequential UPDATEs on the
        hot path)."""
        if not urls:
            return
        await self.execute(
            "UPDATE pages SET search_hit_count = search_hit_count + 1 WHERE url = ANY(%s)",
            (urls,),
        )

    # ---------------------------------------------------------------------------
    # Quota
    # ---------------------------------------------------------------------------

    async def quota_used_limit(self, provider: str) -> tuple[int, int | None]:
        """Current-month (used, limit); limit = runtime override or env default."""
        s = get_settings()
        row = await self.fetch_one(
            "SELECT used, quota_limit FROM provider_quota WHERE provider = %s AND month = %s",
            (provider, _month()),
        )
        used = row["used"] if row else 0
        state = await self.fetch_one(
            "SELECT limit_override FROM provider_state WHERE provider = %s", (provider,)
        )
        limit = (
            state["limit_override"]
            if state and state["limit_override"] is not None
            else s.env_quota_limit(provider)
        )
        return used, limit

    async def quota_bump(self, provider: str) -> None:
        """Record one provider call for the current month, preserving any
        runtime limit override."""
        s = get_settings()
        limit = s.env_quota_limit(provider)
        await self.execute(
            """
            INSERT INTO provider_quota (provider, month, used, quota_limit)
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (provider, month) DO UPDATE
              SET used = provider_quota.used + 1,
                  quota_limit = COALESCE(provider_quota.quota_limit, EXCLUDED.quota_limit)
            """,
            (provider, _month(), limit),
        )
        # refresh the runtime override if one was set
        await self.execute(
            """
            UPDATE provider_quota q
               SET quota_limit = st.limit_override
              FROM provider_state st
             WHERE st.provider = q.provider AND st.provider = %s AND q.month = %s
            """,
            (provider, _month()),
        )

    # ---------------------------------------------------------------------------
    # Provider State
    # ---------------------------------------------------------------------------

    async def get_provider_state(self, provider: str) -> dict[str, Any] | None:
        """The provider_state row for a provider, or None."""
        return await self.fetch_one("SELECT * FROM provider_state WHERE provider = %s", (provider,))

    async def set_provider_state(
        self,
        provider: str,
        *,
        enabled: bool | None = None,
        limit: Any = ...,  # ellipsis = "don't touch"; None/Int = set
        last_served: dt.datetime | None = None,
        last_error: Any = ...,  # ellipsis = "don't touch"
    ) -> None:
        """Upsert the given provider_state fields; args left at the ellipsis
        default are left untouched."""
        cols = ["provider"]
        vals: list[Any] = [provider]
        upsets: list[str] = []
        if enabled is not None:
            cols.append("enabled")
            vals.append(enabled)
            upsets.append("enabled = EXCLUDED.enabled")
        if limit is not ...:
            cols.append("limit_override")
            vals.append(limit)
            upsets.append("limit_override = EXCLUDED.limit_override")
        if last_served is not None:
            cols.append("last_served")
            vals.append(last_served)
            upsets.append("last_served = EXCLUDED.last_served")
        if last_error is not ...:
            cols.append("last_error")
            vals.append(last_error)
            upsets.append("last_error = EXCLUDED.last_error")
        if not upsets:
            return
        upsets.append("updated_at = now()")
        sql = (
            f"INSERT INTO provider_state ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(vals))}) "
            f"ON CONFLICT (provider) DO UPDATE SET {', '.join(upsets)}"
        )
        await self.execute(sql, tuple(vals))

    # ---------------------------------------------------------------------------
    # Provider Order
    # ---------------------------------------------------------------------------

    async def get_provider_order(self) -> list[str] | None:
        """The runtime failover order (dashboard override) or None = env default."""
        rows = await self.fetch_all(
            "SELECT provider, sort_order FROM provider_state WHERE sort_order IS NOT NULL"
        )
        if not rows:
            return None
        return [
            r["provider"]
            for r in sorted(rows, key=lambda r: (r["sort_order"] or 0, r["provider"]))
        ]

    async def set_provider_order(self, order: list[str]) -> None:
        """Persist a full failover order (positions 0..n-1). An empty list
        clears the override (falls back to the env default order)."""
        await self.execute(
            "UPDATE provider_state SET sort_order = NULL WHERE sort_order IS NOT NULL"
        )
        for i, name in enumerate(order):
            await self.execute(
                """
                INSERT INTO provider_state (provider, sort_order) VALUES (%s, %s)
                ON CONFLICT (provider) DO UPDATE SET sort_order = EXCLUDED.sort_order
                """,
                (name, i),
            )

    # ---------------------------------------------------------------------------
    # Logs
    # ---------------------------------------------------------------------------

    async def log_search(
        self,
        query: str,
        source: str,
        local_hits: int | None,
        results: list[dict[str, Any]],
    ) -> None:
        """Record one search (query, source, local hits, results)."""
        import json

        await self.execute(
            "INSERT INTO search_log (query, source, local_hits, results) VALUES (%s, %s, %s, %s)",
            (query, source, local_hits, json.dumps(results, default=str)),
        )

    async def log_crawl(
        self,
        url: str,
        trigger: str,
        status: str,
        ms: int,
        chunks_written: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Record one crawl (url, trigger, status, timing, chunks, detail)."""
        await self.execute(
            "INSERT INTO crawl_log (url, trigger, status, ms, chunks_written, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (url, trigger, status, ms, chunks_written, detail),
        )

    async def log_event(
        self,
        message: str,
        info: dict[str, Any] | None = None,
    ) -> None:
        """One operational event (worker, provider gateway, admin, lifecycle)."""
        import json

        await self.execute(
            "INSERT INTO event_log (message, info) VALUES (%s, %s)",
            (message, json.dumps(info, default=str) if info is not None else None),
        )

    async def prune_logs(self, days: int) -> dict[str, int]:
        """Retention sweep for the log tables. Returns per-table deleted counts."""
        out: dict[str, int] = {}
        for table in ("crawl_log", "event_log", "search_log"):
            out[table] = await self.execute(
                f"DELETE FROM {table} WHERE ts < now() - make_interval(days => %s)",
                (days,),
            )
        return out

    # ---------------------------------------------------------------------------
    # Queue
    # ---------------------------------------------------------------------------

    async def queue_enqueue(
        self,
        url: str,
        source: str,
        lane: str = "fast",
    ) -> bool:
        """Enqueue unless already pending/in-flight. Returns True if inserted.

        ``lane`` defaults to 'fast'; pass 'cf' to enqueue straight onto the
        challenge lane (e.g. when an on-demand fetch probe hits a bot-wall).
        Known-garbage URLs (binary media, archives, executables, HLS segments)
        are rejected here — the single choke point every enqueue path goes
        through — so they never enter the queue.
        """
        reason = garbage_reason(url)
        if reason is not None:
            log.info("rejected garbage URL %s: %s", url, reason)
            return False
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO crawl_queue (url, source, lane) VALUES (%s, %s, %s)
                ON CONFLICT (url) WHERE status IN ('pending', 'in_flight') DO NOTHING
                """,
                (url, source, lane),
            )
            return (cur.rowcount or 0) > 0

    async def queue_reset_in_flight(self) -> int:
        """Boot-time: rows stuck in 'in_flight' (crash mid-drain) go back to pending."""
        return await self.execute(
            "UPDATE crawl_queue SET status = 'pending', last_error = 'reset on boot' "
            "WHERE status = 'in_flight'"
        )

    async def queue_claim(self, url: str) -> bool:
        """Claim a pending queue row for processing (pending → in_flight,
        attempts+1); False if it was not pending."""
        cur_ok = await self.execute(
            "UPDATE crawl_queue SET status = 'in_flight', attempts = attempts + 1 "
            "WHERE url = %s AND status = 'pending' RETURNING id",
            (url,),
        )
        return cur_ok > 0

    async def queue_done(
        self,
        url: str,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Finish a claimed row: done on success, else back to pending (attempts
        left) or failed (attempts exhausted)."""
        if ok:
            await self.execute(
                "UPDATE crawl_queue SET status = 'done' WHERE url = %s AND status = 'in_flight'",
                (url,),
            )
        else:
            row = await self.fetch_one(
                "SELECT attempts FROM crawl_queue WHERE url = %s AND status = 'in_flight'",
                (url,),
            )
            s = get_settings()
            if row and row["attempts"] < s.QUEUE_MAX_ATTEMPTS:
                await self.execute(
                    "UPDATE crawl_queue SET status = 'pending', last_error = %s "
                    "WHERE url = %s AND status = 'in_flight'",
                    (error, url),
                )
            else:
                await self.execute(
                    "UPDATE crawl_queue SET status = 'failed', last_error = %s "
                    "WHERE url = %s AND status = 'in_flight'",
                    (error, url),
                )

    async def queue_route_to_cf(self, url: str) -> bool:
        """Move a fast-lane row (pending or in-flight) onto the CF challenge lane.

        Sets lane='cf', status='pending' (so the CF drain can claim it), and
        resets attempts so the CF lane gets a fresh retry budget. Returns True
        if a row was routed.
        """
        return (
            await self.execute(
                "UPDATE crawl_queue SET lane = 'cf', status = 'pending', attempts = 0, "
                "last_error = 'routed to cf lane' "
                "WHERE url = %s AND status IN ('pending', 'in_flight')",
                (url,),
            )
            > 0
        )


# module-level singleton
db = Database()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _month() -> str:
    """Current UTC month as ``YYYY-MM`` (the provider_quota key)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


async def _register_vector(conn: psycopg.AsyncConnection) -> None:
    """Register the pgvector type adapter on every pooled connection."""
    from pgvector.psycopg import register_vector_async

    await register_vector_async(conn)

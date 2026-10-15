"""Bound dashboard log memory by projecting, filtering, and limiting inside Postgres."""
from __future__ import annotations

from .db import db

WINDOW_SQL = r"""
WITH logs AS (
    SELECT ts, 0 AS source_order, id, 'crawl'::text AS kind,
           'crawl ' || status || ' — ' ||
           rtrim(regexp_replace(url, '^[^:]+://([^/?#]+)/*([^/?#]*).*$', '\1/\2'), '/') AS message,
           jsonb_build_object(
               'url', url, 'trigger', trigger, 'ms', ms,
               'chunks', chunks_written, 'detail', detail
           ) AS info
    FROM crawl_log WHERE ts >= now() - make_interval(secs => %s)
    UNION ALL
    SELECT ts, 1, id, 'search',
           'search ''' || left(coalesce(query, ''), 100) || ''' → ' ||
           CASE WHEN source = 'local'
                THEN 'local (' || coalesce(local_hits, 0)::text || ' hits)'
                ELSE source || ' (' || jsonb_array_length(coalesce(results, '[]'::jsonb))::text || ' results)'
           END,
           jsonb_build_object(
               'query', query, 'source', source, 'local_hits', local_hits,
               'results', jsonb_array_length(coalesce(results, '[]'::jsonb))
           )
    FROM search_log WHERE ts >= now() - make_interval(secs => %s)
    UNION ALL
    SELECT ts, 2, id, 'event', message, coalesce(info, '{}'::jsonb)
    FROM event_log WHERE ts >= now() - make_interval(secs => %s)
), matched AS (
    SELECT * FROM logs
    WHERE %s = '' OR strpos(lower(message), %s) > 0 OR strpos(lower(info::text), %s) > 0
)
SELECT (SELECT count(*) FROM matched) AS total,
       coalesce((
           SELECT jsonb_agg(entry ORDER BY entry.ts DESC)
           FROM (
               SELECT ts, kind, message, info FROM matched
               ORDER BY ts DESC, source_order, id DESC LIMIT %s
           ) AS entry
       ), '[]'::jsonb) AS logs
"""


async def window_logs(
    secs: int,
    limit: int,
    query: str,
) -> dict:
    """Return at most limit compact log entries and the full matching count.

    The old implementation loaded full search results and every row in the
    selected day into Python, even when the caller requested only 200 entries.
    """
    query = (query or "").strip().lower()
    row = await db.fetch_one(WINDOW_SQL, (secs, secs, secs, query, query, query, limit))
    return {"logs": (row or {}).get("logs", []), "total": (row or {}).get("total", 0), "secs": secs}

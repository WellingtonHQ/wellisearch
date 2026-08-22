"""index: store_page(url, markdown) — hash → chunk → embed → upsert.

Transactional. The `unchanged` short-circuit (same content hash AND same
embedding model) skips chunking/embedding entirely. A model change
invalidates vectors even for identical content — that's why the model name
is stored per page (plan §15) and `python -m wellisearch.reindex` exists.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import urlparse

from .chunk import chunk_markdown
from .config import get_settings
from .db import db
from .embed import embed

log = logging.getLogger("wellisearch.index")


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


async def store_page(url: str, markdown: str, title: str | None = None) -> tuple[str, int]:
    """Store one crawled page. Returns (status, chunks_written).

    status ∈ {'ok', 'unchanged'}.
    """
    s = get_settings()
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    # unchanged? (hash + model must both match, else re-embed is required)
    existing = await db.page_get(url)
    if existing and existing.get("content_hash") == digest and existing.get("embedding_model") == s.EMBED_MODEL:
        await db.execute(
            "UPDATE pages SET last_status = 'unchanged', last_crawled = now(), "
            "crawl_count = crawl_count + 1 WHERE url = %s",
            (url,),
        )
        return "unchanged", 0

    # chunk + embed (CPU-bound → keep off the event loop)
    chunks = chunk_markdown(markdown, s.MAX_CHUNK_TOKENS)
    vectors = await asyncio.to_thread(embed, chunks) if chunks else []
    if chunks and len(vectors) != len(chunks):
        raise RuntimeError(f"embed returned {len(vectors)} vectors for {len(chunks)} chunks")

    domain = domain_of(url)
    async with db.transaction() as conn:
        await conn.execute(
            """
            INSERT INTO pages
              (url, title, domain, fit_markdown, content_hash, embedding_model,
               last_crawled, last_status, crawl_count)
            VALUES (%s, %s, %s, %s, %s, %s, now(), 'ok', 1)
            ON CONFLICT (url) DO UPDATE SET
              title           = COALESCE(%s, pages.title),
              domain          = COALESCE(%s, pages.domain),
              fit_markdown    = EXCLUDED.fit_markdown,
              content_hash    = EXCLUDED.content_hash,
              embedding_model = EXCLUDED.embedding_model,
              last_crawled    = now(),
              last_status     = 'ok',
              crawl_count     = pages.crawl_count + 1
            """,
            (url, title, domain, markdown, digest, s.EMBED_MODEL, title, domain),
        )
        await conn.execute("DELETE FROM chunks WHERE url = %s", (url,))
        if chunks:
            # vectors pass as list[float]; the pgvector adapter (db.py) serializes.
            # executemany = batch mode (all executions sent in one message)
            cur = conn.cursor()
            await cur.executemany(
                "INSERT INTO chunks (url, seq, text, embedding, last_crawled) "
                "VALUES (%s, %s, %s, %s, now())",
                [(url, i, text, vec) for i, (text, vec) in enumerate(zip(chunks, vectors))],
            )
    return "ok", len(chunks)

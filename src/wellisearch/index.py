"""index: store_page(url, markdown, title=None) — hash → chunk → embed → upsert.

Transactional. The `unchanged` short-circuit (same content hash AND same
embedding model) skips chunking/embedding entirely. A model change
invalidates vectors even for identical content — that's why the model name is
stored per page (plan §15) and `python -m wellisearch.reindex` exists.

The page title is prepended as an H1 to the chunk source (unless a site
extractor already emitted it), so title words participate in the trigram and
vector legs; fit_markdown itself stays body-only, and the gates read
pages.title separately.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from urllib.parse import urlparse

from .chunk import chunk_markdown
from .config import get_settings
from .db import db
from .embed import embed

log = logging.getLogger("wellisearch.index")

_H1_RE = re.compile(r"^#\s+(\S.*?)\s*$")


def domain_of(url: str) -> str:
    """The URL's lowercased host; empty string when unparseable."""
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


async def store_page(
    url: str,
    markdown: str,
    title: str | None = None,
) -> tuple[str, int]:
    """Store one crawled page. Returns (status, chunks_written).

    status ∈ {'ok', 'unchanged'}. The title is prepended as an H1 to the
    chunk source (see _with_title) so it feeds the trigram + vector legs;
    fit_markdown stays body-only and the hash covers the chunk source.
    """
    s = get_settings()
    chunk_source = _with_title(markdown, title)
    digest = hashlib.sha256(chunk_source.encode("utf-8")).hexdigest()

    # unchanged? (hash + model must both match, else re-embed is required)
    existing = await db.page_get(url)
    if (
        existing
        and existing.get("content_hash") == digest
        and existing.get("embedding_model") == s.EMBED_MODEL
    ):
        # Backfill the title too: pages crawled before titles were stored have
        # title IS NULL, and recrawl/refresh must refresh crawl-time values
        # even when the content hash matches. COALESCE keeps the stored title
        # when no fresh one was crawled (title=None passes NULL through).
        await db.execute(
            "UPDATE pages SET title = COALESCE(%s, title), last_status = 'unchanged', "
            "last_crawled = now(), crawl_count = crawl_count + 1 WHERE url = %s",
            (title, url),
        )
        return "unchanged", 0

    # chunk + embed (CPU-bound → both off the event loop; chunking on the
    # loop stalls every request handler while a page stores)
    chunks = await asyncio.to_thread(chunk_markdown, chunk_source, s.MAX_CHUNK_TOKENS)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _with_title(markdown: str, title: str | None) -> str:
    """Prepend the page title as an H1 so it participates in chunking and
    embedding. Skipped when absent or already present (site extractors emit
    their own `# {title}` heading)."""
    if not title or not markdown.strip():
        return markdown
    first = next((ln for ln in markdown.lstrip().splitlines() if ln.strip()), "")
    m = _H1_RE.match(first)
    if m and " ".join(m.group(1).split()).casefold() == " ".join(title.split()).casefold():
        return markdown
    return f"# {title}\n\n{markdown}"

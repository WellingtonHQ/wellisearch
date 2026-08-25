"""Measure TRUE local scores (fts + trigram + vector legs, exactly as the
API computes them) so a threshold can be chosen on the real scale."""
import asyncio
from wellisearch.config import get_settings
from wellisearch.db import db
from wellisearch.embed import embed_one

QUERIES = [
    ("RELEVANT", "fastapi mcp server"),
    ("RELEVANT", "mcp server"),
    ("RELEVANT", "how to use pgvector for semantic search"),
    ("RELEVANT", "chocolate cake recipe with espresso"),
    ("SPURIOUS", "weather forecast tokyo"),
    ("SPURIOUS", "best pizza recipe"),
    ("SPURIOUS", "what is the stock price of apple"),
    ("SPURIOUS", "quantum computing explained"),
]

async def main():
    s = get_settings()
    print("SEARCH_MIN_SCORE =", s.SEARCH_MIN_SCORE)
    await db.startup()
    for kind, q in QUERIES:
        try:
            qvec = await asyncio.to_thread(embed_one, q)
        except Exception:
            qvec = None
        rows = await db.fetch_all(
            "SELECT url, score FROM fn_search_local(%s, %s::vector, 5)",
            (q, qvec if qvec is not None else None),
        )
        top = [(r["url"].split("/")[-1][:34], round(float(r["score"]), 3)) for r in rows[:3]]
        gate = "PASS" if rows and float(rows[0]["score"]) >= s.SEARCH_MIN_SCORE else "miss"
        print("%-9s %-42s %-8s %s" % (kind, q, gate, top))
    await db.close()

asyncio.run(main())

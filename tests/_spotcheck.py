"""Spot-check the live search API: run a few queries and print source,
degraded flag, result count, and the first URL."""
from __future__ import annotations

import os
import re

import httpx

env = open(os.path.join(os.path.dirname(__file__), "..", ".env"), encoding="utf-8").read()
key = re.search(r"^WELLISEARCH_API_KEY=(.+)$", env, re.M).group(1).strip()
H = {"X-API-Key": key}
for q in [
    "fastapi mcp server",
    "how to use pgvector for semantic search",
    "chocolate cake recipe with espresso",
]:
    r = httpx.get("http://127.0.0.1:8780/api/search", params={"query": q, "k": 3}, headers=H, timeout=30)
    md = r.text
    src = re.search(r"^Source: (\S+)", md, re.M)
    deg = re.search(r"^Degraded: (\S+)", md, re.M)
    m = re.search(r"URL: (\S+)", md)
    print("query=%r src=%s degraded=%s count=%s first=%s" % (
        q,
        src.group(1) if src else "?",
        deg.group(1) if deg else "?",
        len(re.findall(r"^URL: ", md, re.M)),
        m.group(1) if m else "(none)",
    ))

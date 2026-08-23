import httpx, re, os

env = open(os.path.join(os.path.dirname(__file__), "..", ".env"), encoding="utf-8").read()
key = re.search(r"^WELLISEARCH_API_KEY=(.+)$", env, re.M).group(1).strip()
H = {"X-API-Key": key}
for q in ["fastapi mcp server", "how to use pgvector for semantic search", "chocolate cake recipe with espresso"]:
    r = httpx.get("http://127.0.0.1:8780/api/search", params={"query": q, "k": 3}, headers=H, timeout=30)
    j = r.json()
    first = "(none)"
    if j.get("results"):
        block = j["results"].split("\n---\n")[0].splitlines()
        first = next((ln for ln in block if ln.startswith("URL:")), "?")
    print("query=%r src=%s degraded=%s count=%s first=%s" % (q, j["source"], j["degraded"], j["count"], first))

# Engine

## Multi-crawl
When seeding a URL, wellisearch should use crawl4ai to do a breadth/depth crawl and load adjacent URLs as well. 

## Ability to specify a refresh for a specific page.
- Certain pages need to be refreshed daily, or hourly, for example news sites. When seeding a site we should be able to specify this parameter.
- When seeding a URL via the dashboard, it should also allow you to do that.


## Replace Text-Embedding with qwen
See if we could use a higher-quality embedding model. It could potentially take more time but it would result in better results.
qwen3-embedding:4b

## Use LLM to cleanup markdown.
qwen3:4b

## Optional TTL
Pages that haven't been fetched in a specified time (let's say, 1 year) get dropped from the index.
This is optional and is used to save space.

## Crawl-failure handling

Crawl4AI returns HTTP 500 for anti-bot blocks (Cloudflare, DataDome, PerimeterX, Akamai,
403/429) and 400 for its SSRF guard. Neither is a wellisearch bug, but we should handle
both better so we stop re-crawling URLs that can never succeed.

### 500s — anti-bot negative caching
- Treat Crawl4AI anti-bot blocks as a distinct `blocked` status rather than `http_500`.
- Negative-cache known anti-bot hosts (walmart, yelp, nytimes, reuters, medium, web.archive.org, ...)
  so we stop re-attempting them. Failed URLs are currently retried up to 14× — wasted work.

### 400s — filter junk URLs before they reach the crawl queue
Crawl4AI's SSRF guard is correctly blocking these, but the real defect is that we seed them.
Filter at URL-extraction time:
- placeholder/template URLs from doc code blocks: `127.0.0.1:$Port`, `100.x.x.x:3000`, `YOUR_DOMAIN:8090`
- non-numeric / backticked ports: `$`, `PORT`, `bash`, `3000\``
- internal docker service names: `ollama:11434`, `open-terminal:8000`, `sandbox:8080`, `postgres`

## Record Metrics
Publish API response times P50, P95, P99s.

---
# Fetching

## Always return by timeout
Some clients will timeout waiting for a page for fetch.

## Simultaneous fetches will sometimes hang:
```
Executing fetch_page...
INPUT
url
https://www.pedroalonso.net/blog/qwen-mtp-speculative-decoding-4090/
max_chars
15000
Executing fetch_page...
INPUT
url
https://bestllmfor.com/guides/lm-studio-mtp-multi-token-prediction/
max_chars
12000
Executing fetch_page...
INPUT
url
https://www.reddit.com/r/Qwen_AI/comments/1vqzl5l/qwen3827b_at_160k_context_on_a_single_rtx_4090/
max_chars
12000
```

then after 2-5 minutes:
```
INPUT
url
https://bestllmfor.com/guides/lm-studio-mtp-multi-token-prediction/
max_chars
12000
OUTPUT
URL: https://bestllmfor.com/guides/lm-studio-mtp-multi-token-prediction/
Status: failed
Error: couldn't get a connection after 30.00 sec
Time: 53920 ms
View Result from fetch_page
INPUT
url
https://www.reddit.com/r/Qwen_AI/comments/1vqzl5l/qwen3827b_at_160k_context_on_a_single_rtx_4090/
max_chars
12000
OUTPUT
URL: https://www.reddit.com/r/Qwen_AI/comments/1vqzl5l/qwen3827b_at_160k_context_on_a_single_rtx_4090/
Status: failed
Error: https://www.reddit.com/r/Qwen_AI/comments/1vqzl5l/qwen3827b_at_160k_context_on_a_single_rtx_4090/: bot-wall detected; routed to the CF challenge lane
Time: 94552 ms
```

The responses are good but they shouldn't take so long to come back. If a bot-wall is detected, let the client know right away
and tell them to try again in x seconds, rather than keep them waiting.

---

# Search

## Tiered providers
Ability to put providers on tiers:
For example:
Tier 1: [brave, tavily]
Tier 2: [exa]
Tier 3[searxng]

The way it works is as follows. 
1. Get the tier 1 results. If you have multiple in a tier, randomly select one from the list (DO NOT SEARCH ACROSS ALL).
2. If those results are good, you can return.
3. If tier 1 results are not good, move to tier 2.
4. Same logic: if multiple in the list, select randomly from the list. The idea is to evenly distribute the API usage.
5. Move down to the last tier (Tier 3 in this example).

This will help rotate search engines to prevent burning one API limit, and then moving to burn the next one.

## Add new providers:
1. ~~Exa~~ (done)
2. ddgs (duck duck go custom crawler)
3. ~~you.com~~

## Provider auto-ranking
Rather than having the user manually select the tiers, the system itself
will track results from the providers and score the relevancy of the results to the query.
It will periodically reorder the providers to put the highest quality one at the top.

---

# Dashboard

- Drop the "Top pages by search_hit_count"
- Add a section that shows a log of searches only (including terms), a list of URLs provided, and source (local OR provider). Essentially surfaces `search_log` table.
- Light mode: automatically determined via system.

---

# Indexing

## Pause indexing operations
- Add ability to pause indexing so worker ticks won't launch re-indexes and will be skipped altogether.
  Should be a button/toggle on the dashboard.
  
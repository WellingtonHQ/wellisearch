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

---

# Fetching

## Max age param
Add a max-age parameter, similar to search. If an indexed page is older than specified, a new crawl will run. The client will wait for the new crawl to complete.

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

---

# Search

## New param (skip-local: true) to skip the local index entirely. 
This is done if for example on a search, the LLM was unsatisfied 
with the results from the local index and wants the server to
search one of the providers.

## New response field capturing time
Add a field to the response text in the header that indicates
how long the query took over. It should say how much time was spent
searching the postgres index and how much time (if used) was spent
waiting for the provider response.


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
1. Exa — Done 2026-08-29
2. ddgs (duck duck go custom crawler)
3. you.com — Done 2026-08-29

## Provider auto-ranking
Rather than having the user manually select the tiers, the system itself
will track results from the providers and score the relevancy of the results to the query.
It will periodically reorder the providers to put the highest quality one at the top.

## Remove searxng
It is no longer needed or useful.
— Done 2026-08-29 (SearXNG removed; EXA added as third provider).

## Ability to specify specific provider in search
Currently we have the search_mode param that lets you pick between local, provider, and auto modes. 
When you pick provider, you currently don't have any option to specify which specific provider you want to use.
This would give the LLM flexibility in choosing its own provider depending on the context.
And it opens the door for....

## Domain specific searching
Imagine if wellisearch allowed the caller to search Amazon's inventory, or Home Depot's inventory.
How would it do that? Simple. Crawl the site's "search?query=x" pages as a normal user does.
Wellisearch would then return markdown as normal and it would look like any other web result.

---

# Dashboard

- Ability to move/reorder the provider list — Done 2026-08-29 (dashboard ↑/↓ reorder + "reset to default order"; `PUT /api/providers/order`; persists in `provider_state.sort_order`, effective immediately)
- Drop the "Top pages by search_hit_count"
- Add a section that shows a log of searches only (including terms), a list of URLs provided, and source (local OR provider). Essentially surfaces `search_log` table.
- Light mode: automatically determined via system.


---

# Indexing

## Pause indexing operations
- Add ability to pause indexing so worker ticks won't launch re-indexes and will be skipped altogether.
  Should be a button/toggle on the dashboard.
  
# Engine

## Multi-crawl
When seeding a URL, wellisearch should use crawl4ai to do a breadth/depth crawl and load adjacent URLs as well. 

## Ability to specify a refresh for a specific page.
- Certain pages need to be refreshed daily, or hourly, for example news sites. When seeding a site we should be able to specify this parameter.
- When seeding a URL via the dashboard, it should also allow you to do that.

## Optional TTL
Pages that haven't been fetched in a specified time (let's say, 1 year) get dropped from the index.
This is optional and is used to save space.

---

# Search

## Search web provides markdown, like fetch_page
Currently, search_web returns a JSON string with this envelope:
JSON envelope:

| Field | Type | Notes |
|---|---|---|
| `results` | string | the Markdown block(s) above |
| `source` | string | `local` \| `tavily` \| `brave` \| `searxng` \| `error` |
| `degraded` | bool | true only in §6 degraded mode |
| `count` | int | number of result blocks |
| `last_crawled` | string[] | ISO timestamps; present only when `source = local` |
| `provider_errors` | object[] | present only when providers failed |

The results string looks like this:
```
Title: PostgreSQL 18 Documentation
URL: https://www.postgresql.org/docs/current/
Snippet: The PostgreSQL documentation ...
---
Title: FastAPI MCP server
URL: https://...
Snippet: ...
```

Rather than JSON, the API should just return the markdown like this:
```md
Title: PostgreSQL 18 Documentation
URL: https://www.postgresql.org/docs/current/
Source: Local
Degraded: False
Last Crawled: ISO Timestamp
Provider Errors: present only when providers fail
Snippet: The PostgreSQL documentation ...
---
Title: FastAPI MCP server
URL: https://...
Source: Brave
Degraded: False
Last Crawled: ISO Timestamp
Snippet: ...
```


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
1. Exa
2. ddgs (duck duck go custom crawler)
3. you.com

## Provider auto-ranking
Rather than having the user manually select the tiers, the system itself
will track results from the providers and score the relevancy of the results to the query.
It will periodically reorder the providers to put the highest quality one at the top.

## Remove searxng
It is no longer needed or useful.

---


## Explorer fastembed options
See if we could use a higher-quality embedding model. It could potentially take more time but it would result in better results.

## Dashboard
- Ability to move/reorder the provider list
- Drop the "Top pages by search_hit_count"
- Add a section that shows a log of searches only (including terms), a list of URLs provided, and source (local OR provider). Essentially surfaces `search_log` table.
- Light mode: automatically determined via system.
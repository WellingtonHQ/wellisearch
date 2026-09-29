"""search_web pipeline (plan §7): local index → gateway → log → enqueue → return.

No crawl in the response path: local hits cost zero provider credits; on a
miss the provider serves immediately and the top result URLs are enqueued
for background indexing (kicked, debounced). `search_mode` selects the
source: auto (local first, default), local (index only), provider (gateway
only).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
import urllib.parse

from . import queue
from .config import get_settings
from .db import db
from .embed import embed_one
from .providers import GatewayExhausted, get_gateway
from .serialize import format_timing

log = logging.getLogger("wellisearch.search_web")

# search_mode values: which source serves the answer.
#   auto     — local index first, provider gateway on a miss (default)
#   local    — local index only; an error if the index has nothing
#   provider — provider gateway only; the local index is not consulted
SEARCH_MODES = ("auto", "local", "provider")

SNIPPET_MAX_LEN = 400  # max chars of a snippet in search results (local + provider)

# Common two-part TLDs: the registrable domain is three labels, not two.
_TWO_PART_TLDS = ("co.uk", "com.au", "co.in", "com.br", "org.uk", "net.au", "co.nz")


def render_search_markdown(out: dict) -> str:
    """The search response as plain Markdown (no JSON envelope): a
    response-level header (Source / Degraded / Provider Errors) followed by
    Title/URL/Snippet blocks separated by `---` lines. Local hits carry a
    Last Crawled line per result."""
    lines = [
        f"Source: {out['source']}",
        f"Degraded: {'true' if out.get('degraded') else 'false'}",
    ]
    tline = format_timing(out.get("timing"))
    if tline:
        lines.append(tline)
    errors = out.get("provider_errors") or []
    if errors:
        lines.append(
            "Provider Errors: " + "; ".join(f"{e.get('provider')}: {e.get('error')}" for e in errors)
        )
    if out.get("index_error"):
        lines.append(f"Index Error: {out['index_error']}")

    blocks = []
    for r in out.get("results") or []:
        block = [f"Title: {r.get('title') or r['url']}", f"URL: {r['url']}"]
        ts = r.get("last_crawled")
        if ts is not None:
            block.append(f"Last Crawled: {ts.isoformat() if hasattr(ts, 'isoformat') else ts}")
        block.append(f"Snippet: {r.get('snippet') or ''}")
        blocks.append("\n".join(block))

    if not blocks:
        return "\n".join(lines)
    return "\n\n".join(["\n".join(lines), "\n---\n".join(blocks)])


async def search_web(
    query: str,
    num_results: int | None = None,
    max_crawl: int | None = None,
    max_age_days: float | None = None,
    search_mode: str = "auto",
) -> dict:
    """The search pipeline: local index first (auto), provider gateway on a
    miss, then log the search, enqueue top results for background indexing,
    and return the envelope. `search_mode` selects the source (auto/local/provider)."""
    s = get_settings()
    k = max(1, num_results or s.SEARCH_K)  # clamp: negative k would slice rows off the end
    crawl_n = s.SEARCH_MAX_CRAWL if max_crawl is None else max(0, max_crawl)
    if search_mode not in SEARCH_MODES:
        raise ValueError(
            f"invalid search_mode {search_mode!r} (choose from {list(SEARCH_MODES)})"
        )

    t_start = time.monotonic()

    # ---- local index (zero provider cost) — skipped entirely in provider
    # mode (the caller wants a live provider answer, e.g. after being
    # unsatisfied with a prior local result).
    local_rows: list[dict] = []
    index_ms = 0
    index_error: str | None = None
    if search_mode != "provider":
        local_rows, index_ms, index_error = await _search_local_index(query, k, max_age_days)

    # Auto mode: each row must clear all three per-row gates — coverage, similarity,
    # and distinctive-term coverage (a page missing every rare / brand query word
    # cannot serve). A full set of k passing rows serves; an incomplete set can also
    # serve if several pass or one is strongly similar. This lets indexed provider
    # hits satisfy repeat queries without trusting a marginal lone match or off-brand
    # junk. Local mode bypasses the gate; provider mode has no local rows.
    # Auto-mode serving policies (docs/ranking.md): a union candidate window
    # (top-N by score ∪ top-M by similarity) so semantically close pages are
    # always considered, and a job-board de-rank for non-job-intent queries.
    # Local mode serves the raw index order untouched.
    candidates = local_rows
    if search_mode == "auto":
        candidates = _candidate_rows(local_rows)
        if not _job_intent(query, s.job_intent_terms):
            candidates = _apply_job_board_penalty(
                candidates,
                s.job_boards,
                s.SEARCH_JOB_BOARD_PENALTY,
            )
    passing = [r for r in candidates if _passes_local_gate(r)]
    # Among gate-passing rows, similarity is the primary topical signal and score
    # is rank-only (docs/ranking.md) — serve the most topically similar first, with
    # score as the tie-break. This keeps a semantically strong page ahead of pages
    # that only accumulate RRF mass from many lexically matching chunks.
    passing.sort(key=lambda r: (-(r.get("similarity") or 0.0), -(r.get("score") or 0.0)))
    strong = [r for r in passing if _passes_partial_local_gate(r)]

    source: str
    results: list[dict]
    degraded = False
    errors: list[dict] = []
    provider_ms: int | None = None

    if search_mode == "local":
        # local only: serve what the index has — the caller explicitly chose
        # local, so the gate does not apply. No provider fallback.
        if local_rows:
            source, results = await _serve_local(local_rows, k)
        else:
            source = "error"
            results = []
    elif len(passing) >= k:
        # local hit — zero provider credits (the quota-preservation layer);
        # the per-domain cap keeps one site's near-duplicates from flooding
        # the answer set (auto mode only — local mode served above, uncapped)
        source, results = await _serve_local(_cap_per_domain(passing, s.SEARCH_MAX_PER_DOMAIN), k)
    elif passing and len(passing) >= s.LOCAL_PARTIAL_MIN_PASSING:
        # Several qualifying pages are evidence of a useful partial answer.
        source, results = await _serve_local(_cap_per_domain(passing, s.SEARCH_MAX_PER_DOMAIN), k)
    elif strong:
        # One unusually strong page is enough; omit marginal companions.
        source, results = await _serve_local(_cap_per_domain(strong, s.SEARCH_MAX_PER_DOMAIN), k)
    else:
        # ---- provider gateway (auto: no qualifying local set; provider: always)
        source, results, degraded, errors, provider_ms = await _provider_search(
            query, k, crawl_n, search_mode, local_rows
        )

    await db.log_search(query, source, len(results), results)

    # Envelope: structured data only. The Markdown body is rendered at the
    # surfaces (tools.py for MCP, app.py for REST) via render_search_markdown.
    # index_ms is only present when the index leg actually ran — provider
    # mode never consults the index, so `index: 0 ms` would mislead.
    timing: dict = {"total_ms": int((time.monotonic() - t_start) * 1000)}
    if search_mode != "provider":
        timing["index_ms"] = index_ms
    if provider_ms is not None:
        timing["provider_ms"] = provider_ms

    out: dict = {
        "results": results,
        "source": source,
        "degraded": degraded,
        "count": len(results),
        "timing": timing,
    }
    if errors:
        out["provider_errors"] = errors
    # local mode with a failed index leg: the empty row set is a failure, not
    # "nothing indexed" — carry the diagnostics in the envelope (mirrors
    # provider_errors) so the caller can tell the two apart.
    if source == "error" and index_error:
        out["index_error"] = index_error
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _passes_local_gate(r: dict) -> bool:
    """Whether one local row clears all three gate conditions: coverage, similarity,
    and distinctive-term coverage. A page that misses every rare / brand query word
    cannot serve even if it covers the common words (docs/ranking.md)."""
    s = get_settings()
    if (r.get("coverage") or 0.0) < s.LOCAL_MIN_COVERAGE:
        return False
    sim = r.get("similarity")
    if sim is None or sim < s.LOCAL_MIN_SIMILARITY:
        return False
    # Missing/NULL distinctive_coverage means "no info" (e.g. a hand-built row);
    # treat it as satisfied rather than penalizing an unknown.
    dc = r.get("distinctive_coverage")
    if dc is None:
        return True
    return dc >= s.LOCAL_MIN_DISTINCTIVE_COVERAGE


def _passes_partial_local_gate(r: dict) -> bool:
    """Whether a passing row is strong enough to serve without a full set."""
    sim = r.get("similarity")
    return sim is not None and sim >= get_settings().LOCAL_PARTIAL_MIN_SIMILARITY


# ---------------------------------------------------------------------------
# Serving policies (auto mode only; docs/ranking.md)
# ---------------------------------------------------------------------------

def _split_url(url: str) -> tuple[str, str] | None:
    """(lowercased host without www, path) of a URL — scheme optional.
    None on parse failure."""
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "//" + url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    return host, parts.path or ""


def _registrable_domain(url: str) -> str:
    """The registrable domain of a URL (host without scheme/www, last two
    labels — three for common two-part TLDs like co.uk). "" on failure."""
    split = _split_url(url)
    if split is None:
        return ""
    parts = [p for p in split[0].split(".") if p]
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_job_board(url: str, boards: tuple[str, ...]) -> bool:
    """Whether a URL matches a job-board entry (host[/path] prefix): the host
    equals or subdomains the listed host and, when a path is given, starts
    with it."""
    split = _split_url(url)
    if split is None:
        return False
    host, path = split
    for entry in boards:
        spec_host, sep, spec_tail = entry.partition("/")
        spec_host = spec_host.lower().removeprefix("www.")
        if not spec_host:
            continue
        if host != spec_host and not host.endswith("." + spec_host):
            continue
        if sep:
            spec_path = "/" + spec_tail
            if path != spec_path and not path.startswith(spec_path + "/"):
                continue
        return True
    return False


def _job_intent(query: str, terms: tuple[str, ...]) -> bool:
    """Whether a query asks for jobs (word-boundary match on any term) — such
    queries skip the job-board de-rank."""
    pattern = "|".join(re.escape(t) for t in terms if t)
    if not pattern:
        return False
    return re.search(rf"\b({pattern})\b", query, re.IGNORECASE) is not None


def _apply_job_board_penalty(
    rows: list[dict],
    boards: tuple[str, ...],
    penalty: float,
) -> list[dict]:
    """Job-board rows get score and similarity multiplied by `penalty` (a
    de-rank, not a filter — coverage is untouched so the gate still sees
    them). Non-matching rows pass through unchanged."""
    out = []
    for r in rows:
        if _is_job_board(r.get("url") or "", boards):
            r = dict(r)
            score = r.get("score")
            sim = r.get("similarity")
            r["score"] = None if score is None else score * penalty
            r["similarity"] = None if sim is None else sim * penalty
        out.append(r)
    return out


def _cap_per_domain(rows: list[dict], max_per_domain: int) -> list[dict]:
    """Keep at most `max_per_domain` rows per registrable domain (in order);
    rows without a parseable domain always pass."""
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        dom = _registrable_domain(r.get("url") or "")
        if not dom:
            out.append(r)
            continue
        n = seen.get(dom, 0)
        if n < max_per_domain:
            seen[dom] = n + 1
            out.append(r)
    return out


def _candidate_rows(local_rows: list[dict]) -> list[dict]:
    """Auto-mode candidate set: top SEARCH_GATE_MIN_K by score (input order)
    plus the SEARCH_TOP_BY_SIM most similar rows not already included, so a
    semantically close page is considered even when lexical mass buries its
    score (docs/ranking.md)."""
    s = get_settings()
    head = local_rows[: s.SEARCH_GATE_MIN_K]
    seen = {r.get("url") for r in head}
    extra = [
        r
        for r in sorted(
            (r for r in local_rows if r.get("similarity") is not None),
            key=lambda r: -(r["similarity"]),
        )
        if r.get("url") not in seen
    ][: s.SEARCH_TOP_BY_SIM]
    return head + extra


async def _search_local_index(
    query: str,
    k: int,
    max_age_days: float | None,
) -> tuple[list[dict], int, str | None]:
    """The local-index leg: embed the query, rank via fn_search_local, apply
    the optional freshness filter. Returns (rows, index_ms, index_error).

    A bad query embedding degrades to FTS+trigram only (not an error). A
    failed fn_search_local (statement timeout, DB error) degrades to an empty
    row set AND returns the exception as `index_error` — auto mode falls back
    to the provider gateway (the error is hidden there), local mode surfaces
    it in the error envelope so "the index is empty" and "the index is down"
    are distinguishable.
    """
    s = get_settings()
    t_index = time.monotonic()
    try:
        qvec = await asyncio.to_thread(embed_one, query)
    except Exception as e:
        log.warning("query embedding failed (%s) — searching with FTS+trigram only", e)
        qvec = None

    # Fetch a bit more than we'll return so the local-hit gate can see a
    # passing page that ranks just outside the top-k by score, plus the
    # similarity window for auto mode's union candidate set (docs/ranking.md).
    # The extra rows cost little — the legs/fusion are the same; only the final
    # LIMIT and per-row gate columns grow. The vector leg is widened to
    # SEARCH_VECTOR_LEG_LIMIT so semantically close but lexically thin pages
    # enter the fusion at all.
    gate_k = max(k, s.SEARCH_GATE_MIN_K + s.SEARCH_TOP_BY_SIM)
    try:
        rows = await db.fetch_all(
            "SELECT * FROM fn_search_local(%s, %s::vector, %s, %s)",
            (query, qvec if qvec is not None else None, gate_k, s.SEARCH_VECTOR_LEG_LIMIT),
            timeout_ms=s.SEARCH_STATEMENT_TIMEOUT_MS,
        )
    except Exception as e:
        log.exception("fn_search_local failed (timeout_ms=%s)", s.SEARCH_STATEMENT_TIMEOUT_MS)
        return [], int((time.monotonic() - t_index) * 1000), f"{type(e).__name__}: {e}"

    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
        rows = [
            r for r in rows
            if r.get("last_crawled") is None or r["last_crawled"] >= cutoff
        ]
    return rows, int((time.monotonic() - t_index) * 1000), None


def _local_result(r: dict) -> dict:
    """One local-index row as a result dict (snippet clamped to 400 chars)."""
    return {
        "url": r["url"],
        "title": r.get("title") or r["url"],
        "snippet": (r.get("snippet") or "")[:SNIPPET_MAX_LEN],
        "score": r.get("score"),
        "coverage": r.get("coverage"),
        "similarity": r.get("similarity"),
        "last_crawled": r.get("last_crawled"),
        "fetch_count": r.get("fetch_count"),
    }


async def _serve_local(local_rows: list[dict], k: int) -> tuple[str, list[dict]]:
    """Serve local rows as results and mark the search hits.
    Returns (source, results)."""
    results = [_local_result(r) for r in local_rows[:k]]
    await db.mark_search_hits([r["url"] for r in results])
    return "local", results


async def _provider_search(
    query: str,
    k: int,
    crawl_n: int,
    search_mode: str,
    local_rows: list[dict],
) -> tuple[str, list[dict], bool, list[dict], int | None]:
    """The provider-gateway leg: search, map results, and enqueue the top
    result URLs for background indexing. On GatewayExhausted, degrade to the
    local rows when available (§14.12).
    Returns (source, results, degraded, errors, provider_ms)."""
    gw = get_gateway()
    t_prov = time.monotonic()
    try:
        provider_results, provider_name, errors = await gw.search(query, k)
        provider_ms = int((time.monotonic() - t_prov) * 1000)
        source = provider_name
        results = [
            {
                "url": r.url,
                "title": r.title,
                "snippet": r.snippet[:SNIPPET_MAX_LEN],
                "score": r.score,
            }
            for r in provider_results[:k]
        ]
        # speculative pre-indexing: enqueue top result URLs (background)
        for r in provider_results[:crawl_n]:
            await queue.enqueue(r.url, source="search")
        return source, results, False, errors, provider_ms
    except GatewayExhausted as e:
        provider_ms = int((time.monotonic() - t_prov) * 1000)
        log.warning("all providers failed: %s", e)
        errors = e.errors
        if search_mode == "auto" and local_rows:
            # degraded mode: serve whatever local results we have (§14.12)
            source, results = await _serve_local(local_rows, k)
            return source, results, True, errors, provider_ms
        # provider mode has no local fallback; auto only reaches here
        # when the index returned nothing
        return "error", [], False, errors, provider_ms

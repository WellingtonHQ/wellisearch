"""FastAPI app: REST routes + MCP (stateless streamable HTTP) +
static dashboard + worker (§2/§7/§9).

The endpoint surface, request/response contracts, and auth rules live in
docs/api.md — that file is the single source of truth (kept in sync with the
routes below).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, crawler, queue
from .config import get_settings
from .db import db
from .fetch import _OMITTED, _valid_url, fetch_page, fetch_pages
from .fetch import render_fetch_page_markdown, render_fetch_pages_markdown
from .mcp import mcp_asgi, mcp_http_lifespan
from .providers import get_gateway
from .search_web import render_search_markdown
from .search_web import search_web as search_web_pipeline
from .serialize import resolve_format, to_json
from .tools import _index_stats_data
from .worker import STATE as WORKER_STATE
from .worker import crawl_url, run_forever

log = logging.getLogger("wellisearch.app")

# static/ ships inside the package (works in dev layout and installed wheel)
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: the streamable-HTTP session manager's task group must be
    live before the first request (Starlette does not run lifespans of
    mounted sub-apps), then the worker + DB for the app's lifetime."""
    async with mcp_http_lifespan():
        await _startup()
        yield
        await _shutdown()


app = FastAPI(title="wellisearch", version=__version__, lifespan=_lifespan)

_worker_task: asyncio.Task | None = None


# ------------------------------------------------------------------ lifecycle

async def _ev(message: str, info: dict | None = None) -> None:
    """Best-effort event logging (dashboard log view)."""
    try:
        await db.log_event(message, info)
    except Exception as e:
        log.warning("event logging failed: %s", e)


async def _startup() -> None:
    global _worker_task
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    await db.startup()
    await db.queue_reset_in_flight()
    _worker_task = asyncio.create_task(run_forever(), name="worker")
    log.info("wellisearch up (worker task started)")
    await _ev(
        "wellisearch started",
        {"version": app.version, "providers": get_settings().provider_order},
    )


async def _shutdown() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):
            pass
        _worker_task = None
    from .providers import shutdown_gateway

    await shutdown_gateway()
    await db.close()


# ------------------------------------------------------------------------ auth

@app.middleware("http")
async def _auth(request: Request, call_next):
    s = get_settings()
    key = s.WELLISEARCH_API_KEY
    path = request.url.path
    if key and (path.startswith("/api") or path.startswith("/mcp")):
        token: str | None = None
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            token = authz[len("bearer "):].strip()
        elif request.headers.get("x-api-key"):
            token = request.headers["x-api-key"].strip()
        if token is None or not hmac.compare_digest(token, key):
            return JSONResponse({"error": "unauthorized — set Authorization: Bearer <WELLISEARCH_API_KEY>"}, status_code=401)
    return await call_next(request)


# ----------------------------------------------------------------------- routes

@app.get("/health")
async def health() -> dict[str, Any]:
    out: dict[str, Any] = {"status": "ok", "time": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        await db.fetch_one("SELECT 1 AS ok")
        out["database"] = "ok"
    except Exception as e:
        out["database"] = f"error: {e}"
        out["status"] = "degraded"
    try:
        ok, detail = await crawler.health()
        out["crawl4ai"] = detail if ok else f"error: {detail}"
    except Exception as e:
        out["crawl4ai"] = f"error: {e}"
    try:
        gw = get_gateway()
        out["providers"] = [
            {
                "name": p.name,
                "configured": p.configured,
                "state": await db.get_provider_state(p.name),
            }
            for p in gw.providers
        ]
    except Exception as e:
        out["providers"] = f"error: {e}"
    return out


class FetchBody(BaseModel):
    url: str
    max_chars: int | None = None
    format: str | None = None  # "json" | "markdown" (default markdown)


class SeedBody(BaseModel):
    url: str


class RefreshBody(BaseModel):
    url: str


class ProviderPatch(BaseModel):
    enabled: bool | None = None
    limit: int | None = None


class PagePatch(BaseModel):
    disabled: bool = Field(default=False)


def _negotiate(format_param: str | None, request: Request) -> str:
    """Resolve the response format from the explicit `format` param and the
    Accept header. An invalid explicit format is a client error (400)."""
    try:
        return resolve_format(format_param, request.headers.get("accept"))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _respond(out: dict, fmt: str, render_md, status: int = 200) -> Response:
    """The pipeline dict as the negotiated wire format (json | markdown)."""
    if fmt == "json":
        return Response(to_json(out), media_type="application/json", status_code=status)
    return Response(render_md(out), media_type="text/markdown", status_code=status)


@app.get("/api/search")
async def api_search(
    request: Request,
    query: str,
    k: int = 5,
    num_results: int | None = None,  # alias for k (MCP tool + OWUI tool surface name)
    max_crawl: int | None = None,
    max_age_days: float | None = None,
    search_mode: str = "auto",  # "auto" | "local" | "provider"
    format: str | None = None,  # "json" | "markdown" (default markdown)
) -> Any:
    try:
        out = await search_web_pipeline(
            query,
            num_results=num_results or k,
            max_crawl=max_crawl,
            max_age_days=max_age_days,
            search_mode=search_mode,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    status = 502 if out.get("source") == "error" else 200
    return _respond(out, _negotiate(format, request), render_search_markdown, status)


@app.post("/api/fetch")
async def api_fetch(body: FetchBody, request: Request) -> Any:
    if not _valid_url(body.url):
        raise HTTPException(400, "invalid or non-http(s) url")
    out = await fetch_page(body.url, max_chars=body.max_chars)
    return _respond(out, _negotiate(body.format, request), render_fetch_page_markdown)


@app.post("/api/fetch-bulk")
async def api_fetch_bulk(request: Request) -> Any:
    # read raw JSON so we can distinguish omitted keys (→ server default
    # budget) from explicit null/0 (→ unlimited), per BLUEPRINT §7/§10
    raw = await request.json()
    if not isinstance(raw, dict) or not isinstance(raw.get("urls"), list):
        raise HTTPException(400, "body must be JSON: {urls: [...], max_chars?, per_page_chars?, strategy?, format?}")
    out = await fetch_pages(
        raw.get("urls", []),
        max_chars=raw["max_chars"] if "max_chars" in raw else _OMITTED,
        per_page_chars=raw["per_page_chars"] if "per_page_chars" in raw else _OMITTED,
        strategy=raw.get("strategy"),
    )
    return _respond(out, _negotiate(raw.get("format"), request), render_fetch_pages_markdown)


@app.get("/api/stats")
async def api_stats() -> Any:
    data = await _index_stats_data()
    now = dt.datetime.now(dt.timezone.utc)
    last_tick = WORKER_STATE.get("last_tick_at")
    data["runtime"] = {
        "worker": {
            "started_at": WORKER_STATE.get("started_at"),
            "last_tick_at": last_tick,
            "last_tick_stats": WORKER_STATE.get("last_tick_stats"),
            "tick_age_s": round((now - last_tick).total_seconds(), 1) if last_tick else None,
        },
        "in_flight_crawls": sorted(queue.INFLIGHT.urls()),
    }
    last_search = await db.fetch_one(
        "SELECT ts, query, source, local_hits FROM search_log ORDER BY id DESC LIMIT 1"
    )
    data["last_search"] = last_search
    return data


@app.get("/api/providers")
async def api_providers() -> Any:
    s = get_settings()
    gw = get_gateway()
    out = []
    for p in gw.providers:
        state = await db.get_provider_state(p.name) or {}
        used, limit = await db.quota_used_limit(p.name)
        out.append({
            "name": p.name,
            "configured": p.configured,
            "enabled": state.get("enabled", True),
            "limit_runtime": state.get("limit_override"),
            "limit_default": s.env_quota_limit(p.name),
            "quota_used": used,
            "quota_limit": limit,
            "last_served": state.get("last_served"),
            "last_error": state.get("last_error"),
        })
    return {"providers": out}


@app.patch("/api/providers/{name}")
async def api_provider_patch(name: str, body: ProviderPatch) -> Any:
    s = get_settings()
    if name not in s.provider_order:
        raise HTTPException(404, f"unknown provider {name!r}")
    if body.enabled is not None:
        await db.set_provider_state(name, enabled=body.enabled)
    if body.limit is not None:
        await db.set_provider_state(name, limit=body.limit)
    await _ev(
        f"provider {name} updated",
        {"enabled": body.enabled, "limit": body.limit},
    )
    return {"ok": True, "name": name, "enabled": body.enabled, "limit": body.limit}


@app.post("/api/seed")
async def api_seed(body: SeedBody) -> Any:
    if not _valid_url(body.url):
        raise HTTPException(400, "invalid or non-http(s) url")
    inserted = await queue.enqueue(body.url, source="manual")
    await _ev("seed url", {"url": body.url, "newly_queued": inserted})
    return {"ok": True, "url": body.url, "newly_queued": inserted}


@app.post("/api/refresh")
async def api_refresh(body: RefreshBody) -> Any:
    if not _valid_url(body.url):
        raise HTTPException(400, "invalid or non-http(s) url")
    try:
        r = await crawl_url(body.url, trigger="manual")
    except Exception as e:
        raise HTTPException(502, str(e))
    page = await db.page_get(body.url)
    await _ev(
        f"manual refresh — {body.url}",
        {"status": r.get("status"), "chunks": r.get("chunks"), "ms": r.get("ms")},
    )
    return {
        "ok": True,
        "url": body.url,
        "status": r.get("status"),
        "chunks": r.get("chunks"),
        "ms": r.get("ms"),
        "last_crawled": (page or {}).get("last_crawled"),
    }


# {url:path} — the URL is percent-encoded in the path, and uvicorn decodes
# scope["path"] before Starlette matches, so the param must span slashes
@app.patch("/api/pages/{url:path}")
async def api_page_patch(url: str, body: PagePatch) -> Any:
    n = await db.execute("UPDATE pages SET disabled = %s WHERE url = %s", (body.disabled, url))
    if not n:
        raise HTTPException(404, "page not in index")
    await _ev(f"page {'disabled' if body.disabled else 'enabled'}", {"url": url})
    return {"ok": True, "url": url, "disabled": body.disabled}


@app.delete("/api/pages/{url:path}")
async def api_page_delete(url: str) -> Any:
    n = await db.execute("DELETE FROM pages WHERE url = %s", (url,))
    if not n:
        raise HTTPException(404, "page not in index")
    await _ev("page deleted", {"url": url})
    return {"ok": True, "url": url, "deleted": True}


@app.get("/api/pages")
async def api_pages(sort: str = "fetch_count", limit: int = 20) -> Any:
    allowed = {
        "fetch_count": "fetch_count DESC",
        "search_hit_count": "search_hit_count DESC",
        "last_crawled": "last_crawled DESC",
        "first_seen": "first_seen DESC",
    }
    order = allowed.get(sort, allowed["fetch_count"])
    pages = await db.fetch_all(
        f"SELECT url, title, domain, fetch_count, search_hit_count, crawl_count, "
        f"last_crawled, last_status, disabled "
        f"FROM pages ORDER BY {order} LIMIT %s",
        (min(limit, 100),),
    )
    freshness = await db.fetch_all(
        """
        SELECT
          sum(CASE WHEN last_crawled >= now() - interval '1 day' THEN 1 ELSE 0 END) AS lt1d,
          sum(CASE WHEN last_crawled >= now() - interval '7 days'
                    AND last_crawled < now() - interval '1 day' THEN 1 ELSE 0 END) AS d1_7,
          sum(CASE WHEN last_crawled >= now() - interval '30 days'
                    AND last_crawled < now() - interval '7 days' THEN 1 ELSE 0 END) AS d7_30,
          sum(CASE WHEN last_crawled < now() - interval '30 days' THEN 1 ELSE 0 END) AS gt30,
          sum(CASE WHEN last_crawled IS NULL THEN 1 ELSE 0 END) AS never
        FROM pages
        """
    )
    f = freshness[0] if freshness else {}
    return {
        "pages": pages,
        "freshness": {
            "<1d": f.get("lt1d") or 0,
            "1-7d": f.get("d1_7") or 0,
            "7-30d": f.get("d7_30") or 0,
            ">30d": f.get("gt30") or 0,
            "never": f.get("never") or 0,
        },
    }


@app.get("/api/logs/crawls")
async def api_logs_crawls(limit: int = 50) -> Any:
    rows = await db.fetch_all(
        "SELECT ts, url, trigger, status, ms, chunks_written, detail "
        "FROM crawl_log ORDER BY id DESC LIMIT %s",
        (min(limit, 500),),
    )
    return {"crawls": rows}


@app.get("/api/logs/searches")
async def api_logs_searches(limit: int = 50) -> Any:
    rows = await db.fetch_all(
        "SELECT ts, query, source, local_hits, results FROM search_log "
        "ORDER BY id DESC LIMIT %s",
        (min(limit, 500),),
    )
    return {"searches": rows}


WIN_MIN_SECS = 600    # window floor: 10 minutes
WIN_MAX_SECS = 86400  # window ceiling: 24 hours


def _clamp_window(secs: int) -> int:
    return max(WIN_MIN_SECS, min(int(secs), WIN_MAX_SECS))


@app.get("/api/window")
async def api_window(secs: int = 86400) -> Any:
    """Windowed activity stats (searches + crawls), clamped to 10m..24h."""
    secs = _clamp_window(secs)
    srows = await db.fetch_all(
        "SELECT source, count(*) AS n FROM search_log "
        "WHERE ts >= now() - make_interval(secs => %s) GROUP BY source",
        (secs,),
    )
    crows = await db.fetch_all(
        "SELECT status, count(*) AS n FROM crawl_log "
        "WHERE ts >= now() - make_interval(secs => %s) GROUP BY status",
        (secs,),
    )
    by_source = {r["source"]: r["n"] for r in srows}
    s_total = sum(by_source.values())
    return {
        "secs": secs,
        "searches": {
            "total": s_total,
            "by_source": by_source,
            "local_rate": round(by_source.get("local", 0) / s_total, 3) if s_total else 0.0,
        },
        "crawls": {
            "total": sum(r["n"] for r in crows),
            "by_status": {r["status"]: r["n"] for r in crows},
        },
    }


def _short_url(url: str) -> str:
    from urllib.parse import urlparse

    p = urlparse(url)
    first = p.path.strip("/").split("/", 1)[0]
    return f"{p.netloc}/{first}" if first else p.netloc


@app.get("/api/logs")
async def api_logs(secs: int = 86400, limit: int = 200) -> Any:
    """Merged windowed log stream: crawls + searches + events, ts DESC.

    Each row: {ts, kind: crawl|search|event, message, info}.
    """
    secs = _clamp_window(secs)
    limit = max(1, min(int(limit), 500))
    cutoff = "ts >= now() - make_interval(secs => %s)"
    crawls = await db.fetch_all(
        "SELECT ts, url, trigger, status, ms, chunks_written, detail FROM crawl_log "
        f"WHERE {cutoff} ORDER BY id DESC",
        (secs,),
    )
    searches = await db.fetch_all(
        "SELECT ts, query, source, local_hits, results FROM search_log "
        f"WHERE {cutoff} ORDER BY id DESC",
        (secs,),
    )
    events = await db.fetch_all(
        f"SELECT ts, message, info FROM event_log WHERE {cutoff} ORDER BY id DESC",
        (secs,),
    )
    logs: list[dict] = []
    for c in crawls:
        logs.append({
            "ts": c["ts"],
            "kind": "crawl",
            "message": f"crawl {c['status']} — {_short_url(c['url'])}",
            "info": {
                "url": c["url"],
                "trigger": c["trigger"],
                "ms": c["ms"],
                "chunks": c["chunks_written"],
                "detail": c["detail"],
            },
        })
    for srow in searches:
        n_results = len(srow["results"] or [])
        if srow["source"] == "local":
            msg = f"search '{(srow['query'] or '')[:100]}' → local ({srow['local_hits'] or 0} hits)"
        else:
            msg = f"search '{(srow['query'] or '')[:100]}' → {srow['source']} ({n_results} results)"
        logs.append({
            "ts": srow["ts"],
            "kind": "search",
            "message": msg,
            "info": {
                "query": srow["query"],
                "source": srow["source"],
                "local_hits": srow["local_hits"],
                "results": n_results,
            },
        })
    for e in events:
        logs.append({
            "ts": e["ts"],
            "kind": "event",
            "message": e["message"],
            "info": e["info"] or {},
        })
    logs.sort(key=lambda r: r["ts"], reverse=True)
    return {"logs": logs[:limit], "total": len(logs), "secs": secs}


# ---------------------------------------------------------------------- OWUI
# Curated OpenAPI spec for OWUI's OpenAPI tool server: exposes only the three
# user-facing tools (search_web, fetch_page, fetch_pages) with clean
# operationIds, so OWUI never sees the admin endpoints (seed/refresh/providers/
# pages/logs). The spec lives in owui/openapi.json (ships inside the package)
# and is served unauthenticated — it is a public API contract; OWUI still
# sends the bearer token, and the endpoints themselves stay auth-gated.

_OWUI_SPEC_PATH = pathlib.Path(__file__).resolve().parent / "owui" / "openapi.json"


def _load_owui_spec() -> dict[str, Any]:
    with _OWUI_SPEC_PATH.open(encoding="utf-8") as f:
        spec = json.load(f)
    spec["info"]["version"] = __version__  # single source of truth (0.0.0 placeholder on disk)
    return spec


@app.get("/owui/openapi.json")
async def owui_openapi() -> Any:
    return _load_owui_spec()


# ------------------------------------------------------------------------ MCP
# mounted before the catch-all static mount; endpoint: /mcp/http
# (stateless streamable HTTP)

app.mount("/mcp", mcp_asgi(), name="mcp")


# --------------------------------------------------------------------- static
# catch-all last: serves static/index.html at / and any static assets

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
else:  # pragma: no cover
    @app.get("/")
    async def _root() -> Any:
        return {"service": "wellisearch", "docs": "/docs"}


# ---------------------------------------------------------------------- uvicorn

def main() -> None:
    import sys

    import uvicorn

    s = get_settings()
    kwargs: dict = {"host": "0.0.0.0", "port": s.BIND_PORT, "log_level": "info"}
    if sys.platform == "win32":
        # uvicorn 0.36+ forces the proactor loop on Windows; psycopg's async
        # needs the selector loop (see loopfix.py)
        kwargs["loop"] = "wellisearch.loopfix:loop_factory"
    uvicorn.run("wellisearch.app:app", **kwargs)


if __name__ == "__main__":
    main()

"""Shared response serialization + format negotiation (REST + MCP).

The pipelines (search_web / fetch_page / fetch_pages) return structured dicts —
the source of truth. The Markdown renderers (render_*_markdown in search_web.py
/ fetch.py) turn them into the default Markdown wire format. This module adds
the second, on-demand wire format and the negotiation that picks between them:

  - to_json(obj)        -> a uniform JSON envelope (indent=2, Postgres-safe)
  - resolve_format()    -> "json" | "markdown"; the explicit `format` param
                           wins over the HTTP Accept header; markdown is the
                           default when neither signals JSON.

Both the REST surface (app.py) and the MCP surface (tools.py) call the same
two functions, so a JSON request yields byte-identical output whether it
arrives over HTTP or MCP.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
from typing import Any

VALID_FORMATS = ("json", "markdown")


def format_timing(timing: dict | None) -> str | None:
    """Render a timing dict as a `Time:` header line, or None if absent.

    `timing` carries `total_ms` plus whichever legs ran: `index_ms` (Postgres
    index search), `provider_ms` (search gateway wait), `crawl_ms` (fetch
    crawl wait). Only the legs that are present are listed, so a local-only
    search shows `(index: N ms)`, an auto-mode provider search shows
    `(index: N ms, provider: M ms)`, and provider mode (index leg skipped)
    shows `(provider: M ms)`. Legs are per-leg critical paths (the max of the
    pages that ran that leg); when different pages dominate different legs the
    legs' sum can approach or slightly exceed the total.
    """
    if not timing:
        return None
    total = timing.get("total_ms", 0)
    parts = [
        f"{key[:-3]}: {timing[key]} ms" for key in ("index_ms", "provider_ms", "crawl_ms") if key in timing
    ]
    if parts:
        return f"Time: {total} ms ({', '.join(parts)})"
    return f"Time: {total} ms"


def to_json(obj: Any) -> str:
    """The structured pipeline dict as a uniform JSON envelope.

    Indented for LLM/human readability; Postgres types (datetime, Decimal)
    are coerced so the same dict serializes identically on both surfaces.
    """
    return json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default)


def resolve_format(format_param: str | None, accept_header: str | None = None) -> str:
    """Resolve the response format.

    The explicit `format` param ("json" | "markdown") wins over the HTTP
    Accept header. With no explicit param, an Accept header that asks for
    application/json (and not also for markdown) yields JSON. Otherwise
    markdown is the default. Raises ValueError if an explicit `format` is not
    one of VALID_FORMATS.
    """
    if format_param is not None:
        fmt = str(format_param).strip().lower()
        if fmt not in VALID_FORMATS:
            raise ValueError(
                f"invalid format {str(format_param)!r} (choose from {list(VALID_FORMATS)})"
            )
        return fmt
    if accept_header:
        accept = accept_header.lower()
        if "application/json" in accept and "markdown" not in accept:
            return "json"
    return "markdown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(o: Any) -> Any:
    if isinstance(o, (dt.datetime, dt.date, dt.time)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

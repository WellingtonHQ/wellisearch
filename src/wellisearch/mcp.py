"""MCP server (high-level MCPServer) — one transport, one tool surface (§7).

  * Streamable HTTP (stateless):   /mcp/http

Mount `mcp_asgi()` into the FastAPI app and drive `mcp_http_lifespan()` from
the app's lifespan (Starlette does not run a mounted sub-app's lifespan, so
the streamable session manager's task group must be entered by the outer
app). The tool handlers live in tools.py and share the exact same pipeline
code as the REST routes (one implementation, three surfaces).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from .tools import register_tools

INSTRUCTIONS = """
wellisearch — self-hosted web search + page reading for the LLM.

Workflow:
1. search_web(query) → a Markdown document: a Source/Degraded/Time header,
   then Title/URL/Snippet result blocks separated by --- lines. Local hits
   carry a Last Crawled line per result and cost zero provider credits. The
   Time line shows total ms split into index: ms and (when a provider was
   used) provider: ms. Set search_mode to choose the source: "auto" (default,
   local first then provider), "local" (index only), or "provider" (bypass
   the local index and force a live provider answer).
2. Read pages: fetch_page(url) for one, fetch_pages(urls, max_chars, strategy)
   for several under a shared char budget. Both return a Markdown document
   with a Title/URL/From Index/Chars/Truncated header plus a Time line
   (bulk adds a global Strategy/Budget/Pages Fetched/Total Chars/Truncated
   header) — not JSON.
3. If the answer is thin, rephrase and search_web again (≤2 reformulations).

Rules: never invent page content; if a fetch fails, say which URL failed and
re-search. If the header carries Degraded: true, results are local-only
(all providers failed — see the Provider Errors line).
"""

# Explicit transport security: the default auto-allowlist only covers loopback,
# which 421-rejects in-network clients (openwebui via the ``wellisearch``
# docker hostname, opencode via the Tailscale host). Used by the transport.
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "wellisearch:*",
        # bare + :port forms — the SDK's ":*" wildcard requires a port suffix
        "wellingtons-16-macbook-pro-2019.tailc2fbf4.ts.net",
        "wellingtons-16-macbook-pro-2019.tailc2fbf4.ts.net:*",
    ],
    allowed_origins=[
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "http://wellisearch:*",
        "https://wellingtons-16-macbook-pro-2019.tailc2fbf4.ts.net",
        "https://wellingtons-16-macbook-pro-2019.tailc2fbf4.ts.net:*",
    ],
)

_SERVER: MCPServer | None = None
_HTTP_APP: Starlette | None = None


def _build() -> None:
    """One MCPServer backing the transport (idempotent).

    The streamable app is stateless: no server-side session map, so a
    container restart kills nothing.
    """
    global _SERVER, _HTTP_APP
    if _SERVER is not None:
        return
    server = MCPServer("wellisearch", instructions=INSTRUCTIONS)
    register_tools(server)
    _HTTP_APP = server.streamable_http_app(
        streamable_http_path="/http",
        stateless_http=True,
        json_response=False,
        transport_security=TRANSPORT_SECURITY,
    )
    _SERVER = server


_build()


def mcp_asgi() -> Starlette:
    """Stateless streamable HTTP ASGI app: /mcp/http.

    Mount at "/mcp" (§7); the auth middleware's startswith("/mcp") prefix
    covers the endpoint.
    """
    assert _HTTP_APP is not None
    return _HTTP_APP


@asynccontextmanager
async def mcp_http_lifespan() -> AsyncIterator[None]:
    """Hold the streamable-HTTP session manager's task group for app lifetime.

    Starlette does not run lifespans of mounted sub-apps, and without an
    entered task group the first POST to /mcp/http 500s with
    "Task group is not initialized. Make sure to use run()".
    """
    assert _SERVER is not None
    async with _SERVER.session_manager.run():
        yield

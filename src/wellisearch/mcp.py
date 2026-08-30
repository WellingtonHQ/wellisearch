"""MCP server (high-level MCPServer) — one transport, one tool surface (§7).

  * Streamable HTTP (stateless):   /mcp/http

Mount `mcp_asgi()` into the FastAPI app and drive `mcp_http_lifespan()` from
the app's lifespan (Starlette does not run a mounted sub-app's lifespan, so
the streamable session manager's task group must be entered by the outer
app). The SDK's session manager `run()` is one-shot per instance, so each
lifespan entry builds a fresh MCPServer + Starlette app; the mounted app
resolves the active one at request time. The tool handlers live in tools.py
and share the exact same pipeline code as the REST routes (one implementation,
three surfaces).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.types import Receive, Scope, Send

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


# ----------------------------------------------------------------------- runtime


class _Runtime:
    """One MCPServer plus the Starlette app bound to its session manager."""

    __slots__ = ("app", "server")

    def __init__(
        self,
        server: MCPServer,
        app: Starlette,
    ) -> None:
        self.server = server
        self.app = app


def _build_runtime() -> _Runtime:
    """Fresh MCPServer + its streamable-HTTP Starlette app (per lifespan entry).

    The streamable app is stateless: no server-side session map, so a
    container restart kills nothing. A new server per entry is required: the
    SDK's session manager `run()` may only be entered once per instance.
    """
    server = MCPServer("wellisearch", instructions=INSTRUCTIONS)
    register_tools(server)
    app = server.streamable_http_app(
        streamable_http_path="/http",
        stateless_http=True,
        json_response=False,
        transport_security=TRANSPORT_SECURITY,
    )
    return _Runtime(server, app)


async def _respond_not_started(scope: Scope, send: Send) -> None:
    """503 for requests that arrive outside a lifespan (no active manager)."""
    if scope.get("type") != "http":
        raise RuntimeError("MCP server not started: lifespan was not entered")
    body = b'{"error": "MCP server not started: lifespan was not entered"}'
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": 503, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _MCPMount:
    """Stable ASGI app: dispatches each request to the active session manager.

    The mount is created once at import time, but the SDK's session manager
    is one-shot, so the concrete Starlette app is rebuilt per lifespan entry;
    this object just tracks whichever one is live right now.
    """

    def __init__(self) -> None:
        self._runtime: _Runtime | None = None

    def set_runtime(self, runtime: _Runtime | None) -> None:
        self._runtime = runtime

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        runtime = self._runtime
        if runtime is None:
            await _respond_not_started(scope, send)
            return
        await runtime.app(scope, receive, send)


_MOUNT = _MCPMount()


# ------------------------------------------------------------------------ public


def mcp_asgi() -> _MCPMount:
    """Stateless streamable HTTP ASGI app: /mcp/http.

    Mount at "/mcp" (§7); the auth middleware's startswith("/mcp") prefix
    covers the endpoint. The returned app is stable across lifespan
    restarts — it resolves the active session manager per request.
    """
    return _MOUNT


@asynccontextmanager
async def mcp_http_lifespan() -> AsyncIterator[None]:
    """Hold the streamable-HTTP session manager's task group for app lifetime.

    Starlette does not run lifespans of mounted sub-apps, and without an
    entered task group the first POST to /mcp/http 500s with
    "Task group is not initialized. Make sure to use run()".

    A fresh MCPServer is built per entry: the SDK's `run()` is one-shot per
    instance (a second entry raises RuntimeError), so reusing an import-time
    server would crash any second lifespan start in the same process
    (uvicorn --reload, a second TestClient, an in-process restart).
    """
    runtime = _build_runtime()
    _MOUNT.set_runtime(runtime)
    try:
        async with runtime.server.session_manager.run():
            yield
    finally:
        _MOUNT.set_runtime(None)

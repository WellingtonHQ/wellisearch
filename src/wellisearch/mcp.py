"""MCP server (mcp 2.0.0 high-level MCPServer) — SSE at /mcp/sse (BLUEPRINT §7).

Mount `mcp_asgi()` into the FastAPI app; the tool handlers live in tools.py
and share the exact same pipeline code as the REST routes (one implementation,
two surfaces).
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from .tools import register_tools

INSTRUCTIONS = """
wellisearch — self-hosted web search + page reading for the LLM.

Workflow:
1. search_web(query) → a Markdown document: a Source/Degraded header, then
   Title/URL/Snippet result blocks separated by --- lines. Local hits carry a
   Last Crawled line per result and cost zero provider credits.
 2. Read pages: fetch_page(url) for one, fetch_pages(urls, max_chars, strategy)
    for several under a shared char budget. Both return a Markdown document
    with a Title/URL/From Index/Chars/Truncated header (bulk adds a global
    Strategy/Budget/Pages Fetched/Total Chars/Truncated header) — not JSON.
3. If the answer is thin, rephrase and search_web again (≤2 reformulations).

Rules: never invent page content; if a fetch fails, say which URL failed and
re-search. If the header carries Degraded: true, results are local-only
(all providers failed — see the Provider Errors line).
"""


def build_server() -> MCPServer:
    server = MCPServer("wellisearch", instructions=INSTRUCTIONS)
    register_tools(server)
    return server


def mcp_asgi() -> Starlette:
    """ASGI app exposing the MCP SSE endpoint.

    Mount at "/mcp" → endpoints become /mcp/sse and /mcp/messages/ (§7).

    Explicit transport security: the default auto-allowlist only covers
    loopback, which 421-rejects in-network clients (e.g. openwebui connecting via
    the ``wellisearch`` docker hostname).
    """
    return build_server().sse_app(
        sse_path="/sse",
        message_path="/messages/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1:*",
                "localhost:*",
                "[::1]:*",
                "wellisearch:*",
                "wellingtons-16-macbook-pro-2019.tailc2fbf4.ts.net:*",
            ],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                "http://wellisearch:*",
                "https://wellingtons-16-macbook-pro-2019.tailc2fbf4.ts.net:*",
            ],
        ),
    )

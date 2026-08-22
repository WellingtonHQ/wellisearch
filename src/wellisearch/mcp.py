"""MCP server (mcp 2.0.0 high-level MCPServer) — SSE at /mcp/sse (BLUEPRINT §7).

Mount `mcp_asgi()` into the FastAPI app; the tool handlers live in tools.py
and share the exact same pipeline code as the REST routes (one implementation,
two surfaces).
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

from .tools import register_tools

INSTRUCTIONS = """
wellisearch — self-hosted web search + page reading for the LLM.

Workflow:
1. search_web(query) → results Markdown block + metadata (source, degraded, count).
2. Read pages: fetch_page(url) for one, fetch_pages(urls, max_chars, strategy)
   for several under a shared char budget.
3. If the answer is thin, rephrase and search_web again (≤2 reformulations).

Rules: never invent page content; if a fetch fails, say which URL failed and
re-search. If a search response carries degraded:true, results are local-only.
"""


def build_server() -> MCPServer:
    server = MCPServer("wellisearch", instructions=INSTRUCTIONS)
    register_tools(server)
    return server


def mcp_asgi() -> Starlette:
    """ASGI app exposing the MCP SSE endpoint.

    Mount at "/mcp" → endpoints become /mcp/sse and /mcp/messages/ (§7).
    """
    return build_server().sse_app(
        sse_path="/sse",
        message_path="/messages/",
    )

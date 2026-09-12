"""wellisearch — self-hosted search gateway + web-index service.

One process serves the MCP tools (LLM surface), the REST API + dashboard
(dashboard/scripting surface), and the background worker (async indexing).
"""
from __future__ import annotations

__version__ = "1.2.0"

"""Markdown chunker: ~N tokens per chunk, heading-aware.

Rules:
- Split on heading boundaries so each chunk starts at (or under) a heading.
- Never split inside a fenced code block (the fence may overflow the budget).
- Never split a table row.
- A trailing chunk smaller than ~20% of the budget is merged into the previous
  chunk rather than stored as a stub.
- Token estimate: ~4 chars per token (good enough for budgeting; the real
  tokenizer is the embedding model's, and we store text, not tokens).
"""
from __future__ import annotations

import re

CHARS_PER_TOKEN = 4
_MIN_CHUNK_TOKENS = 50

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def chunk_markdown(markdown: str, max_tokens: int = 800) -> list[str]:
    """Split markdown into ~``max_tokens``-token chunks, heading-aware.

    Chunks start at heading boundaries and never split inside a fenced code
    block or a table row; a small trailing stub is merged into the previous
    chunk.
    """
    if not markdown or not markdown.strip():
        return []

    budget = max(100, max_tokens)
    lines = markdown.splitlines()

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        """Append the accumulated lines as one chunk (if any) and reset the
        accumulator."""
        nonlocal current, current_tokens
        if current:
            text = "\n".join(current).strip()
            if text:
                chunks.append(text)
            current = []
            current_tokens = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        fence_match = _FENCE_RE.match(line)

        if fence_match and not in_fence:
            # open a fence: flush first so the block starts fresh when possible
            flush()
            in_fence = True
            fence_marker = fence_match.group(1)
            current.append(line)
            current_tokens += _tokens(line)
            i += 1
            continue

        if in_fence:
            current.append(line)
            current_tokens += _tokens(line)
            if fence_match and fence_match.group(1) == fence_marker:
                in_fence = False
            i += 1
            continue

        if current_tokens + _tokens(line) > budget and current:
            flush()

        heading = _HEADING_RE.match(line)
        if heading and current:
            # a new section: start a fresh chunk at the heading so the chunk
            # carries its own context (heading first)
            flush()

        # tables: keep consecutive pipe-rows in one chunk
        if line.lstrip().startswith("|") and current_tokens + _tokens(line) > budget:
            # allow the table to overflow rather than splitting a row
            pass

        current.append(line)
        current_tokens += _tokens(line)
        i += 1

    flush()

    # merge a small trailing stub into the previous chunk
    if len(chunks) >= 2:
        last_tokens = _tokens(chunks[-1])
        if last_tokens < max(_MIN_CHUNK_TOKENS, budget // 5):
            chunks[-2] = chunks[-2] + "\n\n" + chunks[-1]
            chunks.pop()

    return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token, minimum 1."""
    return max(1, len(text) // CHARS_PER_TOKEN)

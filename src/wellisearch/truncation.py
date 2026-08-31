"""Swappable, boundary-safe truncation strategies for fetch_pages (plan §7).

Strategies:
  smart    — allocate budget by prominence (fetch_count; falls back to even
             when no prominence exists — §15), head-trim each page
  head     — budget proportional to page length; keep the FIRST n chars
  tail     — budget proportional to page length; keep the LAST n chars
  even     — budget split evenly; head-trim each
  priority — budget proportional to fetch_count (+1); head-trim each

Every cut is boundary-safe: it lands on whitespace (never mid-word), and if
the raw cut would land inside an HTML tag, it backs up to the previous `>`.
Every trimmed page carries a `[truncated — N chars omitted, strategy=X]`
marker.
"""
from __future__ import annotations

from dataclasses import dataclass

STRATEGIES = ("smart", "head", "tail", "even", "priority")


def boundary_cut_head(text: str, n: int) -> str:
    """Keep the first n chars, backed up to a safe boundary."""
    if n <= 0:
        return ""
    if n >= len(text):
        return text
    cut = text[:n]
    if cut.count("<") > cut.count(">"):
        gt = cut.rfind(">")
        if gt > 0:
            cut = cut[: gt + 1]
    best = max(cut.rfind("\n"), cut.rfind(" "), cut.rfind("\t"))
    if best > n // 2:
        cut = cut[:best]
    return cut.rstrip()


def boundary_cut_tail(text: str, n: int) -> str:
    """Keep the last n chars, advanced to a safe boundary."""
    if n <= 0:
        return ""
    if n >= len(text):
        return text
    start = len(text) - n
    i = start
    while i < len(text):
        ch = text[i]
        if ch in " \n\t":
            seg = text[i:]
            if seg.count("<") <= seg.count(">"):
                return seg.lstrip()
        i += 1
    return text


def allocate_budgets(
    strategy: str,
    page_lens: list[int],
    page_weights: list[int],
    budget: int | None,
    per_page: int | None,
) -> list[int]:
    """Per-page char budgets. `budget` = total cap (None = unlimited)."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r} (choose from {STRATEGIES})")
    n = len(page_lens)
    if n == 0:
        return []
    if budget is None:
        return list(page_lens)

    lens = [max(1, l) for l in page_lens]
    total_len = sum(lens)

    if strategy == "even":
        weights = [1.0 / n] * n
    elif strategy in ("head", "tail"):
        weights = [l / total_len for l in lens]
    else:  # smart | priority → prominence-based
        w = [max(1, 1 + int(w)) for w in page_weights]
        if max(int(wi) for wi in page_weights) <= 0:
            # no prominence signal (all fetch_count=0) → §15: fall back to even
            weights = [1.0 / n] * n
        else:
            sw = sum(w)
            weights = [x / sw for x in w]

    shares = [budget * w for w in weights]
    if per_page is not None:
        shares = [min(sh, per_page) for sh in shares]
    # never allocate more than the page actually has
    return [max(0, min(int(sh), lens[i])) for i, sh in enumerate(shares)]


def truncate_page(
    content: str,
    chars: int,
    strategy: str,
) -> tuple[str, bool]:
    """Trim one page to `chars` using the strategy's trim side.

    Returns (text, was_truncated).
    """
    if chars <= 0:
        return "", True
    if len(content) <= chars:
        return content, False
    if strategy == "tail":
        return boundary_cut_tail(content, chars), True
    # smart/head/even/priority all keep the lead (article lead = best
    # generic signal-per-char without a query context)
    return boundary_cut_head(content, chars), True


def truncation_marker(omitted: int, strategy: str) -> str:
    """The `[truncated — N chars omitted, strategy=X]` marker carried by trimmed pages."""
    return f"[truncated — {omitted} chars omitted, strategy={strategy}]"


@dataclass(slots=True)
class TruncatedPage:
    """A page after budget allocation: kept text, chars used, and chars omitted."""
    url: str
    title: str
    text: str
    chars_used: int
    truncated: bool
    omitted: int

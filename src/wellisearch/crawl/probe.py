"""On-demand probe budget for the read path (fetch_page/fetch_pages)."""
from __future__ import annotations

import contextvars

_probe_s: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "crawl_probe_s", default=None
)


def get_probe_s() -> float | None:
    """The active per-tier probe budget in seconds; None = full (uncapped)."""
    return _probe_s.get()


def clamp(seconds: float) -> float:
    """Cap `seconds` by the active probe budget; unchanged when no budget is set."""
    cap = get_probe_s()
    if cap is None:
        return float(seconds)
    return min(float(seconds), float(cap))


def set_probe_budget(seconds: float | None) -> contextvars.Token:
    """Set the probe budget for the current task; returns a token for reset()."""
    return _probe_s.set(seconds)


def reset_probe_budget(token: contextvars.Token) -> None:
    """Restore the budget that was in effect before the matching set call."""
    _probe_s.reset(token)

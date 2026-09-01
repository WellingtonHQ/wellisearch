"""Crawl lane context: 'fast' (default) or 'cf' (challenge lane).

The worker sets the lane per task so the browser tier, pool, semaphore, and
timeout can all branch on which lane is running. A contextvar (not a module
global) keeps concurrent tasks in the same loop from leaking each other's lane:
each task copies the current context, so a set_lane() inside one task is invisible
to the others.
"""
from __future__ import annotations

import contextvars

FAST = "fast"
CF = "cf"

_lane: contextvars.ContextVar[str] = contextvars.ContextVar("crawl_lane", default=FAST)


def get_lane() -> str:
    """The current lane for this task ('fast' unless a worker set 'cf')."""
    return _lane.get()


def set_lane(lane: str) -> contextvars.Token:
    """Set the lane for the current task; returns a token for reset_lane()."""
    return _lane.set(lane)


def reset_lane(token: contextvars.Token) -> None:
    """Restore the lane to what it was before the matching set_lane()."""
    _lane.reset(token)

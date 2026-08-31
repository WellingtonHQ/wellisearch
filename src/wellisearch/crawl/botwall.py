"""Bot-wall / challenge detection shared by the tier ladder (design §3.2).

is_botwall() keeps bot-wall (escalate to a higher tier) distinct from
thin-but-real content (is_thin() — accept + flag).
"""
from __future__ import annotations

import re

CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "cf-challenge",
    "challenge-platform",
    "cf-turnstile",
    "pardon our interruption",
    "access denied",
    "are you a robot",
    "robot or human",
    "attention required",
    "unusual traffic",
    "request blocked",
)


def is_botwall(html: str, status: int) -> str | None:
    """First challenge marker in the html (word-boundary matched), or the
    http status when it is >= 400; None when the page looks clean."""
    if status >= 400:
        return f"http_{status}"
    low = html.lower()
    for marker in CHALLENGE_MARKERS:
        if re.search(r"\b" + re.escape(marker) + r"\b", low):
            return marker
    return None


def is_thin(html: str, min_chars: int) -> bool:
    """True when the stripped html is shorter than min_chars."""
    return len(html.strip()) < min_chars

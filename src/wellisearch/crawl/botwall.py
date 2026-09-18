"""Bot-wall / challenge detection shared by the tier ladder (design §3.2).

is_botwall() flags a challenge page so the engine can escalate to a higher tier.
The marker scan only runs on HTML responses: non-HTML bodies (raw source files,
JSON APIs) may legitimately contain marker-like strings ("access denied", ...)
and must not be treated as walls — e.g. a crawler library's own bot-wall code.
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
    "javascript is disabled",
    "verify that you're not a robot",
)

# MIME types that are HTML documents; any other known type skips the marker scan.
_HTML_MIME_TYPES = ("application/xhtml+xml", "text/html")


def is_botwall(
    html: str,
    status: int,
    content_type: str | None = None,
) -> str | None:
    """First challenge marker in the html (word-boundary matched), or the http
    status when it is >= 400; None when the page looks clean. The marker scan
    is skipped for non-HTML content types (a raw .py file containing "access
    denied" is code, not a wall); an absent/unknown type still gets scanned."""
    if status >= 400:
        return f"http_{status}"
    if _is_non_html(content_type):
        return None
    low = html.lower()
    for marker in CHALLENGE_MARKERS:
        if re.search(r"\b" + re.escape(marker) + r"\b", low):
            return marker
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_non_html(content_type: str | None) -> bool:
    """True when the content type is present and clearly not an HTML document."""
    if not content_type:
        return False
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime != "" and mime not in _HTML_MIME_TYPES

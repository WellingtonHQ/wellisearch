"""Bot-wall / challenge detection shared by the tier ladder (design §3.2).

is_botwall() flags a challenge page so the engine can escalate to a higher tier.
The marker scan only runs on HTML responses: non-HTML bodies (raw source files,
JSON APIs) may legitimately contain marker-like strings ("access denied", ...)
and must not be treated as walls — e.g. a crawler library's own bot-wall code.

Two false-positive classes are guarded against (both seen in production):
- Phrase markers ("access denied", ...) inside <script> blobs, code samples,
  or <noscript> warnings on otherwise clean pages: phrase markers only count
  when they appear as visible page text.
- CF asset names ("challenge-platform", "cf-turnstile") embedded by sites that
  use Turnstile / managed challenges normally (form widgets, jsd bootstrap):
  structural markers only count on interstitial-sized pages (little visible
  text), where a real challenge wall lives.
"""
from __future__ import annotations

import re

# Human-readable wall copy: must appear as visible page text to count.
PHRASE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "pardon our interruption",
    "access denied",
    "are you a robot",
    "robot or human",
    "attention required",
    "unusual traffic",
    "request blocked",
    "javascript is disabled",
    "prove your humanity",  # reddit's reCAPTCHA interstitial (title + body text)
    "verify that you're not a robot",
)

# CF internal asset names: live in scripts/attributes, so they only count on
# interstitial-sized pages — content-rich pages embed them legitimately.
STRUCTURAL_MARKERS: tuple[str, ...] = (
    "cf-challenge",
    "challenge-platform",
    "cf-turnstile",
)

CHALLENGE_MARKERS: tuple[str, ...] = PHRASE_MARKERS + STRUCTURAL_MARKERS

# MIME types that are HTML documents; any other known type skips the marker scan.
_HTML_MIME_TYPES = ("application/xhtml+xml", "text/html")

# Max visible-text length (chars) for a page to count as an interstitial when
# structural markers are present. Real CF walls render ~50-200 chars of text;
# content pages with embedded CF assets run into the thousands.
_STRUCTURAL_MAX_VISIBLE_CHARS = 1000

_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.IGNORECASE | re.DOTALL)
_NOSCRIPT_RE = re.compile(r"<noscript\b.*?</noscript>", re.IGNORECASE | re.DOTALL)
_CODE_BLOCK_RE = re.compile(r"</?(?:pre|code)\b[^>]*>.*?</(?:pre|code)>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ATTR_RE = re.compile(r"\s+[a-zA-Z][\w-]*\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_TAG_RE = re.compile(r"<[^>]+>")


def is_botwall(
    html: str,
    status: int,
    content_type: str | None = None,
) -> str | None:
    """First challenge marker in the html (word-boundary matched), or the http
    status when it is >= 400; None when the page looks clean. The marker scan
    is skipped for non-HTML content types (a raw .py file containing "access
    denied" is code, not a wall); an absent/unknown type still gets scanned.
    Phrase markers are matched against visible text only (scripts, styles,
    code samples, and <noscript> warnings never count); structural CF asset
    names only count on interstitial-sized pages."""
    if status >= 400:
        return f"http_{status}"
    if _is_non_html(content_type):
        return None
    visible = _visible_text(html).lower()
    for marker in PHRASE_MARKERS:
        if re.search(r"\b" + re.escape(marker) + r"\b", visible):
            return marker
    if len(visible) <= _STRUCTURAL_MAX_VISIBLE_CHARS:
        low = html.lower()
        for marker in STRUCTURAL_MARKERS:
            if re.search(r"\b" + re.escape(marker) + r"\b", low):
                return marker
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _visible_text(html: str) -> str:
    """Reduce html to its visible text: drop script/style/noscript/code
    content and attribute values, then strip the remaining tags."""
    for rx in (_SCRIPT_RE, _STYLE_RE, _NOSCRIPT_RE, _CODE_BLOCK_RE, _COMMENT_RE):
        html = rx.sub(" ", html)
    html = _ATTR_RE.sub(" ", html)
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def _is_non_html(content_type: str | None) -> bool:
    """True when the content type is present and clearly not an HTML document."""
    if not content_type:
        return False
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime != "" and mime not in _HTML_MIME_TYPES

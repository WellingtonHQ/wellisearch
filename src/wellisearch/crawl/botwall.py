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
    "you've been blocked by network security",
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

_BLOCK_TAGS = ("!--", "code", "noscript", "pre", "script", "style")
_BLOCK_OPEN_RE = re.compile(r"<!--|<(code|noscript|pre|script|style)\b", re.IGNORECASE)
_BLOCK_CLOSE_RES = {
    tag: re.compile(r"</" + tag + r"\s*>", re.IGNORECASE)
    for tag in _BLOCK_TAGS if tag != "!--"
}
_TAG_PART_RE = re.compile(r"[<>\"']")


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


def strip_html_blocks(html: str, tags: tuple[str, ...]) -> str:
    """Strip selected hidden blocks with forward-only scans, including unclosed ones.

    A lazy '.*?</script>' substitution retries the remaining document for every
    unmatched opening tag. One forward closing-tag search per consumed block
    avoids that quadratic behavior on malformed or adversarial pages.
    """
    out: list[str] = []
    pos = 0
    scan = 0
    while opening := _BLOCK_OPEN_RE.search(html, scan):
        scan = opening.end()
        tag = (opening.group(1) or "!--").lower()
        if tag not in tags:
            continue
        if tag == "!--":
            end = html.find("-->", scan)
            scan = len(html) if end == -1 else end + len("-->")
        else:
            closing = _BLOCK_CLOSE_RES[tag].search(html, scan)
            scan = len(html) if closing is None else closing.end()
        out.extend((html[pos:opening.start()], " "))
        pos = scan
    out.append(html[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _visible_text(html: str) -> str:
    """Reduce html to its visible text: drop script/style/noscript/code
    content and attribute values, then strip the remaining tags."""
    html = strip_html_blocks(html, _BLOCK_TAGS)
    return re.sub(r"\s+", " ", _strip_tags(html)).strip()


def _strip_tags(html: str) -> str:
    """Strip tags/attributes in one pass, including quoted '>' and unclosed tags."""
    out: list[str] = []
    pos = 0
    tag_start: int | None = None
    quote: str | None = None
    for token in _TAG_PART_RE.finditer(html):
        char = token.group()
        if tag_start is None:
            if char == "<":
                tag_start = token.start()
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("\"", "'"):
            quote = char
        elif char == ">":
            out.extend((html[pos:tag_start], " "))
            pos = token.end()
            tag_start = None
    out.append(html[pos:] if tag_start is None else html[pos:tag_start])
    return "".join(out)


def _is_non_html(content_type: str | None) -> bool:
    """True when the content type is present and clearly not an HTML document."""
    if not content_type:
        return False
    mime = content_type.split(";", 1)[0].strip().lower()
    return mime != "" and mime not in _HTML_MIME_TYPES

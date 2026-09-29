"""URL canonicalization for storage keys.

The same page is reachable under many URLs — tracking parameters (refId,
utm_*, …), HTML-escaped ampersands (&amp;), locale subdomains, and path slugs
that repeat the job/product ID. store_page and queue_enqueue normalize every
URL through normalize_url so one page = one row; merge_dupes.py collapses
rows already stored under variant URLs.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that carry no content: ad/campaign tracking + referrers.
# utm_* is handled by prefix; the rest are exact (lowercased) matches.
_TRACKING_PARAMS = frozenset({
    "cmpid", "dclid", "fbclid", "gbraid", "gclid", "gi", "hscta_tracker",
    "icid", "igshid", "mkt_tok", "msclkid", "refid", "scm", "share_id",
    "spm", "trackingid", "twclid", "wbraid", "yclid",
})

# LinkedIn job pages: /jobs/view/<slug>-<id> and locale subdomains all point
# at the same posting; canonical form is www.linkedin.com/jobs/view/<id>.
_LINKEDIN_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")
_LINKEDIN_JOB_PATH_RE = re.compile(r"^/jobs/view/([^\s/?#]+)/?$")


def normalize_url(url: str) -> str:
    """Canonical storage form of a URL.

    Unescapes HTML entities (&amp; → &), lowercases the host, drops the
    fragment, removes tracking query parameters (utm_*, refId, …), and
    applies site-specific canonical rules (LinkedIn job postings).
    Content-defining parameters are kept."""
    u = unescape(url.strip())
    linkedin = _linkedin_job_canonical(u)
    if linkedin is not None:
        return linkedin
    parts = urlsplit(u)
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if _is_content_param(k)
    ]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(kept), ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_content_param(name: str) -> bool:
    """True when a query parameter defines content (not tracking)."""
    low = name.lower()
    return not (low.startswith("utm_") or low in _TRACKING_PARAMS)


def _linkedin_job_canonical(url: str) -> str | None:
    """The canonical LinkedIn job URL, or None when the URL is not one."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if _LINKEDIN_HOST_RE.match(parts.netloc.lower()) is None:
        return None
    m = _LINKEDIN_JOB_PATH_RE.match(parts.path)
    if m is None:
        return None
    idm = re.search(r"(\d+)$", m.group(1))
    if idm is None:
        return None
    return f"https://www.linkedin.com/jobs/view/{idm.group(1)}"

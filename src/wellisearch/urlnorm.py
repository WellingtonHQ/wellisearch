"""URL canonicalization for storage keys.

The same page is reachable under many URLs — tracking parameters (refId,
utm_*, …), HTML-escaped ampersands (&amp;), locale subdomains, and path slugs
that repeat the job/product ID. store_page and queue_enqueue normalize every
URL through normalize_url so one page = one row; merge_dupes.py collapses
rows already stored under variant URLs.
"""
from __future__ import annotations

from html import unescape
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that carry no content on any host: industry-standard
# ad/campaign click IDs + referrers. utm_* is handled by prefix; the rest are
# exact (lowercased) matches.
_UNIVERSAL_TRACKING_PARAMS = frozenset({
    "cmpid", "dclid", "fbclid", "gbraid", "gclid", "hscta_tracker",
    "icid", "igshid", "mkt_tok", "msclkid", "psc", "refid", "share_id",
    "trackingid", "twclid", "wbraid", "yclid",
})

# Host-scoped tracking: these names can define content on other sites, so
# they only count as tracking where the site is known to append them.
_AMAZON_TRACKING = frozenset({"edk", "gi", "ref", "ref_", "rsd", "scm", "social_share"})
_ALIBABA_TRACKING = frozenset({"scm", "spm"})

_HOST_TRACKING_PARAMS: dict[str, frozenset[str]] = {
    # Amazon's a.co redirect chain appends ref/ref_/social_share/rsd/edk
    # (plus gi and scm) to product URLs.
    "a.co": _AMAZON_TRACKING,
    "amazon.com": _AMAZON_TRACKING,
    # Alibaba-family hosts append spm/scm campaign codes.
    "1688.com": _ALIBABA_TRACKING,
    "aliexpress.com": _ALIBABA_TRACKING,
    "alibaba.com": _ALIBABA_TRACKING,
    "taobao.com": _ALIBABA_TRACKING,
    "tmall.com": _ALIBABA_TRACKING,
}

# LinkedIn job pages: /jobs/view/<slug>-<id> and locale subdomains all point
# at the same posting; canonical form is www.linkedin.com/jobs/view/<id>.
_LINKEDIN_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")
_LINKEDIN_JOB_PATH_RE = re.compile(r"^/jobs/view/([^\s/?#]+)/?$")


def normalize_url(url: str) -> str:
    """Canonical storage form of a URL.

    Unescapes HTML entities (&amp; → &), lowercases the host, drops the
    fragment, removes tracking query parameters (universal ad-click IDs plus
    host-scoped ones such as Amazon's ref/scm), and applies site-specific
    canonical rules (LinkedIn job postings). Content-defining parameters are
    kept."""
    u = unescape(url.strip())
    linkedin = _linkedin_job_canonical(u)
    if linkedin is not None:
        return linkedin
    parts = urlsplit(u)
    host = parts.netloc.lower()
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if _is_content_param(k, host)
    ]
    return urlunsplit((parts.scheme, host, parts.path, urlencode(kept), ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_content_param(name: str, host: str) -> bool:
    """True when a query parameter defines content (not tracking)."""
    low = name.lower()
    if low.startswith("utm_") or low in _UNIVERSAL_TRACKING_PARAMS:
        return False
    for base, params in _HOST_TRACKING_PARAMS.items():
        if (host == base or host.endswith("." + base)) and low in params:
            return False
    return True


def _linkedin_job_canonical(url: str) -> str | None:
    """The canonical LinkedIn job URL, or None when the url is not one."""
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

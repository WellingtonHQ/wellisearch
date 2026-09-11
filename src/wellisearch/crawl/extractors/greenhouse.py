"""GreenhouseExtractor: Greenhosted careers boards (design §3.2).

Anchors on the JobPosting JSON-LD embedded in Greenhouse job pages, falling
back to the server-rendered .job-description div. Generic content extraction
loses list structure (the duties/requirements bullets) and leaks footer cookie
boilerplate into what must be a full job description, so the known page
structure is parsed instead of run through a content extractor.

Registered under greenhouse.io (covers boards.greenhouse.io & friends) plus
known custom careers domains that run Greenhouse.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup
from markdownify import markdownify as mdify

from ..results import Fitted, Rendered
from . import register
from .base import trim_md

# Real job descriptions clear this comfortably; bot-wall stubs and empty board
# pages do not. Kept above the generic gate so a thin render escalates instead
# of being stored as if it were a full posting.
MIN_JOB_DESCRIPTION_CHARS = 400

_LI_RE = re.compile(r"<li[\s>/]", re.IGNORECASE)


class GreenhouseExtractor:
    """Greenhouse job page: element-anchored extraction + site gate."""

    name = "greenhouse"

    def fit(self, r: Rendered) -> Fitted:
        """Fit a Greenhouse job page into structured markdown (title, meta, body)."""
        soup = _soup(r.html)
        posting = _job_posting(soup) or {}
        desc_html = _description_html(posting, soup)
        body = mdify(desc_html).strip() if desc_html else ""
        title = _title(posting, soup)

        parts: list[str] = []
        if title:
            parts.append(f"# {title}")
        meta = _meta_line(_location(posting), _employment_type(posting))
        if meta:
            parts.append(meta)
        if body:
            parts.append(body)

        return Fitted(
            md=trim_md("\n\n".join(parts)),
            title=title or r.title,
            signals={"bullets": len(_LI_RE.findall(desc_html))},
            flags={"extractor": "greenhouse"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a real job description body (length)."""
        return len(f.md.strip()) >= MIN_JOB_DESCRIPTION_CHARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _soup(html: str) -> BeautifulSoup:
    """Parse HTML into a BeautifulSoup tree."""
    return BeautifulSoup(html, "lxml")


def _job_posting(soup: BeautifulSoup) -> dict | None:
    """First JobPosting JSON-LD block in the page; None when absent or broken."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (TypeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and str(item.get("@type", "")).lower() == "jobposting":
                return item
    return None


def _description_html(posting: dict, soup: BeautifulSoup) -> str:
    """Description HTML: JobPosting JSON-LD (string or list), else .job-description."""
    desc = posting.get("description")
    if isinstance(desc, str):
        return desc.strip()
    if isinstance(desc, list):
        joined = "".join(d for d in desc if isinstance(d, str)).strip()
        if joined:
            return joined
    el = soup.select_one(".job-description")
    if el is not None:
        return el.decode_contents().strip()
    return ""


def _title(posting: dict, soup: BeautifulSoup) -> str | None:
    """Job title from JSON-LD first, falling back to <h1>."""
    t = posting.get("title")
    if isinstance(t, str) and t.strip():
        return t.strip()
    h1 = soup.find("h1")
    if h1 is not None:
        text = h1.get_text(" ", strip=True)
        if text:
            return text
    return None


def _location(posting: dict) -> str | None:
    """First jobLocation as 'Remote, Nationwide, US' style text; None when absent."""
    locs = posting.get("jobLocation")
    if not isinstance(locs, list):
        return None
    for loc in locs:
        addr = (loc or {}).get("address")
        if not isinstance(addr, dict):
            continue
        seen: list[str] = []
        primary = _addr_piece(addr.get("streetAddress")) or _addr_piece(addr.get("addressLocality"))
        candidates = (primary, _addr_piece(addr.get("addressRegion")), _addr_piece(addr.get("addressCountry")))
        for piece in candidates:
            if piece and piece not in seen:
                seen.append(piece)
        if seen:
            return ", ".join(seen)
    return None


def _employment_type(posting: dict) -> str | None:
    """Employment type humanized ('FULL_TIME' → 'Full Time'); None when absent."""
    et = posting.get("employmentType")
    if not isinstance(et, str) or not et.strip():
        return None
    return et.replace("_", " ").title()


def _addr_piece(value: object) -> str | None:
    """A trimmed address field; None when absent or blank."""
    if not isinstance(value, str):
        return None
    t = " ".join(value.split())
    return t or None


def _meta_line(location: str | None, employment_type: str | None) -> str | None:
    """Bolded meta line for location/type; None when both are absent."""
    bits = []
    if location:
        bits.append(f"**Location:** {location}")
    if employment_type:
        bits.append(f"**Type:** {employment_type}")
    return " — ".join(bits) or None


_GREENHOUSE = GreenhouseExtractor()
register(_GREENHOUSE, "careers.ascensus.com")  # Ascensus careers board (custom domain)
register(_GREENHOUSE, "greenhouse.io")  # boards.greenhouse.io and other subdomains

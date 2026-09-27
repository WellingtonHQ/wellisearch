"""RedditExtractor: post body and rendered comment thread for Reddit posts.

Reddit's initial HTML has the post but no comment elements. The Reddit crawl
policy uses a browser so the comment thread can hydrate before extraction.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as mdify

from ...config import get_settings
from ..results import Fitted, Rendered
from . import register
from .base import GenericExtractor, trim_md

_POST_PATH_RE = re.compile(r"^/(?:r/[^/]+/)?comments/[^/]+", re.IGNORECASE)

# Walls an anonymous browser can never hydrate (Reddit's NSFW gate). Their stored
# markdown will never gain a comments heading, so needs_refresh must not loop on them.
_NON_HYDRATABLE_MARKERS = ("this post contains mature content",)


class RedditExtractor:
    """Extract the post and highest-ranked rendered comments."""

    name = "reddit"

    def fit(self, r: Rendered) -> Fitted:
        """Keep post content and comments ranked by score or Reddit Best."""
        soup = BeautifulSoup(r.html, "lxml")
        post = soup.select_one("shreddit-post")
        if post is None:
            return GenericExtractor().fit(r)

        title = _title(post) or r.title
        body = _post_body(post)
        settings = get_settings()
        limit = max(1, settings.CRAWL_REDDIT_MAX_COMMENTS)
        ranking = settings.CRAWL_REDDIT_COMMENT_RANKING
        reported = _reported_comment_count(post)
        comments = _comments(soup, limit, ranking)
        parts = [f"# {title}"] if title else []
        if body:
            parts.append(body)
        parts.append(_comments_heading(ranking, limit))
        if comments:
            parts.extend(comments)
        else:
            parts.append("No comments available.")

        return Fitted(
            md=trim_md("\n\n".join(parts)),
            title=title,
            signals={
                "comments": len(comments),
                "comments_required": 0 if reported == 0 else 1,
                "comments_reported": reported,
            },
            flags={"extractor": "reddit", "ranking": ranking},
        )

    def accept(self, f: Fitted) -> bool:
        """Accept a post once comments are present, or Reddit reports none."""
        if f.flags.get("extractor") != "reddit":
            return GenericExtractor().accept(f)
        required = int(f.signals.get("comments_required", 1))
        return bool(f.title) and int(f.signals.get("comments", 0)) >= required


def comment_request_url(url: str) -> str:
    """Fetch Reddit's Top or Best candidate set without changing the stored URL."""
    if not is_post_url(url):
        return url
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "sort"
    ]
    sort = "top" if get_settings().CRAWL_REDDIT_COMMENT_RANKING == "score" else "best"
    query.append(("sort", sort))
    return urlunparse(parsed._replace(query=urlencode(query)))


def needs_refresh(url: str, md: str) -> bool:
    """Recrawl indexed posts when their ranking mode or limit has changed."""
    if not is_post_url(url):
        return False
    low = md.lower()
    if any(marker in low for marker in _NON_HYDRATABLE_MARKERS):
        return False
    settings = get_settings()
    heading = _comments_heading(
        settings.CRAWL_REDDIT_COMMENT_RANKING,
        max(1, settings.CRAWL_REDDIT_MAX_COMMENTS),
    )
    return heading not in md


def is_post_url(url: str) -> bool:
    """True for a standard reddit.com post URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("reddit.com", "www.reddit.com"):
        return False
    return _POST_PATH_RE.match(parsed.path) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _comments_heading(ranking: str, limit: int) -> str:
    """A readable heading that also marks the indexed ranking and limit."""
    label = "highest score" if ranking == "score" else "Reddit Best"
    return f"## Reddit comments ({label}, up to {limit})"


def _title(post: Tag) -> str | None:
    """Post title from its stable attribute, with a heading fallback."""
    title = post.get("post-title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading = post.select_one('h1[slot="title"]')
    return heading.get_text(" ", strip=True) if heading else None


def _post_body(post: Tag) -> str:
    """Only the post's rich text, excluding menus and related content."""
    body = post.select_one('[property="schema:articleBody"]')
    return _markdown(body) if body else ""


def _reported_comment_count(post: Tag) -> int | None:
    """Reddit's total comment count, or None when it is missing or invalid."""
    try:
        return max(0, int(str(post.get("comment-count"))))
    except ValueError:
        return None


def _comments(
    soup: BeautifulSoup,
    limit: int,
    ranking: str,
) -> list[str]:
    """Choose meaningful comments by score or Reddit's displayed Best order."""
    candidates: list[tuple[int | None, int, Tag, Tag]] = []
    for order, comment in enumerate(soup.select("shreddit-comment")):
        body = comment.select_one('[slot="comment"]')
        if body is None or body.find_parent("shreddit-comment") is not comment:
            continue
        if not body.get_text(" ", strip=True):
            continue
        candidates.append((_score(comment), order, comment, body))
    if ranking == "score":
        candidates.sort(key=lambda item: (item[0] is None, -(item[0] or 0), item[1]))

    out: list[str] = []
    for score, _order, comment, body in candidates:
        md = _markdown(body)
        if not md:
            continue
        author = comment.get("author") or "unknown"
        depth = comment.get("depth") or "0"
        label = f"### {len(out) + 1}. u/{author}"
        details = [f"depth {depth}"]
        if score is not None:
            details.append(f"score {score}")
        out.append(f"{label} ({', '.join(details)})\n\n{md}")
        if len(out) >= limit:
            break
    return out


def _score(comment: Tag) -> int | None:
    """Numeric vote score, with unknown scores ranked last."""
    try:
        return int(str(comment.get("score")))
    except ValueError:
        return None


def _markdown(element: Tag) -> str:
    """Preserve rich text structure while dropping embedded scripts and styles."""
    for tag in element.select("script, style"):
        tag.decompose()
    return mdify(str(element), heading_style="ATX").strip()


register(RedditExtractor(), "reddit.com")

"""URL filter: reject known-garbage URLs (binary media, archives, executables,
HLS video segments) at enqueue time so they never enter the crawl queue.

Pure function of the URL string — no I/O, no settings. Wired into
db.queue_enqueue() (the single enqueue choke point).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Garbage Patterns
# ---------------------------------------------------------------------------

# File extensions that are binary / non-HTML and can never be crawled as a page.
# Observed in the 953-row pending backlog (2026-09-01): mp4, m3u8, m4s, jpg,
# zip, pdf, png, exe, xz, svg, and similar.
GARBAGE_EXTENSIONS: frozenset[str] = frozenset({
    # video
    "3gp", "avi", "flv", "m4s", "m4v", "mkv", "mov", "mp4", "webm", "wmv",
    # hls playlist
    "m3u8",
    # audio
    "aac", "flac", "m4a", "mp3", "ogg", "wav",
    # images
    "bmp", "gif", "ico", "jpeg", "jpg", "png", "svg", "tiff", "webp",
    # archives
    "7z", "bz2", "gz", "rar", "tar", "tgz", "xz", "zip",
    # documents
    "doc", "docx", "ods", "odt", "pdf", "ppt", "pptx", "xls", "xlsx",
    # executables / installers
    "apk", "deb", "dmg", "exe", "msi", "rpm",
})

# HLS video segments: .ts files whose path contains an HLS/segment marker.
# Catches dej02es2pfpm.tnmr.org/hls2/.../seg-N-v1-a1.ts without rejecting
# raw.githubusercontent.com/.../client.ts (TypeScript source).
_HLS_SEGMENT_RE = re.compile(r"(?:hls|seg-)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_garbage_url(url: str) -> bool:
    """True when the URL is a known-garbage pattern (binary media, archive,
    executable, or HLS video segment) that the HTML crawler cannot process."""
    ext = _path_ext(url)
    if ext in GARBAGE_EXTENSIONS:
        return True
    if ext == "ts" and _HLS_SEGMENT_RE.search(urlparse(url).path):
        return True
    return False


def garbage_reason(url: str) -> str | None:
    """Human-readable reason a URL is garbage, or None when it is not."""
    ext = _path_ext(url)
    if ext in GARBAGE_EXTENSIONS:
        return f"binary/non-page file (.{ext})"
    if ext == "ts" and _HLS_SEGMENT_RE.search(urlparse(url).path):
        return "HLS video segment (.ts)"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _path_ext(url: str) -> str:
    """Lowercased file extension from the URL path (query stripped), or ''."""
    path = urlparse(url).path
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return ""
    return last.rsplit(".", 1)[-1].lower()

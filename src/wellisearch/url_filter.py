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
# Observed in the 953-row pending backlog (2026-09-01): exe, jpg, m3u8, m4s,
# mp4, pdf, png, svg, xz, zip, and similar.
GARBAGE_EXTENSIONS: frozenset[str] = frozenset({
    "3gp", "7z", "aac", "apk", "avi", "bmp", "bz2", "deb", "dmg", "doc",
    "docx", "exe", "flac", "flv", "gif", "gz", "ico", "jpeg", "jpg", "m3u8",
    "m4a", "m4s", "m4v", "mkv", "mov", "mp3", "mp4", "msi", "ods", "odt",
    "ogg", "pdf", "png", "ppt", "pptx", "rar", "rpm", "svg", "tar", "tgz",
    "tiff", "wav", "webm", "webp", "wmv", "xls", "xlsx", "xz", "zip",
})

# HLS video segments: .ts files sitting under a path component named "hls" or
# "hls<N>", or whose filename is "seg-<N>". Catches
# dej02es2pfpm.tnmr.org/hls2/.../seg-N-v1-a1.ts without rejecting TypeScript
# source such as /repo/src/client.ts or /repo/src/hls-utils.ts.
_HLS_SEGMENT_RE = re.compile(r"(?:^|/)hls\d*/|(?:^|/)seg-\d+", re.IGNORECASE)


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

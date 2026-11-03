"""T0 Http tier: curl_cffi with Chrome impersonation (design §3.1).

Cheapest tier: a single TLS-fingerprinted request, no browser. Good for the
majority of sites that don't run a JS challenge.
"""
from __future__ import annotations

import logging
import re
import time

from ...config import get_settings
from ..policy import Policy
from ..probe import clamp
from ..results import PayloadRejectedError, Rendered
from . import register

log = logging.getLogger("wellisearch.crawl.tiers.http")

# libcurl's explicit write-abort sentinel; a short/zero return is ignored by
# older curl_cffi write-callback wrappers.
_WRITE_ERROR = 0xFFFFFFFF
_TEXT_APPLICATION_TYPES = ("application/json", "application/ld+json", "application/xhtml+xml", "application/xml")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_CHARSET_RE = re.compile(r"\bcharset\s*=\s*[\"']?([\w.-]+)", re.IGNORECASE)
_TITLE_OPEN_RE = re.compile(r"<title\b[^<>]*>", re.IGNORECASE)
_TITLE_CLOSE_RE = re.compile(r"</title\s*>", re.IGNORECASE)


class HttpTier:
    """curl_cffi AsyncSession with Chrome impersonation."""

    name = "http"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """One impersonated GET, bounded before any response body is buffered.

        Use libcurl callbacks rather than stream=True: curl_cffi's streaming
        queue is unbounded and can fill during a single decompression callback.
        The write callback receives decompressed bytes, including chunked data.
        """
        from curl_cffi import CurlECode, CurlOpt
        from curl_cffi.requests import AsyncSession

        s = get_settings()
        start = time.monotonic()
        body = _HtmlDownload(s.CRAWL_HTML_MAX_BYTES)
        async with AsyncSession(
            impersonate="chrome",
            max_clients=1,
            curl_options={
                CurlOpt.HEADERFUNCTION: body.header,
                CurlOpt.MAXFILESIZE_LARGE: s.CRAWL_HTML_MAX_BYTES,
                CurlOpt.SUPPRESS_CONNECT_HEADERS: 1,
            },
        ) as sess:
            # The tiers are read-only (they fetch public pages and never send data),
            # so untrusted TLS certs are accepted by default — see CRAWL_IGNORE_SSL_ERRORS.
            try:
                r = await sess.get(
                    url,
                    allow_redirects=True,
                    content_callback=body.write,
                    max_redirects=s.CRAWL_HTTP_MAX_REDIRECTS,
                    timeout=clamp(s.CRAWL_TIMEOUT_S),
                    verify=not s.CRAWL_IGNORE_SSL_ERRORS,
                )
            except Exception as error:
                if body.error is not None:
                    raise PayloadRejectedError(body.error) from None
                if getattr(error, "code", None) == CurlECode.FILESIZE_EXCEEDED:
                    # Recent libcurl versions enforce MAXFILESIZE on decoded
                    # bytes before our callback sees the chunk crossing the cap.
                    raise PayloadRejectedError(
                        f"HTML exceeds CRAWL_HTML_MAX_BYTES ({s.CRAWL_HTML_MAX_BYTES} bytes)"
                    ) from None
                raise
        html = body.text()
        ms = int((time.monotonic() - start) * 1000)
        return Rendered(
            html=html,
            title=_extract_title(html),
            status=r.status_code,
            ms=ms,
            engine="http",
            content_type=body.content_type,
        )

    def worst_case_s(self, p: Policy) -> float:
        """Worst-case budget: a single impersonated GET (probe-clamped CRAWL_TIMEOUT_S)."""
        return clamp(get_settings().CRAWL_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Bounded Download
# ---------------------------------------------------------------------------

class _HtmlDownload:
    """Retain at most max_bytes of decoded body; reject final headers early."""

    def __init__(self, max_bytes: int) -> None:
        """Initialize one request's bounded accumulator and final-header state."""
        self.max_bytes = max_bytes
        self.body = bytearray()
        self.content_type: str | None = None
        self.error: str | None = None
        self._content_length: int | None = None
        self._has_location = False
        self._received = 0
        self._status = 0

    def header(self, chunk: bytes) -> int:
        """Inspect each header block without retaining redirect/header buffers."""
        if chunk.startswith(b"HTTP/"):
            self.content_type = None
            self._content_length = None
            self._has_location = False
            self._status = int(chunk.split(maxsplit=2)[1])
        elif not chunk.strip():
            return self._finish_headers(len(chunk))
        else:
            name, _, value = chunk.partition(b":")
            name = name.strip().lower()
            if name == b"content-type":
                self.content_type = value.strip().decode("latin-1")
            elif name == b"content-length" and value.strip().isdigit():
                # Avoid converting arbitrarily long untrusted decimal strings.
                length = value.strip().lstrip(b"0") or b"0"
                self._content_length = int(length) if len(length) <= 20 else self.max_bytes + 1
            elif name == b"location":
                self._has_location = bool(value.strip())
        return len(chunk)

    def write(self, chunk: bytes) -> int:
        """Abort before retaining a chunk that crosses the decompressed limit."""
        self._received += len(chunk)
        if self._received > self.max_bytes:
            self.error = f"HTML exceeds CRAWL_HTML_MAX_BYTES ({self.max_bytes} bytes)"
        if self.error is not None:
            return _WRITE_ERROR
        if not (self._status in _REDIRECT_STATUSES and self._has_location):
            self.body.extend(chunk)
        return len(chunk)

    def text(self) -> str:
        """Decode the bounded bytearray without making another complete byte copy."""
        charset = _CHARSET_RE.search(self.content_type or "")
        encoding = charset.group(1) if charset is not None else "utf-8"
        try:
            return self.body.decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            return self.body.decode("utf-8", errors="replace")

    def _finish_headers(self, size: int) -> int:
        """Only the final response's type/length may reject an HTML download."""
        if self._status < 200 or (self._status in _REDIRECT_STATUSES and self._has_location):
            return size
        mime = (self.content_type or "").split(";", 1)[0].strip().lower()
        if mime and not mime.startswith("text/") and mime not in _TEXT_APPLICATION_TYPES:
            self.error = f"unsupported content type: {mime}"
        elif self._content_length is not None and self._content_length > self.max_bytes:
            self.error = f"HTML exceeds CRAWL_HTML_MAX_BYTES ({self.max_bytes} bytes)"
        return _WRITE_ERROR if self.error is not None else size


# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------

def _extract_title(html: str) -> str | None:
    """Best-effort <title> via a regex; None when absent.

    When the page carries multiple <title> tags, the last non-empty one wins —
    matching how browsers resolve document.title.
    """
    title = None
    pos = 0
    while opening := _TITLE_OPEN_RE.search(html, pos):
        closing = _TITLE_CLOSE_RE.search(html, opening.end())
        if closing is None:
            break
        candidate = html[opening.end():closing.start()].strip()
        if candidate:
            title = candidate
        pos = closing.end()
    return title


register(HttpTier())

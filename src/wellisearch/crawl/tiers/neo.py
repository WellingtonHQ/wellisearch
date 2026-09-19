"""T3 Neo tier: BrowserOS neo persistent-profile browser over MCP (design §3.1).

Last-resort transport for bot-walled sites that defeat http + headless browser:
a real desktop browser with a persistent profile, driven by fixed scripts via
neo's MCP `run` tool — no LLM in the loop. A human can solve a challenge once
in the neo cockpit; the profile then whitelists future crawls (see
docs/browseros-tier.md).

Protocol notes (verified against the live instance):
- The MCP client SDK is already a dependency (wellisearch serves its own MCP).
- `run` executes one async JS body against a `browser` SDK, capped at 30 s.
- Tool results carry machine-readable data in structured_content:
  {ok: true, value: <return>} or {ok: false, error: "..."}.
- browser.evaluate returns an envelope: small values inline as {page, value};
  values over maxChars are written to a local file ({writtenToFile: true}) —
  the script then re-reads them in slices. Run-level returns themselves do not
  truncate, so one run call per navigation covers any page size.
- browser.evaluate's `code` argument must be a plain string literal (a variable
  or concatenation breaks neo's script parser); dynamic values are passed via
  page globals (window.__ws_html / window.__ws_off).
- neo rejects non-local Host headers; a remote endpoint (host.docker.internal)
  must send Host: localhost:<port>.
- Closing the session logs a harmless "Session termination failed: 202" warning
  from the MCP SDK: neo answers the terminating DELETE with 202, which the SDK
  does not count as success. The session is terminated either way.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlparse

import httpx

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ...config import Settings, get_settings
from ..botwall import is_botwall
from ..policy import Policy
from ..results import Rendered
from . import register

log = logging.getLogger("wellisearch.crawl.tiers.neo")


class NeoError(Exception):
    """Base error for the neo tier; message renders into failure_detail()."""


class NeoNotConnectedError(NeoError):
    """The neo app is not running/paired, or its MCP endpoint is unreachable."""


class NeoRunError(NeoError):
    """A run script failed on the neo side (tool error or bad payload)."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NEO_SESSION_NAME = "wellisearch-crawl"  # audit-log label for this tier's session
NEO_EVAL_MAX_CHARS = 200_000  # browser.evaluate inline cap (UTF-8 bytes)
NEO_HTML_SLICE_CHARS = 40_000  # slice size when the HTML exceeds the evaluate cap
NEO_RECONNECT_DELAY_S = 2.0  # pause before a single reconnect retry


# ---------------------------------------------------------------------------
# Run scripts
# ---------------------------------------------------------------------------

_META_JS = (
    "return JSON.stringify({title: document.title || null, "
    "status: (performance.getEntriesByType('navigation')[0] || {}).responseStatus || null, "
    "ct: document.contentType || null})"
)

# Shared extraction body: reads meta + full HTML (chunked past the evaluate cap)
# from an already-open page referenced by `pid`.
_EXTRACT_BODY = """\
  const metaR = await browser.evaluate(pid, {code: __META_JS__, maxChars: __MAX_CHARS__});
  const meta = JSON.parse(metaR.value);
  let html;
  const h = await browser.evaluate(
    pid,
    {code: "return document.documentElement.outerHTML", maxChars: __MAX_CHARS__},
  );
  if (typeof h === "string") {
    html = h;
  } else if (h && !h.writtenToFile) {
    html = h.value;
  } else {
    await browser.evaluate(
      pid,
      {code: "window.__ws_html = document.documentElement.outerHTML; window.__ws_off = 0", maxChars: __MAX_CHARS__},
    );
    const parts = [];
    for (;;) {
      const s = await browser.evaluate(
        pid,
        {code: "var __p = window.__ws_html.slice(window.__ws_off, window.__ws_off + __SLICE__); window.__ws_off += __SLICE__; return __p", maxChars: __MAX_CHARS__},
      );
      parts.push(s.value);
      if (s.value.length < __SLICE__) { break; }
    }
    html = parts.join("");
  }
"""

# Navigates once and leaves the tab open so challenge polls can re-read it in
# place — re-navigating every poll resets auto-solving JS challenges and looks
# bot-like to WAFs. The tier closes the tab itself (see fetch / _close_page).
_LOAD_JS_TEMPLATE = """\
const pid = await browser.pages.newPage(__URL__);
try {
  await browser.wait(pid, {for: "selector", value: "body"});
  await browser.wait(pid, {value: __SETTLE_MS__});
__BODY__
  return JSON.stringify({pid: pid, html: html, title: meta.title, status: meta.status, ct: meta.ct});
} catch (e) {
  try { await browser.pages.close(pid); } catch (_) {}
  throw e;
}
"""

# Re-reads the state of an already-open tab without navigating.
_RECHECK_JS_TEMPLATE = """\
const pid = __PID__;
__BODY__
return JSON.stringify({html: html, title: meta.title, status: meta.status, ct: meta.ct});
"""

_CLOSE_JS_TEMPLATE = "await browser.pages.close(__PID__); return 'closed';"


def _render_body() -> str:
    """The extraction body with the shared constants substituted in."""
    return (
        _EXTRACT_BODY.replace("__META_JS__", json.dumps(_META_JS))
        .replace("__MAX_CHARS__", str(NEO_EVAL_MAX_CHARS))
        .replace("__SLICE__", str(NEO_HTML_SLICE_CHARS))
    )


def _load_script(url: str, settle_ms: int) -> str:
    """The run script that loads one URL and returns a {pid,html,title,status,ct} JSON string."""
    return (
        _LOAD_JS_TEMPLATE.replace("__URL__", json.dumps(url))
        .replace("__SETTLE_MS__", str(settle_ms))
        .replace("__BODY__", _render_body())
    )


def _recheck_script(pid: int) -> str:
    """The run script that re-reads an open tab's state without navigating."""
    return (
        _RECHECK_JS_TEMPLATE.replace("__PID__", str(int(pid)))
        .replace("__BODY__", _render_body())
    )


def _close_script(pid: int) -> str:
    """The run script that closes one tab."""
    return _CLOSE_JS_TEMPLATE.replace("__PID__", str(int(pid)))


def _host_header(endpoint: str) -> dict[str, str]:
    """neo rejects non-local Host headers; map a remote endpoint to localhost:<port>."""
    parts = urlparse(endpoint)
    if parts.hostname in ("localhost", "127.0.0.1"):
        return {}
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return {"Host": f"localhost:{port}"}


# ---------------------------------------------------------------------------
# Tier
# ---------------------------------------------------------------------------

class NeoTier:
    """BrowserOS neo tier: one shared persistent-profile browser over MCP.

    Single-flight by design (one real desktop browser): a lock serializes all
    fetches, and the lazy MCP session reconnects once on transport failure.
    """

    name = "neo"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._session: ClientSession | None = None
        self._transport_cm: object | None = None
        self._client: httpx.AsyncClient | None = None

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Load the URL in neo's persistent-profile browser and return its HTML.

        Navigates once; on a bot-wall, re-reads the same tab every
        CRAWL_NEO_POLL_MS until clean or the challenge budget is spent (a human
        solving it once whitelists the site). Re-navigating each poll would reset
        auto-solving challenges and look bot-like to WAFs. A still-walled page is
        returned as-is so the engine records the marker.
        """
        s = get_settings()
        start = time.monotonic()
        async with self._lock:
            pid, html, title, status, ct = await self._load(url, s.CRAWL_NEO_SETTLE_MS)
            open_pids = [pid]
            try:
                deadline = start + float(s.CRAWL_NEO_CHALLENGE_BUDGET_S)
                while is_botwall(html, status or 200, ct) is not None and time.monotonic() < deadline:
                    await asyncio.sleep(s.CRAWL_NEO_POLL_MS / 1000)
                    try:
                        html, title, status, ct = await self._recheck(pid)
                    except NeoError as e:
                        # Tab/browser gone (neo restart): start over with a fresh navigation.
                        log.warning("neo recheck failed (%s); re-navigating", e)
                        pid, html, title, status, ct = await self._load(url, s.CRAWL_NEO_SETTLE_MS)
                        open_pids.append(pid)
            finally:
                for p_id in open_pids:
                    await self._close_page(p_id)
        ms = int((time.monotonic() - start) * 1000)
        return Rendered(
            html=html,
            title=title,
            status=status or 200,
            ms=ms,
            engine="neo",
            content_type=ct,
        )

    def worst_case_s(self, p: Policy) -> float:
        """Worst case: one run call plus the full challenge poll budget."""
        s = get_settings()
        return float(s.CRAWL_NEO_TIMEOUT_S) + float(s.CRAWL_NEO_CHALLENGE_BUDGET_S)

    # -----------------------------------------------------------------------
    # Page operations
    # -----------------------------------------------------------------------

    async def _load(
        self,
        url: str,
        settle_ms: int,
    ) -> tuple[int, str, str | None, int | None, str | None]:
        """One neo navigation + extraction; returns (pid, html, title, status, ct).

        The tab stays open so challenge polls can re-read it in place.
        """
        payload = await self._run(_load_script(url, settle_ms))
        data = json.loads(payload)
        return int(data["pid"]), data["html"], data.get("title"), data.get("status"), data.get("ct")

    async def _recheck(self, pid: int) -> tuple[str, str | None, int | None, str | None]:
        """Re-read an open tab's state without navigating; returns (html, title, status, ct)."""
        payload = await self._run(_recheck_script(pid))
        data = json.loads(payload)
        return data["html"], data.get("title"), data.get("status"), data.get("ct")

    async def _close_page(self, pid: int) -> None:
        """Close one tab (best-effort; a dead session here is noise)."""
        try:
            await self._run(_close_script(pid))
        except NeoError as e:
            log.debug("neo page close failed: %s", e)

    # -----------------------------------------------------------------------
    # Session lifecycle
    # -----------------------------------------------------------------------

    async def _run(self, script: str) -> str:
        """Run one script in neo; returns the run tool's returned value (a string).

        When an established session dies mid-call (stale session id, transport
        blip), tears it down and retries once. Script errors and first-connect
        failures propagate immediately — retrying them cannot help.
        """
        s = get_settings()
        had_session = self._session is not None
        try:
            return await self._call_run(script, s)
        except NeoError as e:
            if not had_session or isinstance(e, NeoRunError):
                raise
            log.warning("neo run failed (%s); reconnecting", e)
            await self._close_session()
            await asyncio.sleep(NEO_RECONNECT_DELAY_S)
            return await self._call_run(script, s)

    async def _call_run(self, script: str, s: Settings) -> str:
        """One run tool call on a live session; maps failures to named errors."""
        session = await self._ensure_session(s)
        try:
            res = await session.call_tool("run", {"code": script})
        except Exception as e:
            raise NeoNotConnectedError(f"neo MCP call failed: {e}") from e
        return _parse_run(res)

    async def _ensure_session(self, s: Settings) -> ClientSession:
        """Open (once) the lazy MCP session to neo and label it in the audit log."""
        if self._session is not None:
            return self._session
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(s.CRAWL_NEO_TIMEOUT_S),
            headers=_host_header(s.CRAWL_NEO_ENDPOINT),
        )
        transport_cm = streamable_http_client(s.CRAWL_NEO_ENDPOINT, http_client=client)
        try:
            read, write = await transport_cm.__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
        except Exception as e:
            await _aclose(transport_cm)
            raise NeoNotConnectedError(f"cannot reach neo at {s.CRAWL_NEO_ENDPOINT}: {e}") from e
        self._client = client
        self._transport_cm = transport_cm
        self._session = session
        await self._name_session(session)
        log.info("neo tier connected (%s)", s.CRAWL_NEO_ENDPOINT)
        return session

    async def aclose(self) -> None:
        """Tear down the MCP session + transport; call at process exit.

        Idempotent, and waits for an in-flight fetch so shutdown is graceful.
        Without it, the open transport's async generator is finalized during
        loop teardown and anyio logs a cancel-scope traceback.
        """
        async with self._lock:
            await self._close_session()

    async def _close_session(self) -> None:
        """Tear down the MCP session + transport (best-effort)."""
        for closer in (self._session, self._transport_cm):
            if closer is not None:
                await _aclose(closer)
        if self._client is not None:
            await self._client.aclose()
        self._session = None
        self._transport_cm = None
        self._client = None

    async def _name_session(self, session: ClientSession) -> None:
        """Label the neo session in its audit log (best-effort)."""
        try:
            await session.call_tool(
                "name_session",
                {
                    "name": NEO_SESSION_NAME,
                    "category": "internal-tools",
                    "summary": "wellisearch background crawl tier (fixed scripts, no LLM)",
                },
            )
        except Exception as e:
            log.debug("neo name_session failed: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _aclose(cm: object) -> None:
    """Exit an async context manager (best-effort; close failures are noise)."""
    try:
        await cm.__aexit__(None, None, None)  # type: ignore[union-attr]
    except Exception as e:
        log.debug("neo close: %s", e)


def _parse_run(res: object) -> str:
    """Extract the returned value from a run tool result; raise on error."""
    sc = getattr(res, "structured_content", None) or {}
    if sc.get("ok"):
        v = sc.get("value")
        if not isinstance(v, str):
            raise NeoRunError(f"run returned non-string value: {type(v).__name__}")
        return v
    msg = _error_message(res, sc)
    if "not connected" in msg.lower():
        raise NeoNotConnectedError(msg)
    raise NeoRunError(msg)


def _error_message(res: object, sc: dict) -> str:
    """Best-effort error text from a failed run tool result."""
    err = sc.get("error")
    if err:
        return str(err)
    for c in getattr(res, "content", None) or []:
        t = getattr(c, "text", None)
        if t:
            return t[:200]
    return f"run failed (is_error={getattr(res, 'is_error', '?')})"


register(NeoTier())

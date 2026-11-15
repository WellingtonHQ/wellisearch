# Agent usage issues — wellisearch

Append-only log of problems the agent hits while using the wellisearch MCP
server (per the `wellisearch` skill, "Reporting usage issues"). New entries go
at the bottom, in chronological order. The agent only appends; the owner
updates **Status** as issues are fixed and clears resolved entries.

Severity scale:

- **low** — cosmetic or confusing (a field named oddly, an ambiguous error).
- **medium** — a real friction (a workaround was needed, a call failed in a
  way the docs did not promise).
- **high** — blocked the task (the index or worker crashed, work was lost, or
  the page was unreadable through every available path).

---

## Entries

## 2026-11-13 — fetch_page (crawl/extractor) — XDA Forums (XenForo) threads extract only the first post, with duplication and raw JSON noise
- **Task:** Testing how wellisearch extracts forum pages; fetched three XDA Forums threads (`battery-drain.4772343`, `battery-drain-by-system-apps.4751534`, `pixel-9-pro-battery-drain-issue.4697775`).
- **Observed:**
  1. Each thread came back as a tiny flat fragment (420–1354 chars). `Truncated: false` and `Window: 0-N of N` report the fragment as the *complete* page, but the threads have far more posts (e.g. `battery-drain.4772343` is ~7,266 posts / 1,062 pages). Replies, author names, post dates, post numbers, and pagination are all dropped — the primary content of a forum thread is missing.
  2. Extracted text is duplicated: in `battery-drain-by-system-apps` the reply "If you are using Beta, then yes…" appears 3× and "Agreed that all beta users…" 2×; in `battery-drain.4772343` the OP text is repeated twice.
  3. Raw JSON/JS config leaked into the body: `pixel-9-pro-battery-drain-issue` contains a whole lightbox config object (`{"lightbox_close": "Close", …}`) pasted into the extracted text.
  4. No per-post structure at all (contrast: the Reddit output carries `u/user (depth N, score N)` per comment).
- **Expected:** A forum thread page to extract the thread content (posts + replies) with author/timestamp structure, or at minimum not report a 400–1400-char fragment as the full page (`Truncated: true` / a larger window) when the thread clearly continues.
- **Severity:** medium (XDA threads are largely unusable for reading the discussion; a workaround — reading via the @edi browser or the search snippets — was needed; no crash or lost work)
- **Status:** open
- **Comparison (same session):** Reddit extracts cleanly — e.g. `www.reddit.com/r/linuxquestions/comments/1tsibn3/…` returned the title, post, and 24 comments with `u/user (depth, score)` structure and no bot wall (fresh crawl ~10s). The gap is the XenForo/forum extractor, not the crawl path in general.

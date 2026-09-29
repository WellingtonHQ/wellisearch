"use strict";

// Minimal self-contained Markdown renderer for fetched page content.
// No dependencies: the dashboard is vanilla JS with no build step, and the
// service may run offline. All source HTML is escaped before any markup is
// applied, so untrusted crawled pages cannot inject tags or scripts.

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Only http(s) links are rendered; anything else (javascript:, data:, …)
// stays as literal text.
function safeHref(value) {
  const v = String(value || "").trim();
  if (/^\/\//i.test(v)) return "https:" + v;
  try {
    const url = new URL(v);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function inlineMarkdown(text) {
  let s = escapeHtml(text);
  const stash = [];
  const keep = (html) => {
    stash.push(html);
    return `\u0000${stash.length - 1}\u0000`;
  };

  // Code spans first so their contents are never re-formatted.
  s = s.replace(/``([^`]+)``/g, (m, code) => keep(`<code>${code}</code>`));
  s = s.replace(/`([^`\n]+)`/g, (m, code) => keep(`<code>${code}</code>`));

  // Emphasis before links so link text may carry bold/italic.
  s = s.replace(/\*\*(\S(?:.*?\S)?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__(\S(?:.*?\S)?)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|\s)\*([^\s*][^*\n]*?)\*(?=[\s.,;:!?)\]]|$)/g, "$1<em>$2</em>");
  s = s.replace(/(^|\s)_([^_\n]+)_(?=[\s.,;:!?)\]]|$)/g, "$1<em>$2</em>");
  s = s.replace(/~~(\S(?:.*?\S)?)~~/g, "<del>$1</del>");

  // Images and links (hrefs are protocol-checked).
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g, (m, alt, src) => {
    const href = safeHref(src);
    return href ? keep(`<img src="${href}" alt="${alt}" loading="lazy">`) : m;
  });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)/g, (m, label, href) => {
    const safe = safeHref(href);
    return safe ? keep(`<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`) : m;
  });

  // Restore stashed fragments (loop: a stash may contain another stash).
  for (let guard = 0; s.includes("\u0000") && guard < 8; guard++) {
    s = s.replace(/\u0000(\d+)\u0000/g, (m, n) => stash[Number(n)] ?? m);
  }
  return s;
}

function splitTableRow(line) {
  const t = line.trim();
  const cells = [];
  let current = "";
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (c === "\\" && t[i + 1] === "|") { current += "|"; i++; continue; }
    if (c === "|") { cells.push(current); current = ""; continue; }
    current += c;
  }
  cells.push(current);
  if (t.startsWith("|")) cells.shift();
  if (t.endsWith("|") && !t.endsWith("\\|")) cells.pop();
  return cells.map(c => c.trim());
}

function isTableSeparator(line) {
  if (!line.includes("-")) return false;
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every(c => /^:?-+:?$/.test(c));
}

function renderTable(header, aligns, rows) {
  const alignAttr = (idx) => (aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "");
  let html = "<table><thead><tr>";
  header.forEach((cell, idx) => { html += `<th${alignAttr(idx)}>${inlineMarkdown(cell)}</th>`; });
  html += "</tr></thead><tbody>";
  for (const row of rows) {
    html += "<tr>";
    header.forEach((_, idx) => { html += `<td${alignAttr(idx)}>${inlineMarkdown(row[idx] ?? "")}</td>`; });
    html += "</tr>";
  }
  return html + "</tbody></table>";
}

function dedent(linesArr) {
  const indents = linesArr.filter(l => l.trim()).map(l => l.match(/^\s*/)[0].length);
  const min = indents.length ? Math.min(...indents) : 0;
  return linesArr.map(l => (l.trim() ? l.slice(min) : ""));
}

function parseList(lines, start) {
  const first = lines[start].match(/^(\s*)([-*+]|\d+[.)])\s+/);
  const baseIndent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];
  let i = start;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // Blank line: keep going only if the list resumes after it.
      let k = i + 1;
      while (k < lines.length && !lines[k].trim()) k++;
      const nextLine = k < lines.length ? lines[k] : "";
      const m2 = nextLine.match(/^(\s*)([-*+]|\d+[.)])\s+/);
      const resumes = (m2 && m2[1].length >= baseIndent) || /^\s{2,}\S/.test(nextLine);
      if (!resumes) break;
      i++;
      continue;
    }
    const m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (m && m[1].length >= baseIndent && m[1].length <= baseIndent + 1) {
      items.push({ text: m[3], extra: [] });
    } else if (items.length) {
      // Deeper indent or plain continuation belongs to the previous item.
      items[items.length - 1].extra.push(line);
    } else {
      break;
    }
    i++;
  }

  const tag = ordered ? "ol" : "ul";
  let html = `<${tag}>`;
  for (const item of items) {
    let inner = inlineMarkdown(item.text);
    if (item.extra.length) inner += renderMarkdown(dedent(item.extra).join("\n"));
    html += `<li>${inner}</li>`;
  }
  return [html + `</${tag}>`, i];
}

function isBlockStart(lines, i) {
  const line = lines[i];
  if (/^\s{0,3}(`{3,}|~{3,})/.test(line)) return true;
  if (/^\s{0,3}#{1,6}\s/.test(line)) return true;
  if (/^\s{0,3}((-\s*){3,}|(\*\s*){3,}|(_\s*){3,})$/.test(line)) return true;
  if (/^\s{0,3}>/.test(line)) return true;
  if (/^(\s*)([-*+]|\d+[.)])\s+/.test(line)) return true;
  if (line.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) return true;
  return false;
}

function renderMarkdown(source) {
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  let html = "";
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    // Fenced code block.
    const fence = line.match(/^\s{0,3}(`{3,}|~{3,})\s*(\S*)/);
    if (fence) {
      const char = fence[1][0];
      const minLen = fence[1].length;
      const lang = fence[2] ? ` class="language-${escapeHtml(fence[2])}"` : "";
      i++;
      const buf = [];
      while (i < lines.length) {
        const t = lines[i].trim();
        if (t.length >= minLen && [...t].every(c => c === char)) break;
        buf.push(lines[i]);
        i++;
      }
      i++; // skip the closing fence (or run past the end — fine)
      html += `<pre><code${lang}>${escapeHtml(buf.join("\n"))}</code></pre>`;
      continue;
    }

    // ATX heading.
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      const text = heading[2].replace(/\s+#+\s*$/, "");
      html += `<h${level}>${inlineMarkdown(text)}</h${level}>`;
      i++;
      continue;
    }

    // Horizontal rule.
    if (/^\s{0,3}((-\s*){3,}|(\*\s*){3,}|(_\s*){3,})$/.test(line)) {
      html += "<hr>";
      i++;
      continue;
    }

    // Blockquote: collect consecutive '>' lines and render the inside.
    if (/^\s{0,3}>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
        i++;
      }
      html += `<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`;
      continue;
    }

    // List (ordered or unordered, nesting via indentation).
    if (/^(\s*)([-*+]|\d+[.)])\s+/.test(line)) {
      const [listHtml, next] = parseList(lines, i);
      html += listHtml;
      i = next;
      continue;
    }

    // GFM table: header row followed by a separator row.
    if (line.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const header = splitTableRow(line);
      const aligns = splitTableRow(lines[i + 1]).map(cell => {
        const left = cell.startsWith(":");
        const right = cell.endsWith(":");
        return left && right ? "center" : right ? "right" : "";
      });
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      html += renderTable(header, aligns, rows);
      continue;
    }

    // Paragraph: a run of plain lines.
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines, i)) {
      buf.push(lines[i]);
      i++;
    }
    html += `<p>${inlineMarkdown(buf.map(l => l.replace(/\s+$/, "")).join(" "))}</p>`;
  }

  return html;
}

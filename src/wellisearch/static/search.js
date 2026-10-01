"use strict";

const $ = (selector) => document.querySelector(selector);
const modeHelp = {
  auto: "Search the index first, then use a provider if needed.",
  provider: "Go straight to a search provider for fresh results.",
  local: "Search only pages already stored in your index.",
};

let searchSequence = 0;
let searchAbort = null;
let fetchSequence = 0;
let fetchAbort = null;
let fetchTimer = null;
let currentResult = null;
let pendingAction = null;

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function setStatus(element, message, kind = "") {
  element.textContent = message;
  element.className = `status ${kind}`.trim();
}

function showAuth(action) {
  pendingAction = action;
  $("#auth-panel").hidden = false;
  $("#api-key").focus();
}

async function requestJson(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const key = localStorage.getItem("wkey");
  if (key) headers.Authorization = `Bearer ${key}`;
  if (options.body) headers["Content-Type"] = "application/json";

  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => null);
  if (response.status === 401) {
    localStorage.removeItem("wkey");
    const error = new Error("API key needed");
    error.status = 401;
    throw error;
  }
  if (!response.ok && !(response.status === 502 && data?.source === "error")) {
    throw new Error(data?.detail || data?.error || `Request failed (${response.status})`);
  }
  if (!data) throw new Error("The server returned an empty response.");
  return data;
}

function resultCard(result) {
  const url = safeUrl(result.url);
  if (!url) return null;

  const card = document.createElement("article");
  card.className = "result";

  const urlText = document.createElement("div");
  urlText.className = "result-url";
  urlText.textContent = result.url;
  card.append(urlText);

  const heading = document.createElement("h3");
  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = result.title || result.url;
  heading.append(link);
  card.append(heading);

  if (result.snippet) {
    const snippet = document.createElement("p");
    snippet.textContent = result.snippet;
    card.append(snippet);
  }

  const footer = document.createElement("div");
  footer.className = "result-footer";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "View content";
  button.addEventListener("click", () => openContent(result));
  footer.append(button);

  if (result.last_crawled) {
    const crawled = document.createElement("span");
    crawled.className = "muted";
    const date = new Date(result.last_crawled);
    crawled.textContent = Number.isNaN(date.getTime())
      ? "Stored in index"
      : `Last crawled ${date.toLocaleString()}`;
    footer.append(crawled);
  }
  card.append(footer);
  return card;
}

function renderSearch(data, mode) {
  const results = $("#results");
  results.replaceChildren();
  const cards = (data.results || []).map(resultCard).filter(Boolean);
  results.append(...cards);

  const source = data.source === "local" ? "local index" : data.source;
  const elapsed = data.timing?.total_ms;
  $("#results-meta").textContent = cards.length
    ? `${cards.length} result${cards.length === 1 ? "" : "s"} · ${source}` +
      (Number.isFinite(elapsed) ? ` · ${elapsed} ms` : "")
    : "";

  if (cards.length && data.degraded) {
    setStatus($("#search-status"), "Results are available, but provider fallback was degraded.", "warning");
  } else if (cards.length) {
    setStatus($("#search-status"), "");
  } else if (data.index_error) {
    setStatus($("#search-status"), `Index search failed: ${data.index_error}`, "error");
  } else if (data.provider_errors?.length) {
    const details = data.provider_errors.map(item => `${item.provider}: ${item.error}`).join("; ");
    setStatus($("#search-status"), `Search providers failed: ${details}`, "error");
  } else if (mode === "local") {
    setStatus($("#search-status"), "No results found in the local index.");
  } else if (data.source === "error") {
    setStatus($("#search-status"), "Search could not complete. Try again shortly.", "error");
  } else {
    setStatus($("#search-status"), "No results found.");
  }
}

async function runSearch(query, mode) {
  const term = query.trim();
  if (!term) return;
  searchAbort?.abort();
  searchAbort = new AbortController();
  const sequence = ++searchSequence;
  if ($("#content-dialog").open) $("#content-dialog").close();
  $("#results").replaceChildren();
  $("#results-meta").textContent = "";
  $("#search-button").disabled = true;
  setStatus($("#search-status"), "Searching…", "loading");

  const params = new URLSearchParams({ query: term, search_mode: mode, format: "json" });
  try {
    const data = await requestJson(`/api/search?${params}`, { signal: searchAbort.signal });
    if (sequence === searchSequence) renderSearch(data, mode);
  } catch (error) {
    if (sequence !== searchSequence || error.name === "AbortError") return;
    if (error.status === 401) {
      setStatus($("#search-status"), "Enter your API key to run this search.", "warning");
      showAuth(() => runSearch(term, mode));
    } else {
      setStatus($("#search-status"), `Search failed: ${error.message}`, "error");
    }
  } finally {
    if (sequence === searchSequence) $("#search-button").disabled = false;
  }
}

function stopFetch() {
  fetchSequence += 1;
  fetchAbort?.abort();
  clearInterval(fetchTimer);
  fetchTimer = null;
}

function openContent(result) {
  const url = safeUrl(result.url);
  if (!url) return;
  currentResult = result;
  $("#content-title").textContent = result.title || result.url;
  $("#content-url").href = url;
  $("#content-url").textContent = result.url;
  $("#content-dialog").showModal();
  loadContent(result);
}

async function loadContent(result) {
  stopFetch();
  fetchAbort = new AbortController();
  const sequence = fetchSequence;
  const started = Date.now();
  $("#content-markdown").hidden = true;
  $("#content-markdown").textContent = "";
  $("#content-meta").textContent = "";
  $("#retry-fetch").hidden = true;

  const updateWait = () => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    const note = seconds >= 8 ? " A live crawl can take longer." : "";
    setStatus($("#fetch-status"), `Waiting for page content… ${seconds}s.${note}`, "loading");
  };
  updateWait();
  fetchTimer = setInterval(updateWait, 1000);

  try {
    const data = await requestJson("/api/fetch", {
      method: "POST",
      body: JSON.stringify({ url: result.url, format: "json" }),
      signal: fetchAbort.signal,
    });
    if (sequence !== fetchSequence) return;
    if (!data.ok) {
      const botwall = /bot.wall|challenge/i.test(data.error || "");
      setStatus($("#fetch-status"), data.error || "Could not fetch this page.", botwall ? "warning" : "error");
      $("#retry-fetch").hidden = false;
      return;
    }
    setStatus($("#fetch-status"), "Content ready.");
    $("#content-meta").textContent = [
      data.from_index ? "From index" : "Fetched live",
      `${data.chars ?? data.markdown?.length ?? 0} characters`,
      data.truncated ? "truncated" : null,
    ].filter(Boolean).join(" · ");
    const body = $("#content-markdown");
    if (data.markdown) {
      body.innerHTML = renderMarkdown(data.markdown);
    } else {
      body.textContent = "(No Markdown content returned.)";
    }
    body.hidden = false;
  } catch (error) {
    if (sequence !== fetchSequence || error.name === "AbortError") return;
    if (error.status === 401) {
      $("#content-dialog").close();
      showAuth(() => openContent(result));
    } else {
      setStatus($("#fetch-status"), `Fetch failed: ${error.message}`, "error");
      $("#retry-fetch").hidden = false;
    }
  } finally {
    if (sequence === fetchSequence) {
      clearInterval(fetchTimer);
      fetchTimer = null;
    }
  }
}

$("#search-form").addEventListener("submit", event => {
  event.preventDefault();
  runSearch($("#query").value, $("input[name=mode]:checked").value);
});
$("#auth-form").addEventListener("submit", event => {
  event.preventDefault();
  const key = $("#api-key").value.trim();
  if (!key) return;
  localStorage.setItem("wkey", key);
  $("#api-key").value = "";
  $("#auth-panel").hidden = true;
  const action = pendingAction;
  pendingAction = null;
  action?.();
});
document.querySelectorAll("input[name=mode]").forEach(input => {
  input.addEventListener("change", () => {
    $("#mode-help").textContent = modeHelp[input.value];
  });
});
$("#close-dialog").addEventListener("click", () => $("#content-dialog").close());
$("#content-dialog").addEventListener("close", stopFetch);
$("#retry-fetch").addEventListener("click", () => {
  if (currentResult) loadContent(currentResult);
});

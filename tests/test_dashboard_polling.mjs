/** Dashboard polling stays bounded while the server is slow or unavailable. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../src/wellisearch/static/index.html", import.meta.url), "utf8");
const source = html.slice(html.indexOf("const REFRESH_TIMEOUT_MS"), html.indexOf('$("#window-secs").value'));
const requests = [];
const timers = new Map();
const elements = new Map();
let timerId = 0;

const context = vm.createContext({
  AbortController,
  Date,
  SQ_PAGE_SIZE: 10,
  sqPage: 0,
  winSecs: 86400,
  logQ: "",
  localStorage: { getItem: () => null },
  $: selector => {
    if (!elements.has(selector)) elements.set(selector, { style: {} });
    return elements.get(selector);
  },
  api: (path, { signal }) => new Promise((resolve, reject) => {
    requests.push({ path, resolve, signal });
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  }),
  clearTimeout: id => timers.delete(id),
  setTimeout: (fn, delay) => {
    timers.set(++timerId, { fn, delay });
    return timerId;
  },
  renderWorkerToggle() {},
  renderNow() {},
  renderProviders() {},
  renderActivity() {},
  renderIndex() {},
  renderRecentQueries() {},
  renderLog() {},
});
vm.runInContext(source, context);

const first = context.refresh();
for (let i = 0; i < 100; i++) await context.refresh();
assert.equal(requests.length, 7, "overlapping refreshes must reuse the current batch");
for (const request of requests) request.resolve({ logs: [], searches: [], status: "ok" });
await first;
assert(requests.every(request => request.signal.aborted), "completed batches release sibling requests");
assert.equal(timers.size, 1, "all pending refreshes coalesce into one follow-up");
const [nextId, next] = [...timers][0];
assert.equal(next.delay, 0);
timers.delete(nextId);
const second = next.fn();
assert.equal(requests.length, 14);
const deadline = [...timers.values()].find(timer => timer.delay === 15000);
assert(deadline, "each batch needs a finite deadline");
deadline.fn();
await second;
assert(requests.every(request => request.signal.aborted), "timeouts cancel every sibling request");
assert.equal(timers.size, 0, "timeouts release the polling lock without leaking timers");
const third = context.refresh();
assert.equal(requests.length, 21, "polling can recover after a timeout");
for (const request of requests.slice(14)) request.resolve({ logs: [], searches: [], status: "ok" });
await third;
console.log("ALL DASHBOARD POLLING TESTS PASSED");

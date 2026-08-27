"""llm-md-cleanup-bench: benchmark small LLMs on the fit-markdown cleanup task.

Task
----
Given a page's stored ``fit_markdown`` (the baseline), a model rewrites it into
clean markdown WITHOUT adding, inferring, or embellishing content. We compare
five local models (served by Ollama) on:

  quality      deterministic metrics (no-addition, preservation, boilerplate
               removal, structure, length ratio) + a 27B LLM judge scoring
               faithfulness / noise-removal / preservation on a 1-5 rubric.
  performance  time-to-first-token, total latency, completion tok/s, tokens.

The sample is a random set of real URLs (spread across domains) pulled from the
index (Postgres), snapshotted to JSON so the run is reproducible and re-runnable offline.

Models are self-contained: on ``run`` the bench checks Ollama for each model it
will use and auto-downloads any that are missing (one-time). Just start the
stack (``docker compose --env-file .env -f benchmarks/docker-compose.yml up -d``)
and run.

Usage
-----
  python benchmarks/llm_md_cleanup_bench.py sample   # build the sample snapshot from Postgres
  python benchmarks/llm_md_cleanup_bench.py run      # run all models (+ judge) over the sample
  python benchmarks/llm_md_cleanup_bench.py report   # (re)generate the Markdown/JSON report
  python benchmarks/llm_md_cleanup_bench.py all      # sample + run + report
  python benchmarks/llm_md_cleanup_bench.py run --smoke  # 2 pages x first 2 models, quick sanity check

Each run writes to benchmarks/results/:
  llm-cleanup.sample.json    the random input snapshot (reproducible)
  llm-cleanup.results.json   full per-page results (metrics + judge + timing)
  llm-cleanup.report.md      the side-by-side Markdown report

Env (all optional, sensible defaults)
-------------------------------------
  POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
  OLLAMA_BASE_URL          default http://127.0.0.1:11434/v1
  OLLAMA_API_KEY           default "ollama" (Ollama accepts any key)
  JUDGE_BASE_URL           the 27B judge endpoint (required unless --no-judge;
                           or --judge-url)
  JUDGE_MODEL              default qwen3.8-27b
  JUDGE_API_KEY            judge API key (required unless --no-judge; put it
                           in the repo-root .env, not in source)
  BENCH_TEMPERATURE        default 0.0
  BENCH_MAX_OUTPUT_TOKENS  default 2048
  BENCH_SAMPLE_SIZE        default 5
  BENCH_OUT_DIR            default <this dir>/results
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg

HERE = Path(__file__).resolve().parent


def log(msg: str) -> None:
    """Emit a status line prefixed with a local timestamp, flushed immediately.

    Runs are long (model downloads + slow CPU inference), so a wall-clock
    timestamp on every status line makes it obvious when each step happened.
    """
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from the repo-root ``.env`` into ``os.environ``.

    Values already present in the environment win, so explicit exports
    (e.g. ``POSTGRES_HOST=127.0.0.1``) override the file. Dependency-free and
    best-effort: a missing/malformed file is simply ignored. This is what lets
    the judge key (kept in the gitignored ``.env``) be picked up automatically.
    """
    env_file = HERE.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

DEFAULT_MODELS: list[tuple[str, str]] = [
    ("qwen3-8b", "qwen3:8b"),
    # qwen3-4b (thinking) narrates its reasoning into the content (weak
    # instruction-following) -> verbose, slow, wrong output. Use the non-thinking
    # instruct variant instead.
    ("qwen3-4b-instruct", "qwen3:4b-instruct"),
    ("gemma3-12b", "gemma3:12b"),
    ("qwen3-1.7b", "qwen3:1.7b"),
    ("qwen3-0.6b", "qwen3:0.6b"),
]

CLEANUP_SYSTEM_PROMPT = (
    "You are a precise markdown cleaner. Rewrite the markdown provided by the "
    "crawling engine into clean, well-structured markdown.\n\n"
    "Rules:\n"
    "- Remove navigation, menus, footers, cookie/consent text, ads, sponsor content (3rd party products)"
    "'sign in'/'subscribe' prompts, social links, and other boilerplate.\n"
    "- Do NOT add, infer, or paraphrase any facts, numbers, names, or claims. "
    "Only restate what is already present.\n"
    "- Preserve all substantive content: headings, paragraphs, lists, tables, "
    "and code blocks.\n"
    "- Keep the original language.\n"
    "- Output ONLY the cleaned markdown. No explanations, no preamble, and no "
    "code fence around the whole document."
)

JUDGE_SYSTEM_PROMPT = (
    "You are evaluating a markdown-cleaning model. You are given the ORIGINAL "
    "markdown and the CLEANED markdown a model produced from it. Score three "
    "dimensions, each an integer 1-5 (5 = best):\n"
    "- faithfulness: did the cleaned text add, infer, or change any "
    "facts/numbers/names that were NOT in the original? (5 = nothing added or "
    "changed; 1 = significant fabrication)\n"
    "- noise_removal: did it remove boilerplate (nav, ads, cookie, sign-in, "
    "footer) present in the original? (5 = all boilerplate removed; 1 = none)\n"
    "- preservation: did it keep the substantive content (headings, facts, "
    "lists, tables, code) from the original? (5 = nothing substantive lost; "
    "1 = major content lost)\n"
    "Respond with ONLY a JSON object: "
    '{"faithfulness": <int>, "noise_removal": <int>, "preservation": <int>, '
    '"note": "<2-3 sentences>"}'
)

_BOILERPLATE_PATTERNS = [
    r"sign\s+in", r"log\s+in", r"cookie", r"privacy\s+policy", r"terms\s+of",
    r"subscribe", r"newsletter", r"all\s+rights\s+reserved", r"copyright",
    r"facebook", r"twitter", r"linkedin", r"youtube", r"instagram",
    r"navigation", r"skip\s+to\s+content", r"accept\s+all", r"back\s+to\s+top",
    r"related\s+articles", r"share\s+this", r"follow\s+us",
]
_BOILERPLATE_RE = re.compile("|".join(_BOILERPLATE_PATTERNS), re.IGNORECASE)

_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have he her his i if in is it
    its me my no nor not of on or our she so that the their them they this to
    was we were what when where which who will with you your about into than
    then there here how any all each more most other some such only own same
    s t can just don don't should now""".split()
)


@dataclass
class Config:
    postgres_dsn: str
    ollama_base_url: str
    ollama_api_key: str
    judge_base_url: str
    judge_model: str
    judge_api_key: str
    models: list[tuple[str, str]]
    sample_size: int
    temperature: float
    max_output_tokens: int
    timeout_s: int
    use_judge: bool
    concurrency: int
    out_dir: Path
    smoke: bool
    sample_file: Path = field(init=False)
    results_file: Path = field(init=False)
    report_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.sample_file = self.out_dir / "llm-cleanup.sample.json"
        self.results_file = self.out_dir / "llm-cleanup.results.json"
        self.report_file = self.out_dir / "llm-cleanup.report.md"


def load_config(args: argparse.Namespace) -> Config:
    pg_host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    pg_user = os.environ.get("POSTGRES_USER", "wellington")
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "change-me")
    pg_db = os.environ.get("POSTGRES_DB", "wellisearch")
    dsn = (
        f"host={pg_host} port={pg_port} user={pg_user} "
        f"password={pg_pass} dbname={pg_db} sslmode=disable connect_timeout=5"
    )

    models = DEFAULT_MODELS
    if args.models:
        models = []
        for part in args.models.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                label, tag = [p.strip() for p in part.split("=", 1)]
            else:
                label = tag = part
            models.append((label, tag))

    out_dir = Path(args.out_dir or os.environ.get("BENCH_OUT_DIR") or (HERE / "results"))

    judge_base_url = (args.judge_url or os.environ.get("JUDGE_BASE_URL") or "").rstrip("/")
    judge_api_key = os.environ.get("JUDGE_API_KEY") or ""
    use_judge = not args.no_judge
    # only `run`/`all` call the judge — `sample`/`report` must work without it
    should_use_judge = use_judge and args.command in ("run", "all")
    if should_use_judge and (not judge_base_url or not judge_api_key):
        missing = " and ".join(
            name for name, val in (
                ("JUDGE_BASE_URL (or --judge-url)", judge_base_url),
                ("JUDGE_API_KEY", judge_api_key),
            ) if not val
        )
        raise SystemExit(
            f"the LLM judge is enabled but not configured: set {missing} "
            "(repo-root .env works) — or pass --no-judge for deterministic metrics only"
        )

    return Config(
        postgres_dsn=dsn,
        ollama_base_url=(args.ollama_url or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/"),
        ollama_api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
        judge_base_url=judge_base_url,
        judge_model=os.environ.get("JUDGE_MODEL", "qwen3.8-27b"),
        judge_api_key=judge_api_key,
        models=models,
        sample_size=args.sample_size or int(os.environ.get("BENCH_SAMPLE_SIZE", "5")),
        temperature=float(os.environ.get("BENCH_TEMPERATURE", "0.0")),
        max_output_tokens=int(os.environ.get("BENCH_MAX_OUTPUT_TOKENS", "2048")),
        timeout_s=int(os.environ.get("BENCH_TIMEOUT_S", "300")),
        use_judge=use_judge,
        concurrency=max(1, args.concurrency),
        out_dir=out_dir,
        smoke=args.smoke,
    )


# --------------------------------------------------------------------- sampling

# No single domain may contribute more than this many pages to the sample.
PER_DOMAIN_CAP = 3

# Cap on candidate rows pulled from Postgres for the sample (stratified_pick
# then selects within it). Keeps the snapshot step light on a big index and
# bounds the fit_markdown payload; rows are drawn at random (ORDER BY
# random()), so the pool spans domains in proportion to the index.
SAMPLE_POOL_LIMIT = 200


def stratified_pick(
    by_domain: dict[str, list[dict[str, Any]]], target: int
) -> list[dict[str, Any]]:
    """Round-robin across domains; within a domain take pages in arrival order.

    build_sample draws the pool with ORDER BY random(), so the per-domain
    order is already random — this spreads the pick across domains with no
    length bias (document length is effectively random).
    """
    domains = sorted(by_domain, key=lambda d: -len(by_domain[d]))
    # Keep domains diverse, but relax the cap when there are few domains so the
    # target is still reachable.
    cap = max(PER_DOMAIN_CAP, math.ceil(target / max(1, len(domains))))
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    round_idx = 0
    while len(picked) < target:
        progressed = False
        for d in domains:
            if len(picked) >= target:
                break
            if round_idx < min(cap, len(by_domain[d])):
                row = by_domain[d][round_idx]
                if row["url"] not in seen:
                    seen.add(row["url"])
                    picked.append(row)
                    progressed = True
        if not progressed:
            break
        round_idx += 1
    return picked


async def build_sample(cfg: Config) -> list[dict[str, Any]]:
    """Pull a random set of real pages (spread across domains) from the index."""
    conn = await psycopg.AsyncConnection.connect(
        cfg.postgres_dsn, row_factory=psycopg.rows.dict_row
    )
    try:
        cur = await conn.execute(
            """
            SELECT url, title, domain, fit_markdown, length(fit_markdown) AS n
            FROM pages
            WHERE fit_markdown IS NOT NULL
              AND disabled = false
              AND length(fit_markdown) BETWEEN 500 AND 20000
            ORDER BY random()
            LIMIT %s
            """,
            (SAMPLE_POOL_LIMIT,),
        )
        rows = list(await cur.fetchall())
    finally:
        await conn.close()

    if not rows:
        raise SystemExit("no eligible pages in the index (fit_markdown 500..20000 chars, not disabled)")

    by_domain: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_domain.setdefault(r["domain"] or "_", []).append(r)

    target = min(2, cfg.sample_size) if cfg.smoke else cfg.sample_size
    picked = stratified_pick(by_domain, target)
    return [
        {
            "url": r["url"],
            "title": r["title"],
            "domain": r["domain"],
            "input_chars": r["n"],
            "fit_markdown": r["fit_markdown"],
        }
        for r in picked
    ]


def save_sample(cfg: Config, pages: list[dict[str, Any]]) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sample_size": len(pages),
        "pages": pages,
    }
    cfg.sample_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_sample(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.sample_file.exists():
        raise SystemExit(f"no sample at {cfg.sample_file} — run `python llm_md_cleanup_bench.py sample` first")
    payload = json.loads(cfg.sample_file.read_text(encoding="utf-8"))
    pages = payload["pages"]
    if cfg.smoke:
        pages = pages[:2]
    else:
        # Cap at the configured sample size so a default run uses `sample_size`
        # documents even if the stored sample is larger.
        pages = pages[: cfg.sample_size]
    return pages


# --------------------------------------------------------------------- LLM calls

def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _native_base(base_url: str) -> str:
    """Ollama native-API root (``/api/...``) from a base URL that may be the
    OpenAI-compatible one (``.../v1``)."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


async def stream_chat(
    client: httpx.AsyncClient, cfg: Config, base_url: str, api_key: str,
    model: str, messages: list[dict[str, str]],
) -> dict[str, Any]:
    """One streaming chat completion via Ollama's native API (``/api/chat``).

    Sends ``think: false`` so thinking models (Qwen3) answer directly instead of
    spending the whole token budget on reasoning. (The OpenAI-compatible shim
    ignores ``think``; the native API honors it.) Returns text + timing + usage.
    """
    base = _native_base(base_url)
    payload = {
        "model": model,
        "messages": messages,
        "think": False,
        "stream": True,
        "options": {
            "num_predict": cfg.max_output_tokens,
            "temperature": cfg.temperature,
        },
    }
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    parts: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    async with client.stream(
        "POST", f"{base}/api/chat", json=payload, headers=_headers(api_key)
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = chunk.get("message") or {}
            content = msg.get("content")
            if content:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000
                parts.append(content)
            if chunk.get("done"):
                prompt_tokens = chunk.get("prompt_eval_count")
                completion_tokens = chunk.get("eval_count")
    total_ms = (time.perf_counter() - t0) * 1000
    text = "".join(parts)
    if completion_tokens is None and text:
        completion_tokens = max(1, len(text) // 4)
    gen_ms = max(0.0, (total_ms - (ttft_ms or 0.0)))
    tok_s = (completion_tokens / (gen_ms / 1000)) if (completion_tokens and gen_ms > 0) else 0.0
    return {
        "text": text,
        "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
        "total_ms": round(total_ms, 1),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tok_s": round(tok_s, 2),
    }


async def warmup(client: httpx.AsyncClient, cfg: Config, model: str) -> None:
    """Load the model into RAM so measured runs exclude one-time load latency."""
    try:
        await stream_chat(
            client, cfg, cfg.ollama_base_url, cfg.ollama_api_key, model,
            [{"role": "user", "content": "Reply with the single word: ready"}],
        )
    except Exception:
        pass


async def judge_call(
    client: httpx.AsyncClient, cfg: Config, original: str, cleaned: str
) -> dict[str, Any]:
    user = f"=== ORIGINAL ===\n{original}\n\n=== CLEANED ===\n{cleaned}"
    payload = {
        "model": cfg.judge_model,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
    }
    t0 = time.perf_counter()
    r = await client.post(
        f"{cfg.judge_base_url}/chat/completions", json=payload, headers=_headers(cfg.judge_api_key)
    )
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    scores: dict[str, Any] = {}
    if m:
        try:
            obj = json.loads(m.group(0))
            for k in ("faithfulness", "noise_removal", "preservation"):
                if isinstance(obj.get(k), (int, float)):
                    scores[k] = int(obj[k])
            if isinstance(obj.get("note"), str):
                scores["note"] = obj["note"]
        except json.JSONDecodeError:
            pass
    return {"scores": scores, "raw": text, "ms": round((time.perf_counter() - t0) * 1000, 1)}


# ------------------------------------------------------------- deterministic metrics

def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    if len(words) < n:
        n = 1
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _containment(needle: set, haystack: set) -> float:
    if not needle:
        return 0.0
    return len(needle & haystack) / len(needle)


def _structure(text: str) -> dict[str, int]:
    lines = text.splitlines()
    return {
        "headings": sum(1 for l in lines if re.match(r"^\s{0,3}#{1,6}\s", l)),
        "tables": sum(1 for l in lines if "|" in l and l.strip().startswith("|")),
        "code_fences": sum(1 for l in lines if l.strip().startswith("```")),
        "list_items": sum(
            1 for l in lines if re.match(r"^\s*([-*+]|\d+\.)\s+", l)
        ),
    }


def deterministic_metrics(original: str, cleaned: str) -> dict[str, Any]:
    o_words = _words(original)
    c_words = _words(cleaned)
    o_8 = _ngrams(o_words, 8)
    c_8 = _ngrams(c_words, 8)

    no_addition = _containment(c_8, o_8)
    preservation = _containment(set(o_words), set(c_words)) if o_words else 0.0

    o_bp = len(_BOILERPLATE_RE.findall(original))
    c_bp = len(_BOILERPLATE_RE.findall(cleaned))
    boilerplate_removed = (o_bp - c_bp) / o_bp if o_bp else 0.0

    o_struct = _structure(original)
    c_struct = _structure(cleaned)
    struct_preserved = {
        k: (c_struct[k] / o_struct[k] if o_struct[k] else 1.0) for k in o_struct
    }

    return {
        "no_addition": round(no_addition, 4),
        "preservation": round(preservation, 4),
        "boilerplate_removed": round(boilerplate_removed, 4),
        "boilerplate_in": o_bp,
        "boilerplate_out": c_bp,
        "structure_in": o_struct,
        "structure_out": c_struct,
        "structure_preserved": {k: round(v, 4) for k, v in struct_preserved.items()},
        "length_ratio": round(len(cleaned) / max(1, len(original)), 4),
        "input_chars": len(original),
        "output_chars": len(cleaned),
    }


# --------------------------------------------------------------------- run

async def run_model(
    client: httpx.AsyncClient, cfg: Config, label: str, tag: str,
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    await warmup(client, cfg, tag)
    sem = asyncio.Semaphore(cfg.concurrency)
    results: list[dict[str, Any]] = []

    async def one(page: dict[str, Any], idx: int) -> dict[str, Any]:
        async with sem:
            rec: dict[str, Any] = {
                "model": label,
                "url": page["url"],
                "domain": page["domain"],
                "input_chars": page["input_chars"],
            }
            who = f"[run] {label} · page {idx + 1}/{len(pages)} · {page['url']}"
            try:
                out = await stream_chat(
                    client, cfg, cfg.ollama_base_url, cfg.ollama_api_key, tag,
                    [
                        {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
                        {"role": "user", "content": page["fit_markdown"]},
                    ],
                )
                rec.update({k: out[k] for k in ("ttft_ms", "total_ms", "prompt_tokens", "completion_tokens", "tok_s")})
                rec["output"] = out["text"]
                rec["metrics"] = deterministic_metrics(page["fit_markdown"], out["text"])
                stats = (f"model done in {out['total_ms'] / 1000:.0f}s "
                         f"(ttft {out['ttft_ms'] or 0:.0f}ms, {out['completion_tokens'] or 0} tok @ {out['tok_s']} tok/s)")
                if cfg.use_judge and out["text"].strip():
                    log(f"{who} — {stats} → awaiting judge …")
                    rec["judge"] = await judge_call(client, cfg, page["fit_markdown"], out["text"])
                    sc = rec["judge"].get("scores") or {}
                    log(f"{who} — judge done in {rec['judge'].get('ms', 0) / 1000:.0f}s "
                        f"(faith={sc.get('faithfulness')} noise={sc.get('noise_removal')} presv={sc.get('preservation')})")
                else:
                    log(f"{who} — {stats}")
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
                log(f"{who} — ERROR: {rec['error']}")
            return rec

    return list(await asyncio.gather(*(one(p, i) for i, p in enumerate(pages))))


async def ensure_models(client: httpx.AsyncClient, cfg: Config, tags: list[str]) -> None:
    """Auto-download any missing Ollama models before running.

    ``tags`` are the Ollama model names that will actually be used. Uses
    Ollama's native API (derived from the OpenAI-compatible base URL), so the
    benchmark is self-contained: it pulls whatever it needs on first run.
    """
    base = cfg.ollama_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]

    existing: set[str] = set()
    try:
        r = await client.get(f"{base}/api/tags", timeout=15.0)
        r.raise_for_status()
        existing = {m.get("name", "") for m in r.json().get("models", [])}
    except Exception as e:
        raise SystemExit(
            f"could not reach Ollama at {base} to check models: {e}\n"
            "Start it with: docker compose --env-file .env -f benchmarks/docker-compose.yml up -d"
        )

    for tag in tags:
        if tag in existing:
            log(f"[models] {tag} already present")
            continue
        log(f"[models] downloading {tag} … (one-time, can take a while)")
        t0 = time.perf_counter()
        try:
            r = await client.post(
                f"{base}/api/pull",
                json={"name": tag},
                timeout=httpx.Timeout(3600.0, connect=15.0),
            )
            r.raise_for_status()
        except Exception as e:
            raise SystemExit(f"failed to pull {tag}: {e}")
        log(f"[models] {tag} ready in {time.perf_counter() - t0:.0f}s")


async def run_all(cfg: Config) -> dict[str, Any]:
    pages = load_sample(cfg)
    models = cfg.models[:2] if cfg.smoke else cfg.models
    timeout = httpx.Timeout(cfg.timeout_s, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await ensure_models(client, cfg, [tag for _, tag in models])
        all_results: dict[str, list[dict[str, Any]]] = {}
        for label, tag in models:
            log(f"[run] {label} ({tag}) over {len(pages)} pages …")
            t0 = time.perf_counter()
            all_results[label] = await run_model(client, cfg, label, tag, pages)
            log(f"[run] {label} done in {time.perf_counter() - t0:.0f}s")
    payload = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "models": [f"{l}={t}" for l, t in models],
            "ollama_base_url": cfg.ollama_base_url,
            "judge_base_url": cfg.judge_base_url if cfg.use_judge else None,
            "judge_model": cfg.judge_model if cfg.use_judge else None,
            "temperature": cfg.temperature,
            "max_output_tokens": cfg.max_output_tokens,
            "sample_size": len(pages),
            "smoke": cfg.smoke,
        },
        "results": all_results,
    }
    cfg.results_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


# --------------------------------------------------------------------- report

def _stat(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    s = sorted(values)
    n = len(s)
    p95 = s[min(n - 1, int(0.95 * n))]
    return {
        "n": n,
        "median": round(statistics.median(s), 3),
        "mean": round(statistics.fmean(s), 3),
        "min": round(s[0], 3),
        "max": round(s[-1], 3),
        "p95": round(p95, 3),
    }


def aggregate(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for label, recs in payload["results"].items():
        ok = [r for r in recs if "error" not in r and r.get("output")]
        failed = [r for r in recs if "error" in r]
        m = [r["metrics"] for r in ok if r.get("metrics")]
        agg = {
            "pages_total": len(recs),
            "pages_ok": len(ok),
            "pages_failed": len(failed),
            "no_addition": _stat([x["no_addition"] for x in m]),
            "preservation": _stat([x["preservation"] for x in m]),
            "boilerplate_removed": _stat([x["boilerplate_removed"] for x in m]),
            "length_ratio": _stat([x["length_ratio"] for x in m]),
            "ttft_ms": _stat([r["ttft_ms"] for r in ok if r.get("ttft_ms") is not None]),
            "total_ms": _stat([r["total_ms"] for r in ok if r.get("total_ms") is not None]),
            "tok_s": _stat([r["tok_s"] for r in ok if r.get("tok_s")]),
            "completion_tokens": _stat([r["completion_tokens"] for r in ok if r.get("completion_tokens")]),
        }
        if payload["config"].get("judge_model"):
            jf = [r["judge"]["scores"].get("faithfulness") for r in ok if r.get("judge", {}).get("scores", {}).get("faithfulness") is not None]
            jn = [r["judge"]["scores"].get("noise_removal") for r in ok if r.get("judge", {}).get("scores", {}).get("noise_removal") is not None]
            jp = [r["judge"]["scores"].get("preservation") for r in ok if r.get("judge", {}).get("scores", {}).get("preservation") is not None]
            agg["judge_faithfulness"] = _stat([float(x) for x in jf])
            agg["judge_noise_removal"] = _stat([float(x) for x in jn])
            agg["judge_preservation"] = _stat([float(x) for x in jp])
        out[label] = agg
    return out


def _fmt_stat(s: dict[str, Any]) -> str:
    if s.get("n", 0) == 0:
        return "—"
    return f"{s['median']} (p95 {s['p95']})"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def print_summary(cfg: Config, payload: dict[str, Any]) -> None:
    """Print a side-by-side model comparison to the console.

    Centred on the three ways the models are compared:
      speed       wall_s   — total model time over all pages (Σ total_ms)
                             secs/doc — average seconds per page (wall_s / pages)
                             tok_s    — tokens/sec generation rate
      throughput  docs/s   — pages processed per second (pages / wall_s)
      quality     judge    — faithfulness / noise-removal / preservation, 1-5

    wall_s is model-only (excludes the judge), so it isolates how fast each
    model is. The Markdown/JSON report still carries the full per-page detail.
    """
    c = payload["config"]
    judge = bool(c.get("judge_model"))
    log(f"[report] summary — {c['sample_size']} pages, judge={c['judge_model'] or 'off'}")
    log("       speed: wall_s=total model time, secs/doc=avg per page, tok_s=tokens/sec; "
        "docs/s=pages/wall_s; judge scores are 1-5 (5=best)")

    cols = ["model", "pages", "wall_s", "secs/doc", "docs/s", "tok_s"]
    if judge:
        cols += ["j:faith", "j:noise", "j:presv"]

    data = []
    for label, recs in payload["results"].items():
        ok = [r for r in recs if "error" not in r and r.get("output")]
        wall_s = sum(r.get("total_ms", 0.0) for r in ok) / 1000.0
        secs_doc = wall_s / len(ok) if (wall_s > 0 and len(ok) > 0) else 0.0
        docs_s = len(ok) / wall_s if wall_s > 0 else 0.0
        tok_s = _median([r["tok_s"] for r in ok if r.get("tok_s")])

        row = [
            label,
            f"{len(ok)}/{len(recs)}",
            f"{wall_s:.1f}" if wall_s > 0 else "—",
            f"{secs_doc:.1f}" if secs_doc > 0 else "—",
            f"{docs_s:.3f}" if docs_s > 0 else "—",
            f"{tok_s:.2f}" if tok_s is not None else "—",
        ]
        if judge:
            def jmed(key: str) -> str:
                vals = [float(r["judge"]["scores"][key]) for r in ok
                        if r.get("judge", {}).get("scores", {}).get(key) is not None]
                med = _median(vals)
                return f"{med:.1f}" if med is not None else "—"
            row += [jmed("faithfulness"), jmed("noise_removal"), jmed("preservation")]
        data.append(row)

    widths = [max(len(cols[i]), *(len(r[i]) for r in data)) for i in range(len(cols))]

    def line(cells: list[str]) -> str:
        return "  ".join(
            cell.ljust(w) if i == 0 else cell.rjust(w)
            for i, (cell, w) in enumerate(zip(cells, widths))
        )

    print(line(cols), flush=True)
    print("  ".join("-" * w for w in widths), flush=True)
    for r in data:
        print(line(r), flush=True)


def _report_meta_lines(payload: dict[str, Any]) -> list[str]:
    """Title, run config, and the metric legend."""
    c = payload["config"]
    lines = [
        "# LLM fit-markdown cleanup benchmark",
        "",
        f"- ran: {payload['ran_at']}",
        f"- models: {', '.join(c['models'])}",
        f"- sample: {c['sample_size']} pages (smoke={c['smoke']})",
        f"- ollama: {c['ollama_base_url']}",
        f"- judge: {c['judge_model'] or 'off'} @ {c['judge_base_url'] or '—'}",
        f"- temperature={c['temperature']}, max_output_tokens={c['max_output_tokens']}",
        "",
        "Quality: `no_addition` = share of output 8-grams already in the input (≈1 = nothing fabricated). "
        "`preservation` = share of input content-words kept (≈1 = not over-trimmed). "
        "`boilerplate_removed` = fraction of boilerplate patterns removed. "
        "Judge scores are 1-5 (5 = best).",
        "",
    ]
    return lines


def _report_table_lines(
    agg: dict[str, dict[str, Any]], labels: list[str], judge: bool
) -> list[str]:
    """The side-by-side per-model summary table."""
    header = ["model", "pages", "no_addition", "preservation", "boilerplate_rm", "len_ratio"]
    if judge:
        header += ["judge_faith", "judge_noise", "judge_preserve"]
    header += ["ttft_ms", "total_ms", "tok_s", "out_tokens"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for label in labels:
        a = agg[label]
        row = [
            label,
            f"{a['pages_ok']}/{a['pages_total']}",
            _fmt_stat(a["no_addition"]),
            _fmt_stat(a["preservation"]),
            _fmt_stat(a["boilerplate_removed"]),
            _fmt_stat(a["length_ratio"]),
        ]
        if judge:
            row += [_fmt_stat(a.get("judge_faithfulness", {})), _fmt_stat(a.get("judge_noise_removal", {})), _fmt_stat(a.get("judge_preservation", {}))]
        row += [
            _fmt_stat(a["ttft_ms"]),
            _fmt_stat(a["total_ms"]),
            _fmt_stat(a["tok_s"]),
            _fmt_stat(a["completion_tokens"]),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _report_detail_lines(payload: dict[str, Any], labels: list[str]) -> list[str]:
    """The per-page detail tables (one per model)."""
    lines = ["## Per-page detail", ""]
    for label in labels:
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| url | no_add | pres | boil_rm | len_ratio | ttft_ms | total_ms | tok_s | judge(f/n/p) |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in payload["results"][label]:
            if "error" in r:
                lines.append(f"| {r['url']} | ERROR: {r['error'][:60]} | | | | | | | |")
                continue
            mt = r.get("metrics", {})
            js = r.get("judge", {}).get("scores", {})
            judge_cell = f"{js.get('faithfulness','–')}/{js.get('noise_removal','–')}/{js.get('preservation','–')}" if js else "—"
            lines.append(
                f"| {r['url']} | {mt.get('no_addition','–')} | {mt.get('preservation','–')} | "
                f"{mt.get('boilerplate_removed','–')} | {mt.get('length_ratio','–')} | "
                f"{r.get('ttft_ms','–')} | {r.get('total_ms','–')} | {r.get('tok_s','–')} | {judge_cell} |"
            )
        lines.append("")
    return lines


def write_report(cfg: Config, payload: dict[str, Any]) -> None:
    agg = aggregate(payload)
    labels = list(payload["results"].keys())
    judge = bool(payload["config"].get("judge_model"))
    lines = (
        _report_meta_lines(payload)
        + _report_table_lines(agg, labels, judge)
        + _report_detail_lines(payload, labels)
    )
    cfg.report_file.write_text("\n".join(lines), encoding="utf-8")


def report_from_disk(cfg: Config) -> None:
    if not cfg.results_file.exists():
        raise SystemExit(f"no results at {cfg.results_file} — run `python llm_md_cleanup_bench.py run` first")
    payload = json.loads(cfg.results_file.read_text(encoding="utf-8"))
    write_report(cfg, payload)


# --------------------------------------------------------------------- main

def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["sample", "run", "report", "all"])
    p.add_argument("--models", help="comma list label=ollama_tag (default: the 5 benchmark models)")
    p.add_argument("--sample-size", type=int, help="number of pages (default 5)")
    p.add_argument("--no-judge", action="store_true", help="skip the 27B LLM judge")
    p.add_argument("--concurrency", type=int, default=1, help="parallel pages per model (default 1 = fair CPU timing)")
    p.add_argument("--ollama-url", help="Ollama OpenAI-compatible base URL")
    p.add_argument("--judge-url", help="judge OpenAI-compatible base URL")
    p.add_argument("--out-dir", help="output directory (default benchmarks/results)")
    p.add_argument("--smoke", action="store_true", help="2 pages x first 2 models, quick sanity check")
    args = p.parse_args()

    cfg = load_config(args)

    if args.command in ("sample", "all"):
        pages = asyncio.run(build_sample(cfg))
        save_sample(cfg, pages)
        domains = sorted({p_["domain"] for p_ in pages})
        log(f"[sample] {len(pages)} pages across {len(domains)} domains -> {cfg.sample_file}")

    if args.command in ("run", "all"):
        asyncio.run(run_all(cfg))
        log(f"[run] results -> {cfg.results_file}")

    if args.command in ("report", "all"):
        if not cfg.results_file.exists():
            raise SystemExit(f"no results at {cfg.results_file} — run the `run` step first")
        payload = json.loads(cfg.results_file.read_text(encoding="utf-8"))
        write_report(cfg, payload)
        log(f"[report] -> {cfg.report_file}")
        print_summary(cfg, payload)


if __name__ == "__main__":
    main()

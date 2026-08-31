import json
import os
import sys
import urllib.request

DEFAULT_URL = "http://localhost:1234/v1"
url = (os.environ.get("LMSTUDIO_URL") or DEFAULT_URL).rstrip("/")
key = os.environ.get("LMSTUDIO_API_KEY", "")
preferred = os.environ.get("LMSTUDIO_PREFERRED_MODEL", "").strip()
config_path = os.environ.get("OPENCODE_CONFIG") or os.path.join(
    os.path.expanduser("~"), ".config", "opencode", "opencode.json"
)

# Maximum output tokens to advertise to opencode. Capped per-model by the
# context window below; matches the local opencode setup (models.dev catalog).
MAX_OUTPUT = 32768
# Used only if LM Studio's local API is unreachable or reports no window.
FALLBACK_CONTEXT = 125000


def get_json(full_url):
    req = urllib.request.Request(full_url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


print("Querying %s/models" % url)
try:
    data = get_json(url + "/models")
except Exception as e:
    sys.exit("::error::Failed to fetch models from %s: %s" % (url, e))

models = [m["id"] for m in data.get("data", [])]
if not models:
    sys.exit("::error::LM Studio at %s reports no loaded models." % url)
print("Loaded models: " + ", ".join(models))

# Ask LM Studio's local API for the context window each model actually has
# loaded, so the opencode config reflects reality instead of a hardcoded guess.
base = url[:-3] if url.endswith("/v1") else url
ctx = {}
try:
    v0 = get_json(base + "/api/v0/models")
    for m in v0.get("data", []):
        loaded = m.get("loaded_context_length") or 0
        mx = m.get("max_context_length") or 0
        if loaded or mx:
            ctx[m["id"]] = loaded or mx
    if ctx:
        print("Context windows: " + ", ".join("%s=%d" % (k, v) for k, v in ctx.items()))
except Exception as e:
    print(
        "::warning::Could not fetch context windows from %s/api/v0/models: %s "
        "(falling back to %d)" % (base, e, FALLBACK_CONTEXT)
    )

full = ["lmstudio/" + m for m in models]
if preferred:
    pref = preferred if preferred.startswith("lmstudio/") else "lmstudio/" + preferred
    if pref in full:
        pick = pref
    else:
        pick = full[0]
        print("::warning::Preferred model %s is not loaded; using %s" % (pref, pick))
else:
    pick = full[0]


def limits_for(model_id):
    c = ctx.get(model_id) or FALLBACK_CONTEXT
    return {"context": c, "output": min(MAX_OUTPUT, c)}


config = {}
if os.path.exists(config_path):
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:
        config = {}
config.setdefault("$schema", "https://opencode.ai/config.json")
config.setdefault("compaction", {"auto": True, "reserved": 20000})
config.setdefault("permission", {"*": "allow"})
provider = config.setdefault("provider", {}).setdefault("lmstudio", {})
provider.setdefault("npm", "@ai-sdk/openai-compatible")
provider.setdefault("name", "LM Studio (local)")
options = provider.setdefault("options", {})
options.setdefault("apiKey", key)
options.setdefault("baseURL", url)
provider["models"] = {
    m: {"name": m, "limit": limits_for(m)} for m in models
}

d = os.path.dirname(config_path)
if d:
    os.makedirs(d, exist_ok=True)
with open(config_path, "w") as f:
    json.dump(config, f, indent=2)
print("Wrote opencode config with %d model(s) to %s" % (len(models), config_path))
print("Selected model: " + pick)

out = os.environ.get("GITHUB_OUTPUT")
if out:
    with open(out, "a") as f:
        f.write("model=" + pick + "\n")
        f.write("models=" + ",".join(models) + "\n")

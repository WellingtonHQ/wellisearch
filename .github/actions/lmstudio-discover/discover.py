import json
import os
import sys
import urllib.request

DEFAULT_URL = "https://desktop-7n8a289.tailc2fbf4.ts.net:1234/v1"
url = (os.environ.get("LMSTUDIO_URL") or DEFAULT_URL).rstrip("/")
key = os.environ.get("LMSTUDIO_API_KEY", "")
preferred = os.environ.get("LMSTUDIO_PREFERRED_MODEL", "").strip()
config_path = os.environ.get("OPENCODE_CONFIG") or os.path.join(
    os.path.expanduser("~"), ".config", "opencode", "opencode.json"
)

print("Querying %s/models" % url)
try:
    req = urllib.request.Request(url + "/models")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
except Exception as e:
    sys.exit("::error::Failed to fetch models from %s: %s" % (url, e))

models = [m["id"] for m in data.get("data", [])]
if not models:
    sys.exit("::error::LM Studio at %s reports no loaded models." % url)
print("Loaded models: " + ", ".join(models))

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
    m: {"name": m, "limit": {"context": 125000, "output": 8192}} for m in models
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

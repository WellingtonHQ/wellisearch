"""c4ai_llm_thinking - inject Qwen3.8 thinking control into Crawl4AI's LLM content filter.

Auto-loaded at Python interpreter startup by zz_c4ai_llm.pth (site-packages).
Purely additive: wraps LLMContentFilter.__init__ so every instance carries
extra_args={"extra_body": {...}}. Crawl4AI forwards extra_args to litellm's
completion(), and litellm merges extra_body into the request body top level -
the exact layout verified working against LM Studio in functions/better_qwen3_8.py
(top-level reasoning_effort + enable_thinking + chat_template_kwargs).

Env knobs (all optional; patch is a no-op unless LLM_REASONING_EFFORT is set):
  LLM_REASONING_EFFORT   xhigh | medium | low | none   (none = thinking off)
  LLM_PRESERVE_THINKING  true | false                  (default: true)
  LLM_EXTRA_BODY         JSON object merged over the defaults (escape hatch)
"""
import functools
import json
import os

_OFF = ("none", "off", "false", "0")


def _build_extra_body():
    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip()
    if not effort:
        return None
    thinking_on = effort.lower() not in _OFF
    preserve = (os.environ.get("LLM_PRESERVE_THINKING") or "true").strip().lower() in ("1", "true", "yes")
    extra_body = {
        "reasoning_effort": effort,
        "enable_thinking": thinking_on,
        "chat_template_kwargs": {
            "enable_thinking": thinking_on,
            "preserve_thinking": preserve,
        },
    }
    raw = (os.environ.get("LLM_EXTRA_BODY") or "").strip()
    if raw:
        try:
            override = json.loads(raw)
        except ValueError:
            override = None
        if isinstance(override, dict):
            extra_body.update(override)
    return extra_body


def _install():
    extra_body = _build_extra_body()
    if not extra_body:
        return
    try:
        from crawl4ai.content_filter_strategy import LLMContentFilter
    except Exception:
        return
    orig_init = LLMContentFilter.__init__
    if getattr(orig_init, "_c4ai_thinking_patched", False):
        return

    @functools.wraps(orig_init)
    def patched_init(self, *args, **kwargs):
        positional = "extra_args" not in kwargs and len(args) >= 14
        if "extra_args" in kwargs:
            ea_in = kwargs["extra_args"]
        elif positional:
            ea_in = args[13]
        else:
            ea_in = None
        if ea_in is not None and not isinstance(ea_in, dict):
            return orig_init(self, *args, **kwargs)
        ea = dict(ea_in) if isinstance(ea_in, dict) else {}
        inner = ea.get("extra_body")
        merged = dict(inner) if isinstance(inner, dict) else {}
        for k, v in extra_body.items():
            cur = merged.get(k)
            if isinstance(v, dict) and isinstance(cur, dict):
                sub = dict(v)
                sub.update(cur)
                merged[k] = sub
            else:
                merged.setdefault(k, v)
        ea["extra_body"] = merged
        if positional:
            args = args[:13] + (ea,) + args[14:]
            return orig_init(self, *args, **kwargs)
        kwargs["extra_args"] = ea
        return orig_init(self, *args, **kwargs)

    patched_init._c4ai_thinking_patched = True
    LLMContentFilter.__init__ = patched_init


try:
    _install()
except Exception:
    pass

"""ai_hub.py - AutoMind's silent HTTP AI provider bridge.

Builds structured prompts and calls configured APIs with urllib. API keys are
read from environment variables, config.py, or optional git-ignored
secrets_local.py. This module's silent API path never opens a browser.
"""

import re
import urllib.parse
import urllib.request
import urllib.error
import json
import os

import config
import executor

DOMAIN_KEYWORDS = {
    "crypto": {"bitcoin", "btc", "ethereum", "eth", "crypto", "altcoin", "defi",
               "solana", "token", "coin", "blockchain", "xrp", "dogecoin", "bnb"},
    "stocks": {"stock", "stocks", "share", "shares", "nifty", "sensex", "banknifty",
               "equity", "ipo", "dividend", "nse", "bse", "earnings", "portfolio",
               "etf", "trading", "intraday", "options", "futures", "zerodha",
               "groww", "market", "invest", "investing", "mutual", "sip"},
    "tech": {"python", "code", "coding", "android", "app", "api", "bug", "error",
             "script", "javascript", "linux", "database", "sql", "server",
             "algorithm", "debug", "pydroid"},
}

PROFILES = {
    "stocks": {
        "role": "a senior equity research analyst and technical strategist covering Indian (NSE/BSE) and global markets",
        "sections": ["Executive summary (3 lines)", "Technical picture: trend, momentum, key support/resistance",
                     "Fundamental and catalyst checklist", "Bull / base / bear scenarios with trigger levels",
                     "Risks and position-sizing considerations", "What to verify next and where"],
    },
    "crypto": {
        "role": "a crypto market analyst experienced in on-chain data, liquidity and regulation",
        "sections": ["Executive summary (3 lines)", "Price structure and momentum",
                     "On-chain, liquidity and sentiment signals", "Bull / base / bear scenarios with levels",
                     "Regulatory, security and counterparty risks", "What to verify next and where"],
    },
    "tech": {
        "role": "a principal software engineer who writes precise, tested, minimal solutions",
        "sections": ["Direct answer", "Step-by-step solution", "Complete working code",
                     "Common pitfalls", "How to verify it works"],
    },
    "general": {
        "role": "a domain expert researcher who cites reasoning and separates facts from opinion",
        "sections": ["Executive summary (3 lines)", "Key findings", "Evidence, caveats and uncertainty",
                     "Practical next steps", "Open questions"],
    },
}


def detect_domain(text):
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    for domain in ("crypto", "stocks", "tech"):
        if words & DOMAIN_KEYWORDS[domain]:
            return domain
    return "general"


def build_expert_prompt(intent, domain="general", prefs=None):
    prefs = prefs or {}
    prof = PROFILES.get(domain, PROFILES["general"])
    lines = [
        "ROLE: Act as %s." % prof["role"],
        "",
        "OBJECTIVE: %s" % intent.strip(),
        "",
        "CONTEXT: Reader is an individual using a phone. Preferred currency: %s. Reply language: %s."
        % (prefs.get("currency", "INR"), prefs.get("response_language", "English")),
        "",
        "REQUIREMENTS:",
        "- Use current, verifiable information; state the date of any data you use.",
        "- Never invent numbers, quotes or sources; say 'unknown' when unsure.",
        "- Be concise and structured; no filler.",
    ]
    if domain in ("stocks", "crypto"):
        lines.append("- Educational analysis only, not personalised financial advice.")
    lines += ["", "OUTPUT FORMAT (use these headings):"]
    lines += ["%d. %s" % (i, s) for i, s in enumerate(prof["sections"], 1)]
    lines += ["", "End with a one-line CONCLUSION I can save to my notes."]
    return "\n".join(lines)


def fit_quoted(text, limit=None):
    """Trim text so its url-quoted form stays within limit characters."""
    limit = limit or config.MAX_URL_QUERY_CHARS
    quoted = urllib.parse.quote(text)
    while len(quoted) > limit and len(text) > 50:
        text = text[: int(len(text) * limit / len(quoted)) - 10].rstrip()
        quoted = urllib.parse.quote(text)
    return text, quoted


def build_candidates(provider, prompt, use_custom_schemes=False):
    """Ordered launch routes for a provider."""
    target = config.AI_TARGETS[provider]
    _, quoted = fit_quoted(prompt)
    urls = [t.replace("{q}", quoted) for t in target["schemes"] + target["web"]]
    return executor.order_candidates(urls, use_custom_schemes)


def prepare(provider, prompt):
    """Save the prompt to disk and clipboard. Returns (clip_method, path)."""
    path = None
    try:
        with open(config.LAST_PROMPT_PATH, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        path = config.LAST_PROMPT_PATH
    except OSError:
        pass
    return executor.copy_to_clipboard(prompt), path


def launch(provider, prompt, route_index=0, use_custom_schemes=False):
    """Compatibility wrapper: silently call provider API; never open a URL.

    Returns (ok, provider_label, error, attempts_count), preserving the legacy
    four-value return shape while changing the second value from a URL to the
    provider label. Callers must not treat this as a browser URL.
    """
    result = complete_silent(prompt, preferred=provider)
    if result.get("ok"):
        return True, str(result.get("provider", provider)), "", len(result.get("attempts", []))
    return False, "", str(result.get("error", "AI request failed")), len(result.get("attempts", []))


# Silent API provider bridge -------------------------------------------------
# Environment variables are preferred; optional config attributes support local
# config.py files without requiring secrets to be committed to source control.
_PROVIDER_SPECS = {
    "gemini": {"key_env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "config_keys": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "model_env": "GEMINI_MODEL", "default_model": "gemini-2.5-flash"},
    "claude": {"key_env": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"), "config_keys": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"), "model_env": "CLAUDE_MODEL", "default_model": "claude-3-7-sonnet-latest"},
    "groq": {"key_env": ("GROQ_API_KEY",), "config_keys": ("GROQ_API_KEY",), "model_env": "GROQ_MODEL", "default_model": "llama-3.3-70b-versatile"},
    "openrouter": {"key_env": ("OPENROUTER_API_KEY",), "config_keys": ("OPENROUTER_API_KEY",), "model_env": "OPENROUTER_MODEL", "default_model": "openai/gpt-4o-mini"},
}

def _secret(spec):
    for name in spec["key_env"]:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in spec["config_keys"]:
        value = str(getattr(config, name, "") or "").strip()
        if value:
            return value
    try:
        import secrets_local  # optional; add this file locally, never commit it
        for name in spec["config_keys"]:
            value = str(getattr(secrets_local, name, "") or "").strip()
            if value:
                return value
    except ImportError:
        pass
    return ""

def _provider_order(preferred=None):
    names = [preferred] if preferred else []
    names += ["gemini", "claude", "groq", "openrouter"]
    out = []
    for name in names:
        if name in _PROVIDER_SPECS and name not in out and _secret(_PROVIDER_SPECS[name]):
            out.append(name)
    return out

def _http_json(url, payload, headers, timeout=45):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=max(5, min(int(timeout), 90))) as response:
        raw = response.read(2_000_000).decode("utf-8", "replace")
        return json.loads(raw)

def _call_provider(provider, prompt, timeout=45):
    spec = _PROVIDER_SPECS[provider]
    key = _secret(spec)
    model = os.environ.get(spec["model_env"], "").strip() or spec["default_model"]
    if provider == "gemini":
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (urllib.parse.quote(model, safe="-._"), urllib.parse.quote(key, safe=""))
        data = _http_json(url, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}, {}, timeout)
        return "".join(p.get("text", "") for c in data.get("candidates", []) for p in c.get("content", {}).get("parts", []))
    if provider == "claude":
        data = _http_json("https://api.anthropic.com/v1/messages", {"model": model, "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        return "".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
    base = "https://api.groq.com/openai/v1/chat/completions" if provider == "groq" else "https://openrouter.ai/api/v1/chat/completions"
    data = _http_json(base, {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}, {"Authorization": "Bearer " + key}, timeout)
    choices = data.get("choices", [])
    return str(choices[0].get("message", {}).get("content", "")) if choices else ""

def complete_silent(prompt, preferred=None, timeout=45):
    """Call configured providers silently with ordered multi-key fallback.

    Returns a dict: ok, provider, model, text, attempts, error. No browser,
    clipboard, UI prompt, or key is printed. Only providers with keys are tried.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "prompt must be a non-empty string", "attempts": []}
    order = _provider_order(preferred)
    if not order:
        return {"ok": False, "error": "No AI API keys configured. Set GEMINI_API_KEY, ANTHROPIC_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY.", "attempts": []}
    attempts = []
    for provider in order:
        try:
            text = _call_provider(provider, prompt, timeout)
            if not text.strip():
                raise ValueError("provider returned an empty response")
            spec = _PROVIDER_SPECS[provider]
            model = os.environ.get(spec["model_env"], "").strip() or spec["default_model"]
            attempts.append({"provider": provider, "ok": True})
            return {"ok": True, "provider": provider, "model": model, "text": text.strip(), "attempts": attempts}
        except Exception as exc:
            # Provider errors may echo request URLs (Gemini keys are query params).
            # Redact every configured key before returning diagnostics to UI/logs.
            message = str(exc)
            for candidate in _PROVIDER_SPECS.values():
                secret = _secret(candidate)
                if secret:
                    message = message.replace(secret, "[REDACTED]")
                    message = message.replace(urllib.parse.quote(secret, safe=""), "[REDACTED]")
            attempts.append({"provider": provider, "ok": False,
                             "error": (type(exc).__name__ + ": " + message)[:220]})
    return {"ok": False, "error": "All configured providers failed", "attempts": attempts}

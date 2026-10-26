"""
config.py - Central registry for AutoMind (zero API keys).

Holds: on-device AI app / web targets (deep links), Android app registry,
default preferences and filesystem paths.

There are NO API keys, tokens or REST endpoints anywhere in this project.
Package names are informational only: apps are never launched through
`am start` / `monkey` / any shell command, only through Python's
`webbrowser` module using https App Links and optional custom URI schemes.
"""

import os
import sys

APP_NAME = "AutoMind"
VERSION = "2.5.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGINS_DIR = os.path.join(BASE_DIR, "plugins")
KNOWLEDGE_BASE_PATH = os.path.join(BASE_DIR, "knowledge_base.json")
LAST_PROMPT_PATH = os.path.join(BASE_DIR, "last_prompt.txt")
SMART_SETTINGS_PATH = os.path.join(BASE_DIR, "smart_update.json")

MAX_URL_QUERY_CHARS = 6000   # max url-quoted prompt length put into a link

DEFAULT_PREFERENCES = {
    "default_ai": "gemini",
    "cache_ttl_hours": 24 * 30,
    "similarity_threshold": 0.6,
    "use_custom_schemes": False,
    "response_language": "English",
    "currency": "INR",
    "offline_mode": False,
    "auto_remember": False,
}


def is_android():
    """True when running on Android (Pydroid 3, Termux, ...)."""
    # Pydroid may not expose Android env vars to Python. Detect common Android
    # filesystem/runtime signals without importing Kivy's unrelated `android`.
    return bool(
        os.environ.get("ANDROID_ROOT")
        or os.environ.get("ANDROID_DATA")
        or hasattr(sys, "getandroidapilevel")
        or os.path.exists("/system/bin/am")
        or "/android" in sys.executable.lower()
        or "ru.iiec.pydroid3" in sys.executable.lower()
    )


# --------------------------------------------------------------------------
# On-device AI apps / web AIs.  {q} = url-quoted prompt.
#   schemes : custom app deep links (tried first only if use_custom_schemes)
#   web     : https links (Android App Links open the installed app when
#             present, otherwise the browser)
#   prefill : True if the link can carry the prompt; False means the user
#             pastes from the clipboard.
# --------------------------------------------------------------------------
AI_TARGETS = {
    "gemini": {
        "label": "Google Gemini",
        "package": "com.google.android.apps.bard",
        "schemes": [],
        "web": ["https://gemini.google.com/"],
        "prefill": False,
    },
    "chatgpt": {
        "label": "ChatGPT",
        "package": "com.openai.chatgpt",
        "schemes": ["chatgpt://chat?q={q}"],
        "web": ["https://chatgpt.com/?q={q}"],
        "prefill": True,
    },
    "claude": {
        "label": "Anthropic Claude",
        "package": "com.anthropic.claude",
        "schemes": [],
        "web": ["https://claude.ai/new?q={q}"],
        "prefill": True,
    },
    "copilot": {
        "label": "Microsoft Copilot",
        "package": "com.microsoft.copilot",
        "schemes": [],
        "web": ["https://copilot.microsoft.com/?q={q}"],
        "prefill": True,
    },
    "groq": {
        "label": "Groq (web)",
        "package": "",
        "schemes": [],
        "web": ["https://groq.com"],
        "prefill": False,
    },
}
AI_ORDER = ["gemini", "chatgpt", "claude", "copilot", "groq"]

# --------------------------------------------------------------------------
# Android app registry. Templates: {q} url-quoted query, {tag} hashtag-safe,
# {handle} profile handle. Candidates are tried in order.
# --------------------------------------------------------------------------
APPS = {
    "youtube": {
        "label": "YouTube", "package": "com.google.android.youtube",
        "aliases": ["youtube", "yt"],
        "home": ["https://www.youtube.com/"],
        "search": ["https://www.youtube.com/results?search_query={q}"],
    },
    "zerodha": {
        "label": "Zerodha Kite", "package": "com.zerodha.kite3",
        "aliases": ["zerodha", "kite"],
        "home": ["https://kite.zerodha.com/"],
        "search": ["https://kite.zerodha.com/"],
    },
    "groww": {
        "label": "Groww", "package": "com.nextbillion.groww",
        "aliases": ["groww"],
        "home": ["https://groww.in/"],
        "search": ["https://groww.in/search?q={q}"],
    },
    "investing": {
        "label": "Investing.com", "package": "com.fusionmedia.investing",
        "aliases": ["investing", "investing.com"],
        "home": ["https://www.investing.com/"],
        "search": ["https://www.investing.com/search/?q={q}"],
    },
    "instagram": {
        "label": "Instagram", "package": "com.instagram.android",
        "aliases": ["instagram", "insta", "ig"],
        "home": ["instagram://app", "https://www.instagram.com/"],
        "profile": ["instagram://user?username={handle}",
                    "https://www.instagram.com/{handle}/"],
        "search": ["instagram://tag?name={tag}",
                   "https://www.instagram.com/explore/tags/{tag}/"],
    },
    "twitter": {
        "label": "Twitter / X", "package": "com.twitter.android",
        "aliases": ["twitter", "x", "x.com", "tweet"],
        "home": ["twitter://timeline", "https://x.com/home"],
        "search": ["twitter://search?query={q}", "https://x.com/search?q={q}"],
    },
    "browser": {
        "label": "Chrome / Browser", "package": "com.android.chrome",
        "aliases": ["browser", "google", "chrome", "web"],
        "home": ["https://www.google.com/"],
        "search": ["https://www.google.com/search?q={q}"],
    },
}
FINANCE_APPS = {"zerodha", "groww", "investing"}


def load_smart_settings():
    """Read safe, declarative settings; never execute code from this JSON file."""
    import json
    settings = {}
    try:
        with open(SMART_SETTINGS_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        allowed = set(DEFAULT_PREFERENCES)
        for key, value in raw.items():
            if key in allowed and isinstance(value, type(DEFAULT_PREFERENCES[key])):
                if key == "default_ai" and value not in AI_TARGETS:
                    continue
                if key == "similarity_threshold" and not 0 <= value <= 1:
                    continue
                if key == "cache_ttl_hours" and value < 0:
                    continue
                settings[key] = value
    except (OSError, ValueError, TypeError):
        pass
    return settings


def save_smart_setting(name, value):
    """Persist one whitelisted setting in smart_update.json without executing code."""
    import json
    if name not in DEFAULT_PREFERENCES:
        raise ValueError("Unknown setting: " + str(name))
    try:
        with open(SMART_SETTINGS_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    raw[name] = value
    tmp = SMART_SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, SMART_SETTINGS_PATH)

"""
executor.py - Safe native execution engine.

Uses validated HTTP(S) URLs, Android VIEW intent via an argument-list subprocess
(no shell), optional bridge, and webbrowser fallback. Failed launches stay failures.
"""

import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import config

FILLER = {
    "open", "launch", "start", "go", "to", "search", "for", "on", "in", "and",
    "then", "show", "me", "the", "find", "look", "up", "check", "please", "app",
    "my", "a", "with", "using", "via", "at",
}
DIRECT_VERBS = ("open", "launch", "start", "go to", "goto")


@dataclass
class Action:
    app: str
    query: Optional[str] = None

    def describe(self):
        label = config.APPS[self.app]["label"]
        return "%s -> %s" % (label, self.query) if self.query else "%s (home)" % label


@dataclass
class ExecResult:
    app: str
    url: str
    ok: bool
    error: str = ""


# ---------------- clipboard bridge (no shell involved) ----------------
def _clip_pyperclip(text):
    import pyperclip  # optional
    pyperclip.copy(text)
    return True


def _clip_jnius(text):
    from jnius import autoclass, cast  # available on Pydroid 3 via pip
    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    Context = autoclass("android.content.Context")
    ClipData = autoclass("android.content.ClipData")
    manager = cast("android.content.ClipboardManager",
                   activity.getSystemService(Context.CLIPBOARD_SERVICE))
    manager.setPrimaryClip(ClipData.newPlainText("ai_prompt", text))
    return True


def _clip_tk(text):
    # Terminal me Tkinter window nahi ban sakti, isliye bina crash kiye skip karein
    return False
    
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    root.destroy()
    return True


def copy_to_clipboard(text):
    """Best-effort clipboard copy. Returns the method name used, or None."""
    for name, fn in (("pyperclip", _clip_pyperclip), ("jnius", _clip_jnius),
                     ("tkinter", _clip_tk)):
        try:
            if fn(text):
                return name
        except Exception:
            continue
    return None


# ---------------- URL dispatch ----------------
def _fill(template, query):
    q = query or ""
    tag = re.sub(r"[^a-z0-9_]", "", q.lower().replace(" ", ""))
    handle = re.sub(r"[^A-Za-z0-9_.]", "", q.lstrip("@"))
    return (template.replace("{q}", urllib.parse.quote(q))
            .replace("{tag}", tag).replace("{handle}", handle))


def order_candidates(urls, use_custom_schemes=False):
    """https first (safest) unless custom schemes are preferred."""
    web = [u for u in urls if u.startswith(("http://", "https://"))]
    custom = [u for u in urls if u not in web]
    if not config.is_android():
        return web or urls
    return (custom + web) if use_custom_schemes else (web + custom)


def build_candidates(app, query=None, use_custom_schemes=False):
    spec = config.APPS[app]
    if query and query.startswith("@") and "profile" in spec:
        templates = spec["profile"]
    elif query and "search" in spec:
        templates = spec["search"]
    else:
        templates = spec["home"]
    return order_candidates([_fill(t, query) for t in templates], use_custom_schemes)


def open_url(url):
    """Best-effort URL launch without assuming Android/Kivy APIs exist.

    Returns (True, "") only when a bridge or webbrowser reports acceptance.
    A failed launch is not treated as success; callers can display the URL for
    manual opening. The generic ``android`` module is deliberately not used:
    it is not a universal Android API and may be Kivy-specific.
    """
    errors = []
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return False, "Only HTTP(S) URLs are supported by the safe launcher."
    # Pydroid's webbrowser module often has no registered browser. On Android,
    # use the OS Activity Manager directly when available. Never use a shell
    # string: each argument is passed separately to avoid command injection.
    if config.is_android():
        am = shutil.which("am")
        if not am and os.path.isfile("/system/bin/am"):
            am = "/system/bin/am"
        if am:
            # Pydroid/Android builds differ: some reject the default user but
            # accept an explicit user 0. Try both safely; never use shell=True.
            attempts = (
                [am, "start", "-a", "android.intent.action.VIEW", "-d", url],
                [am, "start", "--user", "0", "-a", "android.intent.action.VIEW",
                 "-c", "android.intent.category.BROWSABLE", "-d", url],
            )
            for argv in attempts:
                try:
                    result = subprocess.run(
                        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        universal_newlines=True, timeout=12, check=False,
                    )
                    if result.returncode == 0:
                        return True, ""
                    detail = (result.stderr or result.stdout or "am returned non-zero").strip()
                    errors.append("Android VIEW intent: " + detail[:180])
                except Exception as exc:
                    errors.append("Android VIEW intent: %s: %s" % (type(exc).__name__, exc))
        else:
            errors.append("Android Activity Manager 'am' was not found")
    # Optional SL4A/QPython bridge. Check the expected method before calling.
    try:
        from androidhelper import Android
        droid = Android()
        starter = getattr(droid, "startActivity", None)
        if callable(starter):
            starter("android.intent.action.VIEW", url)
            return True, ""
        errors.append("androidhelper: startActivity is unavailable")
    except ImportError:
        errors.append("androidhelper is not installed")
    except Exception as exc:
        errors.append("androidhelper: " + str(exc))
    # No webbrowser fallback: this project must never create implicit browser
    # popups. URL-based app actions require an explicit Android bridge/intent.
    errors.append("automatic browser launch is disabled")
    return False, "; ".join(errors)


def open_first_working(urls):
    last = "no candidates"
    for url in urls:
        ok, err = open_url(url)
        if ok:
            return True, url, ""
        last = err
    return False, (urls[0] if urls else ""), last


def open_app(app, query=None, use_custom_schemes=False):
    urls = build_candidates(app, query, use_custom_schemes)
    ok, used, err = open_first_working(urls)
    return ExecResult(app, used, ok, err)


# ---------------- intent -> actions ----------------
def _tokens(text):
    return [t.strip(".") for t in re.findall(r"[A-Za-z0-9@\.\-_]+", text)]


def detect_apps(text):
    found = []
    for tok in _tokens(text.lower()):
        for app, spec in config.APPS.items():
            if tok in spec["aliases"] and app not in found:
                found.append(app)
    return found


def is_direct_command(text):
    low = text.lower().strip()
    return low.startswith(DIRECT_VERBS) and bool(detect_apps(low))


def clean_query(text):
    alias_words = {a for s in config.APPS.values() for a in s["aliases"]}
    kept = [t for t in text.split()
            if t.strip(".,").lower() not in FILLER and t.strip(".,").lower() not in alias_words]
    return " ".join(kept).strip() or None


def plan_actions(intent, domain="general"):
    apps = detect_apps(intent)
    query = clean_query(intent) if apps else intent.strip()
    actions = []
    if apps:
        actions = [Action(a, query) for a in apps]
    elif domain == "stocks":
        actions = [Action("investing", query), Action("groww", query),
                   Action("youtube", query + " analysis")]
    elif domain == "crypto":
        actions = [Action("investing", query), Action("youtube", query + " analysis today"),
                   Action("twitter", query)]
    else:
        actions = [Action("browser", query)]
    seen, unique = set(), []
    for act in actions:
        if (act.app, act.query) not in seen:
            seen.add((act.app, act.query))
            unique.append(act)
    return unique[:4]


def execute_plan(actions, use_custom_schemes=False, delay=1.5, log=print):
    results = []
    for i, act in enumerate(actions):
        res = open_app(act.app, act.query, use_custom_schemes)
        log("  [%s] %s%s" % ("OK" if res.ok else "FAIL", act.describe(),
                             "" if res.ok else " (%s)" % res.error))
        if not res.ok:
            log("      open manually: " + res.url)
        results.append(res)
        if i < len(actions) - 1:
            time.sleep(delay)
    return results

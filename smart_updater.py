"""
smart_updater.py - Direct smart hot-update engine / skill injector.

`update` lets the user add features on the fly: paste Python code, point to a
local file, download from a URL, or drop files into plugins/_inbox. The code
is validated, written into /plugins and hot-loaded with importlib - no restart
and no core file is touched. Files dropped straight into /plugins are also
picked up automatically at the next prompt.
"""

import hashlib
import os
import re
import shutil
import time
import urllib.request

import plugin_manager
from plugin_manager import PluginError

MAX_BYTES = 512 * 1024

TEMPLATE = '''PLUGIN_INFO = {
    "name": "hello",
    "version": "1.0.0",
    "description": "Says hello",
    "commands": ["hello"],
}


def run(args, context):
    # context keys: memory, hub, executor, config
    return "Hello, " + (args or "world") + "!"
'''


def _input(prompt):
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _confirm(question):
    while True:
        ans = _input(question + " (y/n): ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            return False


def github_raw(url):
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    return "https://raw.githubusercontent.com/%s/%s/%s" % m.groups() if m else url


class SmartUpdater:
    def __init__(self, manager, memory=None):
        self.manager = manager
        self.memory = memory

    # ---------------- core install ----------------
    def install_source(self, source, origin="pasted code", auto_confirm=False):
        """Validate, confirm, write and hot-load. Returns (ok, message)."""
        try:
            info = plugin_manager.inspect_source(source)
        except PluginError as exc:
            return False, "Rejected: %s" % exc
        stem = plugin_manager.sanitize_stem(info["name"])
        if stem in ("", "init") or stem.startswith("_"):
            return False, "Rejected: unusable plugin name"
        target = os.path.join(self.manager.plugins_dir, stem + ".py")
        existing = self.manager.plugins.get(stem)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]

        print("\n  Plugin : %s v%s" % (info["name"], info["version"]))
        print("  About  : %s" % (info["description"] or "-"))
        print("  Cmds   : %s" % ", ".join(info["commands"]))
        print("  Source : %s  (%d lines, sha256 %s)" % (origin, source.count("\n") + 1, digest))
        if existing:
            print("  Update : replaces v%s" % existing.version)
        if info["warnings"]:
            print("  Notice : " + "; ".join(info["warnings"]))
        print("  Plugins run with full permissions of this app. Install only trusted code.")
        if not auto_confirm and not _confirm("Install and hot-load now?"):
            return False, "Cancelled."

        backup = None
        if os.path.exists(target):
            backup = os.path.join(self.manager.plugins_dir, "_backup",
                                  "%s_%s.py" % (stem, time.strftime("%Y%m%d_%H%M%S")))
            shutil.copy2(target, backup)
        tmp = os.path.join(self.manager.plugins_dir, "." + stem + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(source)
        os.replace(tmp, target)
        try:
            self.manager.load_file(target)
        except PluginError as exc:
            if backup:
                shutil.copy2(backup, target)
            else:
                os.remove(target)
            self.manager.refresh()
            return False, "Load failed, rolled back: %s" % exc
        if self.memory:
            self.memory.log("plugin_install", "%s v%s from %s" % (info["name"], info["version"], origin))
        return True, "Installed '%s' - live now. Commands: %s" % (info["name"], ", ".join(info["commands"]))

    # ---------------- sources ----------------
    def install_file(self, path, auto_confirm=False):
        path = os.path.expanduser(path.strip().strip('"').strip("'"))
        if not os.path.isfile(path):
            return False, "File not found: %s" % path
        if os.path.getsize(path) > MAX_BYTES:
            return False, "File too large (limit 512 KB)."
        try:
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            return False, "Cannot read file: %s" % exc
        return self.install_source(source, "file " + path, auto_confirm)

    def install_url(self, url, auto_confirm=False):
        url = github_raw(url.strip())
        if not url.startswith("https://"):
            return False, "Only HTTPS URLs are allowed for plugin downloads."
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "automind"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read(MAX_BYTES + 1)
        except Exception as exc:
            return False, "Download failed: %s" % exc
        if len(raw) > MAX_BYTES:
            return False, "Download too large (limit 512 KB)."
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False, "Downloaded file is not UTF-8 text."
        return self.install_source(source, url, auto_confirm)

    def install_pasted(self):
        print("Paste your plugin code. Finish with a line containing only END")
        lines = []
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break
            if line.strip() == "END":
                break
            lines.append(line)
        if not "".join(lines).strip():
            return False, "Nothing pasted."
        return self.install_source("\n".join(lines) + "\n", "pasted code")

    def install(self, source, auto_confirm=False):
        """One-step install from a URL or a local path."""
        if source.strip().lower().startswith(("http://", "https://")):
            if not source.strip().lower().startswith("https://"):
                return False, "Only HTTPS URLs are allowed for plugin downloads."
            return self.install_url(source, auto_confirm)
        return self.install_file(source, auto_confirm)

    def process_inbox(self):
        inbox = os.path.join(self.manager.plugins_dir, "_inbox")
        done = os.path.join(inbox, "_processed")
        os.makedirs(done, exist_ok=True)
        files = sorted(f for f in os.listdir(inbox) if f.endswith(".py"))
        if not files:
            return False, "Inbox is empty. Drop .py files into: " + inbox
        installed = 0
        for fn in files:
            path = os.path.join(inbox, fn)
            ok, msg = self.install_file(path)
            print("  %s: %s" % (fn, msg))
            if ok:
                installed += 1
                shutil.move(path, os.path.join(done, "%s.%s" % (fn, time.strftime("%H%M%S"))))
        return installed > 0, "%d of %d inbox file(s) installed." % (installed, len(files))

    def remove(self, name):
        stem = plugin_manager.sanitize_stem(name)
        path = os.path.join(self.manager.plugins_dir, stem + ".py")
        if not os.path.isfile(path):
            return False, "No such plugin: " + name
        if not _confirm("Remove plugin '%s'?" % stem):
            return False, "Cancelled."
        shutil.move(path, os.path.join(self.manager.plugins_dir, "_backup",
                                       "%s_removed_%s.py" % (stem, time.strftime("%Y%m%d_%H%M%S"))))
        self.manager.refresh()
        if self.memory:
            self.memory.log("plugin_remove", stem)
        return True, "Removed '%s' (backup kept in plugins/_backup)." % stem

    # ---------------- interactive menu ----------------
    def show_plugins(self):
        plugins = self.manager.list_plugins()
        if not plugins:
            print("No plugins loaded.")
        for p in plugins:
            print("  %-16s v%-7s cmds: %s\n      %s" % (p.name, p.version, ", ".join(p.commands), p.description))
        for stem, err in self.manager.errors.items():
            print("  [problem] %s: %s" % (stem, err))

    def interactive(self):
        while True:
            print("\n=== Smart Update Engine ===")
            print(" 1) Paste Python code (new skill / feature)")
            print(" 2) Install from local file path")
            print(" 3) Install from URL")
            print(" 4) Process drop inbox (plugins/_inbox)")
            print(" 5) Remove a plugin")
            print(" 6) List plugins")
            print(" 7) Show plugin template")
            print(" 0) Back")
            choice = _input("update> ").strip()
            if choice in ("0", "", "q", "back"):
                return
            if choice == "1":
                ok, msg = self.install_pasted()
            elif choice == "2":
                ok, msg = self.install_file(_input("File path: "))
            elif choice == "3":
                ok, msg = self.install_url(_input("URL: "))
            elif choice == "4":
                ok, msg = self.process_inbox()
            elif choice == "5":
                ok, msg = self.remove(_input("Plugin name: "))
            elif choice == "6":
                self.show_plugins()
                continue
            elif choice == "7":
                print(TEMPLATE)
                continue
            else:
                print("Choose 0-7.")
                continue
            print(("OK: " if ok else "") + msg)

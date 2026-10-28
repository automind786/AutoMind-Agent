"""
plugin_manager.py - Live plugin loader (importlib based hot reload).

A plugin is a .py file in /plugins exposing:
    PLUGIN_INFO = {"name": str, "version": str, "description": str,
                   "commands": [str, ...]}          # literal dict
    def run(args: str, context: dict) -> str
Optional hooks: on_load(context), on_unload().
`refresh()` detects new, changed and deleted files and applies them live.
"""

import ast
import importlib.util
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from typing import Dict, List

import config

RESERVED_COMMANDS = {
    "help", "exit", "quit", "update", "plugins", "memory", "history", "prefs",
    "set", "forget", "notes", "ai",
}
CMD_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
RISKY_IMPORTS = {"subprocess", "ctypes", "socket", "shutil", "pty"}
RISKY_CALLS = {"eval", "exec", "__import__", "system", "popen", "rmtree", "remove", "unlink"}


class PluginError(Exception):
    pass


@dataclass
class Plugin:
    stem: str
    name: str
    version: str
    description: str
    commands: List[str]
    path: str
    signature: tuple
    module: object = field(repr=False, default=None)


def sanitize_stem(name):
    stem = re.sub(r"[^a-z0-9_]", "_", str(name).strip().lower()).strip("_")
    return stem


def inspect_source(source):
    """Static validation (no execution). Returns info dict with 'warnings'."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PluginError("Syntax error line %s: %s" % (exc.lineno, exc.msg))
    info, has_run, warnings = None, False, []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "PLUGIN_INFO":
                    try:
                        info = ast.literal_eval(node.value)
                    except ValueError:
                        raise PluginError("PLUGIN_INFO must be a literal dict")
        elif isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
    if not isinstance(info, dict):
        raise PluginError("Missing module-level PLUGIN_INFO = {...}")
    if not has_run:
        raise PluginError("Missing function: def run(args, context)")
    info = _validate_info(info)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in RISKY_IMPORTS:
                    warnings.append("imports " + alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in RISKY_IMPORTS:
                warnings.append("imports from " + node.module)
        elif isinstance(node, ast.Call):
            fn = node.func
            nm = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if nm in RISKY_CALLS:
                warnings.append("calls " + nm + "()")
    info["warnings"] = sorted(set(warnings))
    return info


def _validate_info(info):
    name = info.get("name")
    if not isinstance(name, str) or not sanitize_stem(name):
        raise PluginError("PLUGIN_INFO['name'] must be a non-empty string")
    cmds = info.get("commands")
    if not isinstance(cmds, (list, tuple)) or not cmds:
        raise PluginError("PLUGIN_INFO['commands'] must be a non-empty list")
    clean = []
    for c in cmds:
        c = str(c).strip().lower()
        if not CMD_RE.match(c):
            raise PluginError("Invalid command name: %r" % c)
        clean.append(c)
    return {"name": name, "version": str(info.get("version", "0.0.0")),
            "description": str(info.get("description", "")), "commands": clean}


class PluginManager:
    def __init__(self, plugins_dir=None, context=None):
        self.plugins_dir = plugins_dir or config.PLUGINS_DIR
        self.context = context if context is not None else {}
        self.plugins = {}          # stem -> Plugin
        self.commands = {}         # command -> stem
        self.errors = {}           # stem -> message
        self._bad_sig = {}         # stem -> signature of last failed load
        for sub in ("", "_inbox", "_backup"):
            os.makedirs(os.path.join(self.plugins_dir, sub), exist_ok=True)

    # ---------- helpers ----------
    @staticmethod
    def _signature(path):
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)

    def _files(self):
        out = {}
        for fn in os.listdir(self.plugins_dir):
            full = os.path.join(self.plugins_dir, fn)
            if fn.endswith(".py") and not fn.startswith(("_", ".")) and os.path.isfile(full):
                out[fn[:-3]] = full
        return out

    def _rebuild_commands(self):
        self.commands = {}
        for stem in sorted(self.plugins):
            for cmd in self.plugins[stem].commands:
                if cmd in RESERVED_COMMANDS:
                    self.errors[stem] = "command '%s' is reserved" % cmd
                elif cmd in self.commands:
                    self.errors[stem] = "command '%s' already used by %s" % (cmd, self.commands[cmd])
                else:
                    self.commands[cmd] = stem

    # ---------- load / unload ----------
    def load_file(self, path):
        """Load (or reload) one plugin file via importlib. Raises PluginError."""
        stem = os.path.basename(path)[:-3]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            info = inspect_source(source)
            code = compile(source, path, "exec")  # fresh code, never stale .pyc
            mod_name = "automind_plugin_" + stem
            spec = importlib.util.spec_from_file_location(mod_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            try:
                exec(code, module.__dict__)
            except BaseException:
                sys.modules.pop(mod_name, None)
                raise
            if not callable(getattr(module, "run", None)):
                raise PluginError("run is not callable")
            hook = getattr(module, "on_load", None)
            if callable(hook):
                hook(self.context)
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError("%s: %s" % (type(exc).__name__, exc))
        self.unload(stem, rebuild=False)
        self.plugins[stem] = Plugin(stem, info["name"], info["version"], info["description"],
                                    info["commands"], path, self._signature(path), module)
        self.errors.pop(stem, None)
        self._bad_sig.pop(stem, None)
        self._rebuild_commands()
        return self.plugins[stem]

    def unload(self, stem, rebuild=True):
        plugin = self.plugins.pop(stem, None)
        if plugin is None:
            return False
        hook = getattr(plugin.module, "on_unload", None)
        if callable(hook):
            try:
                hook()
            except Exception:
                pass
        sys.modules.pop("automind_plugin_" + stem, None)
        if rebuild:
            self._rebuild_commands()
        return True

    def refresh(self):
        """Sync with the plugins folder. Returns a dict of changes."""
        importlib.invalidate_caches()
        changes = {"loaded": [], "reloaded": [], "removed": [], "errors": {}}
        files = self._files()
        for stem in list(self.plugins):
            if stem not in files:
                self.unload(stem)
                changes["removed"].append(stem)
        for stem, path in files.items():
            try:
                sig = self._signature(path)
            except OSError:
                continue
            current = self.plugins.get(stem)
            if current and current.signature == sig:
                continue
            if self._bad_sig.get(stem) == sig:
                continue
            try:
                self.load_file(path)
                changes["reloaded" if current else "loaded"].append(stem)
            except PluginError as exc:
                self.errors[stem] = str(exc)
                self._bad_sig[stem] = sig
                changes["errors"][stem] = str(exc)
        for stem in list(self.errors):
            if stem not in files:
                self.errors.pop(stem, None)
        return changes

    # ---------- use ----------
    def has_command(self, cmd):
        return cmd in self.commands

    def run(self, cmd, args=""):
        plugin = self.plugins[self.commands[cmd]]
        try:
            result = plugin.module.run(args, self.context)
        except Exception:
            tail = traceback.format_exc().strip().splitlines()[-3:]
            return "Plugin '%s' failed:\n  %s" % (plugin.name, "\n  ".join(tail))
        return "" if result is None else str(result)

    def list_plugins(self):
        return [self.plugins[s] for s in sorted(self.plugins)]

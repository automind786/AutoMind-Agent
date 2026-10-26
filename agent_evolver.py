#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_evolver.py  -  Self-modifying code engine for the AutoMind agent
=======================================================================

Pure Python standard library. Works on CPython 3.8+ including Pydroid 3 and
Chaquopy (it never spawns subprocesses).

What it does
------------
1. PLANS     Reads plans (JSON) or plain-text tasks from  <project>/tasks/ ,
             breaks them into ordered implementation steps and executes them.
2. PATCHES   write_file / patch_file / append_file / generate_module put
             Python files into the project directory, atomically.
3. RELOADS   After every change the module is imported or importlib.reload()ed
             in a worker thread with a timeout - no app restart needed.
4. ROLLBACK  Before ANY existing file is touched a timestamped backup is made.
             If the new code fails to compile, breaks a policy rule, throws on
             import/reload, or fails a check, the previous file is restored,
             the module is reloaded back to the old version, and a file that
             did not exist before is deleted again. Plans are transactional:
             one failing step rolls back every earlier step of that plan.
5. HISTORY   Every action, plan, benchmark and error is written to
             evolution_history.json (atomic writes, size-capped, corruption-safe).
6. MATH LINK agent_math_core.py is used for (a) a built-in logic/accuracy
             benchmark that gates every evolution, (b) "math"/"expr" checks in
             plans, and (c) generate_module, which turns a spec such as
             {"name": "gst_total", "params": ["amount","rate"],
              "expression": "amount + amount*rate/100"}
             into a module that evaluates the expression exactly with Decimal.

Honest limits (read these)
--------------------------
* The static CodePolicy is a guardrail against careless or hallucinated code
  (os.system, subprocess, eval, exec, ...). It is NOT a security sandbox: code
  you allow in is executed in your process when it is imported.
* A module stuck in an infinite loop at import time cannot be killed from
  Python. The evolver detects the timeout, restores the files on disk, marks
  the module "quarantined" in the history and tells you to restart the app.
* Objects created from an old version of a module keep the old behaviour until
  they are re-created (normal importlib.reload semantics).
* agent_evolver.py and agent_math_core.py are protected from patching by
  default (EvolverConfig.protected_files).

Quick start
-----------
    from agent_evolver import Evolver
    ev = Evolver()                       # project dir = folder of this file
    ev.write_file("skills/hello.py", "def hi():\\n    return 'hi'\\n",
                  checks=[{"type": "call", "function": "hi", "expect": "hi"}])
    ev.run_cycle()                       # process everything in ./tasks/
    ev.start_background(interval=60)     # or run the loop in a daemon thread

Plan format (JSON file in ./tasks/ or a dict):
    {"goal": "...", "atomic": true, "steps": [
        {"action": "write_file", "path": "a.py", "content": "...", "checks": [...]},
        {"action": "patch_file", "path": "a.py", "edits": [{"old": "x", "new": "y"}]},
        {"action": "append_file", "path": "a.py", "content": "..."},
        {"action": "generate_module", "path": "m.py", "spec": {...}},
        {"action": "generate", "path": "b.py", "description": "..."},   # needs generator hook
        {"action": "validate", "checks": [...]},
        {"action": "benchmark", "min_score": 1.0},
        {"action": "reload", "module": "a"},
        {"action": "note", "text": "..."}]}
Check types: math, expr, call, has_attr, benchmark  (or any callable(module)).

Command line:
    python agent_evolver.py status | history [N] | benchmark | run-once |
                            watch [SECONDS] | plan FILE | restore PATH | selftest
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import keyword
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

__version__ = "1.0.0"

__all__ = [
    "EvolverConfig", "CodePolicy", "Evolver", "EvolutionHistory", "BackupManager",
    "MathBridge", "EvolverError", "PlanError", "parse_task_text", "normalize_plan",
    "build_math_module", "BENCHMARKS", "run_selftest", "main",
]

try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:  # exec()'d or embedded interpreter
    _HERE = os.getcwd()


class EvolverError(Exception):
    """Any deliberate failure of the evolution engine."""


class PlanError(EvolverError):
    """A plan or task could not be understood."""


# --------------------------------------------------------------------------- #
# 1. Small utilities
# --------------------------------------------------------------------------- #

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Write via temp file + os.replace so a crash never leaves half a file."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".evo-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _tb_tail(limit: int = 1500) -> str:
    return traceback.format_exc()[-limit:]


def _run_guarded(fn, timeout: float) -> dict:
    """Run fn() in a daemon thread. Returns {'value':..} | {'error':exc,'tb':..}
    | {'timed_out': True}. Also traps SystemExit raised by generated code."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - generated code may raise anything
            box["error"] = exc
            box["tb"] = _tb_tail()

    th = threading.Thread(target=target, name="evolver-guard", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return {"timed_out": True}
    return box


def _same(actual, expected) -> bool:
    """Numeric-aware comparison: Decimal('1180.00') matches '1180' and 1180."""
    if actual == expected:
        return True
    try:
        a = Decimal(str(actual).replace(",", "").strip())
        b = Decimal(str(expected).replace(",", "").strip())
        if a.is_finite() and b.is_finite():
            return a == b
    except (InvalidOperation, ValueError):
        pass
    return str(actual).strip() == str(expected).strip()


def _ensure_syspath(path: str) -> None:
    if path not in sys.path:
        sys.path.insert(0, path)


# --------------------------------------------------------------------------- #
# 2. Configuration
# --------------------------------------------------------------------------- #

@dataclass
class EvolverConfig:
    project_dir: str = _HERE
    history_file: str = "evolution_history.json"
    state_dir_name: str = ".evolver"          # holds backups/
    tasks_dir_name: str = "tasks"             # drop plans here
    protected_files: tuple = ("agent_evolver.py", "agent_math_core.py")
    keep_backups: int = 20                    # per file
    max_source_bytes: int = 200_000
    import_timeout: float = 20.0
    test_timeout: float = 20.0
    atomic_plans: bool = True                 # roll back whole plan on failure
    benchmark_gate: bool = True               # math benchmark must not regress
    min_benchmark_score: float = 1.0
    max_history_actions: int = 3000
    max_history_errors: int = 1000
    max_history_plans: int = 500
    max_consecutive_failures: int = 5         # background loop circuit breaker


# --------------------------------------------------------------------------- #
# 3. Static code policy (guardrail, not a sandbox)
# --------------------------------------------------------------------------- #

@dataclass
class CodePolicy:
    forbidden_imports: frozenset = frozenset({
        "subprocess", "ctypes", "pty", "marshal", "pickle", "code", "codeop",
        "multiprocessing"})
    forbidden_names: frozenset = frozenset({
        "eval", "exec", "compile", "__import__", "breakpoint"})
    forbidden_call_prefixes: tuple = (
        "os.system", "os.popen", "os.exec", "os.spawn", "os.fork", "os.kill",
        "os.killpg", "subprocess.", "shutil.rmtree", "ctypes.", "pty.")
    forbidden_attrs: frozenset = frozenset({
        "__subclasses__", "__globals__", "__builtins__", "__code__", "__mro__"})

    @staticmethod
    def _dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    def check(self, source: str) -> list:
        """Return a list of human-readable violations (empty list = clean)."""
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            return [f"cannot parse: {exc}"]
        aliases, found = {}, []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    aliases[(a.asname or a.name).split(".")[0]] = a.name
                    if a.name.split(".")[0] in self.forbidden_imports:
                        found.append(f"line {node.lineno}: import of '{a.name}' is not allowed")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[0] in self.forbidden_imports:
                    found.append(f"line {node.lineno}: import from '{mod}' is not allowed")
                for a in node.names:
                    aliases[a.asname or a.name] = f"{mod}.{a.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self._dotted(node.func)
                if not name:
                    continue
                head, _, rest = name.partition(".")
                if head in aliases:
                    name = aliases[head] + ("." + rest if rest else "")
                if name in self.forbidden_names or name.split(".")[-1] in ("__import__",):
                    found.append(f"line {node.lineno}: call to '{name}' is not allowed")
                elif any(name == p or name.startswith(p) for p in self.forbidden_call_prefixes):
                    found.append(f"line {node.lineno}: call to '{name}' is not allowed")
            elif isinstance(node, ast.Attribute) and node.attr in self.forbidden_attrs:
                found.append(f"line {node.lineno}: access to '{node.attr}' is not allowed")
            elif isinstance(node, ast.Name) and node.id in ("__builtins__",):
                found.append(f"line {node.lineno}: access to '__builtins__' is not allowed")
        return found


# --------------------------------------------------------------------------- #
# 4. Evolution history (evolution_history.json)
# --------------------------------------------------------------------------- #

class EvolutionHistory:
    """Structured, atomic, size-capped JSON log of everything the evolver does."""

    def __init__(self, path: str, cfg: EvolverConfig):
        self.path = path
        self.cfg = cfg
        self._lock = threading.RLock()
        self.data = self._load()

    @staticmethod
    def _blank() -> dict:
        return {"schema": 1, "created": _utc_now(), "updated": _utc_now(),
                "counters": {"action": 0, "error": 0}, "baseline": None,
                "quarantined": [], "actions": [], "plans": [], "errors": [],
                "processed_tasks": {}}

    def _load(self) -> dict:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("history root is not an object")
                blank = self._blank()
                for key, val in blank.items():
                    data.setdefault(key, val)
                return data
            except (OSError, ValueError):
                try:
                    os.replace(self.path, f"{self.path}.corrupt-{_stamp()}")
                except OSError:
                    pass
        return self._blank()

    def _save(self) -> None:
        d = self.data
        d["updated"] = _utc_now()
        d["actions"] = d["actions"][-self.cfg.max_history_actions:]
        d["errors"] = d["errors"][-self.cfg.max_history_errors:]
        d["plans"] = d["plans"][-self.cfg.max_history_plans:]
        if len(d["processed_tasks"]) > 2000:
            keep = sorted(d["processed_tasks"].items(), key=lambda kv: kv[1].get("time", ""))[-1000:]
            d["processed_tasks"] = dict(keep)
        payload = json.dumps(d, indent=2, ensure_ascii=False, default=str).encode("utf-8")
        try:
            _atomic_write_bytes(self.path, payload)
        except OSError:
            pass  # history must never crash the evolver

    def add_action(self, kind, status, path=None, plan_id=None, step_id=None,
                   details=None, error=None) -> str:
        with self._lock:
            self.data["counters"]["action"] += 1
            aid = f"act-{self.data['counters']['action']:06d}"
            self.data["actions"].append({
                "id": aid, "time": _utc_now(), "kind": kind, "status": status, "path": path,
                "plan_id": plan_id, "step_id": step_id, "details": details or {}, "error": error})
            if error:
                self.data["counters"]["error"] += 1
                self.data["errors"].append({
                    "id": f"err-{self.data['counters']['error']:06d}", "time": _utc_now(),
                    "action_id": aid, "kind": kind, "path": path, "plan_id": plan_id,
                    "message": str(error)[:2000]})
            self._save()
            return aid

    def begin_plan(self, plan: dict) -> None:
        with self._lock:
            self.data["plans"].append({
                "id": plan["id"], "goal": plan.get("goal", ""), "started": _utc_now(),
                "finished": None, "status": "running", "steps_total": len(plan["steps"]),
                "source": plan.get("source"), "results": []})
            self._save()

    def finish_plan(self, plan_id: str, status: str, results: list, extra=None) -> None:
        with self._lock:
            for rec in reversed(self.data["plans"]):
                if rec["id"] == plan_id:
                    rec.update({"finished": _utc_now(), "status": status, "results": results})
                    if extra:
                        rec.update(extra)
                    break
            self._save()

    def mark_task(self, sha: str, info: dict) -> None:
        with self._lock:
            self.data["processed_tasks"][sha] = dict(info, time=_utc_now())
            self._save()

    def task_done(self, sha: str) -> bool:
        return sha in self.data["processed_tasks"]

    def set_baseline(self, score: float, details: dict) -> None:
        with self._lock:
            self.data["baseline"] = {"time": _utc_now(), "score": score, "details": details}
            self._save()

    def quarantine(self, module: str, reason: str) -> None:
        with self._lock:
            self.data["quarantined"].append({"module": module, "reason": reason, "time": _utc_now()})
            self._save()

    def recent(self, limit: int = 20, kind=None) -> list:
        with self._lock:
            items = [a for a in self.data["actions"] if kind in (None, a["kind"])]
            return items[-limit:]

    def summary(self) -> dict:
        with self._lock:
            by_status: dict = {}
            for a in self.data["actions"]:
                by_status[a["status"]] = by_status.get(a["status"], 0) + 1
            return {"actions": len(self.data["actions"]), "by_status": by_status,
                    "plans": len(self.data["plans"]), "errors": len(self.data["errors"]),
                    "baseline": self.data["baseline"], "quarantined": list(self.data["quarantined"])}


# --------------------------------------------------------------------------- #
# 5. Backups
# --------------------------------------------------------------------------- #

class BackupManager:
    def __init__(self, root: str, keep: int):
        self.root = root
        self.keep = max(1, keep)

    @staticmethod
    def _flat(rel: str) -> str:
        return rel.replace("/", "__").replace("\\", "__")

    def _pattern(self, rel: str):
        return re.compile(re.escape(self._flat(rel)) + r"\.\d{8}T\d{12}Z\.[0-9a-f]{8}\.bak$")

    def backup(self, abs_path: str, rel: str) -> str:
        with open(abs_path, "rb") as fh:
            data = fh.read()
        name = f"{self._flat(rel)}.{_stamp()}.{_sha256(data)[:8]}.bak"
        dest = os.path.join(self.root, name)
        _atomic_write_bytes(dest, data)
        self._prune(rel)
        return dest

    def list(self, rel: str) -> list:
        if not os.path.isdir(self.root):
            return []
        pat = self._pattern(rel)
        return sorted(os.path.join(self.root, n) for n in os.listdir(self.root) if pat.match(n))

    def _prune(self, rel: str) -> None:
        for old in self.list(rel)[:-self.keep]:
            try:
                os.remove(old)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 6. Bridge to agent_math_core.py
# --------------------------------------------------------------------------- #

BENCHMARKS = [
    {"id": "arith-order", "type": "expr", "input": "2 + 3 * 4 ^ 2", "expect": "50"},
    {"id": "float-trap", "type": "expr", "input": "0.1 + 0.2", "expect": "0.3"},
    {"id": "power-assoc", "type": "expr", "input": "2 ** 3 ** 2", "expect": "512"},
    {"id": "unary-power", "type": "expr", "input": "-2 ** 2", "expect": "-4"},
    {"id": "implicit-mult", "type": "expr", "input": "2(3 + 4)", "expect": "14"},
    {"id": "sqrt", "type": "expr", "input": "sqrt(144) + 8", "expect": "20"},
    {"id": "modulo", "type": "expr", "input": "-7 % 3", "expect": "2"},
    {"id": "gst-add", "type": "query", "input": "add 18% GST to 1000", "expect": "1180.00"},
    {"id": "gst-remove", "type": "query", "input": "remove 18% gst from 1180", "expect": "1000.00"},
    {"id": "percent-of", "type": "query", "input": "what is 15% of 2400", "expect": "360"},
    {"id": "words", "type": "query", "input": "12 times 8 plus 5", "expect": "101"},
    {"id": "linear", "type": "query", "input": "solve 2x + 3 = 11", "expect": "x = 4"},
    {"id": "quadratic", "type": "query", "input": "solve x^2 - 5x + 6 = 0", "expect": "x = 2 or x = 3"},
    {"id": "compound", "type": "query", "input": "compound interest on 10000 at 8% for 5 years",
     "expect": "14693.28"},
    {"id": "emi", "type": "query", "input": "EMI for 500000 at 9% for 20 years", "expect": "4498.63"},
    {"id": "simple-int", "type": "query", "input": "simple interest on 5000 at 7% for 3 years",
     "expect": "1050.00"},
    {"id": "reject-escape", "type": "reject", "input": "(1).__class__"},
    {"id": "reject-zero", "type": "reject", "input": "1/0"},
]


class MathBridge:
    """Thin, lazy link to agent_math_core. Looks the module up on every call so
    a reloaded math core is picked up automatically."""

    @staticmethod
    def module():
        try:
            return importlib.import_module("agent_math_core")
        except Exception:  # noqa: BLE001
            return None

    @property
    def available(self) -> bool:
        return self.module() is not None

    def _need(self):
        m = self.module()
        if m is None:
            raise EvolverError("agent_math_core.py is not importable (put it next to agent_evolver.py)")
        return m

    def evaluate(self, expression: str) -> str:
        m = self._need()
        return m.fmt(m.safe_eval(expression))

    def query(self, text: str) -> dict:
        return self._need().solve_query(text)

    def free_names(self, expression: str) -> list:
        return self._need().free_names(expression)

    def run_benchmarks(self) -> dict:
        m = self.module()
        if m is None:
            return {"available": False, "score": 0.0, "passed": 0, "total": len(BENCHMARKS),
                    "failures": [{"id": "*", "error": "agent_math_core not importable"}]}
        passed, failures = 0, []
        for case in BENCHMARKS:
            try:
                if case["type"] == "expr":
                    got = m.fmt(m.safe_eval(case["input"]))
                    ok = _same(got, case["expect"])
                elif case["type"] == "query":
                    res = m.solve_query(case["input"])
                    got = res.get("final_result")
                    ok = res.get("status") == "success" and _same(got, case["expect"])
                else:  # reject: must raise MathCoreError
                    got = None
                    try:
                        m.safe_eval(case["input"])
                        ok = False
                    except m.MathCoreError:
                        ok = True
            except Exception as exc:  # noqa: BLE001
                ok, got = False, f"{type(exc).__name__}: {exc}"
            if ok:
                passed += 1
            else:
                failures.append({"id": case["id"], "expected": case.get("expect"), "got": got})
        total = len(BENCHMARKS)
        return {"available": True, "score": round(passed / total, 4), "passed": passed,
                "total": total, "failures": failures}


# --------------------------------------------------------------------------- #
# 7. Plans, text tasks, and the math-module generator
# --------------------------------------------------------------------------- #

VALID_ACTIONS = {"write_file", "patch_file", "append_file", "generate_module", "generate",
                 "validate", "benchmark", "reload", "note"}

_FENCE_RE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", re.S | re.I)
_INLINE_PATH_RE = re.compile(r"^\s*#\s*(?:file|path|filename)\s*[:=]\s*([A-Za-z0-9_\-./]+\.py)\s*$", re.I)
_PATH_HINT_RE = re.compile(
    r"(?:file|path|filename|save\s+as|create|write|update|patch)\b[^\n`]{0,40}?([A-Za-z0-9_\-./]+\.py)\b", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")
_PY_PATH_RE = re.compile(r"([A-Za-z0-9_\-./]+\.py)\b")
_CODE_VERBS = re.compile(r"\b(create|add|implement|write|generate|patch|update|refactor|fix|build)\b", re.I)


def parse_task_text(text: str, plan_id: str = None) -> dict:
    """Turn a plain-text task into a plan.

    * a fenced ```python block becomes a write_file step; its target file comes
      from a first line like  "# file: tools/foo.py"  or from a nearby phrase
      such as "create tools/foo.py".
    * a bullet / numbered line becomes a 'generate' step if it names a .py file
      and an action verb (needs a generator hook), otherwise a 'note'.
    """
    plan_id = plan_id or "plan-" + _stamp()
    goal = next((ln.strip().lstrip("#").strip() for ln in text.splitlines() if ln.strip()), "")
    found = []  # (position, step)
    for m in _FENCE_RE.finditer(text):
        body = m.group(1)
        lines = body.splitlines()
        path = None
        if lines:
            hit = _INLINE_PATH_RE.match(lines[0])
            if hit:
                path = hit.group(1)
                body = "\n".join(lines[1:]) + "\n"
        if path is None:
            hints = list(_PATH_HINT_RE.finditer(text[max(0, m.start() - 200):m.start()]))
            if hints:
                path = hints[-1].group(1)
        if path:
            found.append((m.start(), {"action": "write_file", "path": path, "content": body,
                                      "description": f"write {path} from task text"}))
        else:
            found.append((m.start(), {"action": "note",
                                      "text": "code block without a target file (add '# file: name.py')"}))
    stripped = _FENCE_RE.sub(lambda mm: " " * (mm.end() - mm.start()), text)
    pos = 0
    for line in stripped.splitlines(keepends=True):
        b = _BULLET_RE.match(line)
        if b:
            item = b.group(1)
            p = _PY_PATH_RE.search(item)
            if p and _CODE_VERBS.search(item):
                found.append((pos, {"action": "generate", "path": p.group(1), "description": item}))
            else:
                found.append((pos, {"action": "note", "text": item}))
        pos += len(line)
    found.sort(key=lambda t: t[0])
    steps = [s for _, s in found]
    if not steps and goal:
        steps = [{"action": "note", "text": goal}]
    return {"id": plan_id, "goal": goal, "source": "text", "steps": steps}


def normalize_plan(plan, default_id: str = None) -> dict:
    """Validate a plan (dict / JSON string / task text) and fill in ids."""
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError:
            plan = parse_task_text(plan, default_id)
    if not isinstance(plan, dict):
        raise PlanError("a plan must be a JSON object")
    plan = dict(plan)
    plan.setdefault("id", default_id or "plan-" + _stamp())
    plan["id"] = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(plan["id"]))[:80]
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanError("plan needs a non-empty 'steps' list")
    out = []
    for i, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise PlanError(f"step {i}: must be an object")
        s = dict(raw)
        s.setdefault("id", f"s{i}")
        act = s.get("action")
        if act not in VALID_ACTIONS:
            raise PlanError(f"step {i}: unknown action {act!r}")
        need = {"write_file": ("path", "content"), "append_file": ("path", "content"),
                "patch_file": ("path", "edits"), "generate_module": ("path", "spec"),
                "generate": ("path",), "reload": ("module",)}.get(act, ())
        for key in need:
            if key not in s:
                raise PlanError(f"step {i} ({act}): missing '{key}'")
        if act == "patch_file":
            ed = s["edits"]
            if not isinstance(ed, list) or not ed or not all(
                    isinstance(e, dict) and isinstance(e.get("old"), str) and isinstance(e.get("new"), str)
                    for e in ed):
                raise PlanError(f"step {i}: 'edits' must be a list of {{'old','new'}} strings")
        out.append(s)
    plan["steps"] = out
    return plan


def build_math_module(spec: dict, bridge: MathBridge = None):
    """Generate Python source from a math spec. Returns (source, checks).

    spec = {"doc": "...", "functions": [{"name": "gst_total", "params": ["amount", "rate"],
            "expression": "amount + amount*rate/100", "round": 2 (optional),
            "doc": "...", "examples": [{"args": ["1000", "18"], "expect": "1180"}]}]}
    Every expression is validated by agent_math_core before any code is written,
    and the generated functions call agent_math_core.safe_eval at run time.
    """
    bridge = bridge or MathBridge()
    if not isinstance(spec, dict) or not isinstance(spec.get("functions"), list) or not spec["functions"]:
        raise EvolverError("spec needs a non-empty 'functions' list")
    reserved = {"safe_eval", "round_money"}
    lines = ["# -*- coding: utf-8 -*-",
             repr(str(spec.get("doc") or "Generated by agent_evolver.py (exact Decimal maths).")),
             "from agent_math_core import safe_eval, round_money", ""]
    checks, seen = [], set()
    for fn in spec["functions"]:
        name, params, expr = fn.get("name"), fn.get("params", []), fn.get("expression")
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name) or name in reserved:
            raise EvolverError(f"invalid function name: {name!r}")
        if name in seen:
            raise EvolverError(f"duplicate function name: {name!r}")
        seen.add(name)
        if (not isinstance(params, list) or len(set(params)) != len(params)
                or not all(isinstance(p, str) and p.isidentifier() and not keyword.iskeyword(p) for p in params)):
            raise EvolverError(f"{name}: params must be unique valid identifiers")
        if not isinstance(expr, str) or not expr.strip():
            raise EvolverError(f"{name}: missing expression")
        try:
            used = bridge.free_names(expr)
        except Exception as exc:  # noqa: BLE001 - MathCoreError or import problem
            raise EvolverError(f"{name}: expression rejected by agent_math_core: {exc}") from None
        missing = [u for u in used if u not in params]
        if missing:
            raise EvolverError(f"{name}: expression uses undeclared variables {missing}")
        digits = fn.get("round")
        if digits is not None and (not isinstance(digits, int) or isinstance(digits, bool) or not 0 <= digits <= 12):
            raise EvolverError(f"{name}: 'round' must be an integer from 0 to 12")
        mapping = "{" + ", ".join(f"{p!r}: {p}" for p in params) + "}"
        lines.append(f"def {name}({', '.join(params)}):")
        lines.append(f"    {repr(str(fn.get('doc') or 'Computes ' + expr))}")
        lines.append(f"    value = safe_eval({expr!r}, {mapping})")
        lines.append("    return round_money(value, %d)" % digits if digits is not None else "    return value")
        lines.append("")
        for ex in fn.get("examples", []):
            checks.append({"type": "call", "function": name, "args": ex.get("args", []),
                           "kwargs": ex.get("kwargs", {}), "expect": ex.get("expect")})
    return "\n".join(lines) + "\n", checks


# --------------------------------------------------------------------------- #
# 8. The evolution engine
# --------------------------------------------------------------------------- #

@dataclass
class _ChangeRecord:
    rel: str
    abs_path: str
    modname: str
    existed: bool
    backup_path: str
    old_sha: str
    new_sha: str
    was_loaded: bool
    ns_names: set
    action_id: str = ""
    timed_out: bool = False


_REL_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")


class Evolver:
    """Autonomous evolution loop with backup, validation and rollback.

    generator: optional callable(step: dict, context: dict) -> str | None used by
    'generate' steps. context = {"goal", "plan_id", "existing_source"}. Plug the
    agent's LLM or template engine in here; it must return the FULL new source.
    extra_checks: optional list of callables(module) -> truthy run on every
    importable change in addition to a step's own checks.
    """

    def __init__(self, config: EvolverConfig = None, generator=None,
                 policy: CodePolicy = None, extra_checks=None):
        self.cfg = config or EvolverConfig()
        self.project_dir = os.path.realpath(self.cfg.project_dir)
        os.makedirs(self.project_dir, exist_ok=True)
        self.policy = policy or CodePolicy()
        self.generator = generator
        self.extra_checks = list(extra_checks or [])
        self.history = EvolutionHistory(os.path.join(self.project_dir, self.cfg.history_file), self.cfg)
        self.backups = BackupManager(os.path.join(self.project_dir, self.cfg.state_dir_name, "backups"),
                                     self.cfg.keep_backups)
        self.math = MathBridge()
        self.tasks_dir = os.path.join(self.project_dir, self.cfg.tasks_dir_name)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._protected = {os.path.normpath(p).replace("\\", "/") for p in self.cfg.protected_files}
        _ensure_syspath(self.project_dir)
        _ensure_syspath(_HERE)

    # -- path / module helpers ------------------------------------------------ #
    def _resolve(self, rel: str):
        if not isinstance(rel, str) or not rel.strip():
            raise EvolverError("path must be a non-empty string")
        rel = rel.strip().replace("\\", "/")
        if "\x00" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            raise EvolverError("absolute or invalid path")
        if not _REL_RE.match(rel):
            raise EvolverError("path contains illegal characters (allowed: letters, digits, _ - . /)")
        parts = rel.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") or p == "__pycache__" for p in parts):
            raise EvolverError("path escapes the project or points at a hidden/cache location")
        if not rel.endswith(".py"):
            raise EvolverError("only .py files can be evolved")
        abs_path = os.path.realpath(os.path.join(self.project_dir, *parts))
        try:
            inside = os.path.commonpath([abs_path, self.project_dir]) == self.project_dir
        except ValueError:
            inside = False
        if not inside:
            raise EvolverError("path resolves outside the project directory")
        if rel in self._protected:
            raise EvolverError(f"{rel} is protected and cannot be modified")
        return rel, abs_path

    @staticmethod
    def module_name(rel: str):
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts or not all(p.isidentifier() and not keyword.iskeyword(p) for p in parts):
            return None
        return ".".join(parts)

    @staticmethod
    def _reserved(modname: str) -> bool:
        top = modname.split(".")[0]
        return top in getattr(sys, "stdlib_module_names", ()) or top in sys.builtin_module_names

    @staticmethod
    def _clear_pycache(abs_path: str) -> None:
        try:
            os.remove(importlib.util.cache_from_source(abs_path))
        except (OSError, NotImplementedError, ValueError):
            pass

    @staticmethod
    def _load_module(modname: str):
        importlib.invalidate_caches()
        if modname in sys.modules:
            return importlib.reload(sys.modules[modname])
        return importlib.import_module(modname)

    # -- checks ----------------------------------------------------------------- #
    def _run_check(self, check, default_module):
        """Returns (ok, message). A check is a callable(module) or a dict spec."""
        if callable(check):
            mod = sys.modules.get(default_module) if default_module else None
            res = check(mod)
            return (bool(res) or res is None), f"callable check returned {res!r}"
        if not isinstance(check, dict):
            return False, f"unsupported check: {check!r}"
        kind = check.get("type", "call")
        if kind == "math":
            if not self.math.available:
                return False, "math check needs agent_math_core"
            res = self.math.query(check["query"])
            ok = res.get("status") == "success" and _same(res.get("final_result"), check["expect"])
            return ok, f"math {check['query']!r}: expected {check['expect']!r}, got {res.get('final_result')!r}"
        if kind == "expr":
            got = self.math.evaluate(check["expression"])
            return _same(got, check["expect"]), f"expr {check['expression']!r}: expected {check['expect']!r}, got {got!r}"
        if kind == "benchmark":
            bench = self.math.run_benchmarks()
            need = float(check.get("min_score", self.cfg.min_benchmark_score))
            return bench["score"] >= need, f"benchmark score {bench['score']} (need {need})"
        modname = check.get("module") or default_module
        if not modname:
            return False, "check has no module to inspect"
        mod = sys.modules.get(modname) or importlib.import_module(modname)
        if kind == "has_attr":
            return hasattr(mod, check["name"]), f"{modname}.{check['name']} exists"
        if kind == "call":
            fn = getattr(mod, check["function"], None)
            if not callable(fn):
                return False, f"{modname}.{check['function']} is not callable"
            got = fn(*check.get("args", []), **check.get("kwargs", {}))
            if "expect" not in check:
                return True, "call succeeded"
            return _same(got, check["expect"]), (
                f"{modname}.{check['function']}(...) expected {check['expect']!r}, got {got!r}")
        return False, f"unknown check type {kind!r}"

    def _dynamic_validate(self, rec: _ChangeRecord, checks):
        self._clear_pycache(rec.abs_path)
        importlib.invalidate_caches()
        if rec.modname:
            r = _run_guarded(lambda: self._load_module(rec.modname), self.cfg.import_timeout)
            if r.get("timed_out"):
                rec.timed_out = True
                return False, f"import/reload timed out after {self.cfg.import_timeout}s"
            if "error" in r:
                err = r["error"]
                return False, f"import failed: {type(err).__name__}: {err}\n{r.get('tb', '')}"
        for chk in list(checks or []) + (self.extra_checks if rec.modname else []):
            r = _run_guarded(lambda c=chk: self._run_check(c, rec.modname), self.cfg.test_timeout)
            if r.get("timed_out"):
                rec.timed_out = True
                return False, f"check timed out after {self.cfg.test_timeout}s"
            if "error" in r:
                err = r["error"]
                return False, f"check raised {type(err).__name__}: {err}"
            ok, msg = r["value"]
            if not ok:
                return False, f"check failed: {msg}"
        return True, ""

    # -- rollback --------------------------------------------------------------- #
    def _rollback_record(self, rec: _ChangeRecord, reason: str, plan_id=None, step_id=None) -> bool:
        problems = []
        try:
            if rec.existed:
                with open(rec.backup_path, "rb") as fh:
                    data = fh.read()
                _atomic_write_bytes(rec.abs_path, data)
                if _sha256(data) != rec.old_sha:
                    problems.append("restored file hash differs from the original")
            elif os.path.exists(rec.abs_path):
                os.remove(rec.abs_path)
        except OSError as exc:
            problems.append(f"could not restore file: {exc}")
        self._clear_pycache(rec.abs_path)
        importlib.invalidate_caches()
        if rec.modname:
            if rec.timed_out:
                self.history.quarantine(rec.modname, "timed out; files restored, restart recommended")
                problems.append("module timed out - restart the app to fully recover it")
            elif rec.was_loaded:
                r = _run_guarded(lambda: self._load_module(rec.modname), self.cfg.import_timeout)
                if "error" in r or r.get("timed_out"):
                    problems.append("old version restored on disk but reloading it failed")
                else:
                    mod = sys.modules.get(rec.modname)
                    if mod is not None:  # drop names that only the failed version defined
                        for key in [k for k in vars(mod) if k not in rec.ns_names]:
                            vars(mod).pop(key, None)
            else:
                sys.modules.pop(rec.modname, None)
        self.history.add_action(
            "rollback", "rolled_back" if not problems else "failed", rec.rel, plan_id, step_id,
            {"reason": reason[:500], "backup": rec.backup_path or None, "restored_sha": rec.old_sha or None},
            error="; ".join(problems) if problems else None)
        return not problems

    # -- single-file changes ---------------------------------------------------- #
    def write_file(self, rel_path, source, *, checks=None, reason="", plan_id=None, step_id=None,
                   txn=None, kind="write_file") -> dict:
        """Create or overwrite one .py file safely. Returns a result dict with
        keys ok, status, path, error, rolled_back, backup, action_id."""
        with self._lock:
            result = {"ok": False, "status": "failed", "path": rel_path, "error": None,
                      "rolled_back": False, "backup": None, "action_id": None}

            def fail(status, msg, **extra):
                result.update(ok=False, status=status, error=msg, **extra)
                result["action_id"] = self.history.add_action(
                    kind, status, result["path"], plan_id, step_id, {"reason": reason}, error=msg)
                return result

            try:
                rel, abs_path = self._resolve(rel_path)
            except EvolverError as exc:
                return fail("rejected", str(exc))
            result["path"] = rel
            if not isinstance(source, str):
                return fail("rejected", "source must be a string")
            source = source.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
            if "\x00" in source:
                return fail("rejected", "source contains NUL bytes")
            if source and not source.endswith("\n"):
                source += "\n"
            data = source.encode("utf-8")
            if len(data) > self.cfg.max_source_bytes:
                return fail("rejected", f"source too large ({len(data)} bytes)")
            modname = self.module_name(rel)
            existed = os.path.isfile(abs_path)
            if not existed and modname and self._reserved(modname):
                return fail("rejected", f"'{modname}' would shadow a standard-library module")
            old = b""
            if existed:
                with open(abs_path, "rb") as fh:
                    old = fh.read()
                if old == data:
                    result.update(ok=True, status="unchanged")
                    return result
            # --- static validation: nothing has touched the disk yet ---
            try:
                compile(source, rel, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                return fail("rejected", f"syntax error: {exc}")
            violations = self.policy.check(source)
            if violations:
                return fail("rejected", "policy violation: " + "; ".join(violations[:5]))
            # --- backup, write, dynamic validation ---
            backup_path = ""
            try:
                if existed:
                    backup_path = self.backups.backup(abs_path, rel)
                    result["backup"] = backup_path
                was_loaded = bool(modname and modname in sys.modules)
                ns_names = set(vars(sys.modules[modname])) if was_loaded else set()
                rec = _ChangeRecord(rel, abs_path, modname, existed, backup_path, _sha256(old) if existed else "",
                                    _sha256(data), was_loaded, ns_names)
                _atomic_write_bytes(abs_path, data)
            except OSError as exc:
                return fail("failed", f"file system error: {exc}")
            ok, err = self._dynamic_validate(rec, checks)
            if not ok:
                restored = self._rollback_record(rec, err, plan_id, step_id)
                return fail("rolled_back", err, rolled_back=True, rollback_clean=restored)
            result.update(ok=True, status="applied")
            result["action_id"] = self.history.add_action(
                kind, "ok", rel, plan_id, step_id,
                {"reason": reason, "created": not existed, "backup": backup_path or None,
                 "sha_before": rec.old_sha or None, "sha_after": rec.new_sha,
                 "module": modname, "bytes": len(data)})
            rec.action_id = result["action_id"]
            if txn is not None:
                txn.append(rec)
            return result

    def patch_file(self, rel_path, edits, **kw) -> dict:
        """Apply exact-text edits [{'old','new','replace_all'?}] to an existing file."""
        with self._lock:
            try:
                rel, abs_path = self._resolve(rel_path)
                if not os.path.isfile(abs_path):
                    raise EvolverError(f"{rel} does not exist; use write_file")
                with open(abs_path, "r", encoding="utf-8") as fh:
                    src = fh.read()
                for i, e in enumerate(edits, 1):
                    old, new = e["old"], e["new"]
                    n = src.count(old) if old else 0
                    if n == 0:
                        raise EvolverError(f"edit {i}: text to replace was not found")
                    if n > 1 and not e.get("replace_all"):
                        raise EvolverError(f"edit {i}: text matches {n} places; make it unique or set replace_all")
                    src = src.replace(old, new)
            except (EvolverError, OSError, UnicodeDecodeError, KeyError, TypeError) as exc:
                aid = self.history.add_action("patch_file", "rejected", str(rel_path), kw.get("plan_id"),
                                              kw.get("step_id"), {}, error=str(exc))
                return {"ok": False, "status": "rejected", "path": rel_path, "error": str(exc),
                        "rolled_back": False, "backup": None, "action_id": aid}
            return self.write_file(rel, src, kind="patch_file", **kw)

    def append_file(self, rel_path, content, **kw) -> dict:
        with self._lock:
            try:
                rel, abs_path = self._resolve(rel_path)
                existing = ""
                if os.path.isfile(abs_path):
                    with open(abs_path, "r", encoding="utf-8") as fh:
                        existing = fh.read()
            except (EvolverError, OSError, UnicodeDecodeError) as exc:
                aid = self.history.add_action("append_file", "rejected", str(rel_path), kw.get("plan_id"),
                                              kw.get("step_id"), {}, error=str(exc))
                return {"ok": False, "status": "rejected", "path": rel_path, "error": str(exc),
                        "rolled_back": False, "backup": None, "action_id": aid}
            sep = "" if not existing or existing.endswith("\n") else "\n"
            return self.write_file(rel, existing + sep + content, kind="append_file", **kw)

    def restore_latest_backup(self, rel_path, index: int = -1) -> dict:
        """Manual undo: put a stored backup back (the current file is backed up
        first, so the undo itself can be undone) and reload the module."""
        with self._lock:
            try:
                rel, abs_path = self._resolve(rel_path)
                backups = self.backups.list(rel)
                if not backups:
                    raise EvolverError(f"no backups stored for {rel}")
                with open(backups[index], "rb") as fh:
                    data = fh.read()
            except (EvolverError, OSError, IndexError) as exc:
                aid = self.history.add_action("restore", "failed", str(rel_path), error=str(exc))
                return {"ok": False, "error": str(exc), "action_id": aid}
            res = self.write_file(rel, data.decode("utf-8", "replace"), reason=f"manual restore of {os.path.basename(backups[index])}",
                                  kind="restore")
            return res

    # -- plan execution ---------------------------------------------------------- #
    def _run_step(self, step, plan, txn) -> dict:
        act, sid, pid = step["action"], step["id"], plan["id"]
        common = {"plan_id": pid, "step_id": sid, "txn": txn,
                  "reason": step.get("description") or plan.get("goal", "")}
        rec = {"step": sid, "action": act, "status": "ok", "message": ""}
        try:
            if act == "note":
                rec.update(status="skipped", message=str(step.get("text", ""))[:300])
                return rec
            if act == "write_file":
                r = self.write_file(step["path"], step["content"], checks=step.get("checks"), **common)
            elif act == "patch_file":
                r = self.patch_file(step["path"], step["edits"], checks=step.get("checks"), **common)
            elif act == "append_file":
                r = self.append_file(step["path"], step["content"], checks=step.get("checks"), **common)
            elif act == "generate_module":
                src, auto = build_math_module(step["spec"], self.math)
                r = self.write_file(step["path"], src, checks=auto + list(step.get("checks") or []), **common)
            elif act == "generate":
                if self.generator is None:
                    raise EvolverError("no code generator configured for 'generate' steps "
                                       "(pass generator=... to Evolver)")
                existing = ""
                try:
                    _, ap = self._resolve(step["path"])
                    if os.path.isfile(ap):
                        with open(ap, "r", encoding="utf-8") as fh:
                            existing = fh.read()
                except EvolverError:
                    pass
                code = self.generator(step, {"goal": plan.get("goal", ""), "plan_id": pid,
                                             "existing_source": existing})
                if not isinstance(code, str) or not code.strip():
                    raise EvolverError("generator returned no code")
                r = self.write_file(step["path"], code, checks=step.get("checks"), **common)
            elif act == "reload":
                r0 = _run_guarded(lambda: self._load_module(step["module"]), self.cfg.import_timeout)
                if r0.get("timed_out") or "error" in r0:
                    msg = "timed out" if r0.get("timed_out") else f"{type(r0['error']).__name__}: {r0['error']}"
                    self.history.add_action("reload", "failed", step["module"], pid, sid, error=msg)
                    rec.update(status="failed", message=msg)
                else:
                    self.history.add_action("reload", "ok", step["module"], pid, sid)
                return rec
            elif act == "validate":
                modname = step.get("module")
                if not modname and step.get("path"):
                    modname = self.module_name(step["path"])
                outcome = _run_guarded(
                    lambda: [self._run_check(c, modname) for c in (step.get("checks") or [])], self.cfg.test_timeout)
                if outcome.get("timed_out") or "error" in outcome:
                    msg = "timed out" if outcome.get("timed_out") else f"{type(outcome['error']).__name__}: {outcome['error']}"
                    rec.update(status="failed", message=msg)
                else:
                    bad = [m for ok, m in outcome["value"] if not ok]
                    if bad:
                        rec.update(status="failed", message="; ".join(bad)[:500])
                self.history.add_action("validate", rec["status"], modname, pid, sid,
                                        error=rec["message"] or None)
                return rec
            elif act == "benchmark":
                bench = self.math.run_benchmarks()
                need = float(step.get("min_score", self.cfg.min_benchmark_score))
                good = bench["available"] and bench["score"] >= need
                self.history.add_action("benchmark", "ok" if good else "failed", None, pid, sid,
                                        {"score": bench["score"], "failures": bench["failures"][:5]},
                                        error=None if good else f"score {bench['score']} below {need}")
                rec.update(status="ok" if good else "failed", message=f"score {bench['score']}")
                return rec
            else:  # unreachable thanks to normalize_plan
                raise EvolverError(f"unsupported action {act}")
            rec["message"] = r.get("error") or r.get("status", "")
            rec["status"] = "ok" if r.get("ok") else "failed"
            rec["result"] = {k: r.get(k) for k in ("status", "path", "backup", "rolled_back", "action_id")}
        except Exception as exc:  # noqa: BLE001 - one bad step must not kill the loop
            rec.update(status="failed", message=f"{type(exc).__name__}: {exc}")
            self.history.add_action(act, "failed", step.get("path"), pid, sid, error=f"{rec['message']}\n{_tb_tail(800)}")
        return rec

    def _rollback_txn(self, txn, reason, plan_id) -> bool:
        clean = True
        for rec in reversed(txn):
            clean = self._rollback_record(rec, reason, plan_id) and clean
        txn.clear()
        return clean

    def execute_plan(self, plan, atomic: bool = None) -> dict:
        """Run a plan. Returns {'plan_id','status','steps','benchmark'}.
        status: completed | rolled_back | partial | failed | no_executable_steps."""
        with self._lock:
            plan = normalize_plan(plan)
            atomic = self.cfg.atomic_plans if atomic is None else atomic
            atomic = bool(plan.get("atomic", atomic))
            pid = plan["id"]
            self.history.begin_plan(plan)
            executable = [s for s in plan["steps"] if s["action"] != "note"]
            results, txn, status, bench_info = [], [], "completed", None
            before = self.math.run_benchmarks() if self.cfg.benchmark_gate else None
            if before is not None and before["available"] and self.history.data["baseline"] is None:
                self.history.set_baseline(before["score"], {"passed": before["passed"], "total": before["total"]})
            if not executable:
                status = "no_executable_steps"
                results = [self._run_step(s, plan, txn) for s in plan["steps"]]
            else:
                for step in plan["steps"]:
                    res = self._run_step(step, plan, txn)
                    results.append(res)
                    if res["status"] == "failed":
                        if atomic:
                            self._rollback_txn(txn, f"step {step['id']} failed: {res['message']}", pid)
                            status = "rolled_back"
                        else:
                            status = "partial" if txn else "failed"
                        break
                if status == "completed" and txn and before is not None and before["available"]:
                    after = self.math.run_benchmarks()
                    required = min(self.cfg.min_benchmark_score, before["score"])
                    bench_info = {"before": before["score"], "after": after["score"], "required": required}
                    good = after["score"] >= required
                    self.history.add_action("benchmark", "ok" if good else "failed", None, pid, None,
                                            dict(bench_info, failures=after["failures"][:5]),
                                            error=None if good else "benchmark regressed after evolution")
                    if not good:
                        self._rollback_txn(txn, "math benchmark regressed", pid)
                        status = "rolled_back"
            summary = {"plan_id": pid, "goal": plan.get("goal", ""), "status": status,
                       "steps": results, "benchmark": bench_info}
            self.history.finish_plan(pid, status, results, {"benchmark": bench_info})
            return summary

    def evolve_text(self, text: str) -> dict:
        """Convenience: plain-text task -> plan -> execute."""
        return self.execute_plan(parse_task_text(text))

    # -- task folder loop -------------------------------------------------------- #
    def _move_task(self, path: str, bucket: str) -> None:
        dest_dir = os.path.join(self.tasks_dir, bucket)
        os.makedirs(dest_dir, exist_ok=True)
        base = os.path.basename(path)
        try:
            os.replace(path, os.path.join(dest_dir, f"{_stamp()}-{base}"))
        except OSError:
            pass

    def run_cycle(self) -> dict:
        """Process every new task/plan file in ./tasks once."""
        out = {"processed": [], "skipped": [], "errors": []}
        if not os.path.isdir(self.tasks_dir):
            return out
        for name in sorted(os.listdir(self.tasks_dir)):
            path = os.path.join(self.tasks_dir, name)
            if not os.path.isfile(path) or not name.lower().endswith((".json", ".txt", ".md")):
                continue
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                sha = _sha256(raw)
                if self.history.task_done(sha):
                    self._move_task(path, "done")
                    out["skipped"].append(name)
                    continue
                text = raw.decode("utf-8", "replace")
                pid = "plan-" + _stamp()
                plan = (json.loads(text) if name.lower().endswith(".json") else parse_task_text(text, pid))
                if isinstance(plan, dict):
                    plan.setdefault("source", name)
                result = self.execute_plan(plan)
                bucket = {"completed": "done", "no_executable_steps": "needs_review"}.get(result["status"], "failed")
                self.history.mark_task(sha, {"file": name, "status": result["status"], "plan_id": result["plan_id"]})
                self._move_task(path, bucket)
                out["processed"].append({"file": name, "status": result["status"], "plan_id": result["plan_id"]})
            except (PlanError, ValueError, OSError) as exc:
                self.history.add_action("task", "failed", name, error=f"{type(exc).__name__}: {exc}")
                self._move_task(path, "failed")
                out["errors"].append({"file": name, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self.history.add_action("task", "failed", name, error=f"{type(exc).__name__}: {exc}\n{_tb_tail(800)}")
                out["errors"].append({"file": name, "error": str(exc)})
        return out

    def start_background(self, interval: float = 30.0) -> threading.Thread:
        """Run run_cycle() every `interval` seconds in a daemon thread. Stops by
        itself after cfg.max_consecutive_failures bad cycles in a row."""
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()

        def loop():
            bad = 0
            while not self._stop.is_set():
                try:
                    res = self.run_cycle()
                    failed = any(p["status"] not in ("completed", "no_executable_steps") for p in res["processed"]) or res["errors"]
                    bad = bad + 1 if failed else 0
                except Exception as exc:  # noqa: BLE001
                    bad += 1
                    self.history.add_action("cycle", "failed", error=f"{type(exc).__name__}: {exc}")
                if bad >= self.cfg.max_consecutive_failures:
                    self.history.add_action("cycle", "failed", error="circuit breaker: loop paused after repeated failures")
                    break
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="agent-evolver", daemon=True)
        self._thread.start()
        return self._thread

    def stop_background(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    # -- introspection ------------------------------------------------------------ #
    def status(self) -> dict:
        s = self.history.summary()
        s.update({"version": __version__, "project_dir": self.project_dir,
                  "history_file": self.history.path, "math_core_available": self.math.available,
                  "background_running": bool(self._thread and self._thread.is_alive()),
                  "recent_actions": self.history.recent(5)})
        return s


# --------------------------------------------------------------------------- #
# 9. Self-test and CLI
# --------------------------------------------------------------------------- #

def run_selftest() -> int:
    """End-to-end test in a throw-away directory. Returns 0 if all pass."""
    tmp = tempfile.mkdtemp(prefix="evolver_selftest_")
    added_path = tmp not in sys.path
    results = []

    def check(label, cond):
        results.append((label, bool(cond)))

    try:
        ev = Evolver(EvolverConfig(project_dir=tmp, import_timeout=10, test_timeout=10))
        add_check = lambda n: [{"type": "call", "function": "add", "args": [2, 3], "expect": n}]

        r = ev.write_file("evo_t_calc.py", "def add(a, b):\n    return a + b\n", checks=add_check(5))
        import evo_t_calc
        check("create new module and import it", r["ok"] and evo_t_calc.add(2, 3) == 5)

        r = ev.patch_file("evo_t_calc.py", [{"old": "a + b", "new": "a + b + 1"}], checks=add_check(6))
        check("patch + hot reload (same module object)", r["ok"] and evo_t_calc.add(2, 3) == 6 and r["backup"])

        r = ev.write_file("evo_t_calc.py", "def add(a, b)\n    return a - b\n")
        check("syntax error rejected before touching disk", not r["ok"] and r["status"] == "rejected" and evo_t_calc.add(2, 3) == 6)

        r = ev.write_file("evo_t_calc.py", "raise RuntimeError('boom')\n")
        check("runtime error on import -> rollback", r["rolled_back"] and evo_t_calc.add(2, 3) == 6)

        r = ev.patch_file("evo_t_calc.py", [{"old": "a + b + 1", "new": "a - b"}], checks=add_check(6))
        check("failed check -> rollback to working version", r["rolled_back"] and evo_t_calc.add(2, 3) == 6)

        r = ev.write_file("evo_t_bad.py", "import subprocess\nsubprocess.run(['ls'])\n")
        check("policy blocks subprocess", r["status"] == "rejected" and not os.path.exists(os.path.join(tmp, "evo_t_bad.py")))

        r1 = ev.write_file("../escape.py", "x = 1\n")
        r2 = ev.write_file("agent_math_core.py", "x = 1\n")
        r3 = ev.write_file("json.py", "x = 1\n")
        check("path traversal / protected / stdlib shadowing blocked",
              r1["status"] == "rejected" and r2["status"] == "rejected" and r3["status"] == "rejected")

        plan = {"goal": "atomic demo", "steps": [
            {"action": "write_file", "path": "evo_t_ok.py", "content": "VALUE = 1\n"},
            {"action": "write_file", "path": "evo_t_fail.py", "content": "raise ValueError('nope')\n"}]}
        res = ev.execute_plan(plan)
        check("atomic plan rolls back earlier steps",
              res["status"] == "rolled_back" and not os.path.exists(os.path.join(tmp, "evo_t_ok.py"))
              and "evo_t_ok" not in sys.modules)

        spec = {"functions": [{"name": "gst_total", "params": ["amount", "rate"],
                               "expression": "amount + amount * rate / 100", "round": 2,
                               "examples": [{"args": ["1000", "18"], "expect": "1180.00"}]}]}
        res = ev.execute_plan({"goal": "math module", "steps": [
            {"action": "generate_module", "path": "evo_t_fin.py", "spec": spec},
            {"action": "validate", "path": "evo_t_fin.py",
             "checks": [{"type": "call", "function": "gst_total", "args": ["2500", "12"], "expect": "2800"}]}]})
        check("generated math module uses agent_math_core", res["status"] == "completed")

        bad_spec = {"functions": [{"name": "f", "params": ["x"], "expression": "x + __import__('os')"}]}
        try:
            build_math_module(bad_spec)
            rejected = False
        except EvolverError:
            rejected = True
        check("unsafe math expression refused at generation time", rejected)

        bench = ev.math.run_benchmarks()
        check("math benchmarks 100%", bench["score"] == 1.0)

        text = ("Goal: add a helper\n\n- create evo_t_txt.py with a greeting\n\n"
                "```python\n# file: evo_t_txt.py\ndef greet():\n    return 'hi'\n```\n")
        parsed = parse_task_text(text)
        check("text task -> steps", [s["action"] for s in parsed["steps"]] == ["generate", "write_file"])

        os.makedirs(ev.tasks_dir, exist_ok=True)
        with open(os.path.join(ev.tasks_dir, "t1.txt"), "w", encoding="utf-8") as fh:
            fh.write("Add a greeter\n```python\n# file: evo_t_txt.py\ndef greet():\n    return 'hi'\n```\n")
        cyc = ev.run_cycle()
        check("run_cycle executes and archives a task",
              cyc["processed"] and cyc["processed"][0]["status"] == "completed"
              and os.path.isdir(os.path.join(ev.tasks_dir, "done")))

        r = ev.restore_latest_backup("evo_t_calc.py", 0)
        check("manual restore from backup", r["ok"] and evo_t_calc.add(2, 3) == 5)

        with open(ev.history.path, "r", encoding="utf-8") as fh:
            hist = json.load(fh)
        kinds = {a["kind"] for a in hist["actions"]}
        check("evolution_history.json is valid and complete",
              hist["actions"] and hist["errors"] and hist["plans"] and {"write_file", "rollback"} <= kinds)
    finally:
        for name in [n for n in sys.modules if n.startswith("evo_t_")]:
            sys.modules.pop(name, None)
        if added_path and tmp in sys.path:
            sys.path.remove(tmp)
        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for label, ok in results:
        print(("PASS  " if ok else "FAIL  ") + label)
        failed += 0 if ok else 1
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "help"
    if cmd in ("selftest", "--selftest"):
        return run_selftest()
    if cmd in ("help", "-h", "--help"):
        print(__doc__.split("Command line:")[-1].strip())
        return 0
    ev = Evolver()
    if cmd == "status":
        _dump(ev.status())
    elif cmd == "history":
        _dump(ev.history.recent(int(argv[1]) if len(argv) > 1 else 20))
    elif cmd == "benchmark":
        _dump(ev.math.run_benchmarks())
    elif cmd == "run-once":
        _dump(ev.run_cycle())
    elif cmd == "watch":
        interval = float(argv[1]) if len(argv) > 1 else 30.0
        print(f"watching {ev.tasks_dir} every {interval}s - Ctrl+C to stop")
        ev.start_background(interval)
        try:
            while ev._thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            ev.stop_background()
    elif cmd == "plan" and len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as fh:
            _dump(ev.execute_plan(fh.read()))
    elif cmd == "restore" and len(argv) > 1:
        _dump(ev.restore_latest_backup(argv[1]))
    else:
        print("unknown command; run: python agent_evolver.py help")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

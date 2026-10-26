#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_board_ui.py - Canvas / execution board for the standalone AutoMind agent.

Two front-ends share ONE framework-agnostic backend (class Backend):

  * Kivy UI  (Android / desktop) - used automatically when `kivy` is importable.
  * Web UI   (pure stdlib http.server, responsive, phone-friendly) - fallback
             when Kivy is missing (e.g. Termux) or when you pass --ui web.

Features
  - Type / paste execution plans, run them through main.py's orchestrator.
  - Codegen button (routed to main.py / agent_evolver.py codegen entry point).
  - Live status: benchmark score, evolution state, active steps, rollback alerts.
  - High-precision math + business calculator (agent_math_core.py first,
    built-in 50-digit Decimal engine as a safe fallback).
  - Async worker pipeline: nothing heavy ever runs on the UI / event loop thread.
  - "Evolution Plan Log" panel fed from evolution_history.json (auto-refresh).
  - Scrollable console (stdout/stderr of worker jobs is captured), error banners.

Usage
  python agent_board_ui.py                 # auto: Kivy if available, else web
  python agent_board_ui.py --ui web --port 8765
  python agent_board_ui.py --plan-entry main:run_plan --evolve-entry agent_evolver:AgentEvolver.run_evolution

Entry points are auto-discovered by name (see SPECS below). If your function
names differ, pass --plan-entry / --codegen-entry / --evolve-entry /
--math-entry as  module:function  or  module:Class.method.

Only the Python standard library (+ Kivy for the native UI) is required.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import concurrent.futures as cf
import hmac
import importlib
import inspect
import io
import itertools
import json
import math
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from decimal import (ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal,
                     DivisionByZero, InvalidOperation, Overflow, localcontext)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "AutoMind Board"
PRECISION = 50
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_HISTORY = Path(os.environ.get("AUTOMIND_HISTORY", BASE_DIR / "evolution_history.json"))

# --------------------------------------------------------------------------
# Entry-point discovery table. Edit here if your names differ.
# --------------------------------------------------------------------------
SPECS = {
    "plan": dict(
        modules=["main"],
        funcs=["board_run_plan", "run_plan", "execute_plan", "run_task", "execute_task", "process_plan",
               "handle_plan", "run_agent_plan"],
        instances=["agent", "automind", "orchestrator", "app", "AGENT"],
        classes=["AutoMind", "AutoMindAgent", "Orchestrator", "AgentOrchestrator", "Agent", "App"],
        methods=["run_plan", "execute_plan", "run_task", "execute_task", "process_plan",
                 "handle_plan", "run", "execute", "process", "handle", "submit"],
    ),
    "codegen": dict(
        modules=["main", "ai_hub", "agent_evolver"],
        funcs=["board_codegen", "generate_code", "run_codegen", "codegen", "write_code", "synthesize_code"],
        instances=["agent", "automind", "orchestrator", "evolver", "app"],
        classes=["AutoMind", "AutoMindAgent", "Orchestrator", "AgentEvolver", "Evolver", "Agent"],
        methods=["generate_code", "run_codegen", "codegen", "write_code", "synthesize_code", "generate"],
    ),
    "evolve": dict(
        modules=["main", "agent_evolver"],
        funcs=["board_run_evolution", "run_evolution", "evolve", "run_evolution_cycle", "evolution_cycle",
               "run_cycle", "self_evolve", "evolve_once"],
        instances=["evolver", "agent_evolver", "agent", "automind"],
        classes=["Evolver", "AgentEvolver", "AutoMindEvolver", "SelfEvolver", "EvolutionEngine"],
        methods=["run_evolution", "evolve", "run_evolution_cycle", "evolution_cycle",
                 "run_cycle", "evolve_text", "run"],
    ),
    "math": dict(
        modules=["agent_math_core"],
        funcs=["solve_query", "evaluate_expression", "safe_eval", "evaluate", "calculate", "calc", "eval_expr", "compute"],
        instances=["engine", "core", "math_core", "calculator"],
        classes=["SafeEvaluator", "MathCore", "AgentMath", "HighPrecisionMath", "Calculator", "MathEngine", "AgentMathCore"],
        methods=["evaluate", "calculate", "calc", "evaluate_expression", "eval_expr", "compute", "eval"],
    ),
    "status": dict(
        modules=["agent_evolver", "main"],
        funcs=["get_status", "get_state", "current_status"],
        instances=["evolver", "agent_evolver", "agent", "automind"],
        classes=["Evolver", "AgentEvolver", "AutoMindEvolver", "SelfEvolver", "EvolutionEngine"],
        methods=["get_status", "status", "get_state", "current_status", "get_benchmark_score",
                 "benchmark_score", "get_score"],
    ),
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _ts(t=None):
    return time.strftime("%H:%M:%S", time.localtime(t or time.time()))


def _clip(s, n=4000):
    s = str(s)
    return s if len(s) <= n else s[:n] + " ...[truncated %d chars]" % (len(s) - n)


def result_to_text(res):
    if res is None:
        return "(no return value)"
    if isinstance(res, Decimal):
        return fmt_decimal(res)
    if isinstance(res, (dict, list, tuple)):
        try:
            return _clip(json.dumps(res, indent=2, default=str, ensure_ascii=False))
        except Exception:
            return _clip(repr(res))
    return _clip(res)


def fmt_decimal(d):
    """Plain (non-exponent) string, <= 34 significant digits, trailing zeros trimmed."""
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    if d.is_nan() or d.is_infinite():
        return str(d)
    with localcontext() as ctx:
        ctx.prec = 34
        d = +d
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "") else s


def pretty_money(d):
    """Grouped 2-decimal hint for big numbers (display only)."""
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return "{:,}".format(q)
    except Exception:
        return ""


def call_flex(fn, *args, **kwargs):
    """Call fn with only the positional/keyword args it accepts; run coroutines."""
    try:
        params = inspect.signature(fn).parameters.values()
        has_var_pos = any(p.kind == p.VAR_POSITIONAL for p in params)
        has_var_kw = any(p.kind == p.VAR_KEYWORD for p in params)
        names = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
        n_pos = sum(1 for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        if not has_var_pos:
            args = args[:n_pos]
        if not has_var_kw:
            kwargs = {k: v for k, v in kwargs.items() if k in names}
    except (TypeError, ValueError):
        pass
    res = fn(*args, **kwargs)
    if inspect.iscoroutine(res):
        res = asyncio.run(res)
    return res


def parse_steps(text):
    steps = []
    for ln in text.splitlines():
        ln = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", ln).strip()
        if ln:
            steps.append(ln)
    return steps or [text.strip()]


# --------------------------------------------------------------------------
# evolution_history.json normalisation
# --------------------------------------------------------------------------
_TS = ("timestamp", "time", "ts", "date", "created_at", "when")
_ACT = ("action", "type", "event", "step", "name", "operation", "change", "mutation", "description")
_STAT = ("status", "result", "outcome", "state")
_SCORE = ("benchmark_score", "score", "benchmark", "fitness", "new_score", "after_score")
_DET = ("message", "detail", "details", "reason", "summary", "notes", "error", "description")
BENCH_KEYS = ("benchmark_score", "benchmark", "score", "best_score", "current_score", "fitness")
STATE_KEYS = ("state", "status", "phase", "stage", "mode")
GEN_KEYS = ("generation", "gen", "cycle", "iteration", "version", "epoch")


def _first(d, keys):
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def normalize_entry(e):
    if not isinstance(e, dict):
        e = {"action": str(e)}
    ts = _first(e, _TS)
    ts_s = ""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 1e9:
        ts_s = time.strftime("%m-%d %H:%M:%S", time.localtime(ts if ts < 1e12 else ts / 1000.0))
    elif ts:
        ts_s = str(ts)[:19].replace("T", " ")
    action = _first(e, _ACT)
    if action is None:
        action = next((v for v in e.values() if isinstance(v, str)), "event")
    status = _first(e, _STAT)
    score = _first(e, _SCORE)
    if isinstance(score, dict):
        score = _first(score, ("score", "value", "after", "current"))
    detail = _first(e, _DET)
    texts = [str(v).lower() for v in e.values() if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    rolled = _first(e, ("rolled_back", "rollback")) is True or any(
        w in t for t in texts for w in ("rollback", "rolled back", "roll back", "reverted"))
    if isinstance(score, bool):
        score = None
    if isinstance(score, (int, float)):
        score = round(float(score), 4)
    return dict(ts=ts_s, action=_clip(action, 160), status=_clip(status, 60) if status else "",
                score=score if isinstance(score, float) else (str(score) if score else None),
                detail=_clip(detail, 240) if detail else "", rolled_back=bool(rolled))


def history_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("history", "entries", "events", "log", "records", "evolutions", "actions", "generations"):
            if isinstance(data.get(k), list):
                return data[k]
        vals = list(data.values())
        if vals and all(isinstance(v, dict) for v in vals):
            return vals
        return [data]
    return []


def extract_status(d):
    out = {}
    if not isinstance(d, dict):
        return out
    low = {str(k).lower(): v for k, v in d.items()}
    for k in BENCH_KEYS:
        if k in low:
            v = low[k]
            if isinstance(v, dict):
                v = _first(v, ("score", "value", "current", "best"))
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out["benchmark"] = float(v)
                break
    for k in STATE_KEYS:
        if isinstance(low.get(k), (str, int)) and not isinstance(low.get(k), bool):
            out["state"] = str(low[k])
            break
    for k in GEN_KEYS:
        if k in low and isinstance(low[k], (int, str)) and not isinstance(low[k], bool):
            out["generation"] = low[k]
            break
    for k in ("rollbacks", "rollback_count"):
        if isinstance(low.get(k), int) and not isinstance(low.get(k), bool):
            out["rollbacks_reported"] = low[k]
    return out


# --------------------------------------------------------------------------
# Built-in high-precision math / business engine (Decimal, 50 digits)
# --------------------------------------------------------------------------
class MathError(Exception):
    pass


class BuiltinMath:
    PI = Decimal("3.14159265358979323846264338327950288419716939937510")
    E = Decimal("2.71828182845904523536028747135266249775724709369995")
    NUM_RE = re.compile(r"(?<![\w.])(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
    PCT_RE = re.compile(r"(?<![\w.)])(\d+(?:\.\d+)?)\s*%(?!\s*[\d(\w.])")

    HELP = ("Operators + - * / // % ^ ( ); 5% literals; consts pi e.\n"
            "Math: sqrt ln log10 log exp abs round(x,n) floor ceil min max sum avg pow factorial\n"
            "Business (rates as 0.05 or 5%): pmt(rate,nper,pv[,fv,when]) fv(rate,nper,pmt[,pv]) "
            "pv(rate,nper,pmt[,fv]) npv(rate,cf0,cf1,..) irr(cf0,cf1,..) cagr(begin,end,years) "
            "roi(gain,cost) margin(rev,cost) markup(cost,price) breakeven(fixed,price,var) "
            "compound(P,rate,m,years) discount(price,pct) addtax(amount,rate)")

    def __init__(self):
        self.funcs = {
            "sqrt": self._sqrt, "ln": self._ln, "log10": self._log10, "log": self._log,
            "exp": lambda x: self._d(x).exp(), "abs": lambda x: abs(self._d(x)),
            "round": self._round, "floor": lambda x: self._d(x).to_integral_value(rounding=ROUND_FLOOR),
            "ceil": lambda x: self._d(x).to_integral_value(rounding=ROUND_CEILING),
            "min": lambda *a: min(self._d(x) for x in a), "max": lambda *a: max(self._d(x) for x in a),
            "sum": lambda *a: sum((self._d(x) for x in a), Decimal(0)),
            "avg": self._avg, "mean": self._avg, "pow": lambda x, y: self._pow(self._d(x), self._d(y)),
            "factorial": self._fact,
            "pmt": self._pmt, "fv": self._fv, "pv": self._pv, "npv": self._npv, "irr": self._irr,
            "cagr": self._cagr, "roi": lambda g, c: (self._d(g) - self._d(c)) / self._d(c),
            "margin": lambda r, c: (self._d(r) - self._d(c)) / self._d(r),
            "markup": lambda c, p: (self._d(p) - self._d(c)) / self._d(c),
            "breakeven": lambda f, p, v: self._d(f) / (self._d(p) - self._d(v)),
            "compound": self._compound,
            "discount": lambda p, pct: self._d(p) * (1 - self._d(pct)),
            "addtax": lambda a, r: self._d(a) * (1 + self._d(r)),
            "tax": lambda a, r: self._d(a) * self._d(r),
        }
        self.consts = {"pi": self.PI, "e": self.E}

    # ---- public ----------------------------------------------------------
    def evaluate(self, expr):
        if not expr or not expr.strip():
            raise MathError("Empty expression")
        if len(expr) > 2000:
            raise MathError("Expression too long")
        src = self._prep(expr)
        try:
            tree = ast.parse(src, mode="eval")
        except (SyntaxError, ValueError, RecursionError):
            raise MathError("Syntax error - check brackets/operators (no thousands separators, e.g. 1000 not 1,000)")
        with localcontext() as ctx:
            ctx.prec = PRECISION
            ctx.Emax, ctx.Emin = 10 ** 6, -10 ** 6
            try:
                val = self._ev(tree.body)
            except DivisionByZero:
                raise MathError("Division by zero")
            except ZeroDivisionError:
                raise MathError("Division by zero")
            except InvalidOperation:
                raise MathError("Invalid operation (domain error, e.g. sqrt of a negative)")
            except Overflow:
                raise MathError("Number too large")
            except RecursionError:
                raise MathError("Expression too deeply nested")
        if not isinstance(val, Decimal):
            raise MathError("Expression did not produce a number")
        return val

    # ---- parsing ---------------------------------------------------------
    def _prep(self, expr):
        s = expr.strip()
        for a, b in (("\u00d7", "*"), ("\u00f7", "/"), ("\u2212", "-"), ("^", "**")):
            s = s.replace(a, b)
        s = re.sub(r"[$\u20ac\u00a3\u00a5]", "", s)
        s = self.PCT_RE.sub(r"(\1/100)", s)
        s = self.NUM_RE.sub(lambda m: "D('%s')" % m.group(0), s)
        return s

    def _ev(self, n):
        if isinstance(n, ast.BinOp):
            a, b = self._ev(n.left), self._ev(n.right)
            op = type(n.op)
            if op is ast.Add: return a + b
            if op is ast.Sub: return a - b
            if op is ast.Mult: return a * b
            if op is ast.Div: return a / b
            if op is ast.FloorDiv: return a // b
            if op is ast.Mod: return a % b
            if op is ast.Pow: return self._pow(a, b)
            raise MathError("Unsupported operator")
        if isinstance(n, ast.UnaryOp):
            v = self._ev(n.operand)
            if isinstance(n.op, ast.USub): return -v
            if isinstance(n.op, ast.UAdd): return +v
            raise MathError("Unsupported unary operator")
        if isinstance(n, ast.Name):
            if n.id in self.consts:
                return self.consts[n.id]
            raise MathError("Unknown name '%s'" % n.id)
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise MathError("Only simple function calls are allowed")
            if n.func.id == "D":
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    return Decimal(a.value)
                raise MathError("Bad number literal")
            fn = self.funcs.get(n.func.id)
            if fn is None:
                raise MathError("Unknown function '%s'. Type 'help' for the list." % n.func.id)
            args = [self._ev(x) for x in n.args]
            kwargs = {k.arg: self._ev(k.value) for k in n.keywords if k.arg}
            try:
                return self._d(fn(*args, **kwargs))
            except TypeError:
                raise MathError("Wrong arguments for %s()" % n.func.id)
        raise MathError("Unsupported syntax")

    # ---- function implementations ---------------------------------------
    @staticmethod
    def _d(x):
        if isinstance(x, Decimal):
            return x
        if isinstance(x, bool):
            raise MathError("Booleans are not numbers")
        return Decimal(x)

    def _pow(self, a, b):
        if b == b.to_integral_value() and abs(b) > 100000:
            raise MathError("Exponent too large")
        return a ** b

    def _sqrt(self, x): return self._d(x).sqrt()
    def _ln(self, x): return self._d(x).ln()
    def _log10(self, x): return self._d(x).log10()

    def _log(self, x, base=None):
        return self._d(x).ln() if base is None else self._d(x).ln() / self._d(base).ln()

    def _round(self, x, n=0):
        n = int(self._d(n))
        return self._d(x).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP)

    def _avg(self, *a):
        if not a:
            raise MathError("avg() needs values")
        return sum((self._d(x) for x in a), Decimal(0)) / len(a)

    def _fact(self, n):
        n = self._d(n)
        if n != n.to_integral_value() or n < 0 or n > 2000:
            raise MathError("factorial needs an integer 0..2000")
        return Decimal(math.factorial(int(n)))

    def _pmt(self, rate, nper, pv, fv=0, when=0):
        r, n, pv, fv, w = map(self._d, (rate, nper, pv, fv, when))
        if n <= 0:
            raise MathError("nper must be > 0")
        if r == 0:
            return (pv + fv) / n
        g = (1 + r) ** n
        return (fv + pv * g) * r / ((1 + r * w) * (g - 1))   # positive payment for a loan of pv

    def _fv(self, rate, nper, pmt=0, pv=0):
        r, n, p, pv = map(self._d, (rate, nper, pmt, pv))
        if r == 0:
            return pv + p * n
        g = (1 + r) ** n
        return pv * g + p * (g - 1) / r

    def _pv(self, rate, nper, pmt=0, fv=0):
        r, n, p, fv = map(self._d, (rate, nper, pmt, fv))
        if r == 0:
            return fv + p * n
        g = (1 + r) ** n
        return fv / g + p * (1 - 1 / g) / r

    def _npv(self, rate, *cfs):
        if not cfs:
            raise MathError("npv(rate, cf0, cf1, ...) needs cash flows (cf0 at t=0)")
        r = self._d(rate)
        return sum((self._d(c) / (1 + r) ** i for i, c in enumerate(cfs)), Decimal(0))

    def _irr(self, *cfs):
        cfs = [self._d(c) for c in cfs]
        if len(cfs) < 2 or not (any(c > 0 for c in cfs) and any(c < 0 for c in cfs)):
            raise MathError("irr needs at least one negative and one positive cash flow")

        def f(r):
            return sum((c / (1 + r) ** i for i, c in enumerate(cfs)), Decimal(0))
        lo, hi = Decimal("-0.9999"), Decimal(100)
        flo, fhi = f(lo), f(hi)
        if flo == 0: return lo
        if fhi == 0: return hi
        if (flo > 0) == (fhi > 0):
            raise MathError("IRR not found in range -99.99%..10000%")
        for _ in range(300):
            mid = (lo + hi) / 2
            fm = f(mid)
            if fm == 0 or (hi - lo) < Decimal("1e-35"):
                return mid
            if (fm > 0) == (flo > 0):
                lo, flo = mid, fm
            else:
                hi = mid
        return (lo + hi) / 2

    def _cagr(self, begin, end, years):
        b, e, y = map(self._d, (begin, end, years))
        if b <= 0 or y <= 0:
            raise MathError("cagr needs begin > 0 and years > 0")
        return (e / b) ** (1 / y) - 1

    def _compound(self, principal, rate, m, years):
        p, r, m, y = map(self._d, (principal, rate, m, years))
        if m <= 0:
            raise MathError("periods per year must be > 0")
        return p * (1 + r / m) ** (m * y)


# --------------------------------------------------------------------------
# Output capture: route print() from worker threads into the console
# --------------------------------------------------------------------------
_tls = threading.local()


class _StreamRouter:
    def __init__(self, orig, backend, level):
        self._orig, self._b, self._level = orig, backend, level
        self._buf = threading.local()

    def write(self, s):
        job = getattr(_tls, "job", None)
        if job is None:
            return self._orig.write(s) if self._orig is not None else len(s)
        if not isinstance(s, str):
            s = str(s)
        buf = getattr(self._buf, "v", "") + s
        *lines, rest = buf.split("\n")
        self._buf.v = rest
        for ln in lines:
            if ln.strip():
                self._b.log(self._level, ln.rstrip(), job=job.id)
        return len(s)

    def drain(self):
        rest = getattr(self._buf, "v", "")
        self._buf.v = ""
        job = getattr(_tls, "job", None)
        if rest.strip() and job is not None:
            self._b.log(self._level, rest.rstrip(), job=job.id)

    def flush(self):
        if self._orig is not None and getattr(_tls, "job", None) is None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        if self._orig is None:
            raise AttributeError(name)
        return getattr(self._orig, name)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
class Job:
    _ids = itertools.count(1)

    def __init__(self, kind, title):
        self.id = next(Job._ids)
        self.kind, self.title = kind, title
        self.status = "queued"
        self.created = time.time()
        self.started = self.finished = None
        self.result = self.error = None

    def public(self):
        end = self.finished or time.time()
        dur = (end - self.started) if self.started else 0.0
        return dict(id=self.id, kind=self.kind, title=_clip(self.title, 80), status=self.status,
                    dur=round(dur, 1), error=self.error)


class BoardError(Exception):
    pass


# --------------------------------------------------------------------------
# Backend (UI-agnostic)
# --------------------------------------------------------------------------
class Backend:
    def __init__(self, history_path=DEFAULT_HISTORY, overrides=None, force_builtin_math=False):
        self.history_path = Path(history_path)
        self.overrides = overrides or {}
        self.force_builtin = force_builtin_math
        self.builtin = BuiltinMath()

        self._lock = threading.RLock()
        self._rev = 0
        self._seq = 0
        self._epoch = 0
        self._console = deque(maxlen=1000)
        self._banners = []
        self._banner_ids = itertools.count(1)
        self._jobs = {}
        self._steps = []
        self._math_hist = deque(maxlen=200)
        self._math_ids = itertools.count(1)
        self._history = []
        self._hist_sig = None
        self._rolled_seen = None
        self._status = dict(benchmark=None, state="idle", generation=None, rollbacks=0,
                            last_action="", evolving=False, history_found=False)
        self._engines = {}
        self._mods, self._mod_err, self._inst = {}, {}, {}
        self._warned = set()

        # agent_pool is single-threaded on purpose: plan/codegen/evolve never run concurrently
        # (agent code is rarely thread-safe). Math has its own pool so it is never blocked.
        self._agent_pool = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent")
        self._math_pool = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="math")
        self._stop = threading.Event()
        self._out = self._err = None

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self._out = _StreamRouter(sys.stdout, self, "out")
        self._err = _StreamRouter(sys.stderr, self, "err")
        sys.stdout, sys.stderr = self._out, self._err
        self.log("info", "%s started. Probing agent modules in background..." % APP_NAME)
        threading.Thread(target=self._bootstrap, name="bootstrap", daemon=True).start()
        threading.Thread(target=self._monitor, name="monitor", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._agent_pool.shutdown(wait=False)
        self._math_pool.shutdown(wait=False)

    def _bootstrap(self):
        for kind in ("math", "plan", "codegen", "evolve"):
            try:
                label, fn = self.find(kind)
            except Exception as e:
                label, fn = None, None
                self.log("warn", "%s probe failed: %s" % (kind, e))
            self._engines[kind] = label or ("builtin" if kind == "math" else "not found")
            if fn:
                self.log("ok", "%s -> %s" % (kind, label))
            elif kind == "math":
                self.log("warn", "agent_math_core entry not found - using built-in Decimal engine.")
            else:
                self.log("warn", "No %s entry point found (use --%s-entry module:function)." % (kind, kind))
        for m, err in self._mod_err.items():
            self.log("warn", "import %s failed: %s" % (m, err))
        self._touch()

    # ---- logging / banners ----------------------------------------------
    def _touch(self):
        with self._lock:
            self._rev += 1

    def log(self, level, text, job=None):
        with self._lock:
            self._seq += 1
            self._console.append(dict(seq=self._seq, ts=_ts(), level=level, text=_clip(text, 3000), job=job))
            self._rev += 1

    def log_once(self, key, level, text):
        if key not in self._warned:
            self._warned.add(key)
            self.log(level, text)

    def banner(self, level, text, key=None):
        with self._lock:
            if key:
                for b in self._banners:
                    if b["key"] == key:
                        b.update(level=level, text=text)
                        self._rev += 1
                        return
            self._banners.append(dict(id=next(self._banner_ids), level=level, text=_clip(text, 300), key=key))
            self._banners = self._banners[-5:]
            self._rev += 1

    def dismiss_banner(self, bid=None):
        with self._lock:
            self._banners = [b for b in self._banners if bid is not None and b["id"] != bid]
            self._rev += 1

    def clear_console(self):
        with self._lock:
            self._console.clear()
            self._epoch += 1
            self._rev += 1

    # ---- module / entry resolution --------------------------------------
    def mod(self, name):
        if name in self._mods:
            return self._mods[name]
        mod = None
        try:
            for p in (str(BASE_DIR), os.getcwd()):
                if p not in sys.path:
                    sys.path.insert(0, p)
            mod = importlib.import_module(name)
        except Exception as e:       # SyntaxError, ImportError, errors raised at import time...
            self._mod_err[name] = "%s: %s" % (type(e).__name__, e)
        self._mods[name] = mod
        return mod

    def _instance(self, mod, cls):
        key = (mod.__name__, cls.__name__)
        if key not in self._inst:
            try:
                self._inst[key] = cls()
            except Exception as e:
                self._inst[key] = None
                self.log("warn", "Could not instantiate %s.%s(): %s" % (mod.__name__, cls.__name__, e))
        return self._inst[key]

    def _resolve_entry(self, entry):
        modname, _, attr = entry.partition(":")
        mod = self.mod(modname)
        if mod is None:
            raise BoardError("Cannot import '%s': %s" % (modname, self._mod_err.get(modname)))
        obj = mod
        parts = attr.split(".")
        for i, part in enumerate(parts):
            if inspect.isclass(obj):
                obj = self._instance(mod, obj)
                if obj is None:
                    raise BoardError("Cannot instantiate class for entry %s" % entry)
            obj = getattr(obj, part, None)
            if obj is None:
                raise BoardError("Entry '%s' not found" % entry)
        if inspect.isclass(obj):
            raise BoardError("Entry '%s' is a class; point to a function or Class.method" % entry)
        return entry, obj

    def find(self, kind):
        """Return (label, callable) or (None, None)."""
        if self.overrides.get(kind):
            return self._resolve_entry(self.overrides[kind])
        spec = SPECS[kind]
        for mn in spec["modules"]:
            mod = self.mod(mn)
            if mod is None:
                continue
            for fn in spec["funcs"]:
                f = getattr(mod, fn, None)
                if callable(f) and not inspect.isclass(f):
                    return "%s.%s" % (mn, fn), f
            for iname in spec["instances"]:
                obj = getattr(mod, iname, None)
                if obj is None or inspect.isclass(obj) or inspect.ismodule(obj) or inspect.isfunction(obj):
                    continue
                for m in spec["methods"]:
                    f = getattr(obj, m, None)
                    if callable(f):
                        return "%s.%s.%s" % (mn, iname, m), f
            for cn in spec["classes"]:
                cls = getattr(mod, cn, None)
                if inspect.isclass(cls):
                    inst = self._instance(mod, cls)
                    if inst is None:
                        continue
                    for m in spec["methods"]:
                        f = getattr(inst, m, None)
                        if callable(f):
                            return "%s.%s.%s" % (mn, cn, m), f
        return None, None

    # ---- job submission --------------------------------------------------
    def _submit(self, pool, kind, title, body):
        job = Job(kind, title)
        with self._lock:
            self._jobs[job.id] = job
            for jid in list(self._jobs)[:-40]:
                del self._jobs[jid]
            self._rev += 1

        def runner():
            _tls.job = job
            job.status, job.started = "running", time.time()
            self._touch()
            try:
                job.result = body(job)
                job.status = "done"
            except (Exception, SystemExit) as e:
                job.status, job.error = "error", "%s: %s" % (type(e).__name__, e)
                self.log("err", "%s #%d failed: %s" % (kind, job.id, job.error), job=job.id)
                tb = traceback.format_exc(limit=6)
                self.log("err", tb, job=job.id)
                self.banner("error", "%s failed: %s" % (kind.capitalize(), e), key="job-%d" % job.id)
            finally:
                for r in (self._out, self._err):
                    try:
                        r.drain()
                    except Exception:
                        pass
                job.finished = time.time()
                _tls.job = None
                self._touch()
        pool.submit(runner)
        return job

    def _busy(self, kind):
        with self._lock:
            return any(j.kind == kind and j.status in ("queued", "running") for j in self._jobs.values())

    # plan / codegen --------------------------------------------------------
    def submit_plan(self, text, kind="plan"):
        text = (text or "").strip()
        if not text:
            self.banner("warn", "Enter a plan first.", key="empty-plan")
            return None
        self.log("cmd", "> %s (%d chars)" % (kind, len(text)))
        if kind == "plan":
            with self._lock:
                self._steps = [dict(idx=i + 1, text=_clip(s, 200), status="queued")
                               for i, s in enumerate(parse_steps(text))]
        return self._submit(self._agent_pool, kind, text.splitlines()[0], lambda job: self._run_plan(job, text, kind))

    def _set_steps(self, status=None, upto=None):
        with self._lock:
            for s in self._steps:
                if status and (upto is None or s["idx"] <= upto) and s["status"] not in ("failed",):
                    s["status"] = status
            self._rev += 1

    def _run_plan(self, job, text, kind):
        if kind == "plan":
            self._set_steps("running")
        label, fn = self.find(kind)
        if fn is None:
            if kind == "plan":
                self._set_steps("skipped")
            self.banner("warn", "No %s entry point found in main.py - dry run only. "
                                "Pass --%s-entry module:function." % (kind, kind), key="no-" + kind)
            return "Dry run: %s entry point not found; nothing executed." % kind
        self.log("info", "%s -> %s" % (kind, label), job=job.id)

        def progress(*a, **k):
            msg = " ".join(str(x) for x in a) + (" " + str(k) if k else "")
            self.log("out", msg.strip(), job=job.id)
            m = re.search(r"step\s*#?\s*(\d+)", msg, re.I)
            if m and kind == "plan":
                n = int(m.group(1))
                with self._lock:
                    for s in self._steps:
                        if s["idx"] < n and s["status"] == "running":
                            s["status"] = "done"
                self._touch()
        try:
            res = call_flex(fn, text, progress=progress, on_progress=progress,
                            progress_callback=progress, on_step=progress, status_callback=progress)
        except BaseException:
            if kind == "plan":
                self._set_steps("failed")
            raise
        if kind == "plan":
            self._set_steps("done")
        out = result_to_text(res)
        self.log("ok", "%s #%d finished:\n%s" % (kind, job.id, out), job=job.id)
        if isinstance(res, dict):
            self._merge_status(extract_status(res))
        return out

    # evolution -------------------------------------------------------------
    def submit_evolve(self, goal=""):
        if self._busy("evolve"):
            self.banner("warn", "An evolution is already queued or running.", key="evolve-busy")
            return None
        with self._lock:
            self._status["evolving"] = True
            self._rev += 1
        self.log("cmd", "> evolve%s" % ((" goal=" + goal) if goal else ""))
        return self._submit(self._agent_pool, "evolve", goal or "evolution cycle",
                            lambda job: self._run_evolve(job, goal))

    def _run_evolve(self, job, goal):
        try:
            label, fn = self.find("evolve")
            if fn is None:
                raise BoardError("No evolution entry point found in agent_evolver.py "
                                 "(use --evolve-entry module:function)")
            self.log("info", "evolve -> %s" % label, job=job.id)
            res = call_flex(fn, goal) if goal else call_flex(fn)
            out = result_to_text(res)
            self.log("ok", "evolution #%d finished:\n%s" % (job.id, out), job=job.id)
            if isinstance(res, dict):
                self._merge_status(extract_status(res))
                blob = json.dumps(res, default=str).lower()
                if "rollback" in blob or "rolled back" in blob:
                    self.banner("warn", "Evolution result mentions a rollback - check the Evolution Plan Log.",
                                key="evolve-rollback")
            return out
        finally:
            with self._lock:
                self._status["evolving"] = False
                self._rev += 1
            self.refresh_history(force=True)

    # math ------------------------------------------------------------------
    def submit_math(self, expr):
        expr = (expr or "").strip()
        if not expr:
            self.banner("warn", "Type an expression first.", key="empty-math")
            return None
        if expr.lower() in ("help", "?"):
            self.log("info", BuiltinMath.HELP)
            self._record_math(expr, BuiltinMath.HELP, "help", True, 0)
            return None
        return self._submit(self._math_pool, "math", expr, lambda job: self._run_math(expr))

    def _record_math(self, expr, result, engine, ok, ms):
        with self._lock:
            self._math_hist.append(dict(id=next(self._math_ids), ts=_ts(), expr=expr, result=result,
                                        engine=engine, ok=ok, ms=ms))
            self._rev += 1

    def _run_math(self, expr):
        t0 = time.perf_counter()
        core_err, text, engine = None, None, None
        if not self.force_builtin:
            try:
                label, fn = self.find("math")
            except BoardError as e:
                label, fn, core_err = None, None, str(e)
            if fn is not None:
                try:
                    raw = call_flex(fn, expr)
                    text, engine = result_to_text(raw), label
                except Exception as e:
                    core_err = "%s: %s" % (type(e).__name__, e)
        if text is None:
            try:
                d = self.builtin.evaluate(expr)
            except MathError as e:
                msg = str(e) + (" [agent_math_core: %s]" % core_err if core_err else "")
                self._record_math(expr, msg, "error", False, 0)
                raise MathError(msg)
            text = fmt_decimal(d)
            hint = pretty_money(d) if abs(d) >= 1000 else ""
            if hint and hint != text:
                text += "   (%s)" % hint
            engine = "builtin Decimal-%d" % PRECISION
            if core_err:
                engine += " (core failed)"
                self.log("warn", "agent_math_core failed (%s); used built-in engine." % core_err)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        self._record_math(expr, text, engine, True, ms)
        self.log("ok", "%s = %s   [%s, %s ms]" % (expr, text, engine, ms))
        return text

    # ---- history + status monitor ---------------------------------------
    def refresh_history(self, force=False):
        p = self.history_path
        try:
            st = p.stat()
        except OSError:
            with self._lock:
                if self._status["history_found"] or force:
                    self._status["history_found"] = False
                    self._history = []
                    self._rev += 1
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._hist_sig and not force:
            return
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw.strip() else []
            except json.JSONDecodeError:
                data = [json.loads(l) for l in raw.splitlines() if l.strip()]   # JSON-lines fallback
        except Exception as e:
            self.log_once("hist-bad-%s" % sig[0], "warn", "evolution_history.json unreadable (%s); will retry." % e)
            return
        self._hist_sig = sig
        entries = [normalize_entry(e) for e in history_items(data)][-300:]
        rolled = sum(1 for e in entries if e["rolled_back"])
        with self._lock:
            self._history = entries
            self._status["history_found"] = True
            self._status["rollbacks"] = max(rolled, self._status.get("rollbacks_reported", 0))
            if entries:
                last = entries[-1]
                self._status["last_action"] = last["action"]
                if self._status["benchmark"] is None:
                    sc = next((e["score"] for e in reversed(entries) if isinstance(e["score"], float)), None)
                    if sc is not None:
                        self._status["benchmark"] = sc
            prev = self._rolled_seen
            self._rolled_seen = rolled
            self._rev += 1
        if prev is not None and rolled > prev:
            last_r = next(e for e in reversed(entries) if e["rolled_back"])
            self.banner("warn", "ROLLBACK: %s %s" % (last_r["action"], last_r["detail"]), key="rollback-%d" % rolled)
            self.log("warn", "Rollback detected in evolution history: %s" % last_r["action"])

    def _merge_status(self, upd):
        with self._lock:
            for k, v in upd.items():
                if k == "state" and self._status.get("evolving"):
                    continue
                self._status[k] = v
            self._rev += 1

    def refresh_status(self):
        label, fn = None, None
        try:
            label, fn = self.find("status")
        except BoardError as e:
            self.log_once("status-entry", "warn", str(e))
        if fn is None:
            return
        try:
            val = call_flex(fn)
        except Exception as e:
            self.log_once("status-err-%s" % type(e).__name__, "warn", "status getter %s failed: %s" % (label, e))
            return
        if isinstance(val, dict):
            self._merge_status(extract_status(val))
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            self._merge_status({"benchmark": float(val)})
        elif isinstance(val, str):
            self._merge_status({"state": val})

    def _monitor(self):
        while not self._stop.is_set():
            try:
                self.refresh_history()
                self.refresh_status()
            except Exception as e:
                self.log_once("monitor-%s" % type(e).__name__, "warn", "monitor error: %s" % e)
            self._stop.wait(2.0)

    # ---- snapshot --------------------------------------------------------
    def snapshot(self, since=0):
        with self._lock:
            lines = [l for l in self._console if l["seq"] > since][-400:]
            jobs = [j.public() for j in list(self._jobs.values())[-12:]]
            st = dict(self._status)
            st["state"] = "evolving" if st["evolving"] else st.get("state") or "idle"
            return dict(rev=self._rev, epoch=self._epoch, last_seq=self._seq, console=lines,
                        banners=[dict(b) for b in self._banners], jobs=jobs,
                        busy=sum(1 for j in self._jobs.values() if j.status in ("queued", "running")),
                        steps=[dict(s) for s in self._steps], status=st,
                        history=list(self._history[-60:]), math=list(self._math_hist)[-60:],
                        engines=dict(self._engines), history_path=str(self.history_path))


def status_line(snap):
    st = snap["status"]
    b = st.get("benchmark")
    bs = "%.4f" % b if isinstance(b, float) else "n/a"
    return "Bench %s | %s | rollbacks %s | jobs %d" % (bs, st["state"], st.get("rollbacks", 0), snap["busy"])


# --------------------------------------------------------------------------
# Kivy front-end
# --------------------------------------------------------------------------
def run_kivy(backend):
    os.environ.setdefault("KIVY_NO_ARGS", "1")
    from kivy.config import Config
    Config.set("kivy", "exit_on_escape", "0")
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.core.clipboard import Clipboard
    from kivy.core.window import Window
    from kivy.graphics import Color, Rectangle
    from kivy.metrics import dp, sp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.label import Label
    from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.textinput import TextInput
    from kivy.utils import escape_markup

    Window.softinput_mode = "below_target"      # soft keyboard pans the focused input into view
    Window.clearcolor = (0.05, 0.07, 0.09, 1)

    C = dict(info="9db4d0", ok="3fb950", warn="d29922", err="f85149", out="c9d1d9", cmd="58a6ff", muted="8b949e")
    BG, CARD, BTN, BTN_ON, TXT = (0.05, 0.07, 0.09, 1), (0.09, 0.11, 0.14, 1), (0.16, 0.2, 0.27, 1), (0.13, 0.4, 0.8, 1), (0.9, 0.93, 0.95, 1)
    BANNER = dict(error=(0.6, 0.12, 0.12, 1), warn=(0.55, 0.4, 0.05, 1), info=(0.1, 0.3, 0.55, 1))
    STEP_ICON = dict(queued=("muted", "."), running=("cmd", ">"), done=("ok", "OK"), failed=("err", "X"), skipped=("warn", "-"))

    def paint(w, rgba):
        with w.canvas.before:
            col = Color(*rgba)
            rect = Rectangle(pos=w.pos, size=w.size)
        w.bind(pos=lambda _w, v: setattr(rect, "pos", v), size=lambda _w, v: setattr(rect, "size", v))
        return col

    def wlabel(text="", size=13, color=TXT, **kw):
        lb = Label(text=text, markup=True, size_hint_y=None, halign="left", valign="top",
                   font_size=sp(size), color=color, **kw)
        lb.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        lb.bind(texture_size=lambda w, v: setattr(w, "height", v[1] + dp(4)))
        return lb

    def button(text, cb, color=BTN, **kw):
        b = Button(text=text, background_normal="", background_color=color, color=TXT, font_size=sp(14), **kw)
        b.bind(on_release=lambda *_: cb())
        return b

    def tinput(hint, multiline=False, **kw):
        return TextInput(hint_text=hint, multiline=multiline, write_tab=False, input_type="text",
                         background_color=CARD, foreground_color=TXT, hint_text_color=(0.5, 0.55, 0.6, 1),
                         cursor_color=(0.35, 0.65, 1, 1), font_size=sp(15),
                         padding=(dp(10), dp(10)), **kw)

    class LogView(ScrollView):
        def __init__(self, max_lines=300, **kw):
            super().__init__(do_scroll_x=False, bar_width=dp(4), **kw)
            self.max_lines = max_lines
            self.box = GridLayout(cols=1, size_hint_y=None, spacing=dp(2), padding=(dp(8), dp(4)))
            self.box.bind(minimum_height=self.box.setter("height"))
            self.add_widget(self.box)

        def add_line(self, markup, copy_text=None, follow=True):
            at_bottom = self.scroll_y <= 0.05 or self.box.height <= self.height
            lb = wlabel(markup)
            if copy_text is not None:
                lb.bind(on_ref_press=lambda *_: Clipboard.copy(copy_text))
            self.box.add_widget(lb)
            while len(self.box.children) > self.max_lines:
                self.box.remove_widget(self.box.children[-1])
            if follow and at_bottom:
                Clock.schedule_once(lambda dt: setattr(self, "scroll_y", 0), 0.05)

        def clear(self):
            self.box.clear_widgets()

        def to_top(self):
            Clock.schedule_once(lambda dt: setattr(self, "scroll_y", 1), 0.05)

    def colored(level, text):
        return "[color=#%s]%s[/color]" % (C.get(level, C["out"]), escape_markup(text))

    class BoardApp(App):
        title = APP_NAME

        def build(self):
            self.seq, self.rev, self.epoch = 0, -1, 0
            self.sigs, self.math_seen, self.cur_banner = {}, 0, None
            root = BoxLayout(orientation="vertical", spacing=dp(4), padding=(dp(6), dp(6), dp(6), dp(4)))

            # status bar
            bar = BoxLayout(size_hint_y=None, height=dp(38), padding=(dp(8), 0))
            paint(bar, CARD)
            self.status_lbl = Label(text="starting...", font_size=sp(13), color=TXT, halign="left", valign="middle",
                                    shorten=True, shorten_from="right")
            self.status_lbl.bind(size=lambda w, v: setattr(w, "text_size", v))
            bar.add_widget(self.status_lbl)
            root.add_widget(bar)

            # banner (tap X to dismiss)
            self.banner_box = BoxLayout(size_hint_y=None, height=0, opacity=0)
            self.banner_col = paint(self.banner_box, BANNER["error"])
            self.banner_lbl = wlabel("", 13, color=(1, 1, 1, 1))
            self.banner_lbl.size_hint_y = 1
            self.banner_lbl.valign = "middle"
            self.banner_box.add_widget(self.banner_lbl)
            self.banner_box.add_widget(button("X", self.dismiss_banner, color=(0, 0, 0, 0.25), size_hint_x=None, width=dp(44)))
            root.add_widget(self.banner_box)

            self.sm = ScreenManager(transition=NoTransition())
            for name, builder in (("plan", self.build_plan), ("math", self.build_math),
                                  ("evolve", self.build_evolve), ("console", self.build_console)):
                scr = Screen(name=name)
                scr.add_widget(builder())
                self.sm.add_widget(scr)
            root.add_widget(self.sm)

            nav = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(4))
            self.nav = {}
            for name, label in (("plan", "Plan"), ("math", "Math"), ("evolve", "Evolve"), ("console", "Console")):
                b = button(label, lambda n=name: self.go(n))
                self.nav[name] = b
                nav.add_widget(b)
            root.add_widget(nav)
            self.go("plan")
            Window.bind(on_keyboard=self.on_key)
            Clock.schedule_interval(self.tick, 0.25)
            return root

        # ---- screens ------------------------------------------------------
        def build_plan(self):
            box = BoxLayout(orientation="vertical", spacing=dp(6))
            self.plan_in = tinput("Paste or type an execution plan.\nOne step per line works best.", multiline=True,
                                  size_hint_y=0.42)
            box.add_widget(self.plan_in)
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            row.add_widget(button("Run plan", lambda: self.submit_plan("plan"), color=BTN_ON))
            row.add_widget(button("Codegen", lambda: self.submit_plan("codegen")))
            row.add_widget(button("Clear", lambda: setattr(self.plan_in, "text", "")))
            box.add_widget(row)
            box.add_widget(wlabel("[b]Active steps[/b]", 13, color=TXT))
            self.steps_view = LogView(size_hint_y=0.4, max_lines=100)
            paint(self.steps_view, CARD)
            box.add_widget(self.steps_view)
            return box

        def build_math(self):
            box = BoxLayout(orientation="vertical", spacing=dp(6))
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            self.math_in = tinput("e.g. pmt(5%/12, 360, 250000)   (type 'help')")
            self.math_in.bind(on_text_validate=lambda *_: self.submit_math())
            row.add_widget(self.math_in)
            row.add_widget(button("=", self.submit_math, color=BTN_ON, size_hint_x=None, width=dp(56)))
            row.add_widget(button("C", lambda: setattr(self.math_in, "text", ""), size_hint_x=None, width=dp(48)))
            box.add_widget(row)
            chips = ScrollView(size_hint_y=None, height=dp(44), do_scroll_y=False, bar_width=0)
            inner = BoxLayout(size_hint_x=None, spacing=dp(4))
            inner.bind(minimum_width=inner.setter("width"))
            for t in ("pmt(", "fv(", "pv(", "npv(", "irr(", "cagr(", "roi(", "margin(", "markup(", "compound(",
                      "sqrt(", "round(", "5%", "pi", ", ", "(", ")", "^"):
                inner.add_widget(button(t, lambda t=t: self.insert_math(t), size_hint_x=None, width=dp(30) + dp(11) * len(t)))
            chips.add_widget(inner)
            box.add_widget(chips)
            box.add_widget(wlabel("Tap a result to copy it.", 11, color=(0.55, 0.6, 0.65, 1)))
            self.math_view = LogView()
            paint(self.math_view, CARD)
            box.add_widget(self.math_view)
            return box

        def build_evolve(self):
            box = BoxLayout(orientation="vertical", spacing=dp(6))
            card = BoxLayout(size_hint_y=None, padding=dp(8))
            card.bind(minimum_height=card.setter("height"))
            paint(card, CARD)
            self.evo_lbl = wlabel("...", 14)
            card.add_widget(self.evo_lbl)
            box.add_widget(card)
            self.goal_in = tinput("Optional goal / note passed to the evolver", size_hint_y=None, height=dp(44))
            box.add_widget(self.goal_in)
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            self.evo_btn = button("Run evolution", self.submit_evolve, color=BTN_ON)
            row.add_widget(self.evo_btn)
            row.add_widget(button("Refresh", lambda: threading.Thread(
                target=lambda: (backend.refresh_history(force=True), backend.refresh_status()), daemon=True).start()))
            box.add_widget(row)
            box.add_widget(wlabel("[b]Evolution Plan Log[/b]  (newest first)", 13))
            self.hist_view = LogView(max_lines=120)
            paint(self.hist_view, CARD)
            box.add_widget(self.hist_view)
            return box

        def build_console(self):
            box = BoxLayout(orientation="vertical", spacing=dp(6))
            self.console = LogView(max_lines=300)
            paint(self.console, CARD)
            box.add_widget(self.console)
            box.add_widget(button("Clear console", backend.clear_console, size_hint_y=None, height=dp(44)))
            return box

        # ---- actions ------------------------------------------------------
        def go(self, name):
            for w in (self.plan_in, self.math_in, self.goal_in):
                w.focus = False
            self.sm.current = name
            for n, b in self.nav.items():
                b.background_color = BTN_ON if n == name else BTN

        def on_key(self, window, key, *args):
            if key == 27 and self.sm.current != "plan":      # Android back button
                self.go("plan")
                return True
            return False

        def submit_plan(self, kind):
            self.plan_in.focus = False
            backend.submit_plan(self.plan_in.text, kind)

        def submit_math(self):
            backend.submit_math(self.math_in.text)
            Clock.schedule_once(lambda dt: setattr(self.math_in, "focus", True), 0.1)   # keep keyboard up

        def insert_math(self, t):
            self.math_in.insert_text(t)
            self.math_in.focus = True

        def submit_evolve(self):
            self.goal_in.focus = False
            backend.submit_evolve(self.goal_in.text.strip())

        def dismiss_banner(self):
            if self.cur_banner is not None:
                backend.dismiss_banner(self.cur_banner)

        def on_pause(self):
            return True

        def on_stop(self):
            backend.stop()

        # ---- polling ------------------------------------------------------
        def tick(self, dt):
            snap = backend.snapshot(self.seq)
            if snap["rev"] == self.rev:
                return
            self.rev = snap["rev"]
            try:
                self.apply(snap)
            except Exception:
                backend.log_once("ui-" + traceback.format_exc()[-80:], "err", "UI update error:\n" + traceback.format_exc(limit=4))

        def apply(self, snap):
            if snap["epoch"] != self.epoch:
                self.epoch, self.seq = snap["epoch"], 0
                self.console.clear()
                snap = backend.snapshot(0)
            for l in snap["console"]:
                tag = "[%s] " % l["ts"] + ("#%s " % l["job"] if l["job"] else "")
                self.console.add_line("[color=#%s]%s[/color]%s" % (C["muted"], tag, colored(l["level"], l["text"])))
            self.seq = snap["last_seq"]
            self.status_lbl.text = status_line(snap)

            b = snap["banners"][-1] if snap["banners"] else None
            self.cur_banner = b["id"] if b else None
            if b:
                self.banner_col.rgba = BANNER.get(b["level"], BANNER["info"])
                self.banner_lbl.text = escape_markup(b["text"])
                self.banner_box.height, self.banner_box.opacity = dp(52), 1
            else:
                self.banner_box.height, self.banner_box.opacity = 0, 0

            sig = repr(snap["steps"])
            if self.sigs.get("steps") != sig:
                self.sigs["steps"] = sig
                self.steps_view.clear()
                for s in snap["steps"]:
                    colr, icon = STEP_ICON.get(s["status"], ("out", "?"))
                    self.steps_view.add_line("[color=#%s][b]%s[/b][/color] %d. %s" % (
                        C[colr], icon, s["idx"], escape_markup(s["text"])), follow=False)
                self.steps_view.to_top()

            for m in snap["math"]:
                if m["id"] > self.math_seen:
                    self.math_seen = m["id"]
                    self.math_view.add_line(
                        "[color=#%s]%s[/color]\n[ref=c][b]%s[/b][/ref]  [color=#%s]%s[/color]" % (
                            C["muted"], escape_markup(m["expr"]),
                            colored("ok" if m["ok"] else "err", m["result"]),
                            C["muted"], escape_markup(m["engine"])),
                        copy_text=m["result"].split("   (")[0])

            st = snap["status"]
            b_ = st.get("benchmark")
            self.evo_lbl.text = (
                "[b]State:[/b] %s\n[b]Benchmark:[/b] %s\n[b]Generation:[/b] %s\n[b]Rollbacks:[/b] %s\n"
                "[b]Last action:[/b] %s\n[color=#%s]history: %s%s[/color]" % (
                    st["state"], "%.4f" % b_ if isinstance(b_, float) else "n/a", st.get("generation") or "-",
                    st.get("rollbacks", 0), escape_markup(st.get("last_action") or "-"), C["muted"],
                    escape_markup(snap["history_path"]), "" if st.get("history_found") else "  (not found yet)"))
            self.evo_btn.disabled = bool(st.get("evolving"))
            self.evo_btn.text = "Evolving..." if st.get("evolving") else "Run evolution"

            sig = repr(snap["history"])
            if self.sigs.get("hist") != sig:
                self.sigs["hist"] = sig
                self.hist_view.clear()
                if not snap["history"]:
                    self.hist_view.add_line(colored("muted", "No evolution history yet."), follow=False)
                for e in reversed(snap["history"][-120:]):
                    lvl = "err" if e["rolled_back"] else "out"
                    head = "%s  %s%s%s" % (e["ts"], e["action"], (" - " + e["status"]) if e["status"] else "",
                                           (" (score %s)" % e["score"]) if e["score"] is not None else "")
                    txt = colored(lvl, ("ROLLBACK  " if e["rolled_back"] else "") + head)
                    if e["detail"]:
                        txt += "\n" + colored("muted", "   " + e["detail"])
                    self.hist_view.add_line(txt, follow=False)
                self.hist_view.to_top()

    BoardApp().run()


# --------------------------------------------------------------------------
# Web front-end (stdlib only, responsive, mobile-first)
# --------------------------------------------------------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1117"><title>AutoMind Board</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--mut:#8b949e;--ok:#3fb950;--warn:#d29922;--err:#f85149;--acc:#2f81f7}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--txt);font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;display:flex;flex-direction:column;
padding-top:env(safe-area-inset-top)}
header{padding:8px 12px;background:var(--card);border-bottom:1px solid var(--line);display:flex;gap:8px;justify-content:space-between;align-items:center}
header b{white-space:nowrap}#chip{font-size:13px;color:var(--mut);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#banners div{padding:10px 12px;display:flex;justify-content:space-between;gap:10px;font-size:14px;color:#fff}
#banners .error{background:#8e1b1b}#banners .warn{background:#8a6407}#banners .info{background:#17497f}
#banners button{background:#0004;border:0;color:#fff;border-radius:6px;padding:0 12px;font-size:16px}
main{flex:1;min-height:0;position:relative}
.screen{display:none;position:absolute;inset:0;overflow:auto;padding:10px;-webkit-overflow-scrolling:touch}.screen.active{display:block}
textarea,input[type=text]{width:100%;background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:10px;font:16px system-ui,sans-serif}
textarea{min-height:34vh;resize:vertical}
.row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap}.row>*{flex:1}
button.b{background:#222b38;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:12px;font-size:15px;min-height:46px}
button.b.p{background:var(--acc);border-color:var(--acc)}button.b:disabled{opacity:.5}
.chips{display:flex;gap:6px;overflow-x:auto;padding:4px 0}.chips button{flex:none;min-width:48px;padding:8px 10px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;margin:8px 0;
font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.panel.scroll{max-height:42vh;overflow:auto}
.ln{padding:1px 0}.ln i{color:var(--mut);font-style:normal}
.info{color:#9db4d0}.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.out{color:#c9d1d9}.cmd{color:#58a6ff}.muted{color:var(--mut)}
.res{cursor:pointer}h4{margin:12px 0 4px}
nav{display:flex;background:var(--card);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
nav button{flex:1;background:none;border:0;color:var(--mut);padding:14px 4px;font-size:15px}nav button.on{color:#fff;border-top:2px solid var(--acc)}
</style></head><body>
<header><b>AutoMind Board</b><span id="chip">connecting...</span></header>
<div id="banners"></div>
<main>
<section class="screen active" id="s-plan">
 <textarea id="plan" placeholder="Paste or type an execution plan. One step per line works best." autocapitalize="sentences"></textarea>
 <div class="row"><button class="b p" id="bRun">Run plan</button><button class="b" id="bCode">Codegen</button><button class="b" id="bClr">Clear</button></div>
 <h4>Active steps</h4><div class="panel scroll" id="steps">-</div>
</section>
<section class="screen" id="s-math">
 <div class="row"><input type="text" id="mexp" placeholder="pmt(5%/12, 360, 250000)  (type help)" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="go">
 <button class="b p" id="bCalc" style="flex:0 0 64px">=</button></div>
 <div class="chips" id="chips"></div>
 <div class="muted" style="font-size:12px">Tap a result to copy it.</div>
 <div class="panel scroll" id="mlog" style="max-height:60vh">-</div>
</section>
<section class="screen" id="s-evo">
 <div class="panel" id="evo">-</div>
 <input type="text" id="goal" placeholder="Optional goal / note passed to the evolver">
 <div class="row"><button class="b p" id="bEvo">Run evolution</button><button class="b" id="bRef">Refresh</button></div>
 <h4>Evolution Plan Log <span class="muted">(newest first)</span></h4><div class="panel scroll" id="hist" style="max-height:50vh">-</div>
</section>
<section class="screen" id="s-con">
 <div class="panel" id="con" style="min-height:70vh;max-height:none"></div>
 <button class="b" id="bCC" style="width:100%">Clear console</button>
</section>
</main>
<nav id="nav"><button data-t="plan" class="on">Plan</button><button data-t="math">Math</button><button data-t="evo">Evolve</button><button data-t="con">Console</button></nav>
<script>
var TOKEN=new URLSearchParams(location.search).get('token')||'';
var since=0,epoch=0,sig={},mathSeen=0,lastOk=Date.now();
function $(i){return document.getElementById(i)}
function el(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!==undefined)e.textContent=txt;return e}
function api(path,body){var o={headers:{'X-Token':TOKEN}};if(body!==undefined){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}
 return fetch(path,o).then(function(r){return r.json()})}
function act(path,body){return api(path,body).then(function(r){if(!r.ok)localBanner(r.error||'Request failed');poll()}).catch(function(e){localBanner('Cannot reach server: '+e)})}
var lb='';function localBanner(t){lb=t;renderBanners(window._b||[])}
function renderBanners(list){window._b=list;var box=$('banners');box.textContent='';
 if(lb){var d=el('div','error');d.appendChild(el('span','',lb));var x=el('button','','X');x.onclick=function(){lb='';renderBanners(window._b)};d.appendChild(x);box.appendChild(d)}
 list.forEach(function(b){var d=el('div',b.level);d.appendChild(el('span','',b.text));var x=el('button','','X');x.onclick=function(){act('/api/dismiss',{id:b.id})};d.appendChild(x);box.appendChild(d)})}
function show(t){document.querySelectorAll('.screen').forEach(function(s){s.classList.remove('active')});$('s-'+t).classList.add('active');
 document.querySelectorAll('nav button').forEach(function(b){b.classList.toggle('on',b.dataset.t===t)});if(document.activeElement)document.activeElement.blur()}
document.querySelectorAll('nav button').forEach(function(b){b.onclick=function(){show(b.dataset.t)}});
$('bRun').onclick=function(){$('plan').blur();act('/api/plan',{text:$('plan').value})};
$('bCode').onclick=function(){$('plan').blur();act('/api/codegen',{text:$('plan').value})};
$('bClr').onclick=function(){$('plan').value=''};
function calc(){act('/api/math',{expr:$('mexp').value});$('mexp').focus()}
$('bCalc').onclick=calc;$('mexp').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();calc()}});
['pmt(','fv(','pv(','npv(','irr(','cagr(','roi(','margin(','markup(','compound(','sqrt(','round(','5%','pi',', ','(',')','^'].forEach(function(t){
 var b=el('button','b',t);b.onclick=function(){var i=$('mexp'),s=i.selectionStart||i.value.length,e=i.selectionEnd||s;i.value=i.value.slice(0,s)+t+i.value.slice(e);i.focus();i.selectionStart=i.selectionEnd=s+t.length};$('chips').appendChild(b)});
$('bEvo').onclick=function(){act('/api/evolve',{goal:$('goal').value.trim()})};
$('bRef').onclick=function(){act('/api/refresh',{})};
$('bCC').onclick=function(){act('/api/clear_console',{})};
function nearBottom(n){return n.scrollHeight-n.scrollTop-n.clientHeight<60}
function render(s){
 if(s.epoch!==epoch){epoch=s.epoch;since=0;$('con').textContent='';poll();return}
 var con=$('con'),sc=nearBottom(document.getElementById('s-con'));
 s.console.forEach(function(l){var d=el('div','ln');d.appendChild(el('i','','['+l.ts+']'+(l.job?' #'+l.job:'')+' '));d.appendChild(el('span',l.level,l.text));con.appendChild(d)});
 while(con.children.length>500)con.removeChild(con.firstChild);
 if(sc&&s.console.length){var c=$('s-con');c.scrollTop=c.scrollHeight}
 since=s.last_seq;
 var st=s.status,b=(typeof st.benchmark==='number')?st.benchmark.toFixed(4):'n/a';
 $('chip').textContent='Bench '+b+' | '+st.state+' | rollbacks '+(st.rollbacks||0)+' | jobs '+s.busy;
 renderBanners(s.banners);
 var k=JSON.stringify(s.steps);if(sig.steps!==k){sig.steps=k;var p=$('steps');p.textContent='';
  if(!s.steps.length)p.textContent='No plan running.';
  var ic={queued:['muted','.'],running:['cmd','>'],done:['ok','OK'],failed:['err','X'],skipped:['warn','-']};
  s.steps.forEach(function(x){var i=ic[x.status]||['out','?'],d=el('div','ln');d.appendChild(el('b',i[0],i[1]+' '));d.appendChild(el('span','out',x.idx+'. '+x.text));p.appendChild(d)})}
 s.math.forEach(function(m){if(m.id>mathSeen){mathSeen=m.id;var p=$('mlog');if(p.dataset.e!=='1'){p.dataset.e='1';p.textContent=''}
  var d=el('div','ln');d.appendChild(el('div','muted',m.expr));var r=el('div',(m.ok?'ok':'err')+' res',m.result);
  var raw=m.result.split('   (')[0];r.onclick=function(){if(navigator.clipboard)navigator.clipboard.writeText(raw)};d.appendChild(r);d.appendChild(el('div','muted',m.engine+(m.ok?' / '+m.ms+' ms':'')));
  d.style.marginBottom='8px';p.appendChild(d);p.scrollTop=p.scrollHeight}});
 $('evo').textContent='State: '+st.state+'\nBenchmark: '+b+'\nGeneration: '+(st.generation||'-')+'\nRollbacks: '+(st.rollbacks||0)+'\nLast action: '+(st.last_action||'-')+
  '\nHistory: '+s.history_path+(st.history_found?'':'  (not found yet)')+'\nEngines: math='+(s.engines.math||'?')+' plan='+(s.engines.plan||'?')+' evolve='+(s.engines.evolve||'?');
 $('bEvo').disabled=!!st.evolving;$('bEvo').textContent=st.evolving?'Evolving...':'Run evolution';
 k=JSON.stringify(s.history);if(sig.hist!==k){sig.hist=k;var h=$('hist');h.textContent='';if(!s.history.length)h.textContent='No evolution history yet.';
  s.history.slice().reverse().forEach(function(e){var d=el('div','ln '+(e.rolled_back?'err':'out'));
   d.textContent=(e.rolled_back?'ROLLBACK  ':'')+e.ts+'  '+e.action+(e.status?' - '+e.status:'')+(e.score!==null&&e.score!==undefined?' (score '+e.score+')':'');
   if(e.detail){d.appendChild(el('div','muted','   '+e.detail))}d.style.marginBottom='6px';h.appendChild(d)})}
}
var busy=false;
function poll(){if(busy)return;busy=true;api('/api/state?since='+since).then(function(s){busy=false;lastOk=Date.now();if(lb.indexOf('Cannot reach')===0){lb=''}render(s)})
 .catch(function(e){busy=false;if(Date.now()-lastOk>4000)localBanner('Cannot reach server - retrying...')})}
setInterval(poll,1000);poll();
</script></body></html>
"""


def run_web(backend, host, port, token, open_browser=False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            if not token:
                return True
            q = parse_qs(urlparse(self.path).query)
            given = self.headers.get("X-Token") or (q.get("token") or [""])[0]
            return hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, HTML_PAGE, "text/html; charset=utf-8")
            if u.path == "/api/state":
                if not self._authed():
                    return self._send(401, json.dumps({"ok": False, "error": "bad token"}))
                try:
                    since = int((parse_qs(u.query).get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                return self._send(200, json.dumps(backend.snapshot(since), default=str))
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

        def do_POST(self):
            if not self._authed():
                return self._send(401, json.dumps({"ok": False, "error": "bad token"}))
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1_000_000:
                    return self._send(413, json.dumps({"ok": False, "error": "payload too large"}))
                body = json.loads(self.rfile.read(n) or b"{}")
                p = urlparse(self.path).path
                if p == "/api/plan":
                    backend.submit_plan(body.get("text", ""), "plan")
                elif p == "/api/codegen":
                    backend.submit_plan(body.get("text", ""), "codegen")
                elif p == "/api/math":
                    backend.submit_math(body.get("expr", ""))
                elif p == "/api/evolve":
                    backend.submit_evolve(str(body.get("goal", "")))
                elif p == "/api/refresh":
                    threading.Thread(target=lambda: (backend.refresh_history(force=True), backend.refresh_status()),
                                     daemon=True).start()
                elif p == "/api/dismiss":
                    backend.dismiss_banner(body.get("id"))
                elif p == "/api/clear_console":
                    backend.clear_console()
                else:
                    return self._send(404, json.dumps({"ok": False, "error": "not found"}))
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}))

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = "http://%s:%d/%s" % (shown, srv.server_address[1], ("?token=" + token) if token else "")
    sys.__stdout__.write("%s web UI: %s\n" % (APP_NAME, url))
    sys.__stdout__.flush()
    backend.log("ok", "Web UI at " + url)
    # Intentionally do not launch a browser automatically. Open the printed
    # localhost URL manually in a browser/WebView if the user wants Web UI.
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--ui", choices=("auto", "kivy", "web"), default="auto")
    ap.add_argument("--host", default="127.0.0.1", help="web UI bind address (default localhost)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="", help="shared secret required for the web API (needed if --host is not loopback)")
    ap.add_argument("--no-open", action="store_true", default=True, help="browser auto-launch is disabled; open localhost URL manually")
    ap.add_argument("--history", default=str(DEFAULT_HISTORY), help="path to evolution_history.json")
    ap.add_argument("--builtin-math", action="store_true", help="skip agent_math_core, use built-in Decimal engine")
    for k in ("plan", "codegen", "evolve", "math"):
        ap.add_argument("--%s-entry" % k, default="", help="override: module:function or module:Class.method")
    args = ap.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        ap.error("--token is required when binding the web UI to a non-loopback address "
                 "(the UI can execute agent plans).")

    overrides = {k: getattr(args, "%s_entry" % k) for k in ("plan", "codegen", "evolve", "math") if getattr(args, "%s_entry" % k)}
    backend = Backend(args.history, overrides, args.builtin_math)
    backend.start()

    mode = args.ui
    if mode == "auto":
        try:
            os.environ.setdefault("KIVY_NO_ARGS", "1")
            import kivy  # noqa: F401
            mode = "kivy"
        except Exception:
            mode = "web"
    try:
        if mode == "kivy":
            try:
                run_kivy(backend)
                return 0
            except ImportError as e:
                backend.log("warn", "Kivy unavailable (%s); falling back to web UI." % e)
            except Exception:
                sys.__stderr__.write("Kivy UI failed, falling back to web UI:\n" + traceback.format_exc())
        run_web(backend, args.host, args.port, args.token, False)
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

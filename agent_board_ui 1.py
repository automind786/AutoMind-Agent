#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_board_ui.py - Canvas / execution board for the standalone AutoMind agent.

Front ends (all share one non-blocking core):
    kivy : native Android/desktop UI (needs only stock Kivy)
    web  : responsive phone-friendly page served on localhost (stdlib only)
    tui  : plain terminal REPL (stdlib only; works in Termux)

Run:
    python agent_board_ui.py                # kivy if installed, else web
    python agent_board_ui.py --mode web     # open http://127.0.0.1:8765
    python agent_board_ui.py --mode tui
    python agent_board_ui.py --history /path/to/evolution_history.json

Backends (imported if present, otherwise the board degrades gracefully):
    agent_math_core.py : high-precision math / business calculations
    agent_evolver.py   : evolution runs, status, benchmark
    evolution_history.json : read-only "Evolution Plan Log"

Because the exact public API of your two modules is discovered at runtime,
the method-name lists in the ADAPTER CONFIG block below are the one place
to edit if your names differ.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import decimal
import importlib
import importlib.util
import inspect
import json
import operator
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (APP_DIR, os.getcwd()):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --------------------------------------------------------------------------
# ADAPTER CONFIG - edit these if your modules use different names
# --------------------------------------------------------------------------
MATH_MODULE = "agent_math_core"
EVOLVER_MODULE = "agent_evolver"

MATH_EVAL_NAMES = ("evaluate", "calculate", "calc", "evaluate_expression",
                   "eval_expr", "safe_eval", "compute")
EVOLVER_CLASS_NAMES = ("AgentEvolver", "Evolver", "SelfEvolver",
                       "EvolutionEngine", "AutoMindEvolver")
EVOLVER_RUN_NAMES = ("run_evolution", "evolve", "run_evolution_cycle",
                     "run_cycle", "evolve_once", "execute_plan", "run_plan",
                     "run", "start_evolution")
EVOLVER_STATUS_NAMES = ("get_status", "status", "snapshot", "get_state", "state")
EVOLVER_BENCH_NAMES = ("run_benchmark", "benchmark", "get_benchmark_score",
                       "evaluate_benchmark")

# parameter names the adapter fills automatically when calling your methods
TEXT_PARAMS = {"plan", "task", "goal", "prompt", "instructions", "text",
               "objective", "description", "request", "query", "plan_text",
               "task_text"}
LIST_PARAMS = {"steps", "tasks", "plan_steps", "actions"}
CALLBACK_PARAMS = {"progress_callback", "on_progress", "on_event", "callback",
                   "log_callback", "emit"}

SCORE_KEYS = ("benchmark_score", "benchmark", "score", "best_score",
              "current_score", "after_score", "new_score", "fitness", "accuracy")
STATE_KEYS = ("state", "status", "phase", "stage", "mode")
STEP_KEYS = ("active_steps", "current_steps", "steps", "pending", "queue", "tasks")
ROLLBACK_COUNT_KEYS = ("rollbacks", "rollback_count", "n_rollbacks")
GENERATION_KEYS = ("generation", "version", "cycle", "iteration", "epoch")
HISTORY_LIST_KEYS = ("history", "entries", "events", "log", "records",
                     "evolutions", "actions")

MAX_EXPR_LEN = 2000
MAX_PLAN_LEN = 20000
HISTORY_VIEW = 40


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
class BackendUnavailable(RuntimeError):
    pass


def _import_optional(name):
    try:
        return importlib.import_module(name), None
    except Exception as exc:  # ImportError or anything raised at import time
        return None, "%s: %s" % (type(exc).__name__, exc)


def fmt_decimal(d):
    """Plain-notation string for a Decimal without touching context precision."""
    if not d.is_finite():
        return str(d)
    sign, digits, exp = d.as_tuple()
    digs = list(digits)
    if not any(digs):
        return "0"
    while len(digs) > 1 and digs[-1] == 0:
        digs.pop()
        exp += 1
    n = len(digs)
    adjusted = exp + n - 1
    s = "".join(str(x) for x in digs)
    pre = "-" if sign else ""
    if -30 <= adjusted <= 60:
        if exp >= 0:
            return pre + s + "0" * exp
        pos = n + exp
        if pos <= 0:
            return pre + "0." + "0" * (-pos) + s
        return pre + s[:pos] + "." + s[pos:]
    rest = s[1:]
    return "%s%s%sE%+d" % (pre, s[0], ("." + rest) if rest else "", adjusted)


def to_text(x):
    if isinstance(x, Decimal):
        return fmt_decimal(x)
    if isinstance(x, (dict, list, tuple)):
        try:
            return json.dumps(x, default=str, ensure_ascii=False)
        except Exception:
            return str(x)
    return str(x)


def short(x, n=300):
    s = x if isinstance(x, str) else to_text(x)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _ci_get(d, key):
    if key in d:
        return d[key]
    lk = key.lower()
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == lk:
            return v
    return None


def _pick(d, keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = _ci_get(d, k)
        if v is not None:
            return v
    return None


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str):
        try:
            f = float(v.strip().rstrip("%"))
            return None if f != f else f
        except ValueError:
            return None
    if isinstance(v, dict):
        return _num(_pick(v, ("score", "value", "mean", "overall")))
    return None


def _truthy(v):
    return bool(v) and str(v).strip().lower() not in ("false", "0", "no", "none", "")


def _fmt_ts(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)) and v > 1e9:
        try:
            return time.strftime("%m-%d %H:%M:%S", time.localtime(v))
        except Exception:
            return str(v)
    return str(v).replace("T", " ")[:19]


def call_flex(fn, *args, **kwargs):
    """Call fn; if it returns an awaitable, run it to completion."""
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        async def _wrap():
            return await res
        res = asyncio.run(_wrap())
    return res


def _find_callable(objs, names):
    for obj in objs:
        if obj is None:
            continue
        for n in names:
            try:
                f = getattr(obj, n, None)
            except Exception:
                f = None
            if callable(f) and not inspect.isclass(f):
                return n, f
    return None, None


def build_call(fn, text, steps, emit):
    """Map (plan text, steps, progress callback) onto fn's real signature."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return (text,), {}
    args, kwargs, used = [], {}, False
    skip = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    for p in params:
        if p.kind in skip:
            continue
        n = p.name.lower()
        if n in TEXT_PARAMS:
            val, used = text, True
        elif n in LIST_PARAMS:
            val, used = list(steps), True
        elif n in CALLBACK_PARAMS:
            val = emit
        else:
            continue
        if p.kind == inspect.Parameter.POSITIONAL_ONLY:
            args.append(val)
        else:
            kwargs[p.name] = val
    if not used:
        for p in params:
            if p.kind in skip or p.name in kwargs:
                continue
            if p.default is inspect.Parameter.empty:
                if p.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(text)
                else:
                    kwargs[p.name] = text
                break
    return tuple(args), kwargs


def interpret_result(res):
    """Return (level, alert, text) for whatever the evolver handed back."""
    text = short(res, 600) if res is not None else "done"
    level, alert = "ok", False
    if isinstance(res, dict):
        for k in ("success", "ok"):
            v = _ci_get(res, k)
            if v is not None and not _truthy(v):
                level = "warn"
        st = str(_pick(res, ("status", "result", "outcome")) or "").lower()
        if "rollback" in st or "rolled" in st or "revert" in st:
            level, alert = "warn", True
        elif st in ("failed", "error", "failure"):
            level, alert = "error", True
    return level, alert, text


def parse_plan(text):
    bullet = re.compile(r"^\s*(?:[-*\u2022\u2013]+|\d+[.)]|\[[ xX]?\]|step\s*\d+\s*[:.)-])\s*", re.I)
    lines = [bullet.sub("", ln).strip() for ln in (text or "")[:MAX_PLAN_LEN].splitlines()]
    steps = [ln for ln in lines if ln]
    if len(steps) == 1 and ";" in steps[0]:
        steps = [p.strip() for p in steps[0].split(";") if p.strip()]
    return [s[:500] for s in steps[:200]]


def parse_precision(v, default=50):
    try:
        p = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    return max(10, min(p, 2000))


# --------------------------------------------------------------------------
# built-in Decimal evaluator (used when agent_math_core can't handle a query,
# or isn't installed). Safe: AST-walked, no eval().
# --------------------------------------------------------------------------
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Mod: operator.mod,
        ast.FloorDiv: operator.floordiv, ast.Pow: operator.pow}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _round(x, n=Decimal(0)):
    return x.quantize(Decimal(1).scaleb(-int(n)), rounding=ROUND_HALF_UP)


def _pmt(rate, nper, pv):
    if rate == 0:
        return pv / nper
    return pv * rate / (1 - (1 + rate) ** (-nper))


def _fv(rate, nper, pmt, pv=Decimal(0)):
    g = (1 + rate) ** nper
    return pv + pmt * nper if rate == 0 else pv * g + pmt * (g - 1) / rate


def _pv(rate, nper, pmt, fv=Decimal(0)):
    if rate == 0:
        return fv + pmt * nper
    g = (1 + rate) ** nper
    return fv / g + pmt * (1 - 1 / g) / rate


def _npv(rate, *flows):
    return sum((cf / (1 + rate) ** i for i, cf in enumerate(flows)), Decimal(0))


def _log(x, b=None):
    return x.ln() if b is None else x.ln() / b.ln()


BUILTIN_FUNCS = {
    "sqrt": lambda x: x.sqrt(), "ln": lambda x: x.ln(), "log10": lambda x: x.log10(),
    "log": _log, "exp": lambda x: x.exp(), "abs": abs, "min": min, "max": max,
    "sum": lambda *a: sum(a, Decimal(0)),
    "avg": lambda *a: sum(a, Decimal(0)) / len(a),
    "round": _round,
    "floor": lambda x: x.to_integral_value(rounding=ROUND_FLOOR),
    "ceil": lambda x: x.to_integral_value(rounding=ROUND_CEILING),
    "pct": lambda x, p: x * p / 100,
    "pmt": _pmt, "fv": _fv, "pv": _pv, "npv": _npv,
    "roi": lambda final, cost: (final - cost) / cost * 100,
    "cagr": lambda begin, end, years: ((end / begin) ** (1 / years) - 1) * 100,
    "margin": lambda revenue, cost: (revenue - cost) / revenue * 100,
    "markup": lambda cost, price: (price - cost) / cost * 100,
    "compound": lambda principal, rate, periods: principal * (1 + rate) ** periods,
    "breakeven": lambda fixed, price, var: fixed / (price - var),
}

BUILTIN_HELP = ("Built-in functions: " + ", ".join(sorted(BUILTIN_FUNCS)) +
                ". Constants: pi, e. Rates are fractions per period (0.05 = 5%). "
                "Use ^ or ** for powers.")


def _pi():
    with localcontext() as ctx:
        ctx.prec += 3
        lasts, t, s, n, na, d, da = 0, Decimal(3), Decimal(3), 1, 0, 0, 24
        while s != lasts:
            lasts = s
            n, na = n + na, na + 8
            d, da = d + da, da + 32
            t = (t * n) / d
            s += t
    return +s


def _ev(node, src):
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("Only numbers are allowed here")
        seg = ast.get_source_segment(src, node) or str(v)
        try:
            return Decimal(seg)
        except decimal.InvalidOperation:
            return Decimal(v)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _ev(node.left, src), _ev(node.right, src)
        if isinstance(node.op, ast.Pow) and abs(right) > 100000:
            raise ValueError("Exponent too large (max 100000)")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
        return _UN[type(node.op)](_ev(node.operand, src))
    if isinstance(node, ast.Name):
        if node.id == "pi":
            return _pi()
        if node.id == "e":
            return Decimal(1).exp()
        raise ValueError("Unknown name: %s" % node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        fn = BUILTIN_FUNCS.get(node.func.id)
        if fn is None:
            raise ValueError("Unknown function: %s()" % node.func.id)
        args = [_ev(a, src) for a in node.args]
        try:
            return fn(*args)
        except TypeError as exc:
            raise ValueError("Wrong arguments for %s(): %s" % (node.func.id, exc))
    raise ValueError("Unsupported syntax: %s" % type(node).__name__)


def normalize_expr(expr):
    expr = expr.strip()
    for a, b in (("\u00d7", "*"), ("\u00f7", "/"), ("\u2212", "-"), ("^", "**"),
                 ("\u00a0", " ")):
        expr = expr.replace(a, b)
    return expr


def builtin_calculate(expr, precision):
    if len(expr) > MAX_EXPR_LEN:
        raise ValueError("Expression too long (max %d characters)" % MAX_EXPR_LEN)
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.Emax, ctx.Emin = 999999, -999999
        tree = ast.parse(expr, mode="eval")
        return fmt_decimal(+_ev(tree.body, expr))


# --------------------------------------------------------------------------
# math bridge
# --------------------------------------------------------------------------
class MathBridge:
    def __init__(self):
        self.mod, self.err = _import_optional(MATH_MODULE)

    def public_functions(self):
        if self.mod is None:
            return []
        out = []
        for n, v in vars(self.mod).items():
            if (not n.startswith("_") and callable(v) and not inspect.isclass(v)
                    and getattr(v, "__module__", None) == self.mod.__name__):
                out.append(n)
        return sorted(out)

    def describe(self):
        if self.mod is None:
            return "built-in Decimal engine (%s not importable: %s)" % (MATH_MODULE, self.err)
        return "%s (%d functions)" % (MATH_MODULE, len(self.public_functions()))

    def calculate(self, expr, precision):
        """Return {'result': str, 'engine': str}. Raises on error."""
        force_builtin = expr.lstrip().startswith("~")
        expr = normalize_expr(expr.lstrip().lstrip("~"))
        if len(expr) > MAX_EXPR_LEN:
            raise ValueError("Expression too long (max %d characters)" % MAX_EXPR_LEN)
        if self.mod is not None and not force_builtin:
            try:
                out = self._via_module(expr, precision)
            except Exception as mod_exc:
                # module couldn't handle it (e.g. a business helper it lacks):
                # try the built-in engine; if that fails too, report the module's error
                try:
                    return {"result": builtin_calculate(expr, precision),
                            "engine": "built-in Decimal (%s said: %s)" % (MATH_MODULE, short(str(mod_exc), 60))}
                except Exception:
                    raise mod_exc
            if out is not None:
                return out
        return {"result": builtin_calculate(expr, precision), "engine": "built-in Decimal"}

    def _via_module(self, expr, precision):
        mod = self.mod
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            tree = None
        # 1) name(args) where name is a public function of agent_math_core
        if (tree is not None and isinstance(tree.body, ast.Call)
                and isinstance(tree.body.func, ast.Name)
                and not tree.body.func.id.startswith("_")):
            fn = getattr(mod, tree.body.func.id, None)
            if callable(fn) and not inspect.isclass(fn):
                with localcontext() as ctx:
                    ctx.prec = precision
                    args = [self._arg(a, expr) for a in tree.body.args]
                    kwargs = {k.arg: self._arg(k.value, expr) for k in tree.body.keywords if k.arg}
                    kwargs = self._add_precision(fn, kwargs, precision)
                    try:
                        res = call_flex(fn, *args, **kwargs)
                    except TypeError as first:
                        conv = [self._to_float(a) for a in args]
                        convk = {k: self._to_float(v) for k, v in kwargs.items()}
                        try:
                            res = call_flex(fn, *conv, **convk)
                        except Exception:
                            raise first
                return {"result": to_text(res), "engine": "%s.%s" % (MATH_MODULE, tree.body.func.id)}
        # 2) generic evaluator inside the module
        name, fn = _find_callable([mod], MATH_EVAL_NAMES)
        if fn is not None:
            kwargs = self._add_precision(fn, {}, precision)
            with localcontext() as ctx:
                ctx.prec = precision
                res = call_flex(fn, expr, **kwargs)
            return {"result": to_text(res), "engine": "%s.%s" % (MATH_MODULE, name)}
        return None

    @staticmethod
    def _add_precision(fn, kwargs, precision):
        try:
            names = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return kwargs
        for cand in ("precision", "prec", "digits", "places"):
            if cand in names and cand not in kwargs:
                kwargs = dict(kwargs)
                kwargs[cand] = precision
                break
        return kwargs

    @staticmethod
    def _arg(node, src):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [MathBridge._arg(x, src) for x in node.elts]
        return _ev(node, src)

    @staticmethod
    def _to_float(v):
        if isinstance(v, Decimal):
            return int(v) if v == v.to_integral_value() and abs(v) < 10 ** 15 else float(v)
        if isinstance(v, list):
            return [MathBridge._to_float(x) for x in v]
        return v


# --------------------------------------------------------------------------
# evolver bridge
# --------------------------------------------------------------------------
class EvolverBridge:
    def __init__(self):
        self.mod, self.err = _import_optional(EVOLVER_MODULE)
        self.instance = None
        self.cls_name = None
        self.init_err = None
        self._lock = threading.Lock()
        self._tried = False

    def _ensure_instance(self):
        if self.mod is None or self._tried:
            return
        with self._lock:
            if self._tried:
                return
            self._tried = True
            names = list(EVOLVER_CLASS_NAMES)
            for n, v in vars(self.mod).items():
                if (inspect.isclass(v) and "evolv" in n.lower() and n not in names
                        and v.__module__ == self.mod.__name__):
                    names.append(n)
            for n in names:
                cls = getattr(self.mod, n, None)
                if inspect.isclass(cls):
                    try:
                        self.instance, self.cls_name = cls(), n
                        return
                    except Exception as exc:
                        self.init_err = "%s(): %s: %s" % (n, type(exc).__name__, exc)

    def _targets(self):
        return [self.instance, self.mod]

    def describe(self):
        """-> (ok, text)"""
        if self.mod is None:
            return False, "%s not importable (%s)" % (EVOLVER_MODULE, self.err)
        self._ensure_instance()
        if self.instance is not None:
            return True, "%s.%s" % (EVOLVER_MODULE, self.cls_name)
        if self.init_err:
            return True, "%s (module functions; %s)" % (EVOLVER_MODULE, self.init_err)
        return True, "%s (module functions)" % EVOLVER_MODULE

    def run(self, text, steps, emit):
        if self.mod is None:
            raise BackendUnavailable("%s is not importable: %s" % (EVOLVER_MODULE, self.err))
        self._ensure_instance()
        name, fn = _find_callable(self._targets(), EVOLVER_RUN_NAMES)
        if fn is None:
            raise BackendUnavailable(
                "%s has no run method (looked for: %s). Edit EVOLVER_RUN_NAMES."
                % (EVOLVER_MODULE, ", ".join(EVOLVER_RUN_NAMES)))
        args, kwargs = build_call(fn, text, steps, emit)
        return call_flex(fn, *args, **kwargs)

    def benchmark(self, emit):
        if self.mod is None:
            raise BackendUnavailable("%s is not importable: %s" % (EVOLVER_MODULE, self.err))
        self._ensure_instance()
        name, fn = _find_callable(self._targets(), EVOLVER_BENCH_NAMES)
        if fn is None:
            raise BackendUnavailable(
                "%s has no benchmark method (looked for: %s). Edit EVOLVER_BENCH_NAMES."
                % (EVOLVER_MODULE, ", ".join(EVOLVER_BENCH_NAMES)))
        args, kwargs = build_call(fn, "", [], emit)
        return call_flex(fn, *args, **kwargs)

    def read_status(self):
        if self.mod is None:
            return None
        self._ensure_instance()
        for obj in self._targets():
            if obj is None:
                continue
            for n in EVOLVER_STATUS_NAMES:
                try:
                    v = getattr(obj, n, None)
                except Exception:
                    continue
                if v is None or inspect.isclass(v):
                    continue
                if callable(v):
                    try:
                        sig = inspect.signature(v)
                        if any(p.default is p.empty and p.kind in
                               (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                               for p in sig.parameters.values()):
                            continue
                    except (TypeError, ValueError):
                        pass
                    return call_flex(v)
                if isinstance(v, (dict, str, int, float)):
                    return v
        return None


def normalize_status(raw):
    out = {"state": None, "score": None, "steps": [], "rollbacks": None, "generation": None}
    if raw is None:
        return out
    if isinstance(raw, dict):
        st = _pick(raw, STATE_KEYS)
        out["state"] = short(st, 60) if st is not None and not isinstance(st, (dict, list)) else None
        out["score"] = _num(_pick(raw, SCORE_KEYS))
        steps = _pick(raw, STEP_KEYS)
        if isinstance(steps, str):
            out["steps"] = [short(steps, 200)]
        elif isinstance(steps, (list, tuple)):
            for s in steps[:50]:
                if isinstance(s, dict):
                    s = _pick(s, ("name", "title", "step", "description", "task")) or to_text(s)
                out["steps"].append(short(str(s), 200))
        rb = _pick(raw, ROLLBACK_COUNT_KEYS)
        if isinstance(rb, (int, float)) and not isinstance(rb, bool):
            out["rollbacks"] = int(rb)
        elif isinstance(rb, (list, tuple)):
            out["rollbacks"] = len(rb)
        gen = _pick(raw, GENERATION_KEYS)
        out["generation"] = short(gen, 30) if gen is not None else None
    elif isinstance(raw, str):
        out["state"] = short(raw, 60)
    else:
        n = _num(raw)
        if n is not None:
            out["score"] = n
    return out


# --------------------------------------------------------------------------
# evolution_history.json reader
# --------------------------------------------------------------------------
_HCACHE = {}


def resolve_history_path(cli_value=None):
    if cli_value:
        return os.path.abspath(cli_value)
    env = os.environ.get("AUTOMIND_HISTORY")
    if env:
        return os.path.abspath(env)
    for base in (os.getcwd(), APP_DIR):
        p = os.path.join(base, "evolution_history.json")
        if os.path.exists(p):
            return p
    return os.path.join(os.getcwd(), "evolution_history.json")


def _history_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in HISTORY_LIST_KEYS:
            v = _ci_get(data, k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and v:
                return [dict(x, id=i) if isinstance(x, dict) else {"action": str(x), "id": i}
                        for i, x in v.items()]
        vals = list(data.values())
        if vals and all(isinstance(v, dict) for v in vals):
            return vals
        return [data]
    return []


def _norm_entry(it):
    if not isinstance(it, dict):
        return {"ts": "", "action": short(it, 200), "status": "", "score": None,
                "rollback": False, "detail": ""}
    action = _pick(it, ("action", "event", "type", "step", "name", "operation",
                        "change", "title"))
    status = _pick(it, ("status", "result", "outcome", "state", "verdict"))
    detail = _pick(it, ("detail", "details", "description", "message", "reason",
                        "summary", "note", "notes", "diff"))
    rb = _truthy(_pick(it, ("rollback", "rolled_back", "reverted", "is_rollback")))
    blob = (str(action or "") + " " + str(status or "")).lower().replace("_", " ").replace("-", " ")
    if "rollback" in blob or "roll back" in blob or "rolled back" in blob or "revert" in blob:
        rb = True
    return {"ts": _fmt_ts(_pick(it, ("timestamp", "ts", "time", "date", "created_at", "when"))),
            "action": short(action if action is not None else "(event)", 160),
            "status": short(status, 60) if status is not None else "",
            "score": _num(_pick(it, SCORE_KEYS)),
            "rollback": rb,
            "detail": short(detail, 240) if detail is not None else ""}


def load_history(path):
    """-> (entries, error). Cached on (mtime, size); tolerant of partial writes."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return [], "No history file yet: %s" % path
    except OSError as exc:
        return [], "Cannot read history: %s" % exc
    if st.st_size > 20 * 1024 * 1024:
        return [], "History file is larger than 20 MB; not loaded."
    key = (path, st.st_mtime_ns, st.st_size)
    if _HCACHE.get("key") == key:
        return _HCACHE["val"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        if not raw.strip():
            val = ([], None)
        else:
            try:
                data = json.loads(raw)
            except ValueError:
                data = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
            val = ([_norm_entry(x) for x in _history_items(data)], None)
    except (ValueError, UnicodeDecodeError) as exc:
        if "val" in _HCACHE:
            return _HCACHE["val"][0], "History file is mid-write or invalid JSON (%s)" % exc
        return [], "History file is not valid JSON: %s" % exc
    except OSError as exc:
        return [], "Cannot read history: %s" % exc
    _HCACHE["key"], _HCACHE["val"] = key, val
    return val


# --------------------------------------------------------------------------
# event bus + status poller + board core (the async pipeline)
# --------------------------------------------------------------------------
class EventBus:
    def __init__(self, cap=600):
        self._lock = threading.Lock()
        self._items = []
        self._seq = 0
        self._cap = cap

    def emit(self, level, src, msg, alert=False):
        with self._lock:
            self._seq += 1
            ev = {"id": self._seq, "t": time.strftime("%H:%M:%S"), "level": level,
                  "src": src, "msg": str(msg)[:4000], "alert": bool(alert)}
            self._items.append(ev)
            if len(self._items) > self._cap:
                del self._items[: len(self._items) - self._cap]
        return ev

    def since(self, last_id):
        with self._lock:
            return [e for e in self._items if e["id"] > last_id], self._seq


class StatusPoller(threading.Thread):
    """Reads evolver status + history file off the UI thread, publishes a snapshot."""

    def __init__(self, core, interval=2.0):
        super().__init__(daemon=True, name="status-poller")
        self.core, self.interval = core, interval
        self._stop_evt, self._wake = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._primed = False
        self._seen_rb = set()
        self._snap = {"updated": "-", "evolver": "starting...", "evolver_ok": False,
                      "math": "starting...", "state": None, "score": None, "score_src": None,
                      "best": None, "delta": None, "generation": None, "rollbacks": 0,
                      "last_rollback": None, "active_steps": [], "history": [],
                      "history_error": None, "history_path": core.history_path,
                      "status_error": None}

    def kick(self):
        self._wake.set()

    def stop(self):
        self._stop_evt.set()
        self._wake.set()

    def get(self):
        with self._lock:
            return dict(self._snap)

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self.refresh()
            except Exception as exc:  # never let the poller die
                self.core.bus.emit("debug", "ui", "poller: %s: %s" % (type(exc).__name__, exc))
            self._wake.wait(self.interval)
            self._wake.clear()

    def refresh(self):
        core = self.core
        hist, herr = load_history(core.history_path)
        serr, status = None, normalize_status(None)
        try:
            status = normalize_status(core.evolver.read_status())
        except Exception as exc:
            serr = "%s: %s" % (type(exc).__name__, exc)
        ok, desc = core.evolver.describe()
        scores = [e["score"] for e in hist if e["score"] is not None]
        score, src = status["score"], "status"
        if score is None and scores:
            score, src = scores[-1], "history"
        delta = scores[-1] - scores[-2] if len(scores) >= 2 else None
        rb_entries = [e for e in hist if e["rollback"]]
        rollbacks = max(len(rb_entries), status["rollbacks"] or 0)
        keys = {(e["ts"], e["action"], e["status"], e["detail"][:60]) for e in rb_entries}
        if self._primed:
            for e in rb_entries:
                k = (e["ts"], e["action"], e["status"], e["detail"][:60])
                if k not in self._seen_rb:
                    core.bus.emit("warn", "evo", "ROLLBACK: %s %s" % (e["action"], e["detail"]), alert=True)
        self._seen_rb, self._primed = keys, True
        snap = {"updated": time.strftime("%H:%M:%S"), "evolver": desc, "evolver_ok": ok,
                "math": core.math.describe(), "state": status["state"], "score": score,
                "score_src": src, "best": max(scores) if scores else None, "delta": delta,
                "generation": status["generation"], "rollbacks": rollbacks,
                "last_rollback": rb_entries[-1] if rb_entries else None,
                "active_steps": status["steps"], "history": hist[-HISTORY_VIEW:],
                "history_error": herr, "history_path": core.history_path,
                "status_error": serr}
        with self._lock:
            self._snap = snap


class BoardCore:
    """All UI front ends talk only to this object. Nothing here blocks the caller."""

    def __init__(self, history_path):
        self.history_path = history_path
        self.bus = EventBus()
        self.math = MathBridge()
        self.evolver = EvolverBridge()
        self.poller = StatusPoller(self)
        self._math_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="math")
        self._evo_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evo")
        self._lock = threading.Lock()
        self._jobs, self._seq = {}, 0
        self._plan, self._evo_busy = [], False
        self._bench, self._last_result = None, None
        self._cancel = threading.Event()
        self.calc_history = deque(maxlen=50)

    # ---- lifecycle
    def start(self):
        self.poller.start()
        if self.math.mod is None:
            self.bus.emit("warn", "math", "%s not importable (%s). Using the built-in Decimal engine."
                          % (MATH_MODULE, self.math.err))
        else:
            self.bus.emit("info", "math", "Loaded %s." % self.math.describe())
        ok, desc = self.evolver.describe()
        if ok:
            self.bus.emit("info", "evo", "Loaded %s." % desc)
        else:
            self.bus.emit("warn", "evo", desc + ". Plans cannot run until it is available.", alert=True)
        self.bus.emit("info", "ui", "History file: %s" % self.history_path)

    def shutdown(self):
        self.poller.stop()
        for pool in (self._math_pool, self._evo_pool):
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=False)

    def wait_idle(self, timeout=60.0):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if not self._jobs:
                    return True
            time.sleep(0.1)
        return False

    # ---- bookkeeping
    def _new_job(self, kind, label):
        with self._lock:
            self._seq += 1
            jid = self._seq
            self._jobs[jid] = {"id": jid, "kind": kind, "label": short(label, 50),
                               "started": time.time()}
        return jid

    def _finish(self, jid):
        with self._lock:
            self._jobs.pop(jid, None)
        self.poller.kick()

    def _set_step(self, idx, state):
        with self._lock:
            if 0 <= idx < len(self._plan):
                self._plan[idx]["state"] = state

    def _set_all(self, state, only=None):
        with self._lock:
            for s in self._plan:
                if only is None or s["state"] in only:
                    s["state"] = state

    def snapshot(self):
        snap = self.poller.get()
        with self._lock:
            snap["plan"] = [dict(s) for s in self._plan]
            snap["busy"] = self._evo_busy
            snap["jobs"] = [dict(j, elapsed=round(time.time() - j["started"], 1))
                            for j in self._jobs.values()]
            snap["bench"] = dict(self._bench) if self._bench else None
            snap["last_result"] = dict(self._last_result) if self._last_result else None
            snap["calc_history"] = list(self.calc_history)[:15]
        if snap["score_src"] != "status" and snap["bench"] and snap["bench"].get("score") is not None:
            snap["score"], snap["score_src"] = snap["bench"]["score"], "benchmark"
        return snap

    def _emit_cb(self):
        def emit(*parts, **_kw):
            self.bus.emit("info", "evo", " ".join(short(p, 400) for p in parts))
        return emit

    # ---- calculations
    def submit_calc(self, expr, precision=50):
        expr = (expr or "").strip()
        if not expr:
            self.bus.emit("warn", "math", "Enter an expression first.", alert=True)
            return None
        jid = self._new_job("calc", expr)
        self._math_pool.submit(self._calc_job, jid, expr, parse_precision(precision))
        return jid

    def _calc_job(self, jid, expr, precision):
        t0 = time.perf_counter()
        try:
            out = self.math.calculate(expr, precision)
            ms = int((time.perf_counter() - t0) * 1000)
            rec = {"id": jid, "expr": short(expr, 200), "result": out["result"],
                   "engine": out["engine"], "ms": ms, "ok": True}
            self.bus.emit("ok", "math", "%s = %s   [%s, %d ms]" % (short(expr, 120), short(out["result"], 400),
                                                                   out["engine"], ms))
        except Exception as exc:
            rec = {"id": jid, "expr": short(expr, 200), "result": "%s: %s" % (type(exc).__name__, exc),
                   "engine": "-", "ms": 0, "ok": False}
            self.bus.emit("error", "math", "%s -> %s: %s" % (short(expr, 120), type(exc).__name__, exc),
                          alert=True)
        finally:
            with self._lock:
                self._last_result = rec
                self.calc_history.appendleft(rec)
            self._finish(jid)

    # ---- plans / evolution
    def _claim_evo(self):
        with self._lock:
            if self._evo_busy:
                return False
            self._evo_busy = True
            self._cancel.clear()
        return True

    def submit_plan(self, plan_text, mode="plan"):
        steps = parse_plan(plan_text)
        if not steps:
            self.bus.emit("warn", "evo", "Plan is empty. Type or paste at least one step.", alert=True)
            return None
        if not self._claim_evo():
            self.bus.emit("warn", "evo", "An evolution job is already running. Wait or cancel it.", alert=True)
            return None
        with self._lock:
            self._plan = [{"text": s, "state": "pending"} for s in steps]
        mode = "steps" if mode == "steps" else "plan"
        jid = self._new_job("plan", "%d step(s), %s mode" % (len(steps), mode))
        self.bus.emit("info", "evo", "Submitted plan: %d step(s), %s mode." % (len(steps), mode))
        self._evo_pool.submit(self._plan_job, jid, "\n".join(steps), steps, mode)
        return jid

    def _plan_job(self, jid, text, steps, mode):
        current = -1
        emit = self._emit_cb()
        try:
            if mode == "steps":
                for i, step in enumerate(steps):
                    if self._cancel.is_set():
                        self._set_all("cancelled", only=("pending",))
                        self.bus.emit("warn", "evo", "Cancelled before step %d." % (i + 1))
                        return
                    current = i
                    self._set_step(i, "running")
                    res = self.evolver.run(step, [step], emit)
                    level, alert, msg = interpret_result(res)
                    self._set_step(i, "failed" if level == "error" else "done")
                    self.bus.emit(level, "evo", "Step %d/%d: %s" % (i + 1, len(steps), msg), alert=alert)
                self.bus.emit("ok", "evo", "All %d steps finished." % len(steps))
            else:
                self._set_all("running")
                res = self.evolver.run(text, steps, emit)
                level, alert, msg = interpret_result(res)
                self._set_all("failed" if level == "error" else "done")
                self.bus.emit(level, "evo", "Plan finished: %s" % msg, alert=alert)
        except BackendUnavailable as exc:
            self._set_all("failed", only=("pending", "running"))
            self.bus.emit("error", "evo", str(exc), alert=True)
        except Exception as exc:
            if current >= 0:
                self._set_step(current, "failed")
            self._set_all("failed" if mode == "plan" else "cancelled", only=("running",) if mode == "plan" else ("pending",))
            tb = traceback.format_exc().strip().splitlines()[-4:]
            self.bus.emit("error", "evo", "%s: %s" % (type(exc).__name__, exc), alert=True)
            self.bus.emit("debug", "evo", " | ".join(t.strip() for t in tb))
        finally:
            with self._lock:
                self._evo_busy = False
            self._finish(jid)

    def submit_benchmark(self):
        if not self._claim_evo():
            self.bus.emit("warn", "evo", "An evolution job is already running.", alert=True)
            return None
        jid = self._new_job("benchmark", "benchmark")
        self.bus.emit("info", "evo", "Running benchmark...")
        self._evo_pool.submit(self._bench_job, jid)
        return jid

    def _bench_job(self, jid):
        try:
            res = self.evolver.benchmark(self._emit_cb())
            score = _num(res)
            if score is None and isinstance(res, dict):
                score = _num(_pick(res, SCORE_KEYS))
            with self._lock:
                self._bench = {"score": score, "at": time.strftime("%H:%M:%S"), "raw": short(res, 300)}
            if score is None:
                self.bus.emit("warn", "evo", "Benchmark finished but no numeric score found: %s" % short(res, 300))
            else:
                self.bus.emit("ok", "evo", "Benchmark score: %.6g" % score)
        except BackendUnavailable as exc:
            self.bus.emit("error", "evo", str(exc), alert=True)
        except Exception as exc:
            self.bus.emit("error", "evo", "Benchmark failed: %s: %s" % (type(exc).__name__, exc), alert=True)
        finally:
            with self._lock:
                self._evo_busy = False
            self._finish(jid)

    def cancel(self):
        with self._lock:
            busy = self._evo_busy
        if not busy:
            self.bus.emit("info", "evo", "Nothing is running.")
            return
        self._cancel.set()
        self.bus.emit("warn", "evo", "Cancel requested. In step-by-step mode the current step finishes first; "
                                     "a whole-plan call cannot be interrupted safely.")


# --------------------------------------------------------------------------
# shared presentation helpers
# --------------------------------------------------------------------------
STEP_ICON = {"pending": "[ ]", "running": "[>]", "done": "[x]", "failed": "[!]", "cancelled": "[-]"}


def fmt_score(s):
    return "-" if s is None else "%.6g" % s


def history_line(e):
    parts = [e["ts"], ("ROLLBACK " if e["rollback"] else "") + e["action"]]
    if e["status"]:
        parts.append("(" + e["status"] + ")")
    if e["score"] is not None:
        parts.append("score " + fmt_score(e["score"]))
    line = "  ".join(p for p in parts if p)
    return line + ("\n    " + e["detail"] if e["detail"] else "")


# --------------------------------------------------------------------------
# WEB front end (stdlib only)
# --------------------------------------------------------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>AutoMind Board</title>
<style>
:root{--bg:#0f1218;--card:#181d27;--fg:#e8ecf3;--mut:#8b95a8;--ok:#3ecf8e;--warn:#f5a524;--err:#f31260;--acc:#4f8cff;--line:#262d3b}
@media (prefers-color-scheme:light){:root{--bg:#f4f6fa;--card:#fff;--fg:#151a23;--mut:#5d687b;--line:#dde2ea}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,Roboto,sans-serif;
 padding:0 0 calc(64px + env(safe-area-inset-bottom));-webkit-text-size-adjust:100%}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
 padding:calc(8px + env(safe-area-inset-top)) 12px 8px;display:flex;gap:8px;align-items:center}
header b{flex:1}#conn{font-size:12px}.ok{color:var(--ok)}.bad{color:var(--err)}
.banner{position:sticky;top:46px;z-index:6;margin:8px 12px;padding:10px 12px;border-radius:10px;color:#fff;display:flex;gap:10px;align-items:flex-start}
.banner[hidden]{display:none}.banner.error{background:var(--err)}.banner.warn{background:#b97a00}
.banner span{flex:1;word-break:break-word}.banner button{background:#0003;color:#fff;border:0;border-radius:8px;padding:2px 10px;font-size:18px}
main{padding:0 12px}
section.tab{display:none}section.tab.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:14px 0 4px}
textarea,input[type=text],input[type=number]{width:100%;font:16px/1.4 ui-monospace,Menlo,Consolas,monospace;color:var(--fg);
 background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px}
textarea{min-height:140px;resize:vertical}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;align-items:center}
button.b{flex:1;min-width:96px;background:var(--acc);color:#fff;border:0;border-radius:10px;padding:12px 10px;font-size:16px;font-weight:600}
button.b.alt{background:var(--line);color:var(--fg)}button.b:disabled{opacity:.45}
button.chip{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:999px;padding:6px 12px;font:14px ui-monospace,monospace}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.k{font-size:12px;color:var(--mut)}.v{font-size:20px;font-weight:700;word-break:break-word}.v.s{font-size:14px;font-weight:500}
.step{font:14px ui-monospace,monospace;padding:3px 0;word-break:break-word}
.step.running{color:var(--acc)}.step.done{color:var(--ok)}.step.failed{color:var(--err)}.step.cancelled{color:var(--mut)}
#result{font:22px ui-monospace,monospace;word-break:break-all;min-height:32px}
#console{height:calc(100vh - 230px);min-height:220px;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px;font:13px/1.35 ui-monospace,Menlo,monospace}
#console div{padding:1px 0;word-break:break-word}
.l.ok{color:var(--ok)}.l.warn{color:var(--warn)}.l.error{color:var(--err)}.l.debug{color:var(--mut)}.l.info{color:var(--fg)}
.entry{border-left:3px solid var(--line);padding:4px 10px;margin:8px 0;font-size:14px;word-break:break-word}
.entry.rb{border-color:var(--err)}.entry .d{color:var(--mut);font-size:13px}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;background:var(--card);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
nav button{flex:1;background:none;border:0;color:var(--mut);padding:14px 4px;font-size:14px}
nav button.on{color:var(--acc);font-weight:700}
@media (max-height:430px){nav{display:none}body{padding-bottom:12px}}
</style></head><body>
<header><b>AutoMind Board</b><span id="conn" class="bad">&#9679;</span></header>
<div id="banner" class="banner error" hidden role="alert"><span id="bmsg"></span><button id="bclose" aria-label="Dismiss">&times;</button></div>
<main>
<section id="t-board" class="tab on">
 <h2>Execution plan</h2>
 <textarea id="plan" placeholder="One step per line. 1. / - / * bullets are fine." autocapitalize="sentences" spellcheck="false"></textarea>
 <div class="row"><label><input type="checkbox" id="stepmode"> run step-by-step</label></div>
 <div class="row"><button class="b" id="run">Run plan</button><button class="b alt" id="cancel">Cancel</button><button class="b alt" id="bench">Benchmark</button></div>
 <h2>Live status</h2>
 <div class="card"><div class="grid">
  <div><div class="k">State</div><div class="v" id="s-state">-</div></div>
  <div><div class="k">Benchmark score</div><div class="v" id="s-score">-</div></div>
  <div><div class="k">Rollbacks</div><div class="v" id="s-rb">0</div></div>
  <div><div class="k">Jobs running</div><div class="v" id="s-jobs">0</div></div>
 </div>
 <div class="k" style="margin-top:8px">Evolver</div><div class="v s" id="s-evo">-</div>
 <div class="k" style="margin-top:6px">Math</div><div class="v s" id="s-math">-</div></div>
 <h2>Active steps</h2><div class="card" id="steps"><span class="k">No plan submitted yet.</span></div>
</section>
<section id="t-calc" class="tab">
 <h2>Calculator</h2>
 <input type="text" id="expr" placeholder="e.g. pmt(0.045/12, 360, 250000)" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" enterkeyhint="go">
 <div class="row"><input type="number" id="prec" value="50" min="10" max="2000" inputmode="numeric" style="width:110px"><span class="k">digits of precision</span></div>
 <div class="row"><button class="b" id="calc">Calculate</button><button class="b alt" id="copy">Copy result</button></div>
 <div class="row" id="chips"></div>
 <div class="card"><div class="k">Result</div><div id="result">-</div><div class="k" id="rmeta"></div></div>
 <div class="k" id="mathhelp"></div>
 <h2>Recent</h2><div id="chist"></div>
</section>
<section id="t-console" class="tab">
 <h2>Console</h2><div id="console" role="log" aria-live="polite"></div>
 <div class="row"><button class="b alt" id="cclear">Clear</button><button class="b alt" id="ccopy">Copy all</button></div>
</section>
<section id="t-log" class="tab">
 <h2>Evolution plan log</h2><div class="k" id="hpath"></div><div class="k bad" id="herr"></div><div id="hist"></div>
</section>
</main>
<nav><button data-tab="board" class="on">Board</button><button data-tab="calc">Calc</button><button data-tab="console">Console</button><button data-tab="log">Plan Log</button></nav>
<script>
(function(){
var $=function(i){return document.getElementById(i)};
var last=0,bt=null,resultId=null,consoleText=[];
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
function api(p,b){return fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined}).then(function(r){return r.json()})}
function banner(m,l){var b=$('banner');b.className='banner '+(l==='error'?'error':'warn');$('bmsg').textContent=m;b.hidden=false;clearTimeout(bt);bt=setTimeout(function(){b.hidden=true},10000)}
$('bclose').onclick=function(){$('banner').hidden=true};
function act(p,b){return api(p,b).then(function(r){if(r&&r.ok===false&&r.error)banner(r.error,'warn');poll();return r}).catch(function(e){banner('Connection problem: '+e,'error')})}
function tab(n){['board','calc','console','log'].forEach(function(k){$('t-'+k).className='tab'+(k===n?' on':'')});
 document.querySelectorAll('nav button').forEach(function(b){b.className=b.dataset.tab===n?'on':''});try{history.replaceState(null,'','#'+n)}catch(e){}window.scrollTo(0,0)}
document.querySelectorAll('nav button').forEach(function(b){b.onclick=function(){tab(b.dataset.tab)}});
var h=(location.hash||'').slice(1);if(['board','calc','console','log'].indexOf(h)>=0)tab(h);
$('run').onclick=function(){$('plan').blur();act('/api/plan',{plan:$('plan').value,mode:$('stepmode').checked?'steps':'plan'})};
$('cancel').onclick=function(){act('/api/cancel',{})};
$('bench').onclick=function(){act('/api/benchmark',{})};
function doCalc(){$('expr').blur();$('result').textContent='working...';act('/api/calc',{expr:$('expr').value,precision:$('prec').value})}
$('calc').onclick=doCalc;
$('expr').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();doCalc()}});
function copyText(t){try{navigator.clipboard.writeText(t).then(function(){banner('Copied.','warn')},function(){fallbackCopy(t)})}catch(e){fallbackCopy(t)}}
function fallbackCopy(t){var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();try{document.execCommand('copy');banner('Copied.','warn')}catch(e){banner('Copy not available here.','warn')}document.body.removeChild(a)}
$('copy').onclick=function(){copyText($('result').textContent)};
$('cclear').onclick=function(){consoleText=[];$('console').textContent=''};
$('ccopy').onclick=function(){copyText(consoleText.join('\n'))};
['pmt(','fv(','pv(','npv(','roi(','cagr(','margin(','markup(','pct(','sqrt(','ln(','round(','^','pi'].forEach(function(c){
 var b=el('button','chip',c);b.type='button';b.onclick=function(){var i=$('expr'),s=i.selectionStart||i.value.length,e=i.selectionEnd||s;i.value=i.value.slice(0,s)+c+i.value.slice(e);i.focus();i.setSelectionRange(s+c.length,s+c.length)};$('chips').appendChild(b)});
function addEvents(evs){var c=$('console'),stick=c.scrollTop+c.clientHeight>=c.scrollHeight-40;
 evs.forEach(function(e){var line=e.t+' '+e.src+' '+e.msg;consoleText.push(line);var d=el('div','l '+e.level,line);c.appendChild(d);
  if(e.alert)banner(e.msg,e.level==='error'?'error':'warn')});
 while(c.childNodes.length>500){c.removeChild(c.firstChild);consoleText.shift()}
 if(stick)c.scrollTop=c.scrollHeight}
var ICON={pending:'[ ]',running:'[>]',done:'[x]',failed:'[!]',cancelled:'[-]'};
function render(s){
 $('s-state').textContent=s.busy?'running':(s.state||'idle');
 var sc=s.score==null?'-':Number(s.score).toPrecision(6).replace(/\.?0+$/,'');
 if(s.delta!=null&&s.score_src==='history')sc+=' ('+(s.delta>=0?'+':'')+Number(s.delta).toPrecision(3)+')';
 $('s-score').textContent=sc;$('s-rb').textContent=s.rollbacks;$('s-rb').className='v'+(s.rollbacks>0?' bad':'');
 $('s-jobs').textContent=s.jobs.length;$('s-evo').textContent=s.evolver;$('s-math').textContent=s.math;
 $('run').disabled=!!s.busy;$('bench').disabled=!!s.busy;
 var st=$('steps');st.textContent='';
 if(!s.plan.length&&!s.active_steps.length)st.appendChild(el('span','k','No plan submitted yet.'));
 s.plan.forEach(function(p,i){st.appendChild(el('div','step '+p.state,ICON[p.state]+' '+(i+1)+'. '+p.text))});
 if(s.active_steps.length){st.appendChild(el('div','k','Reported by evolver:'));s.active_steps.forEach(function(x){st.appendChild(el('div','step',' - '+x))})}
 if(s.last_result&&s.last_result.id!==resultId){resultId=s.last_result.id;var r=s.last_result;
  $('result').textContent=r.result;$('result').style.color=r.ok?'':'var(--err)';$('rmeta').textContent=r.ok?(r.engine+' \u00b7 '+r.ms+' ms'):'error'}
 var ch=$('chist');ch.textContent='';s.calc_history.forEach(function(r){var d=el('div','entry'+(r.ok?'':' rb'));d.appendChild(el('div','',r.expr+' = '+r.result));d.appendChild(el('div','d',r.engine));ch.appendChild(d)});
 var hs=$('hist');hs.textContent='';$('hpath').textContent=s.history_path;$('herr').textContent=s.history_error||'';
 s.history.slice().reverse().forEach(function(e){var d=el('div','entry'+(e.rollback?' rb':''));
  var t=[e.ts,(e.rollback?'ROLLBACK ':'')+e.action,e.status?'('+e.status+')':'',e.score!=null?'score '+e.score:''].filter(Boolean).join('  ');
  d.appendChild(el('div','',t));if(e.detail)d.appendChild(el('div','d',e.detail));hs.appendChild(d)});
 $('mathhelp').textContent=s.math_help||''}
function poll(){api('/api/state?since='+last).then(function(d){if(d.last<last){last=0;return}
 last=d.last;addEvents(d.events);render(d.snap);$('conn').className='ok'}).catch(function(){$('conn').className='bad'})}
setInterval(poll,1500);poll();
})();
</script></body></html>
"""


def make_handler(core):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoMindBoard/1.0"

        def log_message(self, *_a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                self._send(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                try:
                    since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
                except ValueError:
                    since = 0
                events, last = core.bus.since(since)
                snap = core.snapshot()
                snap["math_help"] = BUILTIN_HELP
                self._send(200, {"snap": snap, "events": events, "last": last})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 65536:
                return self._send(413, {"ok": False, "error": "Request too large (64 KB max)."})
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("body must be an object")
            except ValueError as exc:
                return self._send(400, {"ok": False, "error": "Bad request: %s" % exc})
            path = urlparse(self.path).path
            if path == "/api/calc":
                jid = core.submit_calc(str(data.get("expr", "")), data.get("precision", 50))
            elif path == "/api/plan":
                jid = core.submit_plan(str(data.get("plan", "")), str(data.get("mode", "plan")))
            elif path == "/api/benchmark":
                jid = core.submit_benchmark()
            elif path == "/api/cancel":
                core.cancel()
                jid = 0
            else:
                return self._send(404, {"ok": False, "error": "not found"})
            self._send(200, {"ok": jid is not None, "job": jid})

    return Handler


def run_web(core, host, port, open_browser):
    srv = None
    for p in range(port, port + 11):
        try:
            srv = ThreadingHTTPServer((host, p), make_handler(core))
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print("Could not bind any port in %d-%d." % (port, port + 10))
        return 1
    srv.daemon_threads = True
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    print("AutoMind Board running at %s  (Ctrl+C to stop)" % url)
    # Browser auto-launch intentionally disabled; open the localhost URL manually.
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        core.shutdown()
    return 0


# --------------------------------------------------------------------------
# TERMINAL front end (stdlib only)
# --------------------------------------------------------------------------
TUI_HELP = """Commands
  <text>            add a line to the current plan buffer
  /plan             paste multi-line plan (finish with a line containing just .)
  /show             show plan buffer          /clearplan   empty it
  /run [steps]      run plan (steps = one evolver call per step)
  /cancel           request cancellation
  /bench            run benchmark
  /calc <expr>      calculate (also: = <expr>)     /prec <n>  set precision
  /status           live evolution status
  /log [n]          last n evolution-log entries
  /help  /quit
""" + BUILTIN_HELP


def run_tui(core):
    lock, stop = threading.Lock(), threading.Event()
    state = {"last": 0}
    colors = {"ok": "\033[32m", "warn": "\033[33m", "error": "\033[31m", "debug": "\033[90m", "info": ""}
    use_color = sys.stdout.isatty()

    def printer():
        while not stop.is_set():
            evs, last = core.bus.since(state["last"])
            state["last"] = last
            for e in evs:
                c = colors.get(e["level"], "") if use_color else ""
                with lock:
                    print("%s%s %-4s %s%s" % (c, e["t"], e["src"], e["msg"], "\033[0m" if c else ""))
            stop.wait(0.25)

    threading.Thread(target=printer, daemon=True).start()
    plan_lines, prec = [], 50
    print("AutoMind Board (terminal). /help for commands.")
    try:
        while True:
            try:
                line = input("board> ").strip()
            except EOFError:
                core.wait_idle(60)
                time.sleep(0.4)
                break
            if not line:
                continue
            if not line.startswith("/") and not line.startswith("="):
                plan_lines.append(line)
                continue
            cmd, _, arg = line.lstrip("=").partition(" ") if line.startswith("/") else ("calc", "", line[1:])
            cmd = cmd.lstrip("/").lower()
            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "help":
                print(TUI_HELP)
            elif cmd == "plan":
                print("Paste plan, end with a line containing just '.'")
                while True:
                    try:
                        ln = input("... ")
                    except EOFError:
                        break
                    if ln.strip() == ".":
                        break
                    plan_lines.append(ln)
                print("%d step(s) in buffer." % len(parse_plan("\n".join(plan_lines))))
            elif cmd == "show":
                for i, s in enumerate(parse_plan("\n".join(plan_lines)), 1):
                    print("%2d. %s" % (i, s))
            elif cmd == "clearplan":
                plan_lines = []
            elif cmd == "run":
                core.submit_plan("\n".join(plan_lines), "steps" if arg.strip() == "steps" else "plan")
            elif cmd == "cancel":
                core.cancel()
            elif cmd == "bench":
                core.submit_benchmark()
            elif cmd == "calc":
                core.submit_calc(arg, prec)
            elif cmd == "prec":
                prec = parse_precision(arg, prec)
                print("precision = %d" % prec)
            elif cmd == "status":
                s = core.snapshot()
                with lock:
                    print("state: %s | score: %s (%s) | best: %s | rollbacks: %s | jobs: %d"
                          % ("running" if s["busy"] else (s["state"] or "idle"), fmt_score(s["score"]),
                             s["score_src"] or "-", fmt_score(s["best"]), s["rollbacks"], len(s["jobs"])))
                    print("evolver: %s\nmath: %s" % (s["evolver"], s["math"]))
                    for i, p in enumerate(s["plan"], 1):
                        print("  %s %d. %s" % (STEP_ICON[p["state"]], i, p["text"]))
                    for x in s["active_steps"]:
                        print("  evolver step: %s" % x)
                    if s["history_error"]:
                        print("history: %s" % s["history_error"])
            elif cmd == "log":
                try:
                    n = int(arg) if arg.strip() else 15
                except ValueError:
                    n = 15
                s = core.snapshot()
                with lock:
                    for e in s["history"][-n:]:
                        print(history_line(e))
                    if s["history_error"]:
                        print(s["history_error"])
            else:
                print("Unknown command. /help")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        core.shutdown()
    return 0


# --------------------------------------------------------------------------
# KIVY front end (stock Kivy only)
# --------------------------------------------------------------------------
def run_kivy(core):
    from kivy.config import Config
    Config.set("kivy", "exit_on_escape", "0")
    from kivy.core.window import Window
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.graphics import Color, Rectangle
    from kivy.metrics import dp, sp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.label import Label
    from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.textinput import TextInput
    from kivy.uix.togglebutton import ToggleButton
    from kivy.utils import escape_markup as esc
    try:
        from kivy.core.clipboard import Clipboard
    except Exception:
        Clipboard = None
    try:
        Window.softinput_mode = "resize"   # layout shrinks above the soft keyboard
    except Exception:
        pass

    BG, CARD = (0.06, 0.07, 0.09, 1), (0.10, 0.12, 0.16, 1)
    FG, MUT = (0.91, 0.93, 0.95, 1), (0.55, 0.58, 0.66, 1)
    OK, WARN, ERR, ACC = (0.24, 0.81, 0.56, 1), (0.72, 0.48, 0.0, 1), (0.85, 0.07, 0.30, 1), (0.31, 0.55, 1, 1)
    HEX = {"info": "c8cfdb", "ok": "3ecf8e", "warn": "f5a524", "error": "f31260", "debug": "6b7385"}
    STEP_HEX = {"pending": "8b95a8", "running": "4f8cff", "done": "3ecf8e", "failed": "f31260", "cancelled": "8b95a8"}

    class Card(BoxLayout):
        def __init__(self, bg=CARD, **kw):
            super().__init__(**kw)
            with self.canvas.before:
                self._c = Color(*bg)
                self._r = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=self._upd, size=self._upd)

        def _upd(self, *_):
            self._r.pos, self._r.size = self.pos, self.size

        def set_bg(self, rgba):
            self._c.rgba = rgba

    def wrap_label(text="", size=15, color=FG, **kw):
        lbl = Label(text=text, size_hint_y=None, halign="left", valign="top", markup=True,
                    font_size=sp(size), color=color, **kw)
        lbl.bind(width=lambda i, w: setattr(i, "text_size", (w, None)))
        lbl.bind(texture_size=lambda i, s: setattr(i, "height", s[1] + dp(4)))
        return lbl

    def button(text, cb, color=ACC, **kw):
        b = Button(text=text, background_normal="", background_color=color, color=(1, 1, 1, 1),
                   font_size=sp(15), size_hint_y=None, height=dp(48), **kw)
        b.bind(on_release=lambda *_: cb())
        return b

    def text_input(**kw):
        return TextInput(background_color=(0.13, 0.15, 0.20, 1), foreground_color=FG,
                         cursor_color=ACC, hint_text_color=MUT, font_size=sp(15),
                         padding=[dp(10), dp(10), dp(10), dp(10)], **kw)

    def card_with(lbl):
        card = Card(orientation="vertical", size_hint_y=None, padding=dp(10))
        lbl.bind(height=lambda i, h: setattr(card, "height", h + dp(20)))
        card.add_widget(lbl)
        return card

    def scroll_column():
        sv = ScrollView(do_scroll_x=False, bar_width=dp(4))
        col = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8), padding=[dp(8)] * 4)
        col.bind(minimum_height=col.setter("height"))
        sv.add_widget(col)
        return sv, col

    def copy(text):
        if Clipboard is not None:
            try:
                Clipboard.copy(text)
                return True
            except Exception:
                pass
        return False

    class BoardApp(App):
        title = "AutoMind Board"

        def build(self):
            Window.clearcolor = BG
            self.last_event, self.console_lines = 0, []
            self.seen_result, self.hide_ev, self._last_hist = None, None, None
            root = BoxLayout(orientation="vertical", spacing=dp(2))

            # error / rollback banner
            self.banner = Card(bg=ERR, size_hint_y=None, height=0, opacity=0, padding=[dp(10), dp(4)], spacing=dp(6))
            self.banner_lbl = Label(halign="left", valign="middle", font_size=sp(14), color=(1, 1, 1, 1))
            self.banner_lbl.bind(size=lambda i, s: setattr(i, "text_size", s))
            ok_btn = Button(text="OK", size_hint_x=None, width=dp(56), background_normal="",
                            background_color=(0, 0, 0, 0.25))
            ok_btn.bind(on_release=lambda *_: self.hide_banner())
            self.banner.add_widget(self.banner_lbl)
            self.banner.add_widget(ok_btn)
            root.add_widget(self.banner)

            self.header = Label(text="AutoMind", size_hint_y=None, height=dp(30), font_size=sp(13),
                                color=MUT, markup=True, halign="left")
            self.header.bind(size=lambda i, s: setattr(i, "text_size", (s[0] - dp(12), s[1])))
            root.add_widget(self.header)

            self.sm = ScreenManager(transition=NoTransition())
            for name, builder in (("board", self.build_board), ("calc", self.build_calc),
                                  ("console", self.build_console), ("log", self.build_log)):
                scr = Screen(name=name)
                scr.add_widget(builder())
                self.sm.add_widget(scr)
            root.add_widget(self.sm)

            self.nav = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(2))
            for key, label in (("board", "Board"), ("calc", "Calc"), ("console", "Console"), ("log", "Plan Log")):
                tb = ToggleButton(text=label, group="nav", allow_no_selection=False, font_size=sp(14),
                                  state="down" if key == "board" else "normal",
                                  background_normal="", background_down="",
                                  background_color=ACC if key == "board" else CARD)
                tb.bind(state=lambda i, s: setattr(i, "background_color", ACC if s == "down" else CARD))
                tb.bind(on_release=lambda i, k=key: setattr(self.sm, "current", k))
                self.nav.add_widget(tb)
            root.add_widget(self.nav)
            try:
                Window.bind(keyboard_height=self.on_keyboard)
            except Exception:
                pass
            Clock.schedule_interval(self.tick, 0.5)
            return root

        # -- keyboard / lifecycle
        def on_keyboard(self, *_):
            try:
                up = Window.keyboard_height > 0
            except Exception:
                up = False
            self.nav.height = 0 if up else dp(52)
            self.nav.opacity = 0 if up else 1

        def on_pause(self):
            return True

        def on_stop(self):
            core.shutdown()

        # -- banner
        def show_banner(self, ev):
            self.banner_lbl.text = ev["msg"][:300]
            self.banner.set_bg(ERR if ev["level"] == "error" else WARN)
            self.banner.height, self.banner.opacity = dp(60), 1
            if self.hide_ev is not None:
                self.hide_ev.cancel()
            self.hide_ev = Clock.schedule_once(lambda dt: self.hide_banner(), 10)

        def hide_banner(self):
            self.banner.height, self.banner.opacity = 0, 0

        # -- screens
        def build_board(self):
            sv, col = scroll_column()
            col.add_widget(wrap_label("[b]EXECUTION PLAN[/b]", 12, MUT))
            self.plan_in = text_input(multiline=True, size_hint_y=None, height=dp(170),
                                      hint_text="One step per line. 1. / - / * bullets are fine.")
            col.add_widget(self.plan_in)
            row1 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            self.run_btn = button("Run plan", self.do_run)
            row1.add_widget(self.run_btn)
            row1.add_widget(button("Cancel", core.cancel, color=(0.25, 0.28, 0.36, 1)))
            col.add_widget(row1)
            row2 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            self.bench_btn = button("Benchmark", core.submit_benchmark, color=(0.25, 0.28, 0.36, 1))
            row2.add_widget(self.bench_btn)
            row2.add_widget(button("Clear plan", lambda: setattr(self.plan_in, "text", ""),
                                   color=(0.25, 0.28, 0.36, 1)))
            self.step_mode = ToggleButton(text="Step-by-step", font_size=sp(14), background_normal="",
                                          background_down="", background_color=CARD)
            self.step_mode.bind(state=lambda i, s: setattr(i, "background_color", ACC if s == "down" else CARD))
            row2.add_widget(self.step_mode)
            col.add_widget(row2)
            col.add_widget(wrap_label("[b]LIVE STATUS[/b]", 12, MUT))
            self.status_lbl = wrap_label("starting...", 14)
            col.add_widget(card_with(self.status_lbl))
            col.add_widget(wrap_label("[b]ACTIVE STEPS[/b]", 12, MUT))
            self.steps_lbl = wrap_label("No plan submitted yet.", 14)
            col.add_widget(card_with(self.steps_lbl))
            return sv

        def build_calc(self):
            sv, col = scroll_column()
            col.add_widget(wrap_label("[b]CALCULATOR[/b]", 12, MUT))
            self.expr_in = text_input(multiline=False, size_hint_y=None, height=dp(48),
                                      hint_text="e.g. pmt(0.045/12, 360, 250000)")
            self.expr_in.bind(on_text_validate=lambda *_: self.do_calc())
            col.add_widget(self.expr_in)
            prow = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
            self.prec_in = text_input(multiline=False, text="50", input_filter="int", size_hint_x=None,
                                      width=dp(90), size_hint_y=None, height=dp(44))
            prow.add_widget(self.prec_in)
            prow.add_widget(wrap_label("digits of precision", 13, MUT))
            col.add_widget(prow)
            brow = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            brow.add_widget(button("Calculate", self.do_calc))
            brow.add_widget(button("Copy result", self.copy_result, color=(0.25, 0.28, 0.36, 1)))
            col.add_widget(brow)
            chips = ["pmt(", "fv(", "pv(", "npv(", "roi(", "cagr(", "margin(", "markup(", "pct(", "sqrt(", "ln(", "^"]
            grid = GridLayout(cols=4, size_hint_y=None, spacing=dp(6), row_default_height=dp(40),
                              row_force_default=True)
            grid.bind(minimum_height=grid.setter("height"))
            for c in chips:
                b = Button(text=c, font_size=sp(14), background_normal="", background_color=CARD)
                b.bind(on_release=lambda i, t=c: self.insert_chip(t))
                grid.add_widget(b)
            col.add_widget(grid)
            self.result_lbl = wrap_label("-", 22)
            col.add_widget(card_with(self.result_lbl))
            self.result_meta = wrap_label("", 12, MUT)
            col.add_widget(self.result_meta)
            col.add_widget(wrap_label(esc(BUILTIN_HELP), 12, MUT))
            col.add_widget(wrap_label("[b]RECENT[/b]", 12, MUT))
            self.chist_lbl = wrap_label("", 13)
            col.add_widget(self.chist_lbl)
            return sv

        def build_console(self):
            box = BoxLayout(orientation="vertical", spacing=dp(6), padding=[dp(8)] * 4)
            self.console_sv = ScrollView(do_scroll_x=False, bar_width=dp(4))
            self.console_lbl = wrap_label("", 13)
            self.console_sv.add_widget(self.console_lbl)
            box.add_widget(self.console_sv)
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            row.add_widget(button("Clear", self.clear_console, color=(0.25, 0.28, 0.36, 1)))
            row.add_widget(button("Copy all", lambda: copy(self.plain_console()), color=(0.25, 0.28, 0.36, 1)))
            row.add_widget(button("Jump to end", lambda: setattr(self.console_sv, "scroll_y", 0)))
            box.add_widget(row)
            return box

        def build_log(self):
            sv, col = scroll_column()
            col.add_widget(wrap_label("[b]EVOLUTION PLAN LOG[/b]", 12, MUT))
            self.hpath_lbl = wrap_label("", 11, MUT)
            col.add_widget(self.hpath_lbl)
            self.hist_lbl = wrap_label("", 14)
            col.add_widget(self.hist_lbl)
            return sv

        # -- actions
        def do_run(self):
            self.plan_in.focus = False
            core.submit_plan(self.plan_in.text, "steps" if self.step_mode.state == "down" else "plan")

        def do_calc(self):
            self.expr_in.focus = False
            if core.submit_calc(self.expr_in.text, self.prec_in.text):
                self.result_lbl.text = "working..."

        def copy_result(self):
            r = core.snapshot().get("last_result")
            if r and copy(r["result"]):
                self.result_meta.text = "copied to clipboard"

        def insert_chip(self, text):
            self.expr_in.insert_text(text)
            self.expr_in.focus = True

        def clear_console(self):
            self.console_lines = []
            self.console_lbl.text = ""

        def plain_console(self):
            return "\n".join(l[0] for l in self.console_lines)

        # -- periodic UI refresh (cheap: reads snapshots only, never blocks)
        def tick(self, dt):
            events, last = core.bus.since(self.last_event)
            self.last_event = last
            if events:
                stick = self.console_sv.scroll_y <= 0.05
                for e in events:
                    plain = "%s %s %s" % (e["t"], e["src"], e["msg"])
                    self.console_lines.append((plain, "[color=%s]%s[/color]" % (HEX.get(e["level"], "c8cfdb"), esc(plain))))
                    if e["alert"]:
                        self.show_banner(e)
                del self.console_lines[:-400]
                self.console_lbl.text = "\n".join(l[1] for l in self.console_lines)
                if stick:
                    Clock.schedule_once(lambda dt: setattr(self.console_sv, "scroll_y", 0), 0)
            s = core.snapshot()
            state = "running" if s["busy"] else (s["state"] or "idle")
            self.header.text = " %s  |  score %s  |  rollbacks %s  |  jobs %d" % (
                esc(state), fmt_score(s["score"]), s["rollbacks"], len(s["jobs"]))
            self.run_btn.disabled = self.bench_btn.disabled = bool(s["busy"])
            delta = "" if s["delta"] is None else " (%+.3g)" % s["delta"]
            lines = ["[b]State[/b] %s" % esc(state),
                     "[b]Benchmark[/b] %s%s  [color=8b95a8]%s[/color]" % (fmt_score(s["score"]), delta, esc(s["score_src"] or "")),
                     "[b]Best[/b] %s    [b]Rollbacks[/b] %s" % (fmt_score(s["best"]), s["rollbacks"])]
            if s["generation"]:
                lines.append("[b]Generation[/b] %s" % esc(str(s["generation"])))
            if s["last_rollback"]:
                lines.append("[color=f31260]Last rollback: %s[/color]" % esc(short(s["last_rollback"]["action"] + " " + s["last_rollback"]["detail"], 120)))
            lines.append("[b]Evolver[/b] %s" % esc(s["evolver"]))
            lines.append("[b]Math[/b] %s" % esc(s["math"]))
            if s["status_error"]:
                lines.append("[color=f31260]status: %s[/color]" % esc(s["status_error"]))
            self.status_lbl.text = "\n".join(lines)
            steps = ["[color=%s]%s %d. %s[/color]" % (STEP_HEX[p["state"]], STEP_ICON[p["state"]], i, esc(p["text"]))
                     for i, p in enumerate(s["plan"], 1)]
            steps += ["[color=8b95a8]evolver:[/color] %s" % esc(x) for x in s["active_steps"]]
            self.steps_lbl.text = "\n".join(steps) or "No plan submitted yet."
            r = s["last_result"]
            if r and r["id"] != self.seen_result:
                self.seen_result = r["id"]
                self.result_lbl.text = esc(r["result"])
                self.result_lbl.color = FG if r["ok"] else ERR
                self.result_meta.text = "%s  %d ms" % (r["engine"], r["ms"]) if r["ok"] else "error"
                self.chist_lbl.text = "\n".join("%s = %s" % (esc(h["expr"]), esc(short(h["result"], 80)))
                                                for h in s["calc_history"])
            key = (s["updated"], s["history_error"], len(s["history"]))
            if key != self._last_hist:
                self._last_hist = key
                self.hpath_lbl.text = esc(s["history_path"])
                out = []
                for e in reversed(s["history"]):
                    col = "f31260" if e["rollback"] else "c8cfdb"
                    out.append("[color=%s]%s[/color]" % (col, esc(history_line(e))))
                if s["history_error"]:
                    out.insert(0, "[color=f5a524]%s[/color]" % esc(s["history_error"]))
                self.hist_lbl.text = "\n\n".join(out) or "No history entries yet."

    BoardApp().run()
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="AutoMind execution board")
    ap.add_argument("--mode", choices=("auto", "kivy", "web", "tui"), default="auto")
    ap.add_argument("--host", default="127.0.0.1", help="web mode bind address")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--history", help="path to evolution_history.json")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    core = BoardCore(resolve_history_path(args.history))
    core.start()
    mode = args.mode
    if mode == "auto":
        mode = "kivy" if importlib.util.find_spec("kivy") else "web"
    if mode == "kivy":
        if importlib.util.find_spec("kivy") is None:
            print("Kivy is not installed; falling back to web mode.")
            mode = "web"
        else:
            return run_kivy(core)
    if mode == "tui":
        return run_tui(core)
    return run_web(core, args.host, args.port, not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_math_core.py  -  Exact-arithmetic math core for the AutoMind agent
=========================================================================

Pure Python standard library (no numpy / sympy). Tested for Python 3.8+,
including Pydroid 3 on Android.

What it gives you
-----------------
1. EXACT DECIMAL FINANCE (28 significant digits, ROUND_HALF_UP for money)
     gst_calculate()      GST add / remove, CGST+SGST / IGST split
     compound_interest()  any compounding frequency, effective annual rate
     simple_interest()
     emi_calculate()      loan EMI with optional full amortisation schedule
     ledger_balance()     running balance of one account (Dr/Cr aware)
     verify_journal()     double-entry check + trial balance
     percent helpers      percent_of(), what_percent(), percent_change(), ...

2. SAFE EXPRESSION EVALUATOR (no eval/exec at all)
     safe_eval("2 + 3*4^2")                        -> Decimal('50')
     evaluate_expression("sqrt(16) + 2^10")        -> dict with step trace
     solve_equation("2x + 3 = 11")                 -> x = 4
     solve_equation("x^2 - 5x + 6 = 0")            -> x = 2 or x = 3
   Pipeline: length limit -> character whitelist regex -> tokenizer ->
   AST parse -> node whitelist -> custom Decimal tree-walker. Attribute
   access, subscripts, lambdas, comprehensions, strings, imports, names that
   are not declared variables/constants, and unknown functions are all
   rejected. The tree-walker has no access to Python builtins at all, which
   is stricter than eval() with restricted builtins.

3. CHAIN-OF-THOUGHT QUERY HANDLER
     solve_query("add 18% GST to 1000")       -> structured dict
     solve_query_json("EMI for 5 lakh at 9% for 20 years")  -> JSON string
   Response keys: status, operation, query, extracted_numbers, inputs,
   assumptions, breakdown (numbered steps), final_result, details, error.
   All numbers inside the JSON are strings, so no precision is lost.

Command line / Pydroid
----------------------
   python agent_math_core.py "15% of 2400"
   python agent_math_core.py --selftest
   python agent_math_core.py            (interactive prompt)

Integration
-----------
   import agent_math_core as amc
   reply = amc.solve_query("compound interest on 10000 at 8% for 5 years")
   if reply["status"] == "success":
       print(reply["final_result"])
"""

from __future__ import annotations

import ast
import json
import math
import re
import sys
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    localcontext,
)
from functools import wraps

__version__ = "1.0.0"

__all__ = [
    "PRECISION", "CTX", "MathCoreError", "InputError", "ExpressionError",
    "to_decimal", "round_money", "fmt", "to_json",
    "percent_of", "what_percent", "percent_change", "increase_by", "decrease_by",
    "gst_calculate", "simple_interest", "compound_interest", "emi_calculate",
    "ledger_balance", "verify_journal",
    "SafeEvaluator", "safe_eval", "evaluate_expression", "free_names",
    "solve_equation", "solve_query", "solve_query_json", "run_selftest", "main",
]

# --------------------------------------------------------------------------- #
# 1. Configuration, exceptions, decimal context
# --------------------------------------------------------------------------- #

PRECISION = 28

# One private context used for every calculation. It never touches the
# process-wide decimal context, so other code in the agent is unaffected.
CTX = Context(
    prec=PRECISION,
    rounding=ROUND_HALF_UP,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow],
)

MAX_EXPR_LEN = 500        # characters accepted by the evaluator
MAX_AST_NODES = 250       # complexity cap
MAX_EXPONENT = 10000      # largest |exponent| allowed in a ** b
MAX_FACTORIAL = 200
MAX_QUERY_LEN = 1000


class MathCoreError(Exception):
    """Base class for every error raised on purpose by this module."""


class InputError(MathCoreError):
    """A value or query could not be understood or is out of range."""


class ExpressionError(MathCoreError):
    """An expression is invalid, unsafe, or cannot be evaluated."""


def _exact(fn):
    """Run the wrapped function inside the private 28-digit Decimal context."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with localcontext(CTX):
            return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# 2. Number helpers and formatting
# --------------------------------------------------------------------------- #

_CURRENCY_RE = re.compile(
    r"[\u20b9$\u20ac\u00a3]|\b(?:rs|inr|usd|rupees?|rupaye|dollars?)\b\.?", re.I
)


def to_decimal(value, name: str = "value") -> Decimal:
    """Convert int / float / str / Decimal to a finite Decimal without
    binary-float noise (floats go through repr, so 0.1 -> Decimal('0.1'))."""
    if isinstance(value, bool):
        raise InputError(f"{name}: booleans are not valid numbers")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(repr(value))
    elif isinstance(value, str):
        s = _CURRENCY_RE.sub("", value).replace(",", "").replace(" ", "").strip()
        try:
            d = Decimal(s)
        except (InvalidOperation, ValueError):
            raise InputError(f"{name}: cannot read {value!r} as a number") from None
    else:
        raise InputError(f"{name}: unsupported type {type(value).__name__}")
    if not d.is_finite():
        raise InputError(f"{name}: must be a finite number")
    if abs(d.adjusted()) > 100000:
        raise InputError(f"{name}: number is out of range")
    return d


def round_money(value, places: int = 2) -> Decimal:
    """Round half-up to `places` decimals (default 2). Never returns -0.00."""
    d = to_decimal(value)
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP, context=CTX)
    return abs(q) if q.is_zero() else q


def _sci_or_plain(d: Decimal) -> str:
    return format(d, "f") if -40 < d.adjusted() < 60 else format(d, "E")


def fmt(value) -> str:
    """Human-friendly exact string: trailing zeros removed, never '1E+2'."""
    d = value if isinstance(value, Decimal) else to_decimal(value)
    if not d.is_finite():
        return str(d)
    if d == 0:
        return "0"
    return _sci_or_plain(d.normalize(CTX))


def _plain(d: Decimal) -> str:
    """Like str(), but keeps the scale (1180.00 stays 1180.00)."""
    if not d.is_finite():
        return str(d)
    return _sci_or_plain(d)


def _show(d: Decimal) -> str:
    s = fmt(d)
    return f"({s})" if s.startswith("-") else s


def _round_dp(d: Decimal, places: int) -> Decimal:
    try:
        return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP, context=CTX)
    except InvalidOperation:
        return d


def _snap(d: Decimal) -> Decimal:
    """Remove 1e-18 level noise from solver output (3.9999999... -> 4)."""
    try:
        return d.quantize(Decimal("1e-18"), rounding=ROUND_HALF_UP, context=CTX).normalize(CTX)
    except InvalidOperation:
        return d.normalize(CTX)


def _jsonable(obj):
    if isinstance(obj, Decimal):
        return _plain(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def to_json(obj, indent: int = 2) -> str:
    """Serialise any result of this module; Decimals become exact strings."""
    return json.dumps(_jsonable(obj), indent=indent, ensure_ascii=False)


class _Steps:
    """Collects numbered chain-of-thought steps."""

    def __init__(self):
        self.items = []

    def add(self, description: str, calculation: str = "", result: str = ""):
        self.items.append({
            "step": len(self.items) + 1,
            "description": description,
            "calculation": calculation,
            "result": result,
        })


# --------------------------------------------------------------------------- #
# 3. Percentages
# --------------------------------------------------------------------------- #

@_exact
def percent_of(percent, amount) -> Decimal:
    """percent% of amount (not rounded)."""
    return to_decimal(amount, "amount") * to_decimal(percent, "percent") / 100


@_exact
def what_percent(part, whole) -> Decimal:
    """part is what percent of whole (not rounded)."""
    w = to_decimal(whole, "whole")
    if w == 0:
        raise InputError("the whole cannot be zero")
    return to_decimal(part, "part") * 100 / w


@_exact
def percent_change(old, new) -> Decimal:
    """Percentage change from old to new (not rounded)."""
    o = to_decimal(old, "old")
    if o == 0:
        raise InputError("the starting value cannot be zero")
    return (to_decimal(new, "new") - o) * 100 / o


@_exact
def increase_by(amount, percent) -> Decimal:
    a = to_decimal(amount, "amount")
    return a + a * to_decimal(percent, "percent") / 100


@_exact
def decrease_by(amount, percent) -> Decimal:
    a = to_decimal(amount, "amount")
    return a - a * to_decimal(percent, "percent") / 100


# --------------------------------------------------------------------------- #
# 4. Accounting: GST, interest, EMI, ledgers
# --------------------------------------------------------------------------- #

@_exact
def gst_calculate(amount, rate, inclusive: bool = False, interstate: bool = False) -> dict:
    """GST maths with money rounding to 2 decimals.

    inclusive=False: `amount` is the taxable value, GST is added on top.
    inclusive=True : `amount` already contains GST, which is backed out.
    GST is always computed so that taxable_value + gst == total exactly.
    Intra-state GST is split into CGST + SGST; inter-state is all IGST.
    """
    amt = to_decimal(amount, "amount")
    r = to_decimal(rate, "rate")
    if amt < 0:
        raise InputError("amount cannot be negative")
    if r < 0 or r > 100:
        raise InputError("GST rate must be between 0 and 100")
    st = _Steps()
    if inclusive:
        total = round_money(amt)
        st.add("Start with the GST-inclusive amount", "", _plain(total))
        base = round_money(total * 100 / (100 + r))
        st.add("Taxable value = total * 100 / (100 + rate)",
               f"{_plain(total)} * 100 / (100 + {fmt(r)})", _plain(base))
        gst = total - base
        st.add("GST = total - taxable value", f"{_plain(total)} - {_plain(base)}", _plain(gst))
    else:
        base = round_money(amt)
        st.add("Start with the taxable (GST-exclusive) amount", "", _plain(base))
        gst = round_money(base * r / 100)
        st.add("GST = taxable value * rate / 100",
               f"{_plain(base)} * {fmt(r)} / 100", _plain(gst))
        total = base + gst
        st.add("Total = taxable value + GST", f"{_plain(base)} + {_plain(gst)}", _plain(total))
    zero = Decimal("0.00")
    if interstate:
        cgst, sgst, igst = zero, zero, gst
        st.add("Inter-state supply: the whole GST is IGST", "", f"IGST {_plain(igst)}")
    else:
        cgst = round_money(gst / 2)
        sgst = gst - cgst
        igst = zero
        st.add("Intra-state supply: GST splits into CGST + SGST", f"{_plain(gst)} / 2",
               f"CGST {_plain(cgst)} + SGST {_plain(sgst)}")
    return {
        "mode": "gst_inclusive" if inclusive else "gst_exclusive",
        "rate_percent": r, "taxable_value": base, "gst_amount": gst, "total": total,
        "cgst": cgst, "sgst": sgst, "igst": igst, "steps": st.items,
    }


@_exact
def simple_interest(principal, annual_rate, years) -> dict:
    """I = P * r * t / 100"""
    p = to_decimal(principal, "principal")
    r = to_decimal(annual_rate, "annual_rate")
    t = to_decimal(years, "years")
    if p < 0 or r < 0 or t < 0:
        raise InputError("principal, rate and time cannot be negative")
    st = _Steps()
    interest = round_money(p * r * t / 100)
    st.add("Simple interest = P * r * t / 100",
           f"{fmt(p)} * {fmt(r)} * {fmt(t)} / 100", _plain(interest))
    amount = round_money(p) + interest
    st.add("Maturity amount = P + interest", f"{_plain(round_money(p))} + {_plain(interest)}",
           _plain(amount))
    return {"principal": round_money(p), "rate_percent": r, "years": t,
            "interest": interest, "amount": amount, "steps": st.items}


@_exact
def compound_interest(principal, annual_rate, years, compounds_per_year=1,
                      round_each_period: bool = False) -> dict:
    """A = P * (1 + r/(100 n)) ** (n t).

    With round_each_period=True the balance is rounded to 2 decimals after
    every period (how many banks post interest); this needs a whole number of
    periods.
    """
    p = to_decimal(principal, "principal")
    r = to_decimal(annual_rate, "annual_rate")
    t = to_decimal(years, "years")
    n = to_decimal(compounds_per_year, "compounds_per_year")
    if p < 0 or r < 0 or t < 0:
        raise InputError("principal, rate and time cannot be negative")
    if n < 1 or n > 366 or n != n.to_integral_value():
        raise InputError("compounds_per_year must be a whole number from 1 to 366")
    periods = n * t
    if periods > 100000:
        raise InputError("too many compounding periods (max 100000)")
    st = _Steps()
    i = r / 100 / n
    st.add("Periodic rate i = annual rate / 100 / n", f"{fmt(r)} / 100 / {fmt(n)}", fmt(i))
    st.add("Number of periods N = n * years", f"{fmt(n)} * {fmt(t)}", fmt(periods))
    p_rounded = round_money(p)
    if round_each_period:
        if periods != periods.to_integral_value():
            raise InputError("round_each_period needs a whole number of periods")
        bal = p_rounded
        for _ in range(int(periods)):
            bal = round_money(bal * (1 + i))
        amount = bal
        st.add("Compound period by period, rounding to 2 decimals each time",
               f"{int(periods)} periods", _plain(amount))
        factor = None
    else:
        factor = (1 + i) ** periods
        st.add("Growth factor = (1 + i) ^ N", f"(1 + {fmt(i)}) ^ {fmt(periods)}", fmt(factor))
        amount = round_money(p * factor)
        st.add("Maturity amount A = P * factor", f"{fmt(p)} * {fmt(factor)}", _plain(amount))
    interest = amount - p_rounded
    st.add("Compound interest = A - P", f"{_plain(amount)} - {_plain(p_rounded)}", _plain(interest))
    ear = ((1 + i) ** n - 1) * 100
    st.add("Effective annual rate = ((1 + i) ^ n - 1) * 100", f"((1 + {fmt(i)}) ^ {fmt(n)} - 1) * 100",
           fmt(_round_dp(ear, 10)) + "%")
    return {"principal": p_rounded, "rate_percent": r, "years": t, "compounds_per_year": n,
            "periods": periods, "growth_factor": factor, "amount": amount, "interest": interest,
            "effective_annual_rate_percent": _round_dp(ear, 10), "steps": st.items}


@_exact
def emi_calculate(principal, annual_rate, months, schedule: bool = False) -> dict:
    """Loan EMI = P * i * (1+i)^N / ((1+i)^N - 1), i = annual rate / 1200.

    The amortisation is run month by month with 2-decimal rounding and the
    last instalment is adjusted so the balance ends at exactly 0.00.
    """
    p = to_decimal(principal, "principal")
    r = to_decimal(annual_rate, "annual_rate")
    n_dec = to_decimal(months, "months")
    if p < 0 or r < 0:
        raise InputError("principal and rate cannot be negative")
    if n_dec != n_dec.to_integral_value() or not 1 <= n_dec <= 1200:
        raise InputError("tenure must be a whole number of months from 1 to 1200")
    n = int(n_dec)
    st = _Steps()
    i = r / 12 / 100
    st.add("Monthly rate i = annual rate / 12 / 100", f"{fmt(r)} / 12 / 100", fmt(i))
    if i == 0:
        emi = round_money(p / n)
        st.add("Zero-interest loan: EMI = P / N", f"{fmt(p)} / {n}", _plain(emi))
    else:
        g = (1 + i) ** n
        st.add("Growth factor g = (1 + i) ^ N", f"(1 + {fmt(i)}) ^ {n}", fmt(g))
        emi = round_money(p * i * g / (g - 1))
        st.add("EMI = P * i * g / (g - 1)", f"{fmt(p)} * {fmt(i)} * {fmt(g)} / ({fmt(g)} - 1)",
               _plain(emi))
    bal = round_money(p)
    total_interest = Decimal("0.00")
    total_paid = Decimal("0.00")
    rows = []
    last_payment = emi
    for m in range(1, n + 1):
        interest = round_money(bal * i)
        if m == n:
            princ = bal
            pay = princ + interest
        else:
            pay = emi
            princ = pay - interest
            if princ > bal:
                princ = bal
                pay = princ + interest
        opening = bal
        bal = bal - princ
        total_interest += interest
        total_paid += pay
        last_payment = pay
        if schedule:
            rows.append({"month": m, "opening_balance": opening, "payment": pay,
                         "interest": interest, "principal": princ, "closing_balance": bal})
    st.add("Total payable over the loan", f"sum of {n} instalments", _plain(total_paid))
    st.add("Total interest = total payable - principal",
           f"{_plain(total_paid)} - {_plain(round_money(p))}", _plain(total_interest))
    result = {"principal": round_money(p), "rate_percent": r, "months": n, "emi": emi,
              "final_instalment": last_payment, "total_payment": total_paid,
              "total_interest": total_interest, "steps": st.items}
    if schedule:
        result["schedule"] = rows
    return result


@_exact
def ledger_balance(entries, opening_balance=0, normal_side: str = "debit") -> dict:
    """Running balance of ONE account.

    entries: list of dicts {date, description, debit, credit} or tuples
             (description, debit, credit). Amounts must be >= 0 and an entry
             cannot carry both a debit and a credit.
    normal_side: "debit" for assets/expenses, "credit" for liabilities,
                 equity and income. Positive balances are on the normal side.
    """
    side = str(normal_side).lower()
    if side not in ("debit", "credit"):
        raise InputError("normal_side must be 'debit' or 'credit'")
    opening = round_money(opening_balance)
    running = opening
    total_dr = Decimal("0.00")
    total_cr = Decimal("0.00")
    rows = []
    for idx, e in enumerate(entries, 1):
        if isinstance(e, dict):
            date = e.get("date", "")
            desc = e.get("description", e.get("narration", ""))
            dr, cr = e.get("debit", 0), e.get("credit", 0)
        elif isinstance(e, (list, tuple)) and len(e) >= 3:
            date, desc, dr, cr = "", e[0], e[1], e[2]
        else:
            raise InputError(f"entry {idx}: use a dict or (description, debit, credit)")
        dr = round_money(to_decimal(dr or 0, f"entry {idx} debit"))
        cr = round_money(to_decimal(cr or 0, f"entry {idx} credit"))
        if dr < 0 or cr < 0:
            raise InputError(f"entry {idx}: amounts cannot be negative")
        if dr > 0 and cr > 0:
            raise InputError(f"entry {idx}: an entry cannot have both debit and credit")
        total_dr += dr
        total_cr += cr
        running = running + (dr - cr if side == "debit" else cr - dr)
        rows.append({"line": idx, "date": str(date), "description": str(desc),
                     "debit": dr, "credit": cr, "balance": running})
    if running == 0:
        bal_side = "nil"
    elif (running > 0) == (side == "debit"):
        bal_side = "Dr"
    else:
        bal_side = "Cr"
    return {"normal_side": side, "opening_balance": opening, "total_debits": total_dr,
            "total_credits": total_cr, "closing_balance": running,
            "closing_balance_side": bal_side, "entries": rows}


@_exact
def verify_journal(journal) -> dict:
    """Double-entry check.

    journal: list of {id, date, description, lines:[{account, debit, credit}]}
    (lines may also be (account, debit, credit) tuples). Every transaction
    must have equal total debits and credits; a trial balance per account is
    produced and the overall books are checked.
    """
    transactions = []
    accounts = {}
    grand_dr = Decimal("0.00")
    grand_cr = Decimal("0.00")
    all_ok = True
    for t_idx, tx in enumerate(journal, 1):
        if not isinstance(tx, dict) or "lines" not in tx:
            raise InputError(f"transaction {t_idx}: expected a dict with a 'lines' list")
        t_dr = Decimal("0.00")
        t_cr = Decimal("0.00")
        for l_idx, line in enumerate(tx["lines"], 1):
            if isinstance(line, dict):
                acct, dr, cr = line.get("account"), line.get("debit", 0), line.get("credit", 0)
            elif isinstance(line, (list, tuple)) and len(line) >= 3:
                acct, dr, cr = line[0], line[1], line[2]
            else:
                raise InputError(f"transaction {t_idx} line {l_idx}: invalid line")
            if not acct:
                raise InputError(f"transaction {t_idx} line {l_idx}: missing account name")
            dr = round_money(to_decimal(dr or 0, "debit"))
            cr = round_money(to_decimal(cr or 0, "credit"))
            if dr < 0 or cr < 0:
                raise InputError(f"transaction {t_idx} line {l_idx}: negative amount")
            t_dr += dr
            t_cr += cr
            a = accounts.setdefault(str(acct), {"debit": Decimal("0.00"), "credit": Decimal("0.00")})
            a["debit"] += dr
            a["credit"] += cr
        diff = t_dr - t_cr
        ok = diff == 0
        all_ok = all_ok and ok
        grand_dr += t_dr
        grand_cr += t_cr
        transactions.append({"id": tx.get("id", t_idx), "date": str(tx.get("date", "")),
                             "description": str(tx.get("description", "")),
                             "total_debit": t_dr, "total_credit": t_cr,
                             "difference": diff, "balanced": ok})
    trial = []
    for name in sorted(accounts):
        dr, cr = accounts[name]["debit"], accounts[name]["credit"]
        net = dr - cr
        trial.append({"account": name, "debit": dr, "credit": cr,
                      "net_balance": abs(net),
                      "net_side": "Dr" if net > 0 else ("Cr" if net < 0 else "nil")})
    return {"balanced": all_ok and grand_dr == grand_cr, "total_debits": grand_dr,
            "total_credits": grand_cr, "difference": grand_dr - grand_cr,
            "transactions": transactions, "trial_balance": trial}


# --------------------------------------------------------------------------- #
# 5. Safe expression evaluator
# --------------------------------------------------------------------------- #

CONSTANTS = {
    "pi": Decimal("3.141592653589793238462643383"),
    "e": Decimal("2.718281828459045235360287471"),
}
FUNCTIONS = frozenset({
    "sqrt", "abs", "round", "floor", "ceil", "min", "max",
    "ln", "log", "log10", "exp", "pow", "fact", "factorial",
})

_BIN_SYMBOLS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
                ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**"}
_OP_NAMES = {ast.Add: "addition", ast.Sub: "subtraction", ast.Mult: "multiplication",
             ast.Div: "division", ast.FloorDiv: "floor division", ast.Mod: "modulo",
             ast.Pow: "exponentiation"}

# Stage 1 of the sandbox: nothing outside this character set gets any further.
_ALLOWED_CHARS = re.compile(r"^[0-9A-Za-z_\s+\-*/%^().,=]*$")
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>\*\*|//|[-+*/%^(),]))"
)


def _tokenize(expr: str):
    tokens, pos, expr = [], 0, expr.rstrip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise ExpressionError(f"unexpected character near {expr[pos:pos + 8]!r}")
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


def _normalize(expr: str) -> str:
    """Tokens -> canonical source: '^' becomes '**' and implicit
    multiplication is made explicit ('2(3+4)', '3x', '2pi', '(a)(b)')."""
    parts, prev = [], None
    for kind, text in _tokenize(expr):
        if kind == "op" and text == "^":
            text = "**"
        if prev is not None:
            pk, pt = prev
            left_value = (pk == "num" or (pk == "op" and pt == ")")
                          or (pk == "name" and pt.lower() not in FUNCTIONS))
            right_start = kind in ("num", "name") or (kind == "op" and text == "(")
            if left_value and right_start and not (pk == "num" and kind == "num"):
                parts.append("*")
        parts.append(text)
        prev = (kind, text)
    return " ".join(parts)


def _parse(expression: str):
    """Validate + normalise + parse. Returns (normalised_source, ast_tree)."""
    if not isinstance(expression, str):
        raise ExpressionError("expression must be a string")
    expr = expression.strip()
    if not expr:
        raise ExpressionError("empty expression")
    if len(expr) > MAX_EXPR_LEN:
        raise ExpressionError(f"expression too long (max {MAX_EXPR_LEN} characters)")
    if not _ALLOWED_CHARS.match(expr):
        raise ExpressionError("expression contains characters that are not allowed")
    if "__" in expr:
        raise ExpressionError("double underscores are not allowed")
    src = _normalize(expr)
    try:
        tree = ast.parse(src, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise ExpressionError("invalid syntax in expression") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise ExpressionError("expression is too complex")
    return src, tree


def free_names(expression: str):
    """Names used as variables (not constants, not function names)."""
    _, tree = _parse(expression)
    func_nodes = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return sorted({n.id for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and id(n) not in func_nodes
                   and n.id not in CONSTANTS})


def _power(a: Decimal, b: Decimal) -> Decimal:
    if abs(b) > MAX_EXPONENT:
        raise ExpressionError(f"exponent too large (max {MAX_EXPONENT})")
    if a == 0:
        if b < 0:
            raise ExpressionError("division by zero")
        return Decimal(1) if b == 0 else Decimal(0)
    if b == b.to_integral_value():
        return a ** int(b)
    if a < 0:
        raise ExpressionError("a negative base with a fractional exponent is not a real number")
    return a ** b


class SafeEvaluator:
    """Evaluates arithmetic/algebra with Decimal. There is no eval() and no
    access to Python builtins: the AST is walked by hand and only whitelisted
    node types are executed."""

    def __init__(self, variables=None, trace=None):
        self.variables = {str(k): to_decimal(v, f"variable {k}")
                          for k, v in (variables or {}).items()}
        self.trace = trace
        self.normalized = ""
        self.operators = []
        self._src = ""

    # -- public ------------------------------------------------------------ #
    def evaluate(self, expression: str) -> Decimal:
        src, tree = _parse(expression)
        self._src = self.normalized = src
        found = {type(n.op) for n in ast.walk(tree) if isinstance(n, ast.BinOp)}
        self.operators = [_OP_NAMES[k] for k in _BIN_SYMBOLS if k in found]
        with localcontext(CTX):
            try:
                return self._eval(tree.body)
            except ZeroDivisionError:
                raise ExpressionError("division by zero") from None
            except Overflow:
                raise ExpressionError("number too large") from None
            except ArithmeticError:
                raise ExpressionError("invalid mathematical operation") from None
            except RecursionError:
                raise ExpressionError("expression is nested too deeply") from None

    # -- internals ----------------------------------------------------------- #
    def _log(self, calc: str, result: Decimal):
        if self.trace is not None and len(self.trace) < 200:
            self.trace.append((calc, result))

    def _eval(self, node) -> Decimal:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ExpressionError("only plain numbers are allowed")
            seg = ast.get_source_segment(self._src, node)
            val = Decimal(seg)  # exact text, so 0.1 stays 0.1
            if not val.is_finite() or abs(val.adjusted()) > 100000:
                raise ExpressionError("number out of range")
            return val
        if isinstance(node, ast.Name):
            if node.id in self.variables:
                return self.variables[node.id]
            if node.id in CONSTANTS:
                return CONSTANTS[node.id]
            raise ExpressionError(f"unknown variable or constant: {node.id!r}")
        if isinstance(node, ast.BinOp):
            op = type(node.op)
            if op not in _BIN_SYMBOLS:
                raise ExpressionError("operator not allowed")
            left = self._eval(node.left)
            right = self._eval(node.right)
            result = self._binop(op, left, right)
            self._log(f"{_show(left)} {_BIN_SYMBOLS[op]} {_show(right)}", result)
            return result
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -self._eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +self._eval(node.operand)
            raise ExpressionError("operator not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ExpressionError("only simple function calls are allowed")
            fname = node.func.id.lower()
            if fname not in FUNCTIONS:
                raise ExpressionError(f"function not allowed: {node.func.id!r}")
            args = [self._eval(a) for a in node.args]
            result = self._call(fname, args)
            self._log(f"{fname}({', '.join(fmt(a) for a in args)})", result)
            return result
        raise ExpressionError(f"unsupported syntax element: {type(node).__name__}")

    @staticmethod
    def _binop(op, a: Decimal, b: Decimal) -> Decimal:
        if op is ast.Add:
            return a + b
        if op is ast.Sub:
            return a - b
        if op is ast.Mult:
            return a * b
        if op in (ast.Div, ast.FloorDiv, ast.Mod) and b == 0:
            raise ExpressionError("division by zero")
        if op is ast.Div:
            return a / b
        if op is ast.FloorDiv:
            return (a / b).to_integral_value(rounding=ROUND_FLOOR)
        if op is ast.Mod:
            q = (a / b).to_integral_value(rounding=ROUND_FLOOR)
            return a - b * q
        if op is ast.Pow:
            return _power(a, b)
        raise ExpressionError("operator not allowed")

    @staticmethod
    def _call(name: str, args) -> Decimal:
        def need(lo, hi=None):
            hi = lo if hi is None else hi
            if not lo <= len(args) <= hi:
                raise ExpressionError(f"{name}() expects {lo}"
                                      + (f" to {hi}" if hi != lo else "") + " argument(s)")
        a = args[0] if args else None
        if name == "sqrt":
            need(1)
            if a < 0:
                raise ExpressionError("square root of a negative number is not real")
            return a.sqrt()
        if name == "abs":
            need(1)
            return abs(a)
        if name == "round":
            need(1, 2)
            places = Decimal(0) if len(args) == 1 else args[1]
            if places != places.to_integral_value() or not 0 <= places <= 27:
                raise ExpressionError("round() places must be a whole number from 0 to 27")
            return a.quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)
        if name == "floor":
            need(1)
            return a.to_integral_value(rounding=ROUND_FLOOR)
        if name == "ceil":
            need(1)
            return a.to_integral_value(rounding=ROUND_CEILING)
        if name in ("min", "max"):
            need(1, 50)
            return min(args) if name == "min" else max(args)
        if name in ("ln", "log10"):
            need(1)
            if a <= 0:
                raise ExpressionError("logarithm needs a positive number")
            return a.ln() if name == "ln" else a.log10()
        if name == "log":
            need(1, 2)
            if a <= 0:
                raise ExpressionError("logarithm needs a positive number")
            if len(args) == 1:
                return a.ln()
            base = args[1]
            if base <= 0 or base == 1:
                raise ExpressionError("logarithm base must be positive and not 1")
            return a.ln() / base.ln()
        if name == "exp":
            need(1)
            if abs(a) > MAX_EXPONENT:
                raise ExpressionError("exp() argument too large")
            return a.exp()
        if name == "pow":
            need(2)
            return _power(args[0], args[1])
        if name in ("fact", "factorial"):
            need(1)
            if a != a.to_integral_value() or not 0 <= a <= MAX_FACTORIAL:
                raise ExpressionError(f"factorial needs a whole number from 0 to {MAX_FACTORIAL}")
            return +Decimal(math.factorial(int(a)))
        raise ExpressionError(f"function not allowed: {name!r}")


def safe_eval(expression: str, variables=None, trace=None) -> Decimal:
    """Evaluate a maths expression safely. Supports + - * / // % ** ^,
    parentheses, implicit multiplication, constants pi/e, and the functions
    sqrt abs round floor ceil min max ln log log10 exp pow fact."""
    return SafeEvaluator(variables, trace).evaluate(expression)


@_exact
def evaluate_expression(expression: str, variables=None) -> dict:
    """Like safe_eval() but returns the value plus a step-by-step trace that
    follows the order of operations."""
    trace = []
    ev = SafeEvaluator(variables, trace)
    value = ev.evaluate(expression)
    st = _Steps()
    st.add("Normalise the expression (explicit operators)", expression.strip(), ev.normalized)
    st.add("Apply the order of operations: brackets, functions, exponents, "
           "multiplication/division, addition/subtraction")
    for calc, res in trace:
        st.add("Evaluate", calc, fmt(res))
    st.add("Final value", "", fmt(value))
    return {"expression": expression.strip(), "normalized": ev.normalized, "value": value,
            "rounded_10dp": _round_dp(value, 10), "operators": ev.operators, "steps": st.items}


# --------------------------------------------------------------------------- #
# 6. Equation solver (one variable, degree <= 2, exact Decimal)
# --------------------------------------------------------------------------- #

@_exact
def solve_equation(equation: str, variable: str = None) -> dict:
    """Solve a linear or quadratic equation in one variable, e.g.
    '2x + 3 = 11', '3(x - 2) = x + 4', 'x^2 - 5x + 6 = 0'.

    f(x) = left - right is sampled at x = 1, 2, 3 to recover a, b, c of
    a*x^2 + b*x + c exactly, and verified at two more points so anything that
    is not a polynomial of degree <= 2 is rejected rather than mis-solved.
    """
    if not isinstance(equation, str) or equation.count("=") != 1:
        raise ExpressionError("an equation needs exactly one '=' sign")
    lhs, rhs = (s.strip() for s in equation.split("="))
    if not lhs or not rhs:
        raise ExpressionError("both sides of the equation must contain something")
    names = sorted(set(free_names(lhs)) | set(free_names(rhs)))
    st = _Steps()
    if variable is not None:
        var = str(variable)
        extra = [n for n in names if n != var]
        if extra:
            raise ExpressionError(f"unknown symbols besides {var!r}: {', '.join(extra)}")
    elif len(names) == 1:
        var = names[0]
    elif not names:
        left, right = safe_eval(lhs), safe_eval(rhs)
        holds = left == right
        st.add("No variable: compare both sides", f"{fmt(left)} = {fmt(right)}", str(holds))
        return {"variable": None, "equation": equation, "degree": 0, "solution_type": "identity",
                "solutions": [], "holds": holds, "final": "true" if holds else "false",
                "steps": st.items}
    else:
        raise ExpressionError("more than one variable found: " + ", ".join(names))

    def f(x: Decimal) -> Decimal:
        ev = SafeEvaluator({var: x})
        return ev.evaluate(lhs) - ev.evaluate(rhs)

    tol = Decimal("1e-15")
    st.add("Move everything to one side: f(x) = left - right", f"f({var}) = ({lhs}) - ({rhs})")
    good = True
    try:
        f1, f2, f3 = f(Decimal(1)), f(Decimal(2)), f(Decimal(3))
        a = (f3 - 2 * f2 + f1) / 2
        b = (f2 - f1) - 3 * a
        c = f1 - a - b
        for x in (Decimal(4), Decimal(-2)):
            if abs(a * x * x + b * x + c - f(x)) > tol * (1 + abs(f(x))):
                good = False
                break
    except ExpressionError:
        good = False
    if not good:
        raise ExpressionError("only polynomial equations of degree 2 or lower in one variable "
                              "can be solved")
    a, b, c = _snap(a), _snap(b), _snap(c)
    st.add("Recover coefficients of a*x^2 + b*x + c from f(1), f(2), f(3)",
           f"f(1)={fmt(f1)}, f(2)={fmt(f2)}, f(3)={fmt(f3)}",
           f"a={fmt(a)}, b={fmt(b)}, c={fmt(c)}")

    def zero(v):
        return abs(v) <= tol

    solutions, kind = [], ""
    if zero(a):
        degree = 1 if not zero(b) else 0
        if degree == 1:
            x = _snap(-c / b)
            solutions, kind = [x], "unique"
            st.add("Linear equation: x = -c / b", f"-({fmt(c)}) / {fmt(b)}", fmt(x))
        elif zero(c):
            kind = "infinite"
            st.add("0 = 0 for every value", "", "infinitely many solutions")
        else:
            kind = "none"
            st.add("Contradiction: constant is never zero", fmt(c) + " = 0", "no solution")
    else:
        degree = 2
        disc = b * b - 4 * a * c
        st.add("Quadratic: discriminant D = b^2 - 4ac",
               f"{fmt(b)}^2 - 4*{_show(a)}*{_show(c)}", fmt(disc))
        if zero(disc / (1 + b * b)):
            x = _snap(-b / (2 * a))
            solutions, kind = [x], "repeated"
            st.add("D = 0: one repeated root x = -b / 2a", f"-({fmt(b)}) / (2*{_show(a)})", fmt(x))
        elif disc > 0:
            root = disc.sqrt()
            x1, x2 = sorted([_snap((-b - root) / (2 * a)), _snap((-b + root) / (2 * a))])
            solutions, kind = [x1, x2], "two_real"
            st.add("D > 0: two real roots x = (-b +/- sqrt(D)) / 2a",
                   f"sqrt({fmt(disc)}) = {fmt(_round_dp(root, 15))}", f"{fmt(x1)} and {fmt(x2)}")
        else:
            re_part = _snap(-b / (2 * a))
            im_part = _snap((-disc).sqrt() / (2 * abs(a)))
            solutions = [f"{fmt(re_part)} + {fmt(im_part)}i", f"{fmt(re_part)} - {fmt(im_part)}i"]
            kind = "complex"
            st.add("D < 0: two complex roots", f"sqrt({fmt(-disc)}) = {fmt(_round_dp((-disc).sqrt(), 15))}",
                   " and ".join(solutions))
    verification = []
    for s in solutions:
        if isinstance(s, Decimal):
            residual = f(s)
            verification.append({var: s, "left_minus_right": _round_dp(residual, 18)})
    if verification:
        st.add("Check by substituting back (left - right should be 0)", "",
               "; ".join(f"{var}={fmt(v[var])} -> {fmt(v['left_minus_right'])}" for v in verification))
    if kind == "infinite":
        final = "every value works (identity)"
    elif kind == "none":
        final = "no solution"
    else:
        final = " or ".join(f"{var} = {s if isinstance(s, str) else fmt(s)}" for s in solutions)
    return {"variable": var, "equation": equation, "degree": degree,
            "coefficients": {"a": a, "b": b, "c": c}, "solution_type": kind,
            "solutions": solutions, "verification": verification, "final": final,
            "steps": st.items}


# --------------------------------------------------------------------------- #
# 7. Natural-language chain-of-thought handler
# --------------------------------------------------------------------------- #

_NUM = r"\d+(?:\.\d+)?"
_NUM_RE = re.compile(_NUM)
_PERCENT_RE = re.compile(rf"({_NUM})\s*(?:%|percent\b|per\s*cent\b)")
_TIME_RE = re.compile(
    rf"({_NUM})\s*(years?|yrs?|yr|months?|mos?|mths?|saal|sal|varsh|mahine|mahina|days?|din)\b")
_GROUPED_NUM = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{2,3})+(?!\d|,\d)")
_MAGNITUDE_RE = re.compile(
    rf"({_NUM})\s*(lakhs?|lacs?|crores?|cr|thousand|hazaa?r|k|million|mn|billion|bn)\b")
_MAGNITUDES = {"lakh": 10 ** 5, "lac": 10 ** 5, "crore": 10 ** 7, "cr": 10 ** 7,
               "thousand": 10 ** 3, "hazar": 10 ** 3, "hazaar": 10 ** 3, "k": 10 ** 3,
               "million": 10 ** 6, "mn": 10 ** 6, "billion": 10 ** 9, "bn": 10 ** 9}
_UNICODE_OPS = str.maketrans({"\u00d7": "*", "\u00f7": "/", "\u2212": "-", "\u2013": "-",
                              "\u2014": "-"})

_FREQ_PATTERNS = [
    (r"\bdaily\b", 365), (r"\bweekly\b", 52), (r"\bmonthly\b", 12), (r"\bquarterly\b", 4),
    (r"\b(?:half[\s-]?yearly|semi[\s-]?annual\w*|bi[\s-]?annual\w*)\b", 2),
    (r"\b(?:annual\w*|yearly)\b", 1),
]

_WORD_OPS = [
    (r"\bsquare\s+root\s+of\s+(\d+(?:\.\d+)?|\([^)]*\))", r"sqrt(\1)"),
    (r"\bcube\s+root\s+of\s+(\d+(?:\.\d+)?|\([^)]*\))", r"(\1)**(1/3)"),
    (r"\bto\s+the\s+power(?:\s+of)?\b|\braised\s+to(?:\s+the\s+power)?(?:\s+of)?\b|\bpower\b", " ** "),
    (r"\bsquared\b", " **2 "),
    (r"\bcubed\b", " **3 "),
    (r"\bmultiplied\s+by\b|\bmultiply\s+by\b|\btimes\b|\binto\b|\bgunaa?\b", " * "),
    (r"\bdivided\s+by\b|\bdivide\s+by\b|\bover\b|\bbhaa?g\b", " / "),
    (r"\bplus\b|\badded\s+to\b|\badd\b|\bjodo?\b|\bjoda\b", " + "),
    (r"\bminus\b|\bsubtract(?:ed)?(?:\s+by)?\b|\bless\b|\bghata\w*\b|\btake\s+away\b", " - "),
    (r"\bmodulo\b|\bmod\b|\bremainder\b", " % "),
    (r"(?<=\d)\s*x\s*(?=\d)", " * "),
]
_FILLER = re.compile(
    r"\b(?:what|whats|what's|is|are|the|of|value|calculate|calculation|compute|solve|find|"
    r"please|pls|result|answer|equals?|equal|to|me|tell|evaluate|simplify|kya|hai|hota|hoga|"
    r"kitna|kitne|batao|bataiye|nikalo|karo|karna|do|how)\b", re.I)


def _clean_text(text: str) -> str:
    t = text.translate(_UNICODE_OPS).strip().lower()
    t = _CURRENCY_RE.sub(" ", t)
    if not re.search(r"[a-z]\(", t):  # keep commas that separate function arguments
        t = _GROUPED_NUM.sub(lambda m: m.group(0).replace(",", ""), t)

    def expand(m):
        key = m.group(2).rstrip("s") if m.group(2) not in ("cr", "bn", "mn") else m.group(2)
        return " " + format(Decimal(m.group(1)) * _MAGNITUDES.get(key, 1), "f") + " "

    t = _MAGNITUDE_RE.sub(expand, t)
    return re.sub(r"\s+", " ", t).strip()


def _pull_percent(t: str):
    m = _PERCENT_RE.search(t)
    if not m:
        return None, t
    return Decimal(m.group(1)), t[:m.start()] + " " + t[m.end():]


def _pull_time(t: str):
    """Returns ((value, 'years'|'months'|'days') or None, remaining text)."""
    m = _TIME_RE.search(t)
    if not m:
        return None, t
    u = m.group(2).lower()
    if u.startswith(("y", "saal", "sal", "varsh")):
        kind = "years"
    elif u.startswith(("mo", "mth", "mahin")):
        kind = "months"
    else:
        kind = "days"
    return (Decimal(m.group(1)), kind), t[:m.start()] + " " + t[m.end():]


def _pull_rate(t: str):
    pct, rest = _pull_percent(t)
    if pct is not None:
        return pct, rest
    m = re.search(rf"(?:\bat\b|@|\brate\b(?:\s+of)?(?:\s+interest)?)\s*({_NUM})", t)
    if m:
        return Decimal(m.group(1)), t[:m.start()] + " " + t[m.end():]
    return None, t


def _out(operation, steps, final, details, inputs, assumptions):
    return {"operation": operation, "steps": steps, "final": final, "details": details,
            "inputs": inputs, "assumptions": assumptions}


def _ok(query, extracted, out):
    return _jsonable({
        "status": "success", "operation": out["operation"], "query": query,
        "extracted_numbers": extracted, "inputs": out["inputs"],
        "assumptions": out["assumptions"], "breakdown": out["steps"],
        "final_result": out["final"], "details": out["details"], "error": None,
        "precision": PRECISION,
    })


def _err(query, message, operation=None):
    return {"status": "error", "operation": operation, "query": query, "extracted_numbers": [],
            "inputs": {}, "assumptions": [], "breakdown": [], "final_result": None,
            "details": {}, "error": message, "precision": PRECISION}


@_exact
def _handle_gst(low: str) -> dict:
    assumptions = []
    rate, rest = _pull_percent(low)
    if rate is None:
        m = re.search(rf"(?:\bat\b|@|\brate\b(?:\s+of)?)\s*({_NUM})", rest)
        if not m:
            m = re.search(rf"\bgst\s+({_NUM})\s+(?:on|for|to|from|par|pe)\b", rest)
        if m:
            rate = Decimal(m.group(1))
            rest = rest[:m.start()] + " " + rest[m.end():]
    if rate is None:
        rate = Decimal(18)
        assumptions.append("No GST rate found, so the common 18% slab was assumed.")
    nums = [Decimal(x) for x in _NUM_RE.findall(rest)]
    if not nums:
        raise InputError("could not find the amount to apply GST on")
    amount = nums[0]
    inclusive = bool(re.search(
        r"\b(?:inclusive|including|incl|included|remove|removing|extract|reverse|backward|"
        r"nikal\w*|hata\w*)\b", low))
    interstate = bool(re.search(r"\bigst\b|\binter[\s-]?state\b", low))
    gst_only = bool(re.search(
        r"\bgst\s+(?:on|of|amount)\b|\b(?:calculate|compute|find)\s+gst\b|\bhow\s+much\s+gst\b|"
        r"\bgst\s+kitna\b", low)) and not re.search(r"\b(?:add|plus|total|with|jod\w*)\b", low)
    res = gst_calculate(amount, rate, inclusive=inclusive, interstate=interstate)
    if gst_only:
        final, label = res["gst_amount"], "GST amount"
    elif inclusive:
        final, label = res["taxable_value"], "price without GST (taxable value)"
    else:
        final, label = res["total"], "total including GST"
    return _out("gst_remove" if inclusive else "gst_add", res["steps"], final,
                {"answer_is": label, **{k: v for k, v in res.items() if k != "steps"}},
                {"amount": amount, "rate_percent": rate, "amount_includes_gst": inclusive,
                 "inter_state": interstate}, assumptions)


@_exact
def _handle_interest(low: str, compound: bool) -> dict:
    assumptions = []
    time, rest = _pull_time(low)
    rate, rest = _pull_rate(rest)
    if rate is None:
        raise InputError("could not find the interest rate (for example '8%')")
    if time is None:
        raise InputError("could not find the time period (for example '5 years' or '18 months')")
    nums = [Decimal(x) for x in _NUM_RE.findall(rest)]
    if not nums:
        raise InputError("could not find the principal amount")
    principal = nums[0]
    value, kind = time
    years = value if kind == "years" else (value / 12 if kind == "months" else value / 365)
    if not compound:
        res = simple_interest(principal, rate, years)
        return _out("simple_interest", res["steps"], res["interest"],
                    {"answer_is": "simple interest", **{k: v for k, v in res.items() if k != "steps"}},
                    {"principal": principal, "rate_percent": rate, "years": years}, assumptions)
    n = None
    for pattern, freq in _FREQ_PATTERNS:
        if re.search(pattern, low):
            n = freq
            break
    if n is None:
        n = 1
        assumptions.append("No compounding frequency given, so annual compounding was assumed.")
    res = compound_interest(principal, rate, years, n)
    return _out("compound_interest", res["steps"], res["amount"],
                {"answer_is": "maturity amount (principal + compound interest)",
                 **{k: v for k, v in res.items() if k != "steps"}},
                {"principal": principal, "rate_percent": rate, "years": years,
                 "compounds_per_year": n}, assumptions)


@_exact
def _handle_emi(low: str, schedule: bool) -> dict:
    time, rest = _pull_time(low)
    rate, rest = _pull_rate(rest)
    if rate is None:
        raise InputError("could not find the loan interest rate (for example '9%')")
    if time is None:
        raise InputError("could not find the loan tenure (for example '20 years' or '240 months')")
    nums = [Decimal(x) for x in _NUM_RE.findall(rest)]
    if not nums:
        raise InputError("could not find the loan amount")
    value, kind = time
    months = value * 12 if kind == "years" else (value if kind == "months" else value / 30)
    res = emi_calculate(nums[0], rate, months, schedule=schedule)
    return _out("emi", res["steps"], res["emi"],
                {"answer_is": "monthly EMI", **{k: v for k, v in res.items() if k != "steps"}},
                {"loan_amount": nums[0], "annual_rate_percent": rate, "tenure_months": months}, [])


@_exact
def _handle_percent(low: str):
    nums = [Decimal(x) for x in _NUM_RE.findall(low)]
    st = _Steps()
    m = re.search(rf"({_NUM})\s*(?:%|percent\b|per\s*cent\b)\s*of\s*({_NUM})", low)
    if m:
        p, base = Decimal(m.group(1)), Decimal(m.group(2))
        res = base * p / 100
        st.add("Percentage of an amount = amount * percent / 100",
               f"{fmt(base)} * {fmt(p)} / 100", fmt(res))
        return _out("percentage_of", st.items, fmt(res), {"percent": p, "amount": base,
                    "result": fmt(res)}, {"percent": p, "amount": base}, [])
    if len(nums) >= 2 and re.search(
            r"\bwhat\s+(?:%|percent\w*|per\s*cent)|\bas\s+a\s+percent\w*\s+of\b|"
            r"\bkitn[ae]\s+(?:%|percent\w*)", low):
        flipped = re.search(r"\bwhat\s+(?:%|percent\w*|per\s*cent)\s+of\b", low)
        part, whole = (nums[1], nums[0]) if flipped else (nums[0], nums[1])
        res = what_percent(part, whole)
        st.add("Percent = part * 100 / whole", f"{fmt(part)} * 100 / {fmt(whole)}", fmt(res))
        return _out("what_percent", st.items, fmt(res) + "%", {"part": part, "whole": whole,
                    "percent": res}, {"part": part, "whole": whole}, [])
    m = re.search(rf"from\s+({_NUM})\s+to\s+({_NUM})", low)
    if m and re.search(r"percent|%|change|growth|increase|decrease|rise|fall|drop", low):
        old, new = Decimal(m.group(1)), Decimal(m.group(2))
        diff = new - old
        st.add("Change = new - old", f"{fmt(new)} - {fmt(old)}", fmt(diff))
        res = percent_change(old, new)
        st.add("Percent change = change * 100 / old", f"{_show(diff)} * 100 / {fmt(old)}", fmt(res))
        return _out("percent_change", st.items, fmt(res) + "%", {"old": old, "new": new,
                    "percent_change": res}, {"old": old, "new": new}, [])
    pct, rest = _pull_percent(low)
    if pct is not None:
        rest_nums = [Decimal(x) for x in _NUM_RE.findall(rest)]
        dec = re.search(r"\b(?:decrease|reduce|discount|off|less|minus|subtract|deduct|ghata\w*|kam)\b", low)
        inc = re.search(r"\b(?:increase|raise|hike|markup|mark\s+up|grow|add|plus|badha\w*|jod\w*)\b", low)
        if rest_nums and (dec or inc):
            amount = rest_nums[0]
            change = amount * pct / 100
            st.add("Change = amount * percent / 100", f"{fmt(amount)} * {fmt(pct)} / 100", fmt(change))
            if dec:
                res = amount - change
                st.add("New value = amount - change", f"{fmt(amount)} - {fmt(change)}", fmt(res))
                op = "decrease_by_percent"
            else:
                res = amount + change
                st.add("New value = amount + change", f"{fmt(amount)} + {fmt(change)}", fmt(res))
                op = "increase_by_percent"
            return _out(op, st.items, fmt(res), {"original": amount, "percent": pct,
                        "change": change, "new_value": res}, {"amount": amount, "percent": pct}, [])
    return None


def _check_words(text: str):
    bad = []
    for kind, tok in _tokenize(text):
        if kind == "name" and tok.lower() not in FUNCTIONS and tok not in CONSTANTS and len(tok) > 1:
            bad.append(tok)
    if bad:
        raise InputError("could not understand: " + ", ".join(sorted(set(bad))))


@_exact
def _handle_math(low: str) -> dict:
    s = low
    for pattern, repl in _WORD_OPS:
        s = re.sub(pattern, repl, s)
    s = s.replace("?", " ")
    s = _FILLER.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\s=.,]+$", "", s).strip()
    if not s:
        raise InputError("no calculation found in the query")
    if not _ALLOWED_CHARS.match(s):
        raise InputError("the query contains symbols that cannot be calculated")
    _check_words(s.replace("=", " "))
    if s.count("=") == 1:
        sol = solve_equation(s)
        det = {k: v for k, v in sol.items() if k not in ("steps", "equation")}
        return _out("solve_equation", sol["steps"], sol["final"], det, {"equation": s}, [])
    if "=" in s:
        raise InputError("only one '=' sign is allowed in an equation")
    res = evaluate_expression(s)
    return _out("arithmetic_expression", res["steps"], fmt(res["value"]),
                {"expression": res["normalized"], "value": res["value"],
                 "rounded_10dp": res["rounded_10dp"], "operators_detected": res["operators"]},
                {"expression": s}, [])


def _route(low: str, schedule: bool) -> dict:
    if re.search(r"\bemi\b|\binstal+ments?\b|\bmonthly\s+(?:payment|instal+ment)", low):
        return _handle_emi(low, schedule)
    if re.search(r"\bcompound\w*|\bchakra\w*", low):
        return _handle_interest(low, True)
    if re.search(r"\binterest\b|\bvyaj\b|\bbyaj\b|\bsood\b", low):
        return _handle_interest(low, False)
    if re.search(r"\b(?:gst|cgst|sgst|igst)\b", low):
        return _handle_gst(low)
    out = _handle_percent(low)
    if out is not None:
        return out
    if "ledger" in low or "journal" in low:
        raise InputError("ledger and journal checks need structured data: use "
                         "ledger_balance() or verify_journal() directly")
    return _handle_math(low)


def solve_query(query: str, include_schedule: bool = False) -> dict:
    """Natural-language calculation -> structured, JSON-safe dict.

    Understands: GST add/remove, simple & compound interest, EMI, percentages
    (of, what percent, change, increase/decrease), plain arithmetic with words
    ("12 times 8 plus 5"), exponents/roots, and linear/quadratic equations.
    Also understands lakh/crore/k, Indian digit grouping, Rs/INR symbols and a
    few Hinglish words. The query is lower-cased, so use lower-case variables.
    Never raises: failures come back as {"status": "error", ...}.
    """
    try:
        if not isinstance(query, str) or not query.strip():
            raise InputError("query must be a non-empty string")
        if len(query) > MAX_QUERY_LEN:
            raise InputError(f"query too long (max {MAX_QUERY_LEN} characters)")
        with localcontext(CTX):
            low = _clean_text(query)
            extracted = [fmt(Decimal(x)) for x in _NUM_RE.findall(low)]
            out = _route(low, include_schedule)
        return _ok(query, extracted, out)
    except MathCoreError as exc:
        return _err(query, str(exc))
    except (ArithmeticError, ValueError, TypeError, RecursionError, MemoryError) as exc:
        return _err(query, f"calculation failed ({exc.__class__.__name__}): {exc}")
    except Exception as exc:  # last-resort guard so the agent loop never crashes
        return _err(query, f"internal error ({exc.__class__.__name__}): {exc}")


def solve_query_json(query: str, include_schedule: bool = False, indent: int = 2) -> str:
    """Same as solve_query() but returns a JSON string."""
    return json.dumps(solve_query(query, include_schedule), indent=indent, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 8. Self-test and command line
# --------------------------------------------------------------------------- #

def run_selftest() -> int:
    """Runs built-in checks. Returns 0 when everything passes."""
    def ev(s):
        return fmt(safe_eval(s))

    def rejected(s):
        try:
            safe_eval(s)
        except MathCoreError:
            return True
        return False

    def q(text):
        return solve_query(text)

    gst_a = gst_calculate("1000", 18)
    gst_b = gst_calculate("1180", 18, inclusive=True)
    ci = compound_interest(10000, 8, 5, 1)
    emi = emi_calculate(500000, 9, 240)
    led = ledger_balance([("Sale", 0, "100.10"), ("Rent", "40.05", 0)], 0, "credit")
    jrn = verify_journal([{"lines": [("Cash", "100.10", 0), ("Sales", 0, "100.10")]}])
    bad_jrn = verify_journal([{"lines": [("Cash", 100, 0), ("Sales", 0, 99)]}])
    quad = solve_equation("x^2 - 5x + 6 = 0")

    checks = [
        ("0.1 + 0.2 == 0.3 exactly", ev("0.1 + 0.2") == "0.3"),
        ("order of operations", ev("2 + 3 * 4 ^ 2") == "50"),
        ("right-associative power", ev("2 ** 3 ** 2") == "512"),
        ("unary minus vs power", ev("-2 ** 2") == "-4"),
        ("implicit multiplication", ev("2(3 + 4)") == "14"),
        ("1/3 to 28 digits", ev("1/3") == "0." + "3" * 28),
        ("sqrt/exp/functions", ev("sqrt(16) + max(1, 7) + round(2.675, 2)") == "13.68"),
        ("python-style modulo", ev("-7 % 3") == "2" and ev("7 // 2") == "3"),
        ("block attribute escape", rejected("(1).__class__")),
        ("block import", rejected("__import__('os').system('ls')")),
        ("block unknown function", rejected("open(1)")),
        ("block huge power", rejected("9 ** 9 ** 9")),
        ("block division by zero", rejected("1 / 0")),
        ("GST exclusive", gst_a["total"] == Decimal("1180.00") and gst_a["gst_amount"] == Decimal("180.00")),
        ("GST inclusive", gst_b["taxable_value"] == Decimal("1000.00") and gst_b["gst_amount"] == Decimal("180.00")),
        ("GST split balances", gst_a["cgst"] + gst_a["sgst"] == gst_a["gst_amount"]),
        ("compound interest", ci["amount"] == Decimal("14693.28")),
        ("EMI amortises to zero", emi["emi"] == Decimal("4498.63") and emi_calculate(1200, 12, 12, True)["schedule"][-1]["closing_balance"] == 0),
        ("ledger running balance", led["closing_balance"] == Decimal("60.05") and led["closing_balance_side"] == "Cr"),
        ("journal balanced", jrn["balanced"] is True),
        ("journal imbalance found", bad_jrn["balanced"] is False and bad_jrn["difference"] == Decimal("1.00")),
        ("linear equation", solve_equation("2x + 3 = 11")["final"] == "x = 4"),
        ("quadratic equation", quad["final"] == "x = 2 or x = 3"),
        ("NL: GST add", q("add 18% GST to 1000")["final_result"] == "1180.00"),
        ("NL: GST remove", q("remove 18% gst from 1180")["final_result"] == "1000.00"),
        ("NL: percent of", q("what is 15% of 2400")["final_result"] == "360"),
        ("NL: words to maths", q("12 times 8 plus 5")["final_result"] == "101"),
        ("NL: lakh + EMI", q("EMI for 5 lakh at 9% for 20 years")["status"] == "success"),
        ("NL: equation", q("solve 2x + 3 = 11")["final_result"] == "x = 4"),
        ("NL: bad input is an error, not a crash", q("tell me a joke")["status"] == "error"),
    ]
    failed = 0
    for label, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + label)
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        return run_selftest()
    if argv:
        print(solve_query_json(" ".join(argv)))
        return 0
    print("AutoMind math core - type a calculation (blank line or 'exit' to quit).")
    while True:
        try:
            line = input("math> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line or line.lower() in ("exit", "quit", "q"):
            return 0
        print(solve_query_json(line))


if __name__ == "__main__":
    sys.exit(main())

"""scripts/tools.py — deterministic tool implementations for benchmark 03 (tool-using agent).

The two tools below are the agent's surface for benchmark
``03-tool-using-agent``. Both implementations are **fully deterministic** and
**network-free**:

* ``calculator(expr)`` evaluates a numeric arithmetic expression via a tiny
  AST-walking interpreter built on Python's :mod:`decimal` module. The
  whitelist of node types and the lack of attribute access make the path safe
  to expose to model-generated input.
* ``web_search(query)`` returns the canned value for an exact-match key from a
  pre-loaded JSON file (``benchmarks/03-tool-using-agent/search_kb.json``).
  Cache misses surface as the fixed string ``"no results"`` so the model has
  an opportunity to recover.

Determinism is the methodology contract: a tool-loop benchmark whose tool
results depend on wall-clock or live network state cannot be replayed; the
analyzer's byte-stable invariant would be impossible to satisfy. The two
implementations here trade expressiveness for reproducibility.

The ``TOOL_REGISTRY`` dict is the single source of truth that the runner
(``scripts.run_benchmark`` tool-loop branch) and the tests consume.
"""

from __future__ import annotations

import ast
import decimal
import json
import operator
import pathlib
from collections.abc import Callable
from typing import Any

__all__ = [
    "TOOL_REGISTRY",
    "CalculatorError",
    "WebSearchError",
    "calculator",
    "load_search_kb",
    "make_web_search",
    "web_search",
]


class CalculatorError(ValueError):
    """Raised when the calculator cannot evaluate the expression."""


class WebSearchError(ValueError):
    """Raised when the web_search call is malformed (missing query string)."""


# ----------------------------------------------------------------------------
# Calculator
# ----------------------------------------------------------------------------

# A whitelist of safe operations: every node type used by the AST evaluator
# below must appear here. Anything else raises ``CalculatorError``.
_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}

_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_DEC_CONTEXT = decimal.Context(prec=40)


def _to_decimal(x: Any) -> decimal.Decimal:
    if isinstance(x, decimal.Decimal):
        return x
    if isinstance(x, int):
        return decimal.Decimal(x)
    if isinstance(x, float):
        return decimal.Decimal(str(x))
    raise CalculatorError(f"cannot convert {x!r} to Decimal")


def _fn_sqrt(x: decimal.Decimal) -> decimal.Decimal:
    if x < 0:
        raise CalculatorError(f"sqrt of negative number: {x}")
    return _DEC_CONTEXT.sqrt(x)


def _fn_abs(x: decimal.Decimal) -> decimal.Decimal:
    return abs(x)


def _fn_round(x: decimal.Decimal, n: int = 0) -> decimal.Decimal:
    quant = decimal.Decimal(10) ** -int(n)
    return x.quantize(quant, rounding=decimal.ROUND_HALF_EVEN)


_FUNCS: dict[str, Callable[..., decimal.Decimal]] = {
    "sqrt": _fn_sqrt,
    "abs": _fn_abs,
    "round": _fn_round,
}


def _eval(node: ast.AST) -> decimal.Decimal:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return _to_decimal(node.value)
        raise CalculatorError(
            f"unsupported constant {node.value!r}; only numbers are allowed"
        )
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BINOPS:
            raise CalculatorError(
                f"unsupported binary operator {op_type.__name__}"
            )
        left = _eval(node.left)
        right = _eval(node.right)
        return _to_decimal(_BINOPS[op_type](left, right))
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARYOPS:
            raise CalculatorError(
                f"unsupported unary operator {op_type.__name__}"
            )
        return _to_decimal(_UNARYOPS[op_type](_eval(node.operand)))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise CalculatorError(
                "tool calls must use a bare function name, not attribute access"
            )
        fname = node.func.id
        if fname not in _FUNCS:
            raise CalculatorError(
                f"unknown function {fname!r}; allowed: {sorted(_FUNCS)}"
            )
        args = [_eval(a) for a in node.args]
        return _to_decimal(_FUNCS[fname](*args))
    raise CalculatorError(f"unsupported AST node {type(node).__name__}")


def calculator(expr: str) -> str:
    """Evaluate one arithmetic expression and return the result as a string.

    The result string uses :func:`decimal.Decimal` formatting:
    integer-valued Decimals render without a trailing ``.0`` only when the
    expression itself was a pure integer expression; in all other cases the
    Decimal's natural string form is returned (matching what a model would
    paste as the final answer).

    Args:
        expr: A single arithmetic expression. Supported operators: ``+ - *
            / ** % //``. Supported functions: ``sqrt(x)``, ``abs(x)``,
            ``round(x, n)``.

    Returns:
        Result rendered as a string.

    Raises:
        CalculatorError: Parser or evaluator rejected the expression.
    """
    if not isinstance(expr, str):
        raise CalculatorError(f"expr must be a string; got {type(expr).__name__}")
    expr = expr.strip()
    if not expr:
        raise CalculatorError("expr is empty")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"syntax error: {exc.msg}") from exc

    with decimal.localcontext(_DEC_CONTEXT):
        try:
            result = _eval(tree)
        except decimal.InvalidOperation as exc:
            raise CalculatorError(f"decimal invalid operation: {exc}") from exc
        except ZeroDivisionError as exc:
            raise CalculatorError("division by zero") from exc

    # Normalize: drop trailing zeros for cleaner display while preserving
    # decimal precision when the result has a fractional part.
    if result == result.to_integral_value():
        return str(result.to_integral_value())
    return str(result.normalize())


# ----------------------------------------------------------------------------
# Web search (canned KB lookup)
# ----------------------------------------------------------------------------


def load_search_kb(path: str | pathlib.Path) -> dict[str, str]:
    """Load the JSON key→value KB at ``path``.

    Args:
        path: Filesystem path to a JSON object mapping query strings to
            their canned response values.

    Returns:
        Dict of query → response (both strings).

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: The JSON shape is not an object of strings.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"search KB not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"search KB must be a JSON object; got {type(raw).__name__}")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            raise ValueError(f"search KB keys must be strings; got {k!r}")
        out[k] = "" if v is None else str(v)
    return out


_NO_RESULTS_TOKEN = "no results"


def make_web_search(kb: dict[str, str]) -> Callable[[str], str]:
    """Build a ``web_search(query)`` callable bound to ``kb``.

    Exact-match semantics: the model's ``query`` string must match a key
    byte-identically. Any other input returns the literal string
    ``"no results"``.
    """

    def _web_search(query: str) -> str:
        if not isinstance(query, str):
            raise WebSearchError(
                f"query must be a string; got {type(query).__name__}"
            )
        return kb.get(query.strip(), _NO_RESULTS_TOKEN)

    return _web_search


# A default, KB-less web_search used by smoke tests and by the runner before
# the KB is loaded. It always returns "no results" so a model that calls
# web_search without a loaded KB gets an explicit cache-miss signal rather
# than silently succeeding.
def web_search(query: str) -> str:
    """Stub web_search that always returns ``"no results"``.

    Production callers should construct a bound callable via
    :func:`make_web_search` with the loaded KB and register it into the
    runner's per-cell tool dispatch table.
    """
    if not isinstance(query, str):
        raise WebSearchError(
            f"query must be a string; got {type(query).__name__}"
        )
    return _NO_RESULTS_TOKEN


# ----------------------------------------------------------------------------
# Tool registry
# ----------------------------------------------------------------------------

# The runner consumes this registry to map a tool name (from the model's tool
# call) to the actual callable. New tools must be added here AND have a JSON
# schema under ``benchmarks/03-tool-using-agent/prompts/tool_schemas/``.
TOOL_REGISTRY: dict[str, Callable[..., str]] = {
    "calculator": calculator,
    "web_search": web_search,
}

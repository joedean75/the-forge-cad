"""Global parameters & equations (roadmap production-feature 03).

Named driving variables — ``width = 100``, ``hole_d = width / 10`` — that
propagate into feature parameters, the backbone of *design intent*. A feature's
numeric field may be an **expression string** (``"=width"``, ``"=width/10 + 2"``)
resolved against the parameter table at evaluation time; the stored tree keeps
the expression, only the evaluated copy carries numbers (like datum baking).

Units: length parameters (``mm``) and length expressions resolve to **metres**
(the kernel's unit), so ``"=width"`` on a box ``size.x`` is directly correct.
``deg`` / ``count`` / ``ratio`` parameters resolve to their own value.

Evaluation is a restricted-AST arithmetic evaluator — no name lookups beyond the
parameter table, no calls beyond a small whitelist — so a document can never
execute arbitrary code.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Dict, List, Tuple
from app.cad_kernel.core.feature_tree import node_children

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "min": min, "max": max, "abs": abs, "round": round,
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "pi": math.pi, "floor": math.floor, "ceil": math.ceil,
}


class ParameterError(ValueError):
    pass


def _eval_node(node: ast.AST, scope: Dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, scope)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ParameterError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id in scope:
            return float(scope[node.id])
        if node.id in _FUNCS and not callable(_FUNCS[node.id]):
            return float(_FUNCS[node.id])  # pi
        raise ParameterError(f"unknown parameter '{node.id}'")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval_node(node.left, scope), _eval_node(node.right, scope))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval_node(node.operand, scope))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = _FUNCS.get(node.func.id)
        if not callable(fn):
            raise ParameterError(f"unknown function '{node.func.id}'")
        return float(fn(*[_eval_node(a, scope) for a in node.args]))
    raise ParameterError("unsupported expression")


def evaluate_expr(expr: str, scope: Dict[str, float]) -> float:
    expr = expr.strip()
    if expr.startswith("="):
        expr = expr[1:]
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ParameterError(f"invalid expression '{expr}': {exc}")
    return _eval_node(tree, scope)


def resolve_values(params: List, overrides: Dict[str, str] | None = None) -> Tuple[Dict[str, float], List[dict]]:
    """Resolve the ordered parameter list into a name→value scope (SI units).

    ``params`` is a list of DocumentParameter (or dicts). Length (``mm``) values
    are converted to metres; angles/counts/ratios pass through. ``overrides`` (a
    configuration, feature 07) swaps a parameter's expression by name before
    evaluation. Returns the scope plus the parameter dicts with ``value`` cached.
    """
    overrides = overrides or {}
    scope: Dict[str, float] = {}
    resolved: List[dict] = []
    for p in params:
        pd = p if isinstance(p, dict) else p.model_dump()
        name = pd.get("name")
        if not name:
            continue
        expr = str(overrides[name]) if name in overrides else str(pd.get("expr", "0"))
        val = evaluate_expr(expr, scope)
        unit = pd.get("unit", "mm")
        scope[name] = val / 1000.0 if unit == "mm" else val
        pd["value"] = round(scope[name] * 1000.0, 6) if unit == "mm" else round(scope[name], 6)
        resolved.append(pd)
    return scope, resolved


def _resolve_leaf(value, scope: Dict[str, float]):
    if isinstance(value, str) and (value.startswith("=") or _looks_expr(value)):
        return evaluate_expr(value, scope)
    if isinstance(value, dict):
        return {k: _resolve_leaf(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_leaf(v, scope) for v in value]
    return value


def _looks_expr(value: str) -> bool:
    # A bare string that references a known scope name (e.g. "width/2") — but not
    # plain identifiers used as enums ("union", "xy"): require an operator/paren.
    return any(ch in value for ch in "+-*/()") and any(c.isalpha() for c in value)


def _declared_expression(value) -> bool:
    """Whether anything in here is explicitly an expression (`"=width/2"`)."""
    if isinstance(value, str):
        return value.startswith("=")
    if isinstance(value, dict):
        return any(_declared_expression(v) for k, v in value.items() if k != "children")
    if isinstance(value, list):
        return any(_declared_expression(v) for v in value)
    return False


def has_expressions(tree: dict) -> bool:
    """Does this tree bind any parameter expression at all?

    Lets the common case — a document with no parameters and no bindings — skip
    the walk and the copy, while a document that *does* bind one is still
    resolved (and told when the parameter is missing) even with an empty table.
    """
    def walk(node: dict) -> bool:
        if _declared_expression(node.get("params") or {}):
            return True
        return any(walk(child) for child in node_children(node))

    root = tree.get("root")
    return bool(isinstance(root, dict) and walk(root))


def resolve_tree(tree: dict, scope: Dict[str, float]) -> dict:
    """Return a copy of the feature tree with expression-string params evaluated.

    Nothing to do — no parameters *and* no bindings — and the tree comes back
    untouched, so a document that never uses parameters pays nothing.

    **A declared expression that does not resolve raises.** It used to be
    swallowed, to protect enum strings like `"union"` from being read as
    expressions: the two are told apart by the leading `=` instead, because
    swallowing it left the *string* in the tree, and it reached the kernel as
    `unsupported operand type(s) for /: 'str' and 'float'` — a Python error
    naming neither the feature nor the parameter. `derived.error` carries the
    typed message instead, which is the supported "bound before defined" state.
    """
    if not scope and not has_expressions(tree):
        return tree
    import copy

    out = copy.deepcopy(tree)

    def walk(node: dict) -> None:
        params = node.get("params")
        if isinstance(params, dict):
            for k, v in list(params.items()):
                if k == "children":
                    continue
                if _declared_expression(v):
                    params[k] = _resolve_leaf(v, scope)   # raises, named, typed
                    continue
                try:
                    params[k] = _resolve_leaf(v, scope)
                except ParameterError:
                    pass  # a bare string that is an enum, not an expression
            for child in node_children(node):
                walk(child)

    root = out.get("root")
    if isinstance(root, dict):
        walk(root)
    return out

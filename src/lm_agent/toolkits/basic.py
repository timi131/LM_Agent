"""Базовые инструменты: время и калькулятор."""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime

from ..tools import ToolRegistry

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {name: getattr(math, name) for name in ("sqrt", "sin", "cos", "tan", "log", "log10", "exp", "floor", "ceil")}
_FUNCS.update(abs=abs, round=round)
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("слишком большая степень")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError(f"недопустимое выражение: {ast.dump(node)[:80]}")


def register(registry: ToolRegistry) -> None:
    @registry.register
    def get_current_time() -> str:
        """Возвращает текущие локальные дату и время."""
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @registry.register
    def calculate(expression: str) -> str:
        """Вычисляет арифметическое выражение (+ - * / // % **, sqrt, sin, log, pi, e...).

        expression: выражение, например "sqrt(2) * (3 + 4)"
        """
        return str(_eval(ast.parse(expression, mode="eval")))

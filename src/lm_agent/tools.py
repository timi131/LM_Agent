"""Реестр инструментов: обычные Python-функции превращаются в tool-схемы Ollama."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Union, get_type_hints

# Нужно ли подтверждение: всегда/никогда или в зависимости от аргументов вызова.
Confirm = Union[bool, Callable[[dict], bool]]
# Спрашивает пользователя, можно ли выполнить вызов: (имя, аргументы) → да/нет.
Approver = Callable[[str, dict], bool]

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]
    parameters: dict
    confirm: Confirm = False

    def needs_confirm(self, arguments: dict) -> bool:
        return self.confirm(arguments) if callable(self.confirm) else bool(self.confirm)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Первая часть докстринга — описание, строки вида `name: текст` — описания аргументов."""
    summary, params = [], {}
    for line in inspect.cleandoc(doc or "").splitlines():
        key, sep, text = line.strip().partition(":")
        if sep and key.isidentifier() and text.strip():
            params[key] = text.strip()
        elif line.strip() and not params:
            summary.append(line.strip())
    return " ".join(summary), params


def build_tool(func: Callable[..., Any], name: str | None = None, confirm: Confirm = False) -> Tool:
    description, arg_docs = _parse_docstring(func.__doc__ or "")
    hints = get_type_hints(func)
    properties, required = {}, []
    for pname, param in inspect.signature(func).parameters.items():
        prop = {"type": _JSON_TYPES.get(hints.get(pname, str), "string")}
        if pname in arg_docs:
            prop["description"] = arg_docs[pname]
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return Tool(
        name=name or func.__name__,
        description=description,
        func=func,
        parameters={"type": "object", "properties": properties, "required": required},
        confirm=confirm,
    )


class ToolRegistry:
    def __init__(self, approver: Approver | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # Без approver опасные инструменты запрещены — безопасное поведение по умолчанию.
        self.approver = approver

    def register(self, func: Callable[..., Any] | None = None, *, name: str | None = None, confirm: Confirm = False):
        """`@registry.register`, `@registry.register(confirm=True)` или `registry.register(func)`."""

        def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
            tool = build_tool(f, name, confirm)
            self._tools[tool.name] = tool
            return f

        return wrap(func) if func is not None else wrap

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, arguments: dict | str | None) -> str:
        """Выполняет инструмент. Ошибки возвращаются модели текстом, чтобы она могла исправиться."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Ошибка: неизвестный инструмент '{name}'. Доступны: {', '.join(self.names())}"
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                return f"Ошибка: аргументы не являются JSON: {e}"
        arguments = arguments or {}
        if tool.needs_confirm(arguments) and not (self.approver and self.approver(name, arguments)):
            return "Отклонено пользователем: действие не выполнено. Не повторяй его без нового запроса."
        try:
            result = tool.func(**arguments)
        except Exception as e:  # noqa: BLE001 — модель должна увидеть любую ошибку
            return f"Ошибка при выполнении {name}: {type(e).__name__}: {e}"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

"""Цикл агента: модель → вызовы инструментов → результаты → модель … → итоговый ответ."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import Settings
from .tools import ToolRegistry


class ChatClient(Protocol):
    def chat(self, *, model: str, messages: list[dict], tools: list[dict], options: dict) -> Any: ...


# Результаты этих инструментов — снимки состояния (страницы браузера). Актуален только последний,
# старые занимают контекст, поэтому их сокращаем.
SNAPSHOT_TOOLS = ("browser_",)
STALE_NOTE = "\n…(устаревший снимок страницы сокращён; актуальный — ниже или через browser_snapshot)"


@dataclass
class ToolEvent:
    name: str
    arguments: dict
    result: str


def _as_dict(obj: Any) -> dict:
    """Ответ ollama — pydantic-модель; тестовые клиенты могут вернуть обычный dict."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return obj


@dataclass
class Agent:
    client: ChatClient
    tools: ToolRegistry
    settings: Settings
    on_tool: Callable[[ToolEvent], None] | None = None
    # Дополнительный контекст для системного промпта (например, долговременная память).
    context: Callable[[], str] | None = None
    messages: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        system = self.settings.system_prompt
        extra = self.context() if self.context else ""
        if extra:
            system += "\n\n" + extra
        self.messages = [{"role": "system", "content": system}]

    def ask(self, prompt: str) -> str:
        self.messages.append({"role": "user", "content": prompt})
        options = {"temperature": self.settings.temperature, "num_ctx": self.settings.num_ctx, **self.settings.options}

        for _ in range(self.settings.max_steps):
            self._compact_snapshots()
            response = _as_dict(
                self.client.chat(
                    model=self.settings.model,
                    messages=self.messages,
                    tools=self.tools.schemas(),
                    options=options,
                )
            )
            message = response.get("message", {})
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []

            self.messages.append({"role": "assistant", "content": content, **({"tool_calls": tool_calls} if tool_calls else {})})
            if not tool_calls:
                return content

            for call in tool_calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                result = self.tools.call(name, args)
                if self.on_tool:
                    self.on_tool(ToolEvent(name, args if isinstance(args, dict) else {"raw": args}, result))
                self.messages.append({"role": "tool", "content": result, "tool_name": name})

        return f"Остановлено: превышен лимит шагов ({self.settings.max_steps}) без итогового ответа."

    def _compact_snapshots(self) -> None:
        snapshots = [
            m for m in self.messages
            if m["role"] == "tool" and m.get("tool_name", "").startswith(SNAPSHOT_TOOLS)
            and not m["content"].endswith(STALE_NOTE)
        ]
        for m in snapshots[:-1]:
            m["content"] = "\n".join(m["content"].splitlines()[:2]) + STALE_NOTE

"""Общее ядро для всех интерфейсов: один набор инструментов и сессии агента.

Веб-чат, Telegram-бот и задачи по расписанию обращаются к агенту через Hub.ask(сессия, текст, канал).
Канал — объект с методами send(payload) и approve(name, arguments): через него агент сообщает
о вызовах инструментов и спрашивает подтверждение у того, кто сейчас с ним говорит.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any, Protocol

import ollama

from .agent import Agent, ToolEvent
from .config import Settings
from .toolkits import ALL, build_registry, make_browser, make_context


class ChannelLike(Protocol):
    def send(self, payload: dict) -> None: ...
    def approve(self, name: str, arguments: dict) -> bool: ...


class Busy(RuntimeError):
    """Агент уже отвечает в этой сессии."""


class Collector:
    """Канал без человека (HTTP API, расписание): собирает вызовы инструментов,
    подтверждения — только если включён LM_AGENT_AUTO_APPROVE."""

    def __init__(self, auto_approve: bool) -> None:
        self.auto_approve = auto_approve
        self.events: list[dict] = []

    def send(self, payload: dict) -> None:
        if payload.get("type") != "delta":  # кусочки текста не нужны — нужен итог целиком
            self.events.append(payload)

    def approve(self, name: str, arguments: dict) -> bool:
        return self.auto_approve


# Канал текущего запроса. Устанавливается в потоке, где работает агент, поэтому инструменты
# и подтверждения сами находят, кому отвечать.
current_channel: contextvars.ContextVar[ChannelLike | None] = contextvars.ContextVar("channel", default=None)


def error_text(e: Exception, settings: Settings) -> str:
    if isinstance(e, ollama.ResponseError):
        return f"Ошибка Ollama: {e.error}. Модель скачана? ollama pull {settings.model}"
    if isinstance(e, ConnectionError):
        return f"Нет связи с Ollama по адресу {settings.host}. Запущен ли ollama serve?"
    return f"{type(e).__name__}: {e}"


class Hub:
    def __init__(self, settings: Settings, toolkits: tuple[str, ...] = ALL, client: Any = None) -> None:
        self.settings = settings
        self.toolkits = toolkits
        self.client = client or ollama.Client(host=settings.host)
        self.browser = make_browser(settings) if "browser" in toolkits else None
        self.registry, self.memory = build_registry(
            settings, self.client, self._approve, toolkits, browser_instance=self.browser,
        )
        self.sessions: dict[str, Agent] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # --- связь агента с каналом ---

    def _approve(self, name: str, arguments: dict) -> bool:
        if self.settings.auto_approve:
            return True
        channel = current_channel.get()
        return channel.approve(name, arguments) if channel else False

    @staticmethod
    def _on_tool(event: ToolEvent) -> None:
        channel = current_channel.get()
        if channel:
            channel.send({"type": "tool", "name": event.name, "arguments": event.arguments, "result": event.result})

    @staticmethod
    def _on_token(text: str) -> None:
        channel = current_channel.get()
        if channel:
            channel.send({"type": "delta", "text": text})

    # --- сессии ---

    def agent(self, session_id: str) -> Agent:
        with self._guard:
            if session_id not in self.sessions:
                self.sessions[session_id] = Agent(
                    client=self.client, tools=self.registry, settings=self.settings,
                    on_tool=self._on_tool, on_token=self._on_token,
                    context=make_context(self.settings, self.memory, self.toolkits),
                )
                self._locks[session_id] = threading.Lock()
            return self.sessions[session_id]

    def busy(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        return bool(lock and lock.locked())

    def ask(self, session_id: str, text: str, channel: ChannelLike | None = None, fresh: bool = False) -> str:
        """Синхронный запрос к агенту. fresh=True — начать сессию с чистого листа (для расписания)."""
        agent = self.agent(session_id)
        lock = self._locks[session_id]
        if not lock.acquire(blocking=False):
            raise Busy("Агент ещё отвечает в этой сессии — дождитесь ответа")
        token = current_channel.set(channel)
        try:
            if fresh:
                agent.reset()
            return agent.ask(text)
        finally:
            current_channel.reset(token)
            lock.release()

    def reset(self, session_id: str) -> None:
        if session_id in self.sessions and not self.busy(session_id):
            self.sessions[session_id].reset()

    def warmup(self) -> None:
        try:
            self.agent("_warmup").warmup()
        except Exception:  # noqa: BLE001 — Ollama может быть ещё не запущен; это не ошибка
            pass
        with self._guard:
            self.sessions.pop("_warmup", None)
            self._locks.pop("_warmup", None)

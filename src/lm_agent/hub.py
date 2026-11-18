"""Общее ядро для всех интерфейсов: один набор инструментов и сессии агента.

Веб-чат, Telegram-бот и задачи по расписанию обращаются к агенту через Hub.ask(сессия, текст, канал).
Канал — объект с методами send(payload) и approve(name, arguments): через него агент сообщает
о вызовах инструментов и спрашивает подтверждение у того, кто сейчас с ним говорит.
"""

from __future__ import annotations

import contextvars
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import ollama
import httpx

from .agent import Agent, ToolEvent
from .config import Settings
from .models import ExternalModelStore, OpenAICompatibleClient
from .speech import make_transcriber
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


class EventLog:
    """Журнал агента: одна строка JSON на событие. При 5 МБ файл сдвигается в agent.log.1."""

    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, session: str, event: str, data: dict) -> None:
        line = json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "session": session, "event": event, **data},
                          ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                self.path.replace(self.path.with_suffix(".log.1"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(l) for l in lines if l.strip()]


class Hub:
    def __init__(self, settings: Settings, toolkits: tuple[str, ...] = ALL, client: Any = None) -> None:
        self.settings = settings
        self.toolkits = toolkits
        self.local_client = client or ollama.Client(host=settings.host)
        self.client = self.local_client
        self.model_store = ExternalModelStore(settings.data_dir / "external_models.json")
        self.active_external: dict | None = None
        self.browser = make_browser(settings) if "browser" in toolkits else None
        self.registry, self.memory = build_registry(
            settings, self.local_client, self._approve, toolkits, browser_instance=self.browser,
        )
        # распознавание речи — одно на веб-чат и Telegram (модель в памяти одна)
        self.transcriber = make_transcriber(settings)
        self.log = EventLog(settings.data_dir / "logs" / "agent.log")
        self.sessions: dict[str, Agent] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def local_models(self) -> list[dict]:
        try:
            response = self.local_client.list()
            data = response.model_dump() if hasattr(response, "model_dump") else response
            models = data.get("models", []) if isinstance(data, dict) else []
            return [{"id": m.get("model") or m.get("name", ""), "name": m.get("name") or m.get("model", ""),
                     "kind": "local"} for m in models if m]
        except Exception:
            try:
                data = httpx.get(f"{self.settings.host.rstrip('/')}/api/tags", timeout=3).json()
                return [{"id": m["name"], "name": m["name"], "kind": "local"}
                        for m in data.get("models", [])]
            except Exception:
                return []

    def external_models(self) -> list[dict]:
        return [{
            "id": item["id"],
            "name": item.get("name") or item["id"],
            "kind": "external",
            "base_url": item["base_url"],
            "model": item["model"],
        } for item in self.model_store.load()]

    def models(self) -> dict:
        return {"active": self.settings.model, "local": self.local_models(), "external": self.external_models()}

    def add_external_model(self, name: str, base_url: str, model: str, api_key: str = "") -> dict:
        name = name.strip()
        base_url = base_url.strip().rstrip("/")
        model = model.strip()
        if not name or not base_url or not model:
            raise ValueError("name, base_url и model обязательны")
        if base_url.endswith("/chat/completions"):
            base_url = base_url[:-len("/chat/completions")]
        items = self.model_store.load()
        item = next((x for x in items if x["model"] == model and x["base_url"] == base_url), None)
        if item:
            item["name"] = name
            if api_key:
                item["api_key"] = api_key
        else:
            item = {"id": f"external-{len(items) + 1}", "name": name, "base_url": base_url,
                    "model": model, "api_key": api_key}
            items.append(item)
        self.model_store.save(items)
        return {"id": item["id"], "name": item["name"], "kind": "external",
                "base_url": base_url, "model": model}

    def delete_external_model(self, model_id: str) -> bool:
        items = self.model_store.load()
        kept = [item for item in items if item["id"] != model_id]
        if len(kept) == len(items):
            return False
        self.model_store.save(kept)
        if self.active_external and self.active_external["id"] == model_id:
            locals = self.local_models()
            fallback = locals[0]["id"] if locals else "qwen2.5:7b"
            self.select_local_model(fallback)
        return True

    def select_local_model(self, model: str) -> None:
        model = model.strip()
        if not model:
            raise ValueError("Модель не задана")
        self.settings.model = model
        self.client = self.local_client
        self.active_external = None
        with self._guard:
            for agent in self.sessions.values():
                agent.settings.model = model
                agent.client = self.local_client

    def select_external_model(self, model_id: str) -> dict:
        item = next((x for x in self.model_store.load() if x["id"] == model_id), None)
        if not item:
            raise KeyError("Внешняя модель не найдена")
        self.active_external = item
        self.settings.model = item["model"]
        self.client = OpenAICompatibleClient(item["base_url"], item.get("api_key", ""), item["model"])
        with self._guard:
            for agent in self.sessions.values():
                agent.settings.model = item["model"]
                agent.client = self.client
        return {"id": item["id"], "name": item["name"], "kind": "external",
                "base_url": item["base_url"], "model": item["model"]}

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
                    log=lambda event, data, sid=session_id: self.log.write(sid, event, data),
                    retriever=self.registry.retriever,
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
        except Exception as e:
            self.log.write(session_id, "error", {"error": f"{type(e).__name__}: {e}"})
            raise
        finally:
            current_channel.reset(token)
            lock.release()

    def stop(self, session_id: str) -> bool:
        """Просит остановить текущий ответ. Уже идущий инструмент (например, команда shell) доработает."""
        if not self.busy(session_id):
            return False
        self.sessions[session_id].cancel.set()
        return True

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

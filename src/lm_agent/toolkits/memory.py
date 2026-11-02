"""Долговременная память: факты сохраняются в data_dir/memory.json и попадают
в системный промпт при каждом запуске."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..tools import ToolRegistry

MAX_IN_PROMPT = 50


class Memory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, text: str) -> int:
        new_id = max((m["id"] for m in self.items), default=0) + 1
        self.items.append({"id": new_id, "text": text.strip(), "created": datetime.now().isoformat(timespec="seconds")})
        self.save()
        return new_id

    def remove(self, memory_id: int) -> bool:
        before = len(self.items)
        self.items = [m for m in self.items if m["id"] != memory_id]
        self.save()
        return len(self.items) < before

    def as_prompt(self) -> str:
        if not self.items:
            return ""
        lines = "\n".join(f"- [{m['id']}] {m['text']}" for m in self.items[-MAX_IN_PROMPT:])
        return f"Что ты помнишь о пользователе и прошлых разговорах:\n{lines}"


def register(registry: ToolRegistry, memory: Memory) -> None:
    @registry.register
    def remember(fact: str) -> str:
        """Сохраняет факт в долговременную память — он будет доступен в следующих разговорах.

        fact: короткий самодостаточный факт, например "Пользователь предпочитает краткие ответы"
        """
        return f"Запомнил (#{memory.add(fact)})"

    @registry.register
    def list_memories() -> str:
        """Показывает всё, что сохранено в долговременной памяти, с номерами."""
        return "\n".join(f"[{m['id']}] {m['text']} ({m['created']})" for m in memory.items) or "Память пуста"

    @registry.register
    def forget(memory_id: int) -> str:
        """Удаляет запись из долговременной памяти по номеру.

        memory_id: номер записи из list_memories
        """
        return "Удалено" if memory.remove(int(memory_id)) else f"Запись #{memory_id} не найдена"

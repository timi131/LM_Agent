"""Статьи, сохранённые пользователем из чата: по JSON-объекту на строку — формат, который понимает поиск.

Записывает только человек (кнопка в веб-чате): у модели нет такого инструмента, поэтому текст
с сайта или из письма не сможет подтолкнуть её дописать что-то в базу знаний.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

CATEGORY = "Сохранено из чата"


class SavedArticles:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def items(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def add(self, question: str, answer: str) -> dict:
        """Новый ответ на тот же вопрос заменяет старый: так можно исправить статью."""
        item = {
            "id": uuid.uuid4().hex[:8], "category": CATEGORY, "question": question.strip(), "answer": answer.strip(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self._lock:
            self._write([a for a in self.items() if a["question"] != item["question"]] + [item])
        return item

    def remove(self, item_id: str) -> bool:
        with self._lock:
            items = self.items()
            kept = [a for a in items if a["id"] != item_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
        return True

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".saved-", suffix=".tmp")  # сбой не испортит файл
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(a, ensure_ascii=False) + "\n" for a in items)
        os.replace(tmp, self.path)

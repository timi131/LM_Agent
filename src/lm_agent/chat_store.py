"""Persistent chat histories for the web interface."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .agent import KB_PREFIX

_MAX_TITLE = 72


class ChatStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    messages TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def load(self, chat_id: str) -> list[dict] | None:
        with self._connect() as db:
            row = db.execute("SELECT messages FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            return None
        messages = json.loads(row[0])
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise RuntimeError(f"Некорректная история чата {chat_id}")
        return messages

    def save(self, chat_id: str, messages: list[dict]) -> None:
        saved = [message for message in messages if message.get("role") != "system"]
        title = next(
            (str(message.get("content", "")).strip() for message in saved
             if message.get("role") == "user" and str(message.get("content", "")).strip()
             and not str(message.get("content", "")).startswith(KB_PREFIX)),
            "Новый чат",
        )
        title = " ".join(title.split())[:_MAX_TITLE]
        now = datetime.now().astimezone().isoformat(timespec="microseconds")
        encoded = json.dumps(saved, ensure_ascii=False, default=str)
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO chats (id, title, messages, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = CASE WHEN chats.title = 'Новый чат' THEN excluded.title ELSE chats.title END,
                    messages = excluded.messages,
                    updated_at = excluded.updated_at
                """,
                (chat_id, title, encoded, now, now),
            )

    def list(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, title, updated_at
                FROM chats
                ORDER BY updated_at DESC
                """,
            ).fetchall()
        return [{"id": row[0], "title": row[1], "updated_at": row[2]} for row in rows]

    def delete(self, chat_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            return cursor.rowcount > 0

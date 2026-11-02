"""Поиск по вашим документам (RAG) на эмбеддингах Ollama.

Индекс хранится в data_dir/doc_index.json и обновляется инкрементально: заново
обрабатываются только новые и изменённые файлы.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

from ..tools import ToolRegistry

TEXT_EXTS = {".md", ".txt", ".rst", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".htm", ".xml", ".ini", ".sql", ".sh"}
CHUNK = 1200
OVERLAP = 200
BATCH = 32


def chunk_text(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> list[str]:
    """Режет текст на куски ~size символов, стараясь резать по абзацам."""
    text = text.strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(". ", start + size // 2, end)
            if cut != -1:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix == ".docx":
        from docx import Document

        return "\n".join(p.text for p in Document(path).paragraphs)
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        from .web import html_to_text

        text = html_to_text(text)
    return text


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class DocIndex:
    def __init__(self, client: Any, embed_model: str, docs_dir: Path, index_file: Path) -> None:
        self.client = client
        self.embed_model = embed_model
        self.docs_dir = docs_dir
        self.index_file = index_file
        self.files: dict[str, dict] = {}
        self._lock = threading.Lock()  # веб-интерфейс может обновлять индекс из нескольких потоков
        if index_file.exists():
            data = json.loads(index_file.read_text(encoding="utf-8"))
            if data.get("model") == embed_model:
                self.files = data.get("files", {})

    # embeddinggemma обучена с префиксами задач — с ними поиск заметно точнее.
    def _prefix(self, kind: str) -> str:
        if "embeddinggemma" not in self.embed_model:
            return ""
        return "task: search result | query: " if kind == "query" else "title: none | text: "

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        vectors = []
        for i in range(0, len(texts), BATCH):
            batch = [self._prefix(kind) + t for t in texts[i : i + BATCH]]
            response = self.client.embed(model=self.embed_model, input=batch)
            vectors.extend(_normalize(list(v)) for v in response["embeddings"])
        return vectors

    def candidates(self) -> list[Path]:
        if not self.docs_dir.exists():
            return []
        exts = TEXT_EXTS | {".pdf", ".docx"}
        return [
            p for p in sorted(self.docs_dir.rglob("*"))
            if p.is_file() and p.suffix.lower() in exts
            and not any(part.startswith(".") for part in p.relative_to(self.docs_dir).parts)
        ]

    def update(self) -> dict[str, int]:
        with self._lock:
            return self._update()

    def _update(self) -> dict[str, int]:
        stats = {"added": 0, "updated": 0, "removed": 0, "errors": 0, "chunks": 0}
        seen = set()
        for path in self.candidates():
            key = str(path.relative_to(self.docs_dir))
            seen.add(key)
            mtime = path.stat().st_mtime
            entry = self.files.get(key)
            if entry and entry["mtime"] == mtime:
                continue
            try:
                chunks = chunk_text(read_document(path))
                vectors = self.embed(chunks, "doc") if chunks else []
            except Exception:  # noqa: BLE001 — один битый файл не должен ломать индекс
                stats["errors"] += 1
                continue
            stats["updated" if entry else "added"] += 1
            self.files[key] = {"mtime": mtime, "chunks": chunks, "vectors": vectors}
        for key in set(self.files) - seen:
            del self.files[key]
            stats["removed"] += 1
        stats["chunks"] = sum(len(f["chunks"]) for f in self.files.values())
        if stats["added"] or stats["updated"] or stats["removed"]:
            self.save()
        return stats

    def save(self) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"model": self.embed_model, "files": self.files}
        self.index_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def search(self, query: str, top_k: int = 5) -> list[tuple[float, str, str]]:
        (q,) = self.embed([query], "query")
        scored = [
            (sum(a * b for a, b in zip(q, vec)), key, chunk)
            for key, entry in self.files.items()
            for chunk, vec in zip(entry["chunks"], entry["vectors"])
        ]
        return sorted(scored, reverse=True)[:top_k]


def register(registry: ToolRegistry, index: DocIndex) -> None:
    @registry.register
    def search_documents(query: str, top_k: int = 5) -> str:
        """Ищет по смыслу в документах пользователя и возвращает самые подходящие фрагменты с именами файлов.

        query: что ищем, своими словами
        top_k: сколько фрагментов вернуть (1–10)
        """
        index.update()
        if not index.files:
            return f"Документов нет. Положите файлы в {index.docs_dir.resolve()}"
        hits = index.search(query, max(1, min(int(top_k), 10)))
        return "\n\n".join(f"[{key} · релевантность {score:.2f}]\n{chunk}" for score, key, chunk in hits)

    @registry.register
    def index_documents() -> str:
        """Переиндексирует папку документов (новые и изменённые файлы) и сообщает статистику."""
        s = index.update()
        return (
            f"Папка: {index.docs_dir.resolve()}. Файлов: {len(index.files)}, фрагментов: {s['chunks']}. "
            f"Добавлено {s['added']}, обновлено {s['updated']}, удалено {s['removed']}, ошибок {s['errors']}."
        )

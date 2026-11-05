"""Поиск по вашим документам (RAG) на эмбеддингах Ollama.

Индекс хранится в data_dir/doc_index.json и обновляется инкрементально: заново
обрабатываются только новые и изменённые файлы.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
from pathlib import Path
from typing import Any

from ..tools import ToolRegistry

TEXT_EXTS = {".md", ".txt", ".rst", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".jsonl", ".html", ".htm", ".xml", ".ini", ".sql", ".sh"}
CHUNK = 1200
READ_PART = 6000  # документ отдаётся модели частями: она читает ~165 токенов/с
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


# Версия формата индекса: при изменении разбора документов индекс перестраивается.
INDEX_FORMAT = 4

# Автоматический поиск перед ответом: сходство ниже порога — статья не про это.
RETRIEVE_TOP_K = 3
RETRIEVE_MIN_SCORE = 0.55
RETRIEVE_SPREAD = 0.1  # статьи намного слабее лучшей только сбивают модель


def _table_rows(path: Path) -> list[list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        rows = [["" if v is None else str(v) for v in r] for r in wb.worksheets[0].iter_rows(values_only=True)]
        wb.close()
        return rows
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel_tab if suffix == ".tsv" else csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def table_to_text(rows: list[list[str]]) -> str:
    """Каждая строка таблицы — отдельный абзац «Колонка: значение; …»: так у каждого фрагмента
    есть названия колонок, а режется текст по границам строк, а не посреди них."""
    if not rows:
        return ""
    header = [h.strip() or f"Колонка {i + 1}" for i, h in enumerate(rows[0])]
    records = []
    for row in rows[1:]:
        pairs = [f"{h}: {v.strip()}" for h, v in zip(header, row) if v and v.strip()]
        if pairs:
            records.append("; ".join(pairs))
    return "\n\n".join(records)


def markdown_sections(text: str) -> list[str]:
    """Markdown с заголовками → по фрагменту на статью (самый частый уровень заголовков), с названием раздела.
    Так поиск находит ровно нужную статью FAQ, а не обрывок двух соседних."""
    lines = text.splitlines()
    levels = [len(m.group(1)) for l in lines if (m := re.match(r"^(#{1,6})\s+\S", l))]
    if len(levels) < 3:
        return []
    article = max(set(levels), key=levels.count)
    parents: dict[int, str] = {}
    sections, current = [], []

    def flush():
        body = "\n".join(current).strip()
        if body and re.search(r"\w", body.split("\n", 1)[-1] if "\n" in body else ""):
            trail = " / ".join(parents[l] for l in sorted(parents) if l < article)
            sections.append((f"Раздел: {trail}\n" if trail else "") + body)

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m and len(m.group(1)) <= article:
            flush()
            current = []
            level = len(m.group(1))
            if level < article:
                parents = {l: t for l, t in parents.items() if l < level}
                parents[level] = m.group(2).strip()
                continue
        current.append(line)
    flush()
    out = []
    for sec in sections:  # слишком длинные статьи дорезаем, сохраняя заголовок
        if len(sec) <= CHUNK * 2:
            out.append(sec)
        else:
            head = sec.split("\n", 2)[:2]
            out.extend(chunk if i == 0 else "\n".join(head) + "\n…" + chunk for i, chunk in enumerate(chunk_text(sec)))
    return out


def json_records(text: str) -> list[str]:
    """JSON-список записей (например, шаблоны ответов {keywords, answer}) → по фрагменту на запись."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not (isinstance(data, list) and data and all(isinstance(x, dict) for x in data)):
        return []
    labels = {"keywords": "Ключевые слова", "answer": "Ответ", "question": "Вопрос", "title": "Тема"}
    records = []
    for item in data:
        parts = []
        for key, value in item.items():
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            if str(value).strip():
                parts.append(f"{labels.get(key, key)}: {value}")
        if parts:
            records.append("\n".join(parts))
    return records


def jsonl_records(text: str) -> list[str]:
    """По JSON-объекту на строке (FAQ {category, question, answer, metadata}) → по фрагменту на запись.
    Определяем по содержимому: у базы Jivo-бота такой файл называется faq.md."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].lstrip().startswith("{"):
        return []
    labels = {"category": "Раздел", "question": "Вопрос", "answer": "Ответ"}
    records = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            return []  # это не построчный JSON
        if not isinstance(item, dict):
            return []
        parts = [f"{label}: {item[key]}" for key, label in labels.items() if item.get(key)]
        for key, value in (item.get("metadata") or {}).items():
            parts.append(f"{str(key).capitalize()}: {value}")
        if parts:
            records.append("\n".join(parts))
    return records


def document_chunks(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        records = json_records(path.read_text(encoding="utf-8", errors="replace"))
        if records:
            return records
    if suffix in (".jsonl", ".md", ".markdown", ".txt"):
        records = jsonl_records(path.read_text(encoding="utf-8", errors="replace"))
        if records:
            return records
    text = read_document(path)
    if suffix in (".md", ".markdown"):
        sections = markdown_sections(text)
        if sections:
            return sections
    return chunk_text(text)


OCR_SOURCE = Path(__file__).with_name("ocr_pdf.swift")


def ocr_pdf(path: Path) -> str:
    """Распознаёт текст PDF встроенным движком macOS (Vision). На других системах — пусто."""
    import shutil
    import subprocess
    import sys

    if sys.platform != "darwin" or not shutil.which("swiftc"):
        return ""
    binary = Path("~/.lm_agent/bin/ocr_pdf").expanduser()
    if not binary.exists() or binary.stat().st_mtime < OCR_SOURCE.stat().st_mtime:
        binary.parent.mkdir(parents=True, exist_ok=True)  # компилируем один раз, дальше запуск мгновенный
        subprocess.run(["swiftc", "-O", str(OCR_SOURCE), "-o", str(binary)], check=True, capture_output=True, timeout=300)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "text.txt"
        subprocess.run([str(binary), str(path), str(out)], capture_output=True, timeout=600)
        return out.read_text(encoding="utf-8").strip() if out.exists() else ""


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".xlsx"):
        return table_to_text(_table_rows(path))
    if suffix == ".pdf":
        from pypdf import PdfReader

        text = "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        if len(text.strip()) < 20:  # текстового слоя нет (скан или «печать в PDF» картинками) — распознаём
            text = ocr_pdf(path) or text
        return text
    if suffix == ".docx":
        from docx import Document

        return "\n".join(p.text for p in Document(path).paragraphs)
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        from .web import html_to_text

        text = html_to_text(text)
    return text


def all_documents(docs_dir: Path, extra: list[Path] = ()) -> list[tuple[str, Path]]:
    out = [(str(p.relative_to(docs_dir)), p) for p in list_documents(docs_dir)]
    for source in extra:
        source = Path(source).expanduser()
        if source.is_file():
            out.append((source.name, source))
        elif source.is_dir():
            out.extend((f"{source.name}/{p.relative_to(source)}", p) for p in list_documents(source))
    return out


def list_documents(docs_dir: Path) -> list[Path]:
    if not docs_dir.exists():
        return []
    exts = TEXT_EXTS | {".pdf", ".docx", ".xlsx"}
    return [
        p for p in sorted(docs_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in exts
        and not any(part.startswith(".") for part in p.relative_to(docs_dir).parts)
    ]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class DocIndex:
    def __init__(self, client: Any, embed_model: str, docs_dir: Path, index_file: Path, extra: list[Path] = ()) -> None:
        self.client = client
        self.embed_model = embed_model
        self.docs_dir = docs_dir
        # дополнительные источники только для чтения: папки или отдельные файлы (например, база знаний другого проекта)
        self.extra = [Path(p).expanduser() for p in extra]
        self.index_file = index_file
        self.files: dict[str, dict] = {}
        self._lock = threading.Lock()  # веб-интерфейс может обновлять индекс из нескольких потоков
        if index_file.exists():
            data = json.loads(index_file.read_text(encoding="utf-8"))
            if data.get("model") == embed_model and data.get("format") == INDEX_FORMAT:
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

    def candidates(self) -> list[tuple[str, Path]]:
        """(имя для модели, путь): файлы docs/ — по относительному пути, внешние — с именем источника."""
        return all_documents(self.docs_dir, self.extra)

    def update(self) -> dict[str, int]:
        with self._lock:
            return self._update()

    def _update(self) -> dict[str, int]:
        stats = {"added": 0, "updated": 0, "removed": 0, "errors": 0, "chunks": 0}
        seen = set()
        for key, path in self.candidates():
            seen.add(key)
            mtime = path.stat().st_mtime
            entry = self.files.get(key)
            if entry and entry["mtime"] == mtime:
                continue
            try:
                chunks = document_chunks(path)
                vectors = self.embed(chunks, "doc") if chunks else []
            except ConnectionError:
                raise  # нет связи с Ollama — это не битый файл, индекс должен об этом сказать
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
        payload = {"model": self.embed_model, "format": INDEX_FORMAT, "files": self.files}
        self.index_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def knowledge_keys(self) -> set[str]:
        return {key for key, _ in all_documents(Path("/nonexistent"), self.extra)}

    def retrieve(self, question: str) -> str:
        """Автоматический поиск перед ответом: только по подключённым базам знаний (LM_AGENT_KNOWLEDGE).
        Возвращает текст для модели или пустую строку, если подходящих статей нет."""
        self.update()
        keys = self.knowledge_keys()
        hits = [h for h in self.search(question, RETRIEVE_TOP_K, only=keys) if h[0] >= RETRIEVE_MIN_SCORE]
        if not hits:
            return ""
        best = hits[0][0]
        articles = [chunk for score, _, chunk in hits if score >= best - RETRIEVE_SPREAD]
        return "\n\n---\n\n".join(articles)

    def search(self, query: str, top_k: int = 5, only: set[str] | None = None) -> list[tuple[float, str, str]]:
        (q,) = self.embed([query], "query")
        scored = [
            (sum(a * b for a, b in zip(q, vec)), key, chunk)
            for key, entry in self.files.items() if only is None or key in only
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
            return f"Документов нет. Положите файлы в {index.docs_dir.resolve()} или подключите базу знаний (LM_AGENT_KNOWLEDGE)"
        hits = index.search(query, max(1, min(int(top_k), 10)))
        return "\n\n".join(f"[{key} · релевантность {score:.2f}]\n{chunk}" for score, key, chunk in hits)

    def find_document(name: str) -> Path | None:
        items = index.candidates()
        name = str(name).strip()
        for key, path in items:
            if name in (key, path.name):
                return path
        matches = [path for key, path in items if name.lower() in key.lower()]
        return matches[0] if len(matches) == 1 else None

    # Имя в реестре задано отдельно: функция модуля read_document не должна быть перекрыта.
    @registry.register(name="read_document")
    def read_whole_document(file: str, part: int = 1) -> str:
        """Читает документ пользователя (PDF, Word, Excel, текст) целиком, по частям. Нужен, чтобы проанализировать или пересказать весь файл.

        file: имя файла, например "договор.pdf"
        part: номер части, начиная с 1
        """
        path = find_document(file)
        if path is None:
            names = ", ".join(key for key, _ in index.candidates()[:30]) or "нет"
            return f"Ошибка: документ '{file}' не найден или его формат не поддерживается. Доступны: {names}"
        text = read_document(path).strip()
        if not text:
            return f"В файле {path.name} не удалось найти текст."
        total = max(1, -(-len(text) // READ_PART))
        n = max(1, min(int(part), total))
        more = f"\n…дальше: read_document(file, part={n + 1})" if n < total else ""
        return f"{path.name}, часть {n} из {total}\n{text[(n - 1) * READ_PART : n * READ_PART]}{more}"

    @registry.register
    def index_documents() -> str:
        """Переиндексирует папку документов (новые и изменённые файлы) и сообщает статистику."""
        s = index.update()
        return (
            f"Папка: {index.docs_dir.resolve()}. Файлов: {len(index.files)}, фрагментов: {s['chunks']}. "
            f"Добавлено {s['added']}, обновлено {s['updated']}, удалено {s['removed']}, ошибок {s['errors']}."
        )

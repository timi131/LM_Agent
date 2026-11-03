"""Таблицы (CSV, TSV, Excel): вопросы «сколько», «кто чаще», «среднее» — через SQL в DuckDB.

Каждый файл загружается в отдельную базу в памяти как таблица `t`. После загрузки доступ
DuckDB к файловой системе выключается и блокируется: SQL модели может только читать `t`.
"""

from __future__ import annotations

import csv
import difflib
import re
import threading
from pathlib import Path

from ..tools import ToolRegistry

TABLE_EXTS = {".csv", ".tsv", ".xlsx"}
MAX_ROWS = 50
MAX_CELL = 200
CATEGORY_LIMIT = 15  # колонки с таким числом разных значений показываем полностью: модель не угадывает
AUTO_FIX_RATIO = 0.75  # насколько значение из SQL должно быть похоже на настоящее, чтобы заменить его само


def find_tables(folders: list[Path]) -> list[Path]:
    seen, out = set(), []
    for folder in folders:
        if not folder.exists():
            continue
        for p in sorted(folder.rglob("*")):
            rel = p.relative_to(folder)
            if p.is_file() and p.suffix.lower() in TABLE_EXTS and not any(x.startswith(".") for x in rel.parts):
                if p.resolve() not in seen:
                    seen.add(p.resolve())
                    out.append(p)
    return out


def _xlsx_to_csv(src: Path, dst: Path) -> None:
    from openpyxl import load_workbook

    wb = load_workbook(src, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in ws.iter_rows(values_only=True):
            writer.writerow(["" if v is None else v for v in row])
    wb.close()


def _cell(value) -> str:
    text = "" if value is None else str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"


class Tables:
    def __init__(self, folders: list[Path], cache_dir: Path) -> None:
        self.folders = folders
        self.cache_dir = cache_dir
        self._dbs: dict[Path, tuple[float, object, threading.Lock]] = {}
        self._categories: dict[Path, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def files(self) -> list[Path]:
        return find_tables(self.folders)

    def resolve(self, name: str) -> Path:
        """Имя файла, его часть или номер из list_tables — моделям трудно повторить длинное имя точно."""
        files = self.files()
        if not files:
            raise FileNotFoundError("таблиц нет: загрузите CSV или Excel в документы")
        name = str(name).strip()
        if name.isdigit() and 1 <= int(name) <= len(files):
            return files[int(name) - 1]
        for p in files:
            if name in (p.name, str(p)):
                return p
        matches = [p for p in files if name.lower() in p.name.lower()] or [
            p for p in files if p.stem.lower().startswith(name.lower()[:8])
        ]
        if len(matches) == 1:
            return matches[0]
        listing = "; ".join(f"{i}. {p.name}" for i, p in enumerate(files, 1))
        raise FileNotFoundError(f"не понял, какой файл '{name}'. Доступны: {listing}. Можно указать номер")

    def db(self, path: Path):
        """База DuckDB с таблицей t для файла (перезагружается, если файл изменился)."""
        import duckdb

        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._dbs.get(path)
            if cached and cached[0] == mtime:
                return cached[1], cached[2]
            source = path
            if path.suffix.lower() == ".xlsx":
                source = self.cache_dir / (path.stem + ".csv")
                _xlsx_to_csv(path, source)
            con = duckdb.connect()
            delim = "\t" if path.suffix.lower() == ".tsv" else None
            opts = {"header": True, "sample_size": -1}
            if delim:
                opts["delim"] = delim
            args = ", ".join(f"{k}=${k}" for k in opts)
            con.execute(f"CREATE TABLE t AS SELECT * FROM read_csv($path, {args})", {"path": str(source), **opts})
            # дальше — только таблица t: никаких файлов, сети и смены настроек из SQL модели
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
            entry = (mtime, con, threading.Lock())
            self._dbs[path] = entry
            return con, entry[2]

    def categories(self, path: Path) -> dict[str, list[tuple[str, int]]]:
        """Текстовые колонки с немногими разными значениями: {колонка: [(значение, сколько раз)]}."""
        mtime = path.stat().st_mtime
        cached = self._categories.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        out = {}
        for name, ctype, *_ in self.query(path, "DESCRIBE t")[1]:
            if ctype != "VARCHAR":
                continue
            _, rows = self.query(
                path, f'SELECT "{name}", count(*) FROM t WHERE "{name}" IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT {CATEGORY_LIMIT + 1}'
            )
            if 0 < len(rows) <= CATEGORY_LIMIT:
                out[name] = [(str(v), n) for v, n in rows]
        self._categories[path] = (mtime, out)
        return out

    def fix_literals(self, path: Path, sql: str) -> tuple[str, list[str], list[str]]:
        """Строки из SQL, сравниваемые через = / IN, которых нет среди значений колонок-категорий.
        Если похожее значение одно и очень близкое — подставляет его. Возвращает (sql, замены, подсказки)."""
        known = {v for values in self.categories(path).values() for v, _ in values}
        if not known:
            return sql, [], []
        fixed, hints = [], []
        for literal in dict.fromkeys(re.findall(r"(?:=|\bin\s*\(|,)\s*'((?:[^']|'')*)'", sql, flags=re.I)):
            value = literal.replace("''", "'")
            if not value or value in known:
                continue
            close = difflib.get_close_matches(value, known, n=3, cutoff=0.5)
            best = difflib.get_close_matches(value, known, n=2, cutoff=AUTO_FIX_RATIO)
            if len(best) == 1:
                sql = re.sub(rf"'{re.escape(literal)}'", "'" + best[0].replace("'", "''") + "'", sql)
                fixed.append(f"'{value}' → '{best[0]}'")
            elif close:
                hints.append(f"значения '{value}' в таблице нет — возможно, {', '.join(repr(c) for c in close)}")
        return sql, fixed, hints

    def query(self, path: Path, sql: str):
        con, lock = self.db(path)
        with lock:
            cur = con.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        return columns, rows


def normalize_sql(sql: str, columns: list[str]) -> str:
    """Чинит частые ошибки модели: [Колонка] (диалект MS SQL) и `Колонка` (MySQL) → "Колонка"."""
    known = set(columns)

    def quote(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return f'"{name}"' if name in known else m.group(0)

    return re.sub(r"\[([^\[\]'\"]+)\]|`([^`]+)`", quote, sql)


def format_rows(columns: list[str], rows: list[tuple]) -> str:
    more = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    lines = [" | ".join(columns)] + [" | ".join(_cell(v) for v in r) for r in rows]
    if more:
        lines.append(f"…показаны первые {MAX_ROWS} строк; уточни запрос (WHERE, LIMIT, GROUP BY)")
    if not rows:
        lines.append("(нет строк)")
    return "\n".join(lines)


def register(registry: ToolRegistry, tables: Tables) -> Tables:
    @registry.register
    def list_tables() -> str:
        """Показывает таблицы пользователя (CSV, TSV, Excel) с номерами."""
        files = tables.files()
        if not files:
            return "Таблиц нет. Загрузите CSV или Excel в «Документы»."
        return "\n".join(f"{i}. {p.name} ({p.stat().st_size // 1024} КБ)" for i, p in enumerate(files, 1))

    @registry.register
    def table_info(file: str) -> str:
        """Описывает таблицу: число строк, колонки с типами и примерами значений. Вызывай перед table_query.

        file: имя файла, его часть или номер из list_tables
        """
        path = tables.resolve(file)
        (count,), = tables.query(path, "SELECT count(*) FROM t")[1]
        columns, _ = tables.query(path, "SELECT * FROM t LIMIT 0")
        schema = tables.query(path, "DESCRIBE t")[1]
        categories = tables.categories(path)
        lines = [f"Файл: {path.name}", f"Строк: {count}. Таблица называется t.", "Колонки (имя в SQL — в двойных кавычках):"]
        for name, ctype, *_ in schema:
            if name in categories:
                values = ", ".join(f"'{v}' ({n})" for v, n in categories[name])
                lines.append(f'- "{name}" ({ctype}), все значения: {values}')
                continue
            _, sample = tables.query(path, f'SELECT DISTINCT "{name}" FROM t WHERE "{name}" IS NOT NULL LIMIT 3')
            examples = ", ".join(_cell(r[0])[:40] for r in sample)
            lines.append(f'- "{name}" ({ctype}), примеры: {examples}')
        lines.append("В условиях WHERE используй значения точно как в списке выше.")
        return "\n".join(lines)

    @registry.register
    def table_query(file: str, sql: str) -> str:
        """Выполняет SQL (DuckDB) над таблицей t из файла: подсчёты, группировки, фильтры, сортировки.

        file: имя файла, его часть или номер из list_tables
        sql: запрос, например SELECT "Статус", count(*) FROM t GROUP BY 1 ORDER BY 2 DESC
        """
        path = tables.resolve(file)
        all_columns = tables.query(path, "SELECT * FROM t LIMIT 0")[0]
        sql = normalize_sql(sql, all_columns)
        sql, fixed, hints = tables.fix_literals(path, sql)
        try:
            columns, rows = tables.query(path, sql)
        except Exception as e:  # noqa: BLE001 — подскажем модели, как исправить запрос
            first_line = str(e).splitlines()[0]
            cols = ", ".join(f'"{c}"' for c in all_columns)
            return (
                f"Ошибка SQL: {first_line}\n"
                'Это DuckDB (диалект как в PostgreSQL): имена колонок — в двойных кавычках ("Статус"), '
                "строки — в одинарных ('Открытое'), таблица — t. Исправь запрос, а не повторяй его.\n"
                f"Колонки таблицы t: {cols}"
            )
        result = format_rows(columns, rows)
        if fixed:
            result += "\nℹ️ Значения из запроса исправлены по данным таблицы: " + ", ".join(fixed) + ". Результат уже с исправлением."
        if hints:
            result += "\n⚠️ Проверь запрос: " + "; ".join(hints) + ". Исправь значение и выполни запрос заново."
        return result

    return tables

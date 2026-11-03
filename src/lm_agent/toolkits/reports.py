"""Документы и отчёты: Word (.docx), PDF, Excel (.xlsx).

Агент пишет содержимое в Markdown, а здесь оно превращается в файл с нормальной вёрсткой.
Таблицы из данных (export_query) попадают в Excel прямо из базы — модель не переписывает числа.
Файлы сохраняются в workspace/reports; результат инструмента содержит строку «Файл готов: …»,
по которой веб-чат показывает кнопку «Скачать», а Telegram-бот присылает файл.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from ..tools import ToolRegistry

REPORTS = "reports"
FILE_READY = "Файл готов: "  # маркер для веб-чата и бота
MAX_EXPORT_ROWS = 100_000

PDF_CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: Arial, "Helvetica Neue", sans-serif; font-size: 11pt; line-height: 1.45; color: #1a1a1a; }
h1 { font-size: 20pt; color: #c1232f; margin: 0 0 10pt; }
h2 { font-size: 14pt; color: #c1232f; margin: 16pt 0 6pt; }
h3 { font-size: 12pt; margin: 12pt 0 4pt; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 10pt; }
th, td { border: 1px solid #d0d4dc; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #fce4e6; }
tr:nth-child(even) td { background: #f7f8fc; }
code { font-family: Menlo, monospace; font-size: 9.5pt; background: #f4f5f8; padding: 0 2pt; }
pre { background: #f4f5f8; padding: 6pt; white-space: pre-wrap; }
"""


# --- общее ---

def safe_name(name: str, ext: str) -> str:
    """Имя от модели → безопасное имя файла: без папок («/»), спецсимволов и чужого расширения."""
    stem = re.sub(r"\.(docx|pdf|md|xlsx|csv|txt)$", "", str(name).strip(), flags=re.I)
    stem = re.sub(r"[^\w\s.\-()]+", "", stem, flags=re.UNICODE).strip(" .") or "отчёт"
    return f"{stem[:80]}.{ext}"


def _out(workspace: Path, name: str, ext: str) -> Path:
    folder = workspace / REPORTS
    folder.mkdir(parents=True, exist_ok=True)
    return folder / safe_name(name, ext)


def _ready(path: Path, workspace: Path) -> str:
    size = path.stat().st_size
    shown = f"{size / 1024:.0f} КБ" if size >= 1024 else f"{size} Б"
    return f"{FILE_READY}{path.relative_to(workspace).as_posix()} ({shown})"


def _table_block(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # строка-разделитель |---|---|
        rows.append(cells)
    return rows


# --- Markdown → Word ---

_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|_[^_\s][^_]*_)")


def _add_inline(paragraph, text: str) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Menlo"
        elif (part.startswith("*") and part.endswith("*")) or (part.startswith("_") and part.endswith("_")):
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def markdown_to_docx(md: str, path: Path, title: str = "") -> None:
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(11)
    for level in (1, 2, 3):
        heading = doc.styles[f"Heading {level}"]
        heading.font.name = "Arial"
        heading.font.color.rgb = RGBColor(0xC1, 0x23, 0x2F) if level < 3 else RGBColor(0x1A, 0x1A, 0x1A)
    if title and not md.lstrip().startswith("# "):
        doc.add_heading(title, level=1)

    lines = md.replace("\r\n", "\n").split("\n")
    i, para = 0, []

    def flush():
        if para:
            _add_inline(doc.add_paragraph(), " ".join(para))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            run = doc.add_paragraph().add_run("\n".join(code))
            run.font.name = "Menlo"
            run.font.size = Pt(9.5)
        elif m := re.match(r"^(#{1,6})\s+(.*)", stripped):
            flush()
            doc.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 3))
        elif stripped.startswith("|"):
            flush()
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = _table_block(block)
            if rows:
                width = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=width)
                table.style = "Table Grid"
                for r, row in enumerate(rows):
                    for c in range(width):
                        cell = table.cell(r, c)
                        cell.text = ""
                        _add_inline(cell.paragraphs[0], row[c] if c < len(row) else "")
                        if r == 0:
                            for run in cell.paragraphs[0].runs:
                                run.bold = True
            continue
        elif m := re.match(r"^\s*([-*•])\s+(.*)", line):
            flush()
            _add_inline(doc.add_paragraph(style="List Bullet"), m.group(2))
        elif m := re.match(r"^\s*\d+[.)]\s+(.*)", line):
            flush()
            _add_inline(doc.add_paragraph(style="List Number"), m.group(1))
        elif not stripped:
            flush()
        else:
            para.append(stripped)
        i += 1
    flush()
    doc.save(path)


# --- Markdown → PDF (через Chrome) ---

def markdown_to_html(md: str, title: str = "") -> str:
    import html

    import markdown

    body = markdown.markdown(md, extensions=["tables", "sane_lists", "fenced_code"])
    heading = f"<h1>{html.escape(title)}</h1>" if title and not md.lstrip().startswith("# ") else ""
    return (f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{PDF_CSS}</style></head><body>{heading}{body}</body></html>")


def html_to_pdf(html_text: str, path: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 — Chromium не скачан: берём установленный Chrome
            if "Executable doesn't exist" not in str(e):
                raise
            browser = p.chromium.launch(channel="chrome", headless=True)
        try:
            # текст отчёта пишет модель: никаких скриптов и обращений в сеть при печати
            context = browser.new_context(java_script_enabled=False)
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content(html_text, wait_until="domcontentloaded")
            page.pdf(path=str(path), format="A4", print_background=True)
        finally:
            browser.close()


# --- Excel ---

_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?$")


def _value(text: str):
    t = text.strip()
    if _NUMBER.fullmatch(t) and not (len(t) > 1 and t.startswith("0") and t[1].isdigit()):  # 007 — код, не число
        return float(t.replace(",", ".")) if re.search(r"[.,]", t) else int(t)
    return t


def parse_rows(data: str) -> list[list[str]]:
    lines = [l for l in data.strip().splitlines() if l.strip()]
    if lines and lines[0].strip().startswith("|"):
        return _table_block(lines)
    try:
        dialect = csv.Sniffer().sniff(data[:3000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(data.strip()), dialect) if any(c.strip() for c in row)]


def write_xlsx(path: Path, columns: list[str], rows: list[list], sheet: str = "Данные") -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\[\]:*?/\\]", "", sheet)[:31] or "Данные"
    ws.append(list(columns))
    for row in rows:
        ws.append(list(row))
    head_fill = PatternFill("solid", fgColor="FCE4E6")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    for col in ws.columns:
        longest = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(10, longest + 2), 60)
    wb.save(path)


# --- инструменты ---

def register(registry: ToolRegistry, workspace: Path, tables=None) -> None:
    @registry.register
    def create_document(name: str, content: str, format: str = "docx", title: str = "") -> str:
        """Создаёт файл-отчёт: Word (docx), PDF или Excel (xlsx) из текста в Markdown (заголовки #, списки, таблицы |, **жирный**).

        name: имя файла без расширения, например "Отчёт по заявкам за сентябрь"
        content: текст в Markdown; для xlsx — таблица Markdown или CSV с заголовком
        format: docx — Word (по умолчанию), pdf — PDF, xlsx — Excel, md — Markdown. Если пользователь просит Excel — xlsx
        title: заголовок, если его нет в тексте
        """
        fmt = {"word": "docx", "doc": "docx", "excel": "xlsx", "xls": "xlsx"}.get(format.lower().strip(". "), format.lower().strip(". "))
        if fmt == "xlsx":
            rows = parse_rows("\n".join(l for l in content.splitlines() if l.strip().startswith("|")) or content)
            if len(rows) < 2:
                return "Ошибка: для Excel нужна таблица с заголовком (Markdown | или CSV). Для точных данных из таблицы используй export_query"
            path = _out(workspace, name, "xlsx")
            write_xlsx(path, rows[0], [[_value(c) for c in r] for r in rows[1:]])
            return _ready(path, workspace) + f", строк: {len(rows) - 1}"
        if fmt not in ("docx", "pdf", "md"):
            return "Ошибка: format должен быть docx, pdf, xlsx или md"
        path = _out(workspace, name, fmt)
        if fmt == "docx":
            markdown_to_docx(content, path, title)
        elif fmt == "pdf":
            html_to_pdf(markdown_to_html(content, title), path)
        else:
            path.write_text((f"# {title}\n\n" if title else "") + content, encoding="utf-8")
        return _ready(path, workspace)

    @registry.register
    def create_spreadsheet(name: str, data: str, sheet: str = "Данные") -> str:
        """Создаёт таблицу Excel (xlsx) из данных: CSV (первая строка — заголовки) или таблица Markdown.

        name: имя файла без расширения
        data: строки таблицы — CSV или Markdown-таблица с заголовком
        sheet: название листа
        """
        rows = parse_rows(data)
        if not rows:
            return "Ошибка: нет данных для таблицы"
        path = _out(workspace, name, "xlsx")
        write_xlsx(path, rows[0], [[_value(c) for c in r] for r in rows[1:]], sheet)
        return _ready(path, workspace) + f", строк: {len(rows) - 1}"

    if tables is None:
        return

    from .tables import normalize_sql

    @registry.register
    def export_query(file: str, sql: str, name: str) -> str:
        """Excel прямо из данных: выгружает результат SQL по таблице пользователя в xlsx — все строки, точные числа. Лучший способ «выгрузить в Excel».

        file: имя файла таблицы, его часть или номер из list_tables
        sql: запрос к таблице t (как в table_query), например SELECT "Ответственный", count(*) AS "Заявок" FROM t GROUP BY 1 ORDER BY 2 DESC
        name: имя файла Excel без расширения
        """
        path_in = tables.resolve(file)
        all_columns = tables.query(path_in, "SELECT * FROM t LIMIT 0")[0]
        sql_fixed, fixed, _ = tables.fix_literals(path_in, normalize_sql(sql, all_columns))
        con, lock = tables.db(path_in)
        with lock:
            cur = con.execute(sql_fixed)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_EXPORT_ROWS)
        out = _out(workspace, name, "xlsx")
        write_xlsx(out, columns, rows)
        note = f"; значения исправлены по данным: {', '.join(fixed)}" if fixed else ""
        return _ready(out, workspace) + f", строк: {len(rows)}{note}"

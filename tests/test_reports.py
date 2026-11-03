import pytest

from lm_agent import ToolRegistry
from lm_agent.toolkits import reports, tables

REPORT_MD = """# Отчёт по заявкам

За сентябрь пришло **362** заявки.

## Итоги
- закрыто: 327
- открыто: 13

| Ответственный | Заявок |
|---|---|
| Анна | 100 |
| Борис | 64 |
"""


@pytest.fixture
def reg(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "cases.csv").write_text(
        "Номер;Статус;Ответственный\n1;Закрытое;Анна\n2;Закрытое;Анна\n3;Открытое;Борис\n", encoding="utf-8"
    )
    registry = ToolRegistry()
    store = tables.Tables([docs_dir], tmp_path / "cache")
    tables.register(registry, store)
    reports.register(registry, tmp_path / "ws", store)
    return registry, tmp_path / "ws"


def test_docx_from_markdown(reg):
    registry, ws = reg
    out = registry.call("create_document", {"name": "Отчёт: сентябрь/2026", "content": REPORT_MD})
    assert out.startswith("Файл готов: reports/Отчёт сентябрь2026.docx (")  # недопустимые символы убраны
    from docx import Document

    doc = Document(ws / "reports" / "Отчёт сентябрь2026.docx")
    styles = [(p.style.name, p.text) for p in doc.paragraphs if p.text]
    assert ("Heading 1", "Отчёт по заявкам") in styles and ("Heading 2", "Итоги") in styles
    assert ("List Bullet", "закрыто: 327") in styles
    para = next(p for p in doc.paragraphs if "362" in p.text)
    assert any(r.bold and r.text == "362" for r in para.runs)  # **жирный** стал жирным
    table = doc.tables[0]
    assert [c.text for c in table.rows[0].cells] == ["Ответственный", "Заявок"]
    assert [c.text for c in table.rows[2].cells] == ["Борис", "64"]


def test_xlsx_from_csv_and_markdown(reg):
    from openpyxl import load_workbook

    registry, ws = reg
    out = registry.call("create_spreadsheet", {"name": "итоги", "data": "Оператор;Доставлено;Доля\nМТС;120;0,95\nБилайн;80;0.9\nКод;007;x"})
    assert "строк: 3" in out
    sheet = load_workbook(ws / "reports" / "итоги.xlsx").active
    assert [c.value for c in sheet[1]] == ["Оператор", "Доставлено", "Доля"]
    assert [c.value for c in sheet[2]] == ["МТС", 120, 0.95]  # числа — числами
    assert sheet["B4"].value == "007"  # код с ведущим нулём не превращён в 7
    assert sheet.freeze_panes == "A2" and sheet["A1"].font.bold

    registry.call("create_spreadsheet", {"name": "md", "data": "| A | B |\n|---|---|\n| 1 | 2 |"})
    assert [c.value for c in load_workbook(ws / "reports" / "md.xlsx").active[2]] == [1, 2]


def test_export_query_exact_numbers(reg):
    from openpyxl import load_workbook

    registry, ws = reg
    out = registry.call("export_query", {
        "file": "cases", "name": "по ответственным",
        "sql": """SELECT [Ответственный], count(*) AS "Заявок" FROM t WHERE "Статус" = 'Закрыто' GROUP BY 1""",
    })
    assert "строк: 1" in out and "'Закрыто' → 'Закрытое'" in out  # и скобки, и значение исправлены
    sheet = load_workbook(ws / "reports" / "по ответственным.xlsx").active
    assert [[c.value for c in row] for row in sheet.iter_rows()] == [["Ответственный", "Заявок"], ["Анна", 2]]


def test_pdf_with_cyrillic(reg):
    registry, ws = reg
    out = registry.call("create_document", {"name": "справка", "content": REPORT_MD, "format": "pdf"})
    if out.startswith("Ошибка") and ("Executable" in out or "chrome" in out.lower()):
        pytest.skip("нет браузера для печати PDF")
    data = (ws / "reports" / "справка.pdf").read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 5000


def test_bad_format(reg):
    registry, _ = reg
    assert registry.call("create_document", {"name": "x", "content": "y", "format": "rtf"}).startswith("Ошибка")


def test_create_document_can_make_excel(reg):
    from openpyxl import load_workbook

    registry, ws = reg
    md = "Итоги по ответственным:\n\n| Ответственный | Заявок |\n|---|---|\n| Анна | 100 |\n| Борис | 64 |\n"
    out = registry.call("create_document", {"name": "итоги", "content": md, "format": "Excel"})
    assert out.startswith("Файл готов: reports/итоги.xlsx") and "строк: 2" in out
    assert [c.value for c in load_workbook(ws / "reports" / "итоги.xlsx").active[3]] == ["Борис", 64]
    assert "нужна таблица" in registry.call("create_document", {"name": "x", "content": "просто текст", "format": "xlsx"})


def test_reports_are_not_source_tables(reg):
    registry, ws = reg
    registry.call("export_query", {"file": "cases", "sql": "SELECT * FROM t", "name": "выгрузка"})
    assert "выгрузка.xlsx" not in registry.call("list_tables", {})  # готовый отчёт — не исходная таблица

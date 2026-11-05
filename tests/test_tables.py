import pytest

from lm_agent import Settings, ToolRegistry
from lm_agent.toolkits import docs, make_context, tables

CSV = (
    "﻿Номер;\"Тема\";Статус;Ответственный\n"
    "1;Не приходит SMS;Закрытое;Анна\n"
    "2;\"Вопрос по счёту; срочно\";Закрытое;Борис\n"
    "3;Не приходит SMS;Открытое;Анна\n"
)


@pytest.fixture
def setup(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "6711_70377_cases_bf3ee317.csv").write_text(CSV, encoding="utf-8")
    reg = ToolRegistry()
    tables.register(reg, tables.Tables([docs_dir], tmp_path / "cache"))
    return reg, docs_dir


def test_info_and_query(setup):
    reg, _ = setup
    assert "1. 6711_70377_cases_bf3ee317.csv" in reg.call("list_tables", {})
    info = reg.call("table_info", {"file": "1"})
    assert "Строк: 3" in info and '"Ответственный"' in info
    out = reg.call("table_query", {"file": "cases", "sql": 'SELECT "Ответственный", count(*) AS n FROM t GROUP BY 1 ORDER BY n DESC'})
    assert out.splitlines()[:2] == ["Ответственный | n", "Анна | 2"]
    # кавычки и разделитель внутри поля разобраны правильно
    assert "Вопрос по счёту; срочно" in reg.call("table_query", {"file": "6711", "sql": 'SELECT "Тема" FROM t WHERE "Номер" = 2'})


def test_sql_error_hints_columns(setup):
    reg, _ = setup
    out = reg.call("table_query", {"file": "1", "sql": "SELECT Статус_нет FROM t"})
    assert out.startswith("Ошибка SQL") and '"Статус"' in out


def test_sql_cannot_touch_filesystem(setup, tmp_path):
    reg, _ = setup
    secret = tmp_path / "secret.txt"
    secret.write_text("пароль")
    assert "Ошибка SQL" in reg.call("table_query", {"file": "1", "sql": f"SELECT * FROM read_csv('{secret}')"})
    assert "Ошибка SQL" in reg.call("table_query", {"file": "1", "sql": f"COPY t TO '{tmp_path}/out.csv'"})
    assert not (tmp_path / "out.csv").exists()
    assert "Ошибка SQL" in reg.call("table_query", {"file": "1", "sql": "SET enable_external_access = true"})


def test_xlsx(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Город", "Продажи"])
    wb.active.append(["Казань", 10])
    wb.active.append(["Казань", 5])
    wb.save(tmp_path / "sales.xlsx")
    reg = ToolRegistry()
    tables.register(reg, tables.Tables([tmp_path], tmp_path / "cache"))
    out = reg.call("table_query", {"file": "sales", "sql": 'SELECT sum("Продажи") FROM t'})
    assert out.splitlines()[1] == "15"


def test_csv_indexed_row_by_row(setup):
    _, docs_dir = setup
    text = docs.read_document(docs_dir / "6711_70377_cases_bf3ee317.csv")
    rows = text.split("\n\n")
    assert len(rows) == 3
    assert rows[1] == "Номер: 2; Тема: Вопрос по счёту; срочно; Статус: Закрытое; Ответственный: Борис"


def test_context_lists_files(setup, tmp_path):
    _, docs_dir = setup
    (docs_dir / "регламент.md").write_text("текст")
    settings = Settings(docs_dir=docs_dir, workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    ctx = make_context(settings, None)()
    assert "регламент.md" not in ctx and "подключены" in ctx  # имена документов сбивали модель
    assert "- 6711_70377_cases_bf3ee317.csv" in ctx


def test_categories_listed_and_wrong_value_flagged(setup):
    reg, _ = setup
    info = reg.call("table_info", {"file": "1"})
    assert """"Статус" (VARCHAR), все значения: 'Закрытое' (2), 'Открытое' (1)""" in info
    # близкое значение одно — подставлено само, и об этом сказано
    out = reg.call("table_query", {"file": "1", "sql": """SELECT count(*) FROM t WHERE "Статус" = 'Открыт'"""})
    assert out.splitlines()[1] == "1"
    assert "'Открыт' → 'Открытое'" in out
    # похожих несколько или сходство слабое — только подсказка
    out = reg.call("table_query", {"file": "1", "sql": """SELECT count(*) FROM t WHERE "Статус" = 'рытое'"""})
    assert out.splitlines()[1] == "0" and "возможно" in out
    ok = reg.call("table_query", {"file": "1", "sql": """SELECT count(*) FROM t WHERE "Статус" = 'Открытое'"""})
    assert "⚠️" not in ok


def test_brackets_and_backticks_fixed(setup):
    reg, _ = setup
    out = reg.call("table_query", {"file": "1", "sql": "SELECT count(*) FROM t WHERE [Статус] = 'Открытое' AND `Ответственный` = 'Анна'"})
    assert out.splitlines()[1] == "1"
    err = reg.call("table_query", {"file": "1", "sql": "SELEKT 1"})
    assert "двойных кавычках" in err and "не повторяй" in err

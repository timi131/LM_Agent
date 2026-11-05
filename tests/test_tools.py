import json

import httpx
import pytest

from lm_agent import ToolRegistry
from lm_agent.toolkits import basic, docs, files, memory, services, shell
from lm_agent.toolkits.web import html_to_text


@pytest.fixture
def registry():
    reg = ToolRegistry(approver=lambda name, args: True)
    basic.register(reg)
    return reg


def make_files(tmp_path, approve=True):
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    outside.mkdir(parents=True)
    reg = ToolRegistry(approver=lambda name, args: approve)
    fa = files.register(reg, workspace, [outside])
    return reg, fa, workspace, outside


def test_schema_from_signature():
    reg = ToolRegistry()

    @reg.register
    def greet(name: str, times: int = 1) -> str:
        """Приветствует человека.

        name: имя
        times: сколько раз
        """
        return "hi " * times + name

    fn = reg.schemas()[0]["function"]
    assert fn["name"] == "greet"
    assert fn["description"] == "Приветствует человека."
    assert fn["parameters"]["properties"]["times"] == {"type": "integer", "description": "сколько раз"}
    assert fn["parameters"]["required"] == ["name"]


def test_calculate(registry):
    assert registry.call("calculate", {"expression": "sqrt(16) + 2**3"}) == "12.0"
    assert "Ошибка" in registry.call("calculate", {"expression": "__import__('os')"})


def test_arguments_as_json_string(registry):
    assert registry.call("calculate", '{"expression": "1+1"}') == "2"


def test_confirmation_denied_without_approver():
    reg = ToolRegistry()  # approver не задан — опасное запрещено
    ran = []

    @reg.register(confirm=True)
    def danger() -> str:
        """Опасно."""
        ran.append(1)
        return "done"

    assert "Отклонено" in reg.call("danger", {})
    assert not ran


def test_files_in_workspace(tmp_path):
    reg, _, workspace, _ = make_files(tmp_path, approve=False)
    reg.call("write_file", {"path": "notes/a.txt", "content": "привет"})  # внутри workspace — без вопроса
    assert reg.call("read_file", {"path": "notes/a.txt"}) == "привет"
    assert reg.call("list_files", {}) == "notes/"
    assert reg.call("find_files", {"pattern": "*.txt"}) == str((workspace / "notes/a.txt").resolve())


def test_files_outside_workspace(tmp_path):
    reg, _, _, outside = make_files(tmp_path, approve=False)
    (outside / "x.txt").write_text("данные")
    assert reg.call("read_file", {"path": str(outside / "x.txt")}) == "данные"  # чтение разрешено
    assert "Отклонено" in reg.call("write_file", {"path": str(outside / "y.txt"), "content": "1"})
    assert not (outside / "y.txt").exists()

    reg.approver = lambda n, a: True
    assert "Записано" in reg.call("write_file", {"path": str(outside / "y.txt"), "content": "1"})


def test_files_outside_roots_blocked(tmp_path):
    reg, *_ = make_files(tmp_path)
    assert "вне разрешённых папок" in reg.call("read_file", {"path": "/etc/passwd"})
    assert "вне разрешённых папок" in reg.call("read_file", {"path": "../../../etc/passwd"})


def test_shell(tmp_path):
    reg, fa, *_ = make_files(tmp_path, approve=True)
    shell.register(reg, fa, timeout=10)
    assert "stdout:\nhello" in reg.call("run_command", {"command": "echo hello"})

    reg.approver = lambda n, a: False
    assert "Отклонено" in reg.call("run_command", {"command": "echo hello"})


def test_html_to_text():
    html = "<html><head><title>t</title><script>alert(1)</script></head><body><h1>Заголовок</h1><p>Текст <b>жирный</b></p></body></html>"
    assert html_to_text(html) == "Заголовок\nТекст жирный"


def test_services(tmp_path, monkeypatch):
    cfg = tmp_path / "services.json"
    cfg.write_text(json.dumps({"crm": {"description": "CRM", "base_url": "https://crm.test/api", "headers": {"Authorization": "Bearer ${CRM_TOKEN}"}}}))
    monkeypatch.setenv("CRM_TOKEN", "secret")
    seen = {}

    def fake_request(method, url, headers=None, params=None, json=None, **kw):
        seen.update(method=method, url=url, headers=headers, params=params, json=json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    approvals = []
    reg = ToolRegistry(approver=lambda n, a: approvals.append(n) or False)
    services.register(reg, cfg)

    assert "crm: CRM" in reg.call("list_services", {})
    out = reg.call("call_service", {"service": "crm", "path": "/leads", "query": '{"limit": 2}'})
    assert out.startswith("HTTP 200")
    assert seen["url"] == "https://crm.test/api/leads"
    assert seen["headers"] == {"Authorization": "Bearer secret"}
    assert seen["params"] == {"limit": 2}
    assert not approvals  # GET — без подтверждения

    assert "Отклонено" in reg.call("call_service", {"service": "crm", "path": "/leads", "method": "POST", "body": "{}"})
    assert approvals == ["call_service"]
    assert "относительным" in reg.call("call_service", {"service": "crm", "path": "https://evil.test/x"})


def test_memory(tmp_path):
    path = tmp_path / "memory.json"
    reg = ToolRegistry()
    memory.register(reg, memory.Memory(path))
    reg.call("remember", {"fact": "Любит краткие ответы"})
    assert "[1] Любит краткие ответы" in memory.Memory(path).as_prompt()  # пережило «перезапуск»
    assert reg.call("forget", {"memory_id": 1}) == "Удалено"
    assert memory.Memory(path).as_prompt() == ""


class FakeEmbedClient:
    """Эмбеддинг = частоты ключевых слов — достаточно, чтобы проверить ранжирование."""

    WORDS = ["кошка", "собака", "налог", "отчёт"]

    def __init__(self):
        self.calls = 0

    def embed(self, model, input):
        self.calls += 1
        return {"embeddings": [[t.lower().count(w) + 0.01 for w in self.WORDS] for t in input]}


def test_chunk_text():
    text = "\n\n".join(f"Абзац {i}. " + "слово " * 50 for i in range(20))
    chunks = docs.chunk_text(text, size=500, overlap=100)
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[0].startswith("Абзац 0") and "Абзац 19" in chunks[-1]


def test_read_whole_document(tmp_path):
    from docx import Document

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    word = Document()
    word.add_paragraph("Срок договора — 12 месяцев")
    word.save(docs_dir / "Договор.docx")
    (docs_dir / "long.txt").write_text("а" * 6000 + "конец")
    (docs_dir / "photo.png").write_bytes(b"\x89PNG")
    outside = tmp_path / "secret.txt"
    outside.write_text("пароль")
    reg = ToolRegistry()
    docs.register(reg, docs.DocIndex(FakeEmbedClient(), "fake", docs_dir, tmp_path / "idx.json"))

    assert "12 месяцев" in reg.call("read_document", {"file": "Договор.docx"})
    assert "12 месяцев" in reg.call("read_document", {"file": "договор"})  # часть имени

    first = reg.call("read_document", {"file": "long.txt"})
    assert "часть 1 из 2" in first and "конец" not in first and "part=2" in first
    assert reg.call("read_document", {"file": "long.txt", "part": 2}).endswith("конец")

    for name in ("photo.png", "../secret.txt", str(outside)):
        result = reg.call("read_document", {"file": name})
        assert result.startswith("Ошибка") and "пароль" not in result


def test_doc_search(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "pets.md").write_text("Кошка и собака живут дома. Кошка спит.")
    (docs_dir / "tax.txt").write_text("Налоговый отчёт сдаётся до апреля. Налог платится.")
    client = FakeEmbedClient()
    index = docs.DocIndex(client, "fake", docs_dir, tmp_path / "idx.json")
    reg = ToolRegistry()
    docs.register(reg, index)

    assert reg.call("search_documents", {"query": "налог", "top_k": 1}).startswith("[tax.txt")
    calls = client.calls
    reg.call("search_documents", {"query": "кошка", "top_k": 1})
    assert client.calls == calls + 1  # файлы не переиндексированы — эмбеддинг только для запроса

    (docs_dir / "tax.txt").unlink()
    assert "удалено 1" in reg.call("index_documents", {})
    assert docs.DocIndex(client, "fake", docs_dir, tmp_path / "idx.json").files.keys() == {"pets.md"}


FAQ = """# База знаний

## Имена отправителей

### Q1. Как проверить статус имени?
- ответ:
  Откройте раздел «Имена» в личном кабинете.

### Q2. Почему имя недоступно у оператора?
- ответ:
  Оно ещё не зарегистрировано у этого оператора.
- требует проверки: да

## API

### Q8. Почему SMS не доставляются?
- ответ:
  Проверьте статус сообщения.
"""


def test_markdown_split_by_articles():
    sections = docs.markdown_sections(FAQ)
    assert len(sections) == 3  # ровно по статье на вопрос
    assert sections[1].startswith("Раздел: База знаний / Имена отправителей\n### Q2.")
    assert "требует проверки: да" in sections[1] and "Q8" not in sections[1]
    assert sections[2].startswith("Раздел: База знаний / API\n### Q8.")


def test_json_records_and_extra_sources(tmp_path):
    kb = tmp_path / "knowledge"
    kb.mkdir()
    (kb / "faq.md").write_text(FAQ, encoding="utf-8")
    templates = tmp_path / "templates.json"
    templates.write_text(json.dumps([{"keywords": ["модерация"], "answer": "Сообщение на проверке"}]), encoding="utf-8")
    assert docs.json_records(templates.read_text()) == ["Ключевые слова: модерация\nОтвет: Сообщение на проверке"]

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "своё.txt").write_text("заметка")
    found = {key for key, _ in docs.all_documents(docs_dir, [kb, templates, tmp_path / "нет"])}
    assert found == {"своё.txt", "knowledge/faq.md", "templates.json"}  # несуществующий источник пропущен

    index = docs.DocIndex(FakeEmbedClient(), "fake", docs_dir, tmp_path / "idx.json", [kb, templates])
    index.update()
    assert len(index.files["knowledge/faq.md"]["chunks"]) == 3 and len(index.files["templates.json"]["chunks"]) == 1


def test_jsonl_faq_even_named_md(tmp_path):
    faq = tmp_path / "faq.md"
    faq.write_text(
        json.dumps({"id": "Q1", "category": "Доставка", "question": "Почему нет SMS?", "answer": "Проверьте статус.",
                    "metadata": {"требует проверки": "да", "уточнить": "логин"}}, ensure_ascii=False) + "\n"
        + json.dumps({"id": "Q2", "question": "Как оплатить?", "answer": "Картой."}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert docs.document_chunks(faq) == [
        "Раздел: Доставка\nВопрос: Почему нет SMS?\nОтвет: Проверьте статус.\nТребует проверки: да\nУточнить: логин",
        "Вопрос: Как оплатить?\nОтвет: Картой.",
    ]
    assert docs.jsonl_records("# Обычный Markdown\n\nТекст") == []


def test_retrieve_only_from_knowledge_sources(tmp_path):
    kb = tmp_path / "kb.md"
    kb.write_text(json.dumps({"question": "Что такое налог?", "answer": "Платёж."}, ensure_ascii=False), encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "заявки.txt").write_text("кошка кошка кошка")
    index = docs.DocIndex(FakeEmbedClient(), "fake", docs_dir, tmp_path / "idx.json", [kb])
    assert "Платёж" in index.retrieve("налог")
    assert index.retrieve("кошка") == ""  # чужой документ не подмешивается, а слабое совпадение отбрасывается

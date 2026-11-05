"""Тесты браузера на настоящем Chromium (headless) и локальной HTML-странице.
Пропускаются, если Playwright или Chromium не установлены."""

import pytest

from lm_agent import Agent, Settings, ToolRegistry
from lm_agent.agent import STALE_NOTE
from lm_agent.toolkits import browser as browser_kit

playwright = pytest.importorskip("playwright.sync_api")

PAGE = """<!doctype html><html><head><title>Тестовая форма</title></head><body>
<h1>Заказ пиццы</h1>
<a href="#about">О нас</a>
<form onsubmit="event.preventDefault(); document.getElementById('out').textContent = 'Заказ: ' + this.q.value;">
  <label>Пожелания <input name="q" placeholder="без лука"></label>
  <label>Пароль <input type="password" name="p"></label>
  <select name="size"><option>Маленькая</option><option>Большая</option></select>
  <button type="submit">Оформить заказ</button>
</form>
<button onclick="document.getElementById('out').textContent = 'Показано меню'">Показать меню</button>
<p id="out"></p>
</body></html>"""


@pytest.fixture(scope="module")
def page_url(tmp_path_factory):
    path = tmp_path_factory.mktemp("site") / "index.html"
    path.write_text(PAGE, encoding="utf-8")
    return path.as_uri()


@pytest.fixture
def setup(tmp_path):
    def factory(approve=True, mode="risky"):
        asked = []
        reg = ToolRegistry(approver=lambda name, args: asked.append((name, args)) or approve)
        b = browser_kit.Browser(tmp_path / "profile", tmp_path / "shots", headless=True)
        browser_kit.register(reg, b, mode)
        return reg, b, asked

    made = []

    def tracked(*a, **kw):
        made.append(factory(*a, **kw))
        return made[-1]

    yield tracked
    for _, b, _ in made:
        b.close()


def ref_of(snapshot: str, text: str) -> int:
    line = next(l for l in snapshot.splitlines() if text in l and l.startswith("["))
    return int(line[1 : line.index("]")])


def test_open_and_snapshot(setup, page_url):
    reg, _, _ = setup()
    try:
        snap = reg.call("browser_open", {"url": page_url})
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Chromium недоступен: {e}")
    if snap.startswith("Ошибка") and "Браузер не найден" in snap:
        pytest.skip("Нет ни Chromium, ни Chrome")
    assert "Тестовая форма" in snap
    assert 'link "О нас"' in snap and "#about" not in snap  # у ссылки с текстом адрес не нужен
    assert "(пароль — вводит пользователь)" in snap
    assert "варианты: Маленькая | Большая" in snap
    assert '"Оформить заказ" (отправляет форму)' in snap
    assert "Заказ пиццы" in snap  # текст страницы


def test_click_safe_without_confirmation(setup, page_url):
    reg, _, asked = setup()
    snap = reg.call("browser_open", {"url": page_url})
    if "Браузер не найден" in snap:
        pytest.skip("Нет ни Chromium, ни Chrome")
    out = reg.call("browser_click", {"ref": ref_of(snap, "Показать меню")})
    assert "Показано меню" in out
    assert asked == []


def test_submit_requires_confirmation(setup, page_url):
    reg, _, asked = setup(approve=False)
    snap = reg.call("browser_open", {"url": page_url})
    if "Браузер не найден" in snap:
        pytest.skip("Нет ни Chromium, ни Chrome")
    out = reg.call("browser_type", {"ref": ref_of(snap, "Пожелания"), "text": "побольше сыра"})
    assert '= "побольше сыра"' in out and asked == []  # просто ввод — без вопросов

    denied = reg.call("browser_click", {"ref": ref_of(out, "Оформить заказ")})
    assert denied.startswith("Отклонено")
    name, shown = asked[0]
    assert name == "browser_click" and "Оформить заказ" in shown["что это"]

    reg.approver = lambda n, a: True
    done = reg.call("browser_click", {"ref": ref_of(out, "Оформить заказ")})
    assert "Заказ: побольше сыра" in done


def test_read_page_in_parts(setup, tmp_path):
    reg, _, _ = setup()
    long_page = tmp_path / "long.html"
    long_page.write_text("<html><body>" + "".join(f"<p>Абзац {i} " + "текст " * 40 + "</p>" for i in range(60)) + "</body></html>")
    snap = reg.call("browser_open", {"url": long_page.as_uri()})
    if "Браузер не найден" in snap:
        pytest.skip("Нет ни Chromium, ни Chrome")
    assert "Абзац 59" not in snap  # в снимке только видимая часть
    first = reg.call("browser_read_page", {})
    assert first.startswith("Часть 1 из ") and "browser_read_page(part=2)" in first
    total = int(first.split()[3])
    last = reg.call("browser_read_page", {"part": total})
    assert "Абзац 59" in last and "дальше" not in last


def test_password_refused_and_select(setup, page_url):
    reg, _, _ = setup()
    snap = reg.call("browser_open", {"url": page_url})
    if "Браузер не найден" in snap:
        pytest.skip("Нет ни Chromium, ни Chrome")
    assert "пароли агент не вводит" in reg.call("browser_type", {"ref": ref_of(snap, "пароль"), "text": "123"})
    out = reg.call("browser_select", {"ref": ref_of(snap, "варианты"), "option": "Большая"})
    assert '= "Большая"' in out


def test_stale_snapshots_compacted(tmp_path):
    class FakeClient:
        def __init__(self):
            self.responses = [
                {"message": {"content": "", "tool_calls": [{"function": {"name": "browser_snapshot", "arguments": {}}}]}},
                {"message": {"content": "", "tool_calls": [{"function": {"name": "browser_snapshot", "arguments": {}}}]}},
                {"message": {"content": "готово"}},
            ]

        def chat(self, **kw):
            return self.responses.pop(0)

    reg = ToolRegistry()
    counter = iter(range(100))

    @reg.register
    def browser_snapshot() -> str:
        """Снимок."""
        return f"Вкладка 1\nURL: x\n" + "строка\n" * 50 + str(next(counter))

    agent = Agent(client=FakeClient(), tools=reg, settings=Settings(workspace=tmp_path))
    agent.ask("go")
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert tool_msgs[0]["content"].endswith(STALE_NOTE)
    assert not tool_msgs[1]["content"].endswith(STALE_NOTE)


def test_unreachable_site_explained(setup):
    reg, _, _ = setup()
    import socket

    with socket.socket() as sock:  # свободный порт, где никто не слушает
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    out = reg.call("browser_open", {"url": f"http://127.0.0.1:{port}/"})
    if "Браузер не найден" in out:
        pytest.skip("Нет ни Chromium, ни Chrome")
    assert out.startswith("⚠️ Сайт не открылся: сервер отклонил подключение (ERR_CONNECTION_REFUSED)")
    assert "не агента" in out and "Элементы" not in out  # без бесполезного снимка страницы ошибки


def test_typing_into_non_input_explained(setup, page_url):
    reg, _, _ = setup()
    snap = reg.call("browser_open", {"url": page_url})
    if "Браузер не найден" in snap:
        pytest.skip("Нет ни Chromium, ни Chrome")
    out = reg.call("browser_type", {"ref": ref_of(snap, "Показать меню"), "text": "x"})
    assert "нельзя ввести текст — это button, а не поле ввода" in out

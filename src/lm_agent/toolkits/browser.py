"""Управление настоящим браузером (Chromium через Playwright).

Модель видит страницу как текст: список интерактивных элементов с номерами [N] и видимый текст.
Действия ссылаются на элементы по номеру. Браузер использует отдельный профиль (data_dir/browser):
в нужные сайты пользователь входит сам в окне браузера, пароли агенту не передаются.

Playwright (sync API) привязан к потоку, в котором запущен, а агент в веб-режиме работает
в разных потоках, — поэтому все вызовы браузера идут через один выделенный поток.
"""

from __future__ import annotations

import atexit
import queue
import re
import threading
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..tools import ToolRegistry

# Снимок — только то, что на экране и рядом: каждый токен снимка модель читает ~6 мс.
MAX_ELEMENTS = 60
MAX_TEXT = 2500
NEAR_PX = 300  # насколько за краем экрана ещё учитывать элементы
READ_CHUNK = 5000
CALL_TIMEOUT = 120

# Клики и нажатия, после которых что-то меняется необратимо, — спрашиваем пользователя.
RISKY = re.compile(
    r"отправ|оплат|купи|заказ|оформ|удал|подтвер|опублик|сохран|подпис|перев[её]с|выйти|"
    r"send|submit|pay|buy|purchase|order|checkout|delete|remove|confirm|publish|save|subscribe|transfer|log ?out|sign ?out",
    re.I,
)

SNAPSHOT_JS = r"""
() => {
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link],'
    + ' [role=tab], [role=menuitem], [role=checkbox], [role=radio], [role=option], [role=switch], [role=combobox],'
    + ' [contenteditable=""], [contenteditable=true], [onclick]';
  const MAX = %d, MAX_TEXT = %d, NEAR = %d;
  document.querySelectorAll('[data-lm-ref]').forEach(e => e.removeAttribute('data-lm-ref'));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const near = r => r.bottom > -NEAR && r.top < innerHeight + NEAR;
  const out = [];
  let above = 0, below = 0;
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || el.disabled) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (!near(r) || out.length >= MAX) { if (r.top < 0) above++; else below++; continue; }
    const ref = out.length + 1;
    el.setAttribute('data-lm-ref', String(ref));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'input' ? (type || 'text') : tag);
    let name = el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
      || (tag !== 'input' && tag !== 'select' && tag !== 'textarea' ? el.innerText : '')
      || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('name') || '';
    if (!clean(name)) { const img = el.querySelector('img[alt]'); if (img) name = img.alt; }
    if (!clean(name) && tag === 'input' && ['submit', 'button'].includes(type)) name = el.value;
    const item = { ref, role, name: clean(name).slice(0, 60) };
    if (tag === 'input' || tag === 'textarea') {
      if (type === 'password') item.password = true;
      else if (['checkbox', 'radio'].includes(type)) item.checked = el.checked;
      else if (el.value && !['submit', 'button'].includes(type)) item.value = el.value.slice(0, 60);
    }
    if (tag === 'select') {
      item.value = clean(el.options[el.selectedIndex] && el.options[el.selectedIndex].text);
      item.options = [...el.options].slice(0, 15).map(o => clean(o.text));
    }
    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      // у ссылки с понятным текстом адрес модели почти не нужен — экономим токены
      if (!item.name || href.startsWith('http')) item.href = href.slice(0, 60);
    }
    if ((tag === 'button' || (tag === 'input' && ['submit', 'image'].includes(type))) && el.form && el.type === 'submit') item.submit = true;
    out.push(item);
  }
  // текст только видимой части страницы
  const parts = [];
  let total = 0;
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && total < MAX_TEXT) {
    const node = walker.currentNode, p = node.parentElement;
    const t = clean(node.textContent);
    if (!t || !p || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(p.tagName)) continue;
    const r = p.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.top > innerHeight) continue;
    parts.push(t);
    total += t.length + 1;
  }
  const doc = document.documentElement;
  return {
    title: document.title, url: location.href, elements: out, above, below,
    text: parts.join(' ').slice(0, MAX_TEXT),
    scrollY: Math.round(scrollY), scrollMax: Math.max(0, doc.scrollHeight - innerHeight),
  };
}
""" % (MAX_ELEMENTS, MAX_TEXT, NEAR_PX)

READ_JS = "() => (document.body && document.body.innerText || '').replace(/\\n{3,}/g, '\\n\\n').trim()"


class _Worker(threading.Thread):
    """Поток, в котором живёт Playwright. Все операции с браузером выполняются здесь по очереди."""

    def __init__(self, idle: Callable[[], None] | None = None, interval: float = 0.5) -> None:
        super().__init__(daemon=True, name="lm-agent-browser")
        self.jobs: queue.Queue = queue.Queue()
        # idle вызывается после каждой задачи и раз в interval секунд простоя (кадры для живой панели)
        self.idle = idle
        self.interval = interval
        self.start()

    def run(self) -> None:
        while True:
            try:
                fn, fut = self.jobs.get(timeout=self.interval)
            except queue.Empty:
                self._idle()
                continue
            if fn is None:
                fut.set_result(None)
                return
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: BLE001 — исключение уходит вызывающему потоку
                fut.set_exception(e)
            self._idle()

    def _idle(self) -> None:
        if self.idle:
            try:
                self.idle()
            except Exception:  # noqa: BLE001 — кадр не снялся — не страшно
                pass

    def call(self, fn: Callable[[], Any], timeout: float = CALL_TIMEOUT) -> Any:
        fut: Future = Future()
        self.jobs.put((fn, fut))
        return fut.result(timeout)


def describe(item: dict) -> str:
    parts = [f"[{item['ref']}] {item['role']}"]
    if item.get("name"):
        parts.append(f'"{item["name"]}"')
    if item.get("password"):
        parts.append("(пароль — вводит пользователь)")
    if "value" in item and item["value"]:
        parts.append(f'= "{item["value"]}"')
    if "checked" in item:
        parts.append("☑" if item["checked"] else "☐")
    if item.get("options"):
        parts.append("варианты: " + " | ".join(item["options"]))
    if item.get("href"):
        parts.append(f"→ {item['href']}")
    if item.get("submit"):
        parts.append("(отправляет форму)")
    return " ".join(parts)


class Browser:
    def __init__(self, profile_dir: Path, screenshots_dir: Path, headless: bool = False, channel: str = "auto") -> None:
        self.profile_dir = profile_dir
        self.screenshots_dir = screenshots_dir
        self.headless = headless
        # auto — Chromium от Playwright, а если он не скачан — установленный Google Chrome;
        # chromium / chrome / msedge — только указанный.
        self.channel = channel
        self.elements: dict[int, dict] = {}  # последний снимок: номер → описание элемента
        self.url = ""
        self._worker: _Worker | None = None
        self._pw = None
        self._context = None
        self._page = None
        # Живая панель: кто смотрит экран браузера (callback(jpeg, meta)), последний кадр.
        self._viewers: set[Callable[[bytes, dict], None]] = set()
        self._last_frame: tuple[bytes, dict] | None = None
        self.agent_active = False
        atexit.register(self.close)

    # --- всё ниже, кроме call/close, выполняется в потоке браузера ---

    def call(self, fn: Callable[[], Any]) -> Any:
        if self._worker is None:
            self._worker = _Worker(idle=self._capture)
        return self._worker.call(fn)

    # --- живая панель: кадры экрана и действия пользователя ---

    def add_viewer(self, callback: Callable[[bytes, dict], None]) -> None:
        self._viewers.add(callback)
        if self._last_frame:
            callback(*self._last_frame)

    def remove_viewer(self, callback: Callable[[bytes, dict], None]) -> None:
        self._viewers.discard(callback)

    def status(self) -> dict:
        return {"open": self._context is not None, "agent": self.agent_active, "url": self.url}

    def _capture(self) -> None:
        """Выполняется в потоке браузера: снимает кадр, если кто-то смотрит."""
        if not self._viewers or self._context is None or self._page is None or self._page.is_closed():
            return
        page = self._page
        data = page.screenshot(type="jpeg", quality=60, timeout=3_000, animations="allow")
        meta = {"url": page.url, "title": page.title(), "agent": self.agent_active}
        if self._last_frame and self._last_frame[0] == data and self._last_frame[1] == meta:
            return  # ничего не изменилось — не гоняем одинаковые кадры
        self._last_frame = (data, meta)
        for callback in list(self._viewers):
            callback(data, meta)

    def _safe_capture(self) -> None:
        try:
            self._capture()
        except Exception:  # noqa: BLE001
            pass

    def user_action(self, kind: str, **kw: Any) -> None:
        """Действие пользователя из живой панели (клик, ввод, прокрутка, переход). Подтверждения не нужны."""

        def job():
            page = self._ensure()
            if kind == "click":
                page.mouse.click(float(kw["x"]), float(kw["y"]))
            elif kind == "type":
                page.keyboard.type(str(kw["text"])[:2000])
            elif kind == "key":
                page.keyboard.press(str(kw["key"])[:40])
            elif kind == "scroll":
                page.mouse.wheel(0, max(-3000, min(3000, float(kw.get("dy", 0)))))
            elif kind == "navigate":
                url = str(kw["url"]).strip()
                if not re.match(r"^(https?|file)://", url):
                    url = "https://" + url
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            elif kind == "back":
                page.go_back(wait_until="domcontentloaded", timeout=30_000)
            elif kind == "open":
                pass  # просто запустить браузер
            else:
                raise ValueError(f"неизвестное действие {kind!r}")
            page.wait_for_timeout(150)
            self._last_frame = None  # следующий кадр — обязательно отправить

        self.call(job)

    def _ensure(self):
        if self._context is None:
            from playwright.sync_api import sync_playwright

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            if self._pw is None:
                self._pw = sync_playwright().start()
            self._context = self._launch()
            self._context.on("page", self._on_new_page)
            # пользователь закрыл окно — при следующем действии браузер откроется заново
            self._context.on("close", lambda _: self._forget_context())
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        if self._page is None or self._page.is_closed():
            pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = pages[-1] if pages else self._context.new_page()
        return self._page

    def _launch(self):
        options = dict(
            headless=self.headless, locale="ru-RU", viewport={"width": 1280, "height": 860},
            # Встроенный DNS-резолвер Chrome и QUIC плохо работают через VPN — используем системные.
            args=["--disable-features=AsyncDns,DnsOverHttps", "--disable-quic"],
        )
        launch = self._pw.chromium.launch_persistent_context
        if self.channel not in ("auto", "chromium", ""):
            return launch(str(self.profile_dir), channel=self.channel, **options)
        try:
            return launch(str(self.profile_dir), **options)
        except Exception as e:
            if self.channel != "auto" or "Executable doesn't exist" not in str(e):
                raise
            try:
                return launch(str(self.profile_dir), channel="chrome", **options)
            except Exception:
                raise RuntimeError(
                    "Браузер не найден. Установите Chromium (uv run playwright install chromium) или Google Chrome."
                ) from e

    def _on_new_page(self, page) -> None:
        self._page = page  # новая вкладка (target=_blank, window.open) становится текущей

    def _forget_context(self) -> None:
        self._context = self._page = None

    def _settle(self, page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:  # noqa: BLE001 — медленная страница не повод падать
            pass
        page.wait_for_timeout(700)

    def _locator(self, page, ref: int):
        loc = page.locator(f'[data-lm-ref="{int(ref)}"]')
        if loc.count() == 0:
            raise LookupError(f"элемента [{ref}] нет на странице — вызови browser_snapshot и возьми новый номер")
        return loc.first

    def _snapshot(self, page) -> str:
        data = page.evaluate(SNAPSHOT_JS)
        self.elements = {e["ref"]: e for e in data["elements"]}
        self.url = data["url"]
        pages = [p for p in self._context.pages if not p.is_closed()]
        lines = [
            f"Вкладка {pages.index(page) + 1} из {len(pages)}: {data['title']}",
            f"URL: {data['url']}",
            f"Прокрутка: {data['scrollY']} из {data['scrollMax']} px",
            "",
            "Элементы на экране (используй номер в browser_click / browser_type / browser_select):",
            *(describe(e) for e in data["elements"]),
        ]
        if data["above"] or data["below"]:
            lines.append(f"…ещё элементов: выше {data['above']}, ниже {data['below']} — прокрути (browser_scroll), чтобы увидеть")
        lines += [
            "", "Видимый текст (всю страницу целиком читай через browser_read_page):",
            data["text"] or "(пусто)",
        ]
        return "\n".join(lines)

    # --- действия (вызываются из любого потока) ---

    def run(self, action: Callable[[Any], Any], snapshot: bool = True) -> str:
        def job():
            self.agent_active = True
            try:
                page = self._ensure()
                result = action(page)
                self._safe_capture()  # кадр сразу после действия — зритель видит, что сделал агент
                if not snapshot:
                    return result
                self._settle(self._ensure())  # действие могло открыть новую вкладку
                self._safe_capture()
                return self._snapshot(self._ensure())
            finally:
                self.agent_active = False

        return self.call(job)

    def close(self) -> None:
        if self._worker is None:
            return

        def job():
            for step in (lambda: self._context and self._context.close(), lambda: self._pw and self._pw.stop()):
                try:
                    step()
                except Exception:  # noqa: BLE001 — закрываем сколько получится
                    pass
            self._context = self._page = self._pw = None

        try:
            self._worker.call(job, timeout=15)
            self._worker.jobs.put((None, Future()))  # остановить поток
        except Exception:  # noqa: BLE001 — при выходе из программы не мешаем завершению
            pass
        self._worker = None

    def element(self, ref: Any) -> dict | None:
        try:
            return self.elements.get(int(ref))
        except (TypeError, ValueError):
            return None


def register(registry: ToolRegistry, browser: Browser, confirm_mode: str = "risky") -> Browser:
    """confirm_mode: risky — спрашивать перед отправкой форм и «опасными» кнопками; all — перед любым
    действием на странице; none — никогда."""

    def risky_click(args: dict) -> bool:
        if confirm_mode != "risky":
            return confirm_mode == "all"
        item = browser.element(args.get("ref"))
        return item is None or bool(item.get("submit")) or bool(RISKY.search(item.get("name", "")))

    def risky_submit(args: dict) -> bool:
        if confirm_mode != "risky":
            return confirm_mode == "all"
        return bool(args.get("submit"))

    def risky_key(args: dict) -> bool:
        if confirm_mode != "risky":
            return confirm_mode == "all"
        return str(args.get("key", "")).lower() in ("enter", "return")

    def all_only(args: dict) -> bool:
        return confirm_mode == "all"

    def explain(args: dict) -> str:
        item = browser.element(args.get("ref"))
        target = describe(item) if item else "элемент не найден в последнем снимке"
        return f"{target} на {browser.url}" if "ref" in args else browser.url

    @registry.register
    def browser_open(url: str) -> str:
        """Открывает адрес в браузере (выполняет JavaScript, сохраняет вход в аккаунты) и возвращает снимок страницы.

        url: адрес, например "https://example.com"
        """
        if not re.match(r"^(https?|file)://", url):
            url = "https://" + url
        return browser.run(lambda page: page.goto(url, wait_until="domcontentloaded", timeout=30_000))

    @registry.register
    def browser_snapshot() -> str:
        """Возвращает текущее состояние страницы: элементы с номерами и текст."""
        return browser.run(lambda page: None)

    @registry.register(confirm=risky_click, explain=explain)
    def browser_click(ref: int) -> str:
        """Кликает по элементу страницы по его номеру из снимка.

        ref: номер элемента, например 12
        """
        return browser.run(lambda page: browser._locator(page, ref).click(timeout=10_000))

    @registry.register(confirm=risky_submit, explain=explain)
    def browser_type(ref: int, text: str, submit: bool = False) -> str:
        """Вводит текст в поле (заменяя прежнее содержимое). submit=true — затем нажать Enter.

        ref: номер поля из снимка
        text: что ввести
        submit: нажать Enter после ввода (отправит форму или поиск)
        """
        item = browser.element(ref)
        if item and item.get("password"):
            return "Ошибка: пароли агент не вводит. Попроси пользователя ввести пароль самому в окне браузера."

        def action(page):
            loc = browser._locator(page, ref)
            loc.fill(text, timeout=10_000)
            if submit:
                loc.press("Enter")

        return browser.run(action)

    @registry.register(confirm=all_only, explain=explain)
    def browser_select(ref: int, option: str) -> str:
        """Выбирает вариант в выпадающем списке (select) по его тексту.

        ref: номер списка из снимка
        option: текст варианта
        """
        return browser.run(lambda page: browser._locator(page, ref).select_option(label=option, timeout=10_000))

    @registry.register(confirm=risky_key, explain=explain)
    def browser_press(key: str) -> str:
        """Нажимает клавишу на странице: Enter, Escape, Tab, ArrowDown, PageDown и т. п.

        key: название клавиши
        """
        return browser.run(lambda page: page.keyboard.press(key))

    @registry.register
    def browser_read_page(part: int = 1) -> str:
        """Возвращает весь текст текущей страницы частями — чтобы прочитать статью или длинный список.

        part: номер части, начиная с 1
        """

        def job():
            text = browser._ensure().evaluate(READ_JS)
            total = max(1, -(-len(text) // READ_CHUNK))
            n = max(1, min(int(part), total))
            chunk = text[(n - 1) * READ_CHUNK : n * READ_CHUNK]
            more = f"\n…дальше: browser_read_page(part={n + 1})" if n < total else ""
            return f"Часть {n} из {total} — {browser.url}\n{chunk}{more}"

        return browser.call(job)

    @registry.register
    def browser_scroll(direction: str = "down") -> str:
        """Прокручивает страницу на экран вниз или вверх.

        direction: "down" или "up"
        """
        delta = -700 if direction.lower() == "up" else 700
        return browser.run(lambda page: page.mouse.wheel(0, delta))

    @registry.register
    def browser_back() -> str:
        """Возвращается на предыдущую страницу."""
        return browser.run(lambda page: page.go_back(wait_until="domcontentloaded", timeout=30_000))

    @registry.register
    def browser_tabs(switch_to: int = 0) -> str:
        """Показывает открытые вкладки; switch_to — номер вкладки, на которую переключиться.

        switch_to: номер вкладки (с 1); 0 — только показать список
        """

        def job():
            browser._ensure()
            pages = [p for p in browser._context.pages if not p.is_closed()]
            if switch_to:
                if not 1 <= int(switch_to) <= len(pages):
                    return f"Ошибка: вкладки {switch_to} нет, открыто {len(pages)}"
                browser._page = pages[int(switch_to) - 1]
                browser._page.bring_to_front()
                return browser._snapshot(browser._page)
            return "\n".join(
                f"{i}. {p.title()} — {p.url}" + (" (текущая)" if p is browser._page else "")
                for i, p in enumerate(pages, 1)
            )

        return browser.call(job)

    @registry.register
    def browser_screenshot() -> str:
        """Сохраняет скриншот текущей страницы в рабочую папку и возвращает путь к файлу (для пользователя)."""
        browser.screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = browser.screenshots_dir / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        browser.run(lambda page: page.screenshot(path=str(path)), snapshot=False)
        return f"Скриншот сохранён: {path.resolve()}"

    return browser

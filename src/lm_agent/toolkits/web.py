"""Интернет: поиск (DuckDuckGo, без ключа) и чтение страниц."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from ..tools import ToolRegistry

_MAX_PAGE = 15_000
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) LM-Agent/0.1"


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def register(registry: ToolRegistry) -> None:
    @registry.register
    def web_search(query: str, max_results: int = 5) -> str:
        """Ищет в интернете и возвращает заголовки, ссылки и фрагменты. Для подробностей открой ссылку через fetch_url.

        query: поисковый запрос
        max_results: сколько результатов вернуть (3–10)
        """
        from ddgs import DDGS

        results = DDGS().text(query, max_results=max(3, min(int(max_results), 10)))
        if not results:
            return "Ничего не найдено"
        return "\n\n".join(f"{r.get('title')}\n{r.get('href')}\n{r.get('body')}" for r in results)

    @registry.register
    def fetch_url(url: str) -> str:
        """Скачивает страницу по ссылке и возвращает её текст (HTML очищается от разметки).

        url: адрес страницы, http:// или https://
        """
        if not url.startswith(("http://", "https://")):
            return "Ошибка: нужен адрес http:// или https://"
        resp = httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        text = html_to_text(resp.text) if "html" in ctype else resp.text
        if len(text) > _MAX_PAGE:
            text = text[:_MAX_PAGE] + "\n…(обрезано)"
        return f"[{resp.status_code} {resp.url}]\n{text}"

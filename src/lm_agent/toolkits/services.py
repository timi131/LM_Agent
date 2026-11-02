"""Внешние сервисы и HTTP API.

Сервисы описываются в services.json (см. services.example.json). Токены берутся из переменных
окружения через ${VAR} и подставляются в заголовки на стороне агента — модель их не видит.
Чтение (GET) выполняется сразу, изменяющие запросы (POST/PUT/PATCH/DELETE) — с подтверждением.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

from ..tools import ToolRegistry

_MAX_RESPONSE = 15_000
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)


def load_services(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _format(resp: httpx.Response) -> str:
    try:
        body = json.dumps(resp.json(), ensure_ascii=False, indent=1)
    except ValueError:
        body = resp.text
    if len(body) > _MAX_RESPONSE:
        body = body[:_MAX_RESPONSE] + "\n…(обрезано)"
    return f"HTTP {resp.status_code}\n{body}"


def _parse_json(text: str, what: str):
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{what} должен быть JSON: {e}") from e


def _unsafe(args: dict) -> bool:
    return str(args.get("method", "GET")).upper() not in _SAFE_METHODS


def register(registry: ToolRegistry, services_file: Path) -> None:
    def services() -> dict[str, dict]:
        return load_services(services_file)  # перечитываем, чтобы правки конфига работали без перезапуска

    @registry.register
    def list_services() -> str:
        """Показывает подключённые внешние сервисы (CRM, API) и их описание."""
        items = services()
        if not items:
            return f"Сервисы не настроены. Добавьте их в {services_file}"
        return "\n".join(f"- {name}: {cfg.get('description', '')} ({cfg.get('base_url')})" for name, cfg in items.items())

    @registry.register(confirm=_unsafe)
    def call_service(service: str, path: str, method: str = "GET", query: str = "", body: str = "") -> str:
        """Вызывает API подключённого сервиса (авторизация подставляется автоматически). Список — list_services.

        service: имя сервиса из list_services
        path: путь относительно базового адреса, например "/leads" или "/contacts/42"
        method: GET, POST, PUT, PATCH или DELETE
        query: параметры запроса в виде JSON-объекта, например {"limit": 10}
        body: тело запроса в виде JSON
        """
        cfg = services().get(service)
        if cfg is None:
            return f"Ошибка: сервис '{service}' не найден. Доступны: {', '.join(services()) or 'нет'}"
        if "://" in path:
            return "Ошибка: path должен быть относительным путём, а не полным адресом"
        url = cfg["base_url"].rstrip("/") + "/" + path.lstrip("/")
        headers = {k: _expand(v) for k, v in cfg.get("headers", {}).items()}
        params = {k: _expand(str(v)) for k, v in cfg.get("params", {}).items()}
        params.update(_parse_json(query, "query") or {})
        resp = httpx.request(
            method.upper(), url, headers=headers, params=params,
            json=_parse_json(body, "body"), timeout=cfg.get("timeout", 30),
        )
        return _format(resp)

    @registry.register(confirm=_unsafe)
    def http_request(url: str, method: str = "GET", body: str = "", headers: str = "") -> str:
        """Произвольный HTTP-запрос к любому API (для неподключённых сервисов). Изменяющие запросы требуют подтверждения.

        url: полный адрес
        method: GET, POST, PUT, PATCH или DELETE
        body: тело запроса в виде JSON
        headers: заголовки в виде JSON-объекта
        """
        resp = httpx.request(
            method.upper(), url, headers=_parse_json(headers, "headers") or {},
            json=_parse_json(body, "body"), timeout=30, follow_redirects=True,
        )
        return _format(resp)

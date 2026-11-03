"""Наборы инструментов агента. `build_registry` собирает всё вместе по настройкам."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from ..tools import Approver, ToolRegistry
from . import basic, browser, docs, files, mail, memory, services, shell, tables, web

ALL = ("basic", "files", "web", "browser", "shell", "services", "mail", "docs", "tables", "memory")


def make_browser(settings: Settings) -> browser.Browser:
    return browser.Browser(
        settings.data_dir / "browser", settings.workspace / "screenshots",
        settings.browser_headless, settings.browser_channel,
    )


def build_registry(
    settings: Settings,
    client: Any,
    approver: Approver | None = None,
    enabled: tuple[str, ...] = ALL,
    browser_instance: browser.Browser | None = None,
) -> tuple[ToolRegistry, memory.Memory | None]:
    registry = ToolRegistry(approver=approver)
    mem = None
    if "basic" in enabled:
        basic.register(registry)
    fa = files.register(registry, settings.workspace, settings.file_roots) if "files" in enabled else None
    if "web" in enabled:
        web.register(registry)
    if "browser" in enabled:
        b = browser_instance or make_browser(settings)
        browser.register(registry, b, settings.browser_confirm)
    if "shell" in enabled:
        shell.register(registry, fa or files.FileAccess(settings.workspace, settings.file_roots), settings.command_timeout)
    if "services" in enabled:
        services.register(registry, settings.services_file)
    if "mail" in enabled:
        mail.register(registry)
    if "docs" in enabled:
        index = docs.DocIndex(client, settings.embed_model, settings.docs_dir, settings.data_dir / "doc_index.json")
        docs.register(registry, index)
    if "tables" in enabled:
        tables.register(registry, tables.Tables([settings.docs_dir, settings.workspace], settings.data_dir / "tables"))
    if "memory" in enabled:
        mem = memory.Memory(settings.data_dir / "memory.json")
        memory.register(registry, mem)
    return registry, mem


MAX_FILES_IN_PROMPT = 30


def make_context(settings: Settings, mem: memory.Memory | None, enabled: tuple[str, ...] = ALL):
    """Что подставляется в системный промпт при каждом новом диалоге: память и список файлов пользователя."""

    def context() -> str:
        parts = []
        if mem is not None and mem.as_prompt():
            parts.append(mem.as_prompt())
        if "docs" in enabled or "tables" in enabled:
            names = [str(p.relative_to(settings.docs_dir)) for p in docs.list_documents(settings.docs_dir)]
            table_names = {p.name for p in tables.find_tables([settings.docs_dir, settings.workspace])}
            if names or table_names:
                shown = sorted(set(names) | table_names)[:MAX_FILES_IN_PROMPT]
                lines = "\n".join(
                    f"- {n}" + (" (таблица: отвечай через table_info/table_query)" if Path(n).name in table_names else "")
                    for n in shown
                )
                parts.append(f"Файлы пользователя, загруженные в «Документы»:\n{lines}")
        return "\n\n".join(parts)

    return context

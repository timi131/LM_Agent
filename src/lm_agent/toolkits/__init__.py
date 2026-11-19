"""Наборы инструментов агента. `build_registry` собирает всё вместе по настройкам."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..tools import Approver, ToolRegistry
from . import basic, browser, docs, files, mail, memory, reports, services, shell, tables, vscode, web

ALL = ("basic", "files", "web", "browser", "shell", "services", "mail", "docs", "tables", "reports", "memory", "vscode")


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
    file_access: files.FileAccess | None = None,
) -> tuple[ToolRegistry, memory.Memory | None]:
    registry = ToolRegistry(approver=approver)
    mem = None
    if "basic" in enabled:
        basic.register(registry)
    fa = file_access or files.FileAccess(
        settings.workspace, settings.file_roots, settings.data_dir / "workspace_folders.json",
    )
    settings.workspace = fa.workspace
    if "files" in enabled:
        files.register(registry, fa)
    registry.file_access = fa
    if "web" in enabled:
        web.register(registry)
    if "browser" in enabled:
        b = browser_instance or make_browser(settings)
        browser.register(registry, b, settings.browser_confirm)
    if "shell" in enabled:
        shell.register(registry, fa, settings.command_timeout)
    if "vscode" in enabled:
        vscode.register(registry, settings.vscode_url, settings.vscode_token_file or settings.data_dir / "vscode_token")
    if "services" in enabled:
        services.register(registry, settings.services_file)
    if "mail" in enabled:
        mail.register(registry)
    if "docs" in enabled:
        index = docs.DocIndex(
            client, settings.embed_model, settings.docs_dir, settings.data_dir / "doc_index.json", settings.knowledge,
        )
        docs.register(registry, index)
        if settings.knowledge:
            registry.retriever = index.retrieve
    table_store = None
    if "tables" in enabled:
        table_store = tables.Tables(
            list(dict.fromkeys([settings.docs_dir, *fa.project_folders])), settings.data_dir / "tables",
        )
        tables.register(registry, table_store)
    registry.table_store = table_store
    if "reports" in enabled:
        settings.workspace.mkdir(parents=True, exist_ok=True)
        reports.register(
            registry, settings.workspace.resolve(), table_store,
            workspace_provider=lambda: fa.workspace,
            confirm_output=lambda: fa.requires_confirmation("reports"),
        )
    if "memory" in enabled:
        mem = memory.Memory(settings.data_dir / "memory.json")
        memory.register(registry, mem)
    return registry, mem


MAX_FILES_IN_PROMPT = 30


def make_context(
    settings: Settings, mem: memory.Memory | None, enabled: tuple[str, ...] = ALL,
    file_access: files.FileAccess | None = None,
):
    """Что подставляется в системный промпт при каждом новом диалоге: память и список файлов пользователя."""

    def context() -> str:
        parts = []
        if mem is not None and mem.as_prompt():
            parts.append(mem.as_prompt())
        if file_access is not None:
            folders = file_access.managed_folders
            if folders:
                lines = [
                    f"- {'рабочая папка' if item['active'] else 'доступная папка'}: {item['path']}"
                    for item in folders
                ]
                parts.append("Папки, выбранные пользователем:\n" + "\n".join(lines))
        if "tables" in enabled:
            folders = [settings.docs_dir, *(file_access.project_folders if file_access else [settings.workspace])]
            names = [p.name for p in tables.find_tables(folders)][:MAX_FILES_IN_PROMPT]
            if names:
                lines = "\n".join(f"- {n}" for n in names)
                parts.append(f"Таблицы пользователя (отвечай по ним через table_info и table_query):\n{lines}")
        if "docs" in enabled and docs.all_documents(settings.docs_dir, settings.knowledge):
            parts.append("Документы и база знаний подключены: ищи в них через search_documents, открывать файлы не нужно.")
        return "\n\n".join(parts)

    return context

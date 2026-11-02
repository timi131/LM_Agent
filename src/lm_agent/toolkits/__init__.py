"""Наборы инструментов агента. `build_registry` собирает всё вместе по настройкам."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..tools import Approver, ToolRegistry
from . import basic, docs, files, mail, memory, services, shell, web

ALL = ("basic", "files", "web", "shell", "services", "mail", "docs", "memory")


def build_registry(
    settings: Settings,
    client: Any,
    approver: Approver | None = None,
    enabled: tuple[str, ...] = ALL,
) -> tuple[ToolRegistry, memory.Memory | None]:
    registry = ToolRegistry(approver=approver)
    mem = None
    if "basic" in enabled:
        basic.register(registry)
    fa = files.register(registry, settings.workspace, settings.file_roots) if "files" in enabled else None
    if "web" in enabled:
        web.register(registry)
    if "shell" in enabled:
        shell.register(registry, fa or files.FileAccess(settings.workspace, settings.file_roots), settings.command_timeout)
    if "services" in enabled:
        services.register(registry, settings.services_file)
    if "mail" in enabled:
        mail.register(registry)
    if "docs" in enabled:
        index = docs.DocIndex(client, settings.embed_model, settings.docs_dir, settings.data_dir / "doc_index.json")
        docs.register(registry, index)
    if "memory" in enabled:
        mem = memory.Memory(settings.data_dir / "memory.json")
        memory.register(registry, mem)
    return registry, mem

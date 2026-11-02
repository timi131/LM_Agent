"""Файлы. Относительные пути — от workspace; абсолютные — только внутри разрешённых папок.

Чтение и запись внутри workspace — без вопросов; запись в любом другом месте — с подтверждением.
"""

from __future__ import annotations

from pathlib import Path

from ..tools import ToolRegistry

_MAX_READ = 20_000
_MAX_LIST = 200


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class FileAccess:
    def __init__(self, workspace: Path, roots: list[Path]) -> None:
        self.workspace = workspace.resolve()
        self.roots = [self.workspace, *(r.expanduser().resolve() for r in roots)]

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        target = (p if p.is_absolute() else self.workspace / p).resolve()
        if not any(_within(target, r) for r in self.roots):
            allowed = ", ".join(str(r) for r in self.roots)
            raise PermissionError(f"путь вне разрешённых папок ({allowed})")
        return target

    def outside_workspace(self, path: str) -> bool:
        try:
            return not _within(self.resolve(path), self.workspace)
        except PermissionError:
            return False  # всё равно упадёт при выполнении — спрашивать незачем


def register(registry: ToolRegistry, workspace: Path, roots: list[Path]) -> FileAccess:
    workspace.mkdir(parents=True, exist_ok=True)
    fa = FileAccess(workspace, roots)

    @registry.register
    def list_files(path: str = ".") -> str:
        """Показывает содержимое папки. Относительный путь — от рабочей папки, можно абсолютный или ~/...

        path: путь к папке, по умолчанию рабочая папка
        """
        entries = sorted(fa.resolve(path).iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"{p.name}/" if p.is_dir() else p.name for p in entries[:_MAX_LIST]]
        if len(entries) > _MAX_LIST:
            lines.append(f"…и ещё {len(entries) - _MAX_LIST}")
        return "\n".join(lines) or "(пусто)"

    @registry.register
    def find_files(pattern: str, path: str = ".") -> str:
        """Ищет файлы по шаблону имени рекурсивно (например "*.pdf" или "отчёт*").

        pattern: шаблон имени файла
        path: папка, где искать
        """
        root = fa.resolve(path)
        found = []
        for p in root.rglob(pattern):
            if any(part.startswith(".") for part in p.relative_to(root).parts):
                continue
            found.append(str(p))
            if len(found) >= _MAX_LIST:
                break
        return "\n".join(found) or "Ничего не найдено"

    @registry.register
    def read_file(path: str) -> str:
        """Читает текстовый файл.

        path: путь к файлу (относительный — от рабочей папки)
        """
        text = fa.resolve(path).read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= _MAX_READ else text[:_MAX_READ] + "\n…(обрезано)"

    @registry.register(confirm=lambda a: fa.outside_workspace(a.get("path", "")))
    def write_file(path: str, content: str) -> str:
        """Записывает текст в файл (перезаписывает существующий). Вне рабочей папки спросит пользователя.

        path: путь к файлу (относительный — от рабочей папки)
        content: содержимое файла
        """
        target = fa.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Записано {len(content)} символов в {target}"

    return fa

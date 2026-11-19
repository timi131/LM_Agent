"""Файлы. Относительные пути — от workspace; абсолютные — только внутри разрешённых папок.

Чтение и запись внутри workspace — без вопросов; запись в любом другом месте — с подтверждением.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..tools import ToolRegistry

_MAX_READ = 20_000
_MAX_LIST = 200


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class FileAccess:
    def __init__(self, workspace: Path, roots: list[Path], state_file: Path | None = None) -> None:
        self.default_workspace = workspace.expanduser().resolve()
        self.workspace = self.default_workspace
        self._configured_roots = [self.default_workspace, *(r.expanduser().resolve() for r in roots)]
        self._managed_roots: list[Path] = []
        self._lock = threading.RLock()
        self.state_file = state_file
        if state_file and state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                folders = data.get("folders", [])
                if not isinstance(folders, list) or not all(isinstance(item, str) for item in folders):
                    raise ValueError("список folders должен содержать пути")
                for value in folders:
                    path = Path(value).expanduser().resolve()
                    if path.is_dir() and path not in self._managed_roots:
                        self._managed_roots.append(path)
                active = data.get("active")
                if active:
                    path = Path(active).expanduser().resolve()
                    if path.is_dir() and (path == self.default_workspace or path in self._managed_roots):
                        self.workspace = path
            except (OSError, json.JSONDecodeError, AttributeError, ValueError) as error:
                raise RuntimeError(f"Не удалось прочитать список рабочих папок {state_file}: {error}") from error
        self._refresh_roots()

    def _refresh_roots(self) -> None:
        self.roots = list(dict.fromkeys([self.workspace, *self._configured_roots, *self._managed_roots]))

    @property
    def managed_folders(self) -> list[dict]:
        with self._lock:
            return [{"path": str(path), "active": path == self.workspace} for path in self._managed_roots]

    @property
    def project_folders(self) -> list[Path]:
        with self._lock:
            return list(dict.fromkeys([self.default_workspace, *self._managed_roots]))

    def _save(self) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"active": str(self.workspace), "folders": [str(path) for path in self._managed_roots]},
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_file)

    def add_folder(self, value: str, active: bool = True) -> dict:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("Укажите полный абсолютный путь к папке")
        try:
            path = path.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"Папка не найдена: {path}") from error
        if not path.is_dir():
            raise ValueError(f"Это не папка: {path}")
        if path == Path(path.anchor):
            raise ValueError("Нельзя добавлять корень диска целиком")
        with self._lock:
            if path not in self._managed_roots:
                self._managed_roots.append(path)
            if active:
                self.workspace = path
            self._refresh_roots()
            self._save()
            return {"path": str(path), "active": path == self.workspace}

    def set_active_folder(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        with self._lock:
            if path not in self._managed_roots:
                raise ValueError("Сначала добавьте папку в список доступных")
            if not path.is_dir():
                raise ValueError(f"Папка не найдена: {path}")
            self.workspace = path
            self._refresh_roots()
            self._save()
            return path

    def remove_folder(self, value: str) -> bool:
        path = Path(value).expanduser().resolve()
        with self._lock:
            if path not in self._managed_roots:
                return False
            self._managed_roots.remove(path)
            if self.workspace == path:
                self.workspace = self.default_workspace
            self._refresh_roots()
            self._save()
            return True

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        with self._lock:
            workspace, roots = self.workspace, list(self.roots)
        target = (p if p.is_absolute() else workspace / p).resolve()
        if not any(_within(target, r) for r in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise PermissionError(f"путь вне разрешённых папок ({allowed})")
        return target

    def outside_workspace(self, path: str) -> bool:
        try:
            target = self.resolve(path)
            with self._lock:
                return not _within(target, self.workspace)
        except PermissionError:
            return False  # всё равно упадёт при выполнении — спрашивать незачем

    def requires_confirmation(self, path: str) -> bool:
        try:
            target = self.resolve(path)
            with self._lock:
                managed = any(_within(target, root) for root in self._managed_roots)
                return managed or not _within(target, self.workspace)
        except PermissionError:
            return False


def register(
    registry: ToolRegistry, workspace: Path | FileAccess, roots: list[Path] | None = None,
    state_file: Path | None = None,
) -> FileAccess:
    fa = workspace if isinstance(workspace, FileAccess) else FileAccess(workspace, roots or [], state_file)
    fa.workspace.mkdir(parents=True, exist_ok=True)

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

    @registry.register(confirm=lambda a: fa.requires_confirmation(a.get("path", "")))
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

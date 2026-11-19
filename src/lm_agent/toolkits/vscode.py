"""Инструменты для работы с VS Code через локальное расширение-мост."""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..tools import ToolRegistry

_MAX_RESPONSE = 1_000_000
_TIMEOUT = 20
_SELECTION_TIMEOUT = 300


class VSCodeBridge:
    def __init__(self, base_url: str, token_file: Path) -> None:
        parsed = urllib.parse.urlparse(base_url)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("Некорректный порт LM_AGENT_VSCODE_URL") from error
        if (
            parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "localhost")
            or port is None or not 1 <= port <= 65535
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment
        ):
            raise ValueError("LM_AGENT_VSCODE_URL должен быть localhost URL, например http://127.0.0.1:8765")
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file.expanduser()
        self.token = self._get_or_create_token()

    def _get_or_create_token(self) -> str:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            token = self.token_file.read_text(encoding="utf-8").strip()
            if not token:
                raise RuntimeError(f"Файл токена VS Code пуст: {self.token_file}")
            os.chmod(self.token_file, 0o600)
            return token
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(token + "\n")
        return token

    def request(self, endpoint: str, data: dict | None = None, timeout: int = _TIMEOUT) -> Any:
        payload = json.dumps(data or {}).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(_MAX_RESPONSE + 1)
                if len(body) > _MAX_RESPONSE:
                    raise RuntimeError("Ответ VS Code превышает допустимый размер")
                result = json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read(2000).decode("utf-8", errors="replace")
            raise RuntimeError(f"VS Code вернул HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"Нет связи с расширением VS Code по адресу {self.base_url}. "
                "Установите и активируйте расширение LM Agent для VS Code."
            ) from error
        if not isinstance(result, dict):
            raise RuntimeError("Расширение VS Code вернуло некорректный ответ")
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result


def register(registry: ToolRegistry, base_url: str, token_file: Path) -> None:
    bridge = VSCodeBridge(base_url, token_file)

    @registry.register
    def vscode_status() -> str:
        """Проверяет доступность расширения LM Agent в VS Code."""
        result = bridge.request("health")
        project = result.get("project_folder") or "(папка проекта не выбрана)"
        analysis = result.get("analysis_folder") or "(папка анализа не выбрана)"
        return f"VS Code подключён. Проект: {project}. Папка анализа: {analysis}."

    @registry.register
    def vscode_select_project_folder() -> str:
        """Открывает диалог VS Code для выбора папки проекта. Выбор сохраняется между перезапусками."""
        result = bridge.request("select_project", {}, timeout=_SELECTION_TIMEOUT)
        if result.get("cancelled"):
            return "Выбор папки проекта отменён; текущая папка не изменилась."
        return f"Папка проекта выбрана: {result.get('path')}"

    @registry.register
    def vscode_select_analysis_folder() -> str:
        """Открывает диалог VS Code для выбора папки с файлами только для чтения и анализа."""
        result = bridge.request("select_analysis", {}, timeout=_SELECTION_TIMEOUT)
        if result.get("cancelled"):
            return "Выбор папки для анализа отменён; текущая папка не изменилась."
        return f"Папка для анализа выбрана: {result.get('path')}"

    @registry.register
    def vscode_list_files(path: str = ".", scope: str = "project") -> str:
        """Показывает файлы и папки внутри выбранного проекта или папки анализа.

        path: относительный путь внутри выбранной папки
        scope: project для папки проекта, analysis для папки анализа
        """
        result = bridge.request("list", {"path": path, "scope": scope})
        return json.dumps(result, ensure_ascii=False)

    @registry.register
    def vscode_read_file(path: str, scope: str = "project") -> str:
        """Читает текстовый файл выбранного проекта или папки анализа (папка analysis доступна только для чтения).

        path: путь к файлу относительно выбранной папки
        scope: project для папки проекта, analysis для папки анализа
        """
        result = bridge.request("read", {"path": path, "scope": scope})
        return json.dumps(result, ensure_ascii=False)

    @registry.register
    def vscode_context(include_selection: bool = True) -> str:
        """Читает открытый файл и, при наличии, выделение в активном редакторе."""
        result = bridge.request("context", {"include_selection": include_selection})
        return json.dumps(result, ensure_ascii=False)

    @registry.register
    def vscode_problems() -> str:
        """Показывает ошибки и предупреждения VS Code из панели Problems."""
        result = bridge.request("problems")
        items = result.get("problems", [])
        return json.dumps({"count": len(items), "problems": items, "truncated": result.get("truncated", False)},
                          ensure_ascii=False)

    @registry.register
    def vscode_open_file(path: str, line: int = 1, column: int = 1) -> str:
        """Открывает файл из выбранной папки проекта в VS Code и переводит курсор на строку и колонку.

        path: путь относительно выбранной папки проекта
        line: номер строки, начиная с 1
        column: номер колонки, начиная с 1
        """
        result = bridge.request("open", {"path": path, "line": line, "column": column})
        return f"Открыт файл {result.get('path')}:{result.get('line')}:{result.get('column')}"

    @registry.register(confirm=True, explain=lambda args: f"перезаписать файл проекта {args.get('path', '')}")
    def vscode_edit_file(path: str, content: str) -> str:
        """Заменяет всё содержимое существующего файла проекта и сохраняет его.

        path: путь относительно выбранной папки проекта
        content: новое полное содержимое файла
        """
        result = bridge.request("edit", {"path": path, "content": content})
        return f"Файл проекта сохранён: {result.get('path')}"

    @registry.register(confirm=True, explain=lambda args: f"создать новый файл проекта {args.get('path', '')}")
    def vscode_create_file(path: str, content: str) -> str:
        """Создаёт новый текстовый файл проекта, включая недостающие родительские папки.

        path: путь нового файла относительно выбранной папки проекта
        content: содержимое файла
        """
        result = bridge.request("create", {"path": path, "content": content})
        return f"Файл проекта создан: {result.get('path')}"

    @registry.register(confirm=True, explain=lambda args: f"отправить команду в терминал VS Code: {args.get('command', '')}")
    def vscode_run_terminal(command: str) -> str:
        """Отправляет команду в терминал VS Code, открытый в папке выбранного проекта. Результат не считывается.

        command: команда для текущей рабочей папки
        """
        result = bridge.request("terminal", {"command": command})
        return str(result.get("message", "Команда отправлена в терминал VS Code"))

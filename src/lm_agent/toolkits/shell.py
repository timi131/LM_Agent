"""Запуск команд в shell. Каждый вызов — только с подтверждением пользователя."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..tools import ToolRegistry
from .files import FileAccess

_MAX_OUTPUT = 10_000


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_OUTPUT else text[: _MAX_OUTPUT // 2] + "\n…(обрезано)…\n" + text[-_MAX_OUTPUT // 2 :]


def register(registry: ToolRegistry, files: FileAccess, timeout: int) -> None:
    @registry.register(confirm=True)
    def run_command(command: str, cwd: str = ".") -> str:
        """Выполняет команду в shell (zsh/bash) и возвращает код выхода и вывод. Пользователь подтверждает каждый запуск.

        command: команда, например "ls -la" или "python3 script.py"
        cwd: рабочая папка команды (относительная — от рабочей папки агента)
        """
        workdir: Path = files.resolve(cwd)
        try:
            proc = subprocess.run(
                command, shell=True, cwd=workdir, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return f"Ошибка: команда не завершилась за {timeout} с"
        out = [f"exit code: {proc.returncode}"]
        if proc.stdout:
            out.append("stdout:\n" + _clip(proc.stdout))
        if proc.stderr:
            out.append("stderr:\n" + _clip(proc.stderr))
        return "\n".join(out)

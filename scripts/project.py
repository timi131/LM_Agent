#!/usr/bin/env python3
"""Локальный лаунчер LM Agent."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / ".lm-agent.pid"
LOG_FILE = ROOT / ".lm-agent.log"
WEB_URL = "http://127.0.0.1:8000"


def url_ready(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return None


def ollama_ready() -> bool:
    return url_ready("http://127.0.0.1:11434/api/tags")


def web_ready() -> bool:
    return url_ready(f"{WEB_URL}/api/info")


def ensure_ollama() -> None:
    if ollama_ready():
        return
    subprocess.run(["open", "-a", "Ollama"], check=False)
    for _ in range(30):
        if ollama_ready():
            return
        time.sleep(1)
    raise RuntimeError("Ollama не запустилась за 30 секунд")


def start() -> None:
    existing = read_pid()
    if existing:
        print(f"Сервер уже запущен (PID {existing}).")
        return
    if web_ready():
        print(f"Сервер уже запущен: {WEB_URL}")
        return

    ensure_ollama()
    subprocess.run(["uv", "sync"], cwd=ROOT, check=True)

    log = LOG_FILE.open("ab")
    process = subprocess.Popen(
        ["uv", "run", "lm-agent-web"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(process.pid))

    for _ in range(30):
        if url_ready(f"{WEB_URL}/api/info"):
            print(f"Сервер запущен: {WEB_URL}")
            print(f"PID: {process.pid}")
            return
        if process.poll() is not None:
            raise RuntimeError(f"Сервер завершился. Лог: {LOG_FILE}")
        time.sleep(1)

    process.terminate()
    PID_FILE.unlink(missing_ok=True)
    raise RuntimeError(f"Сервер не ответил за 30 секунд. Лог: {LOG_FILE}")


def stop() -> None:
    pid = read_pid()
    if not pid:
        print("Сервер не запущен.")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.25)
    PID_FILE.unlink(missing_ok=True)
    print("Сервер остановлен.")


def status() -> None:
    pid = read_pid()
    if pid and web_ready():
        print(f"Сервер запущен (PID {pid}, {WEB_URL}).")
    elif web_ready():
        print(f"Сервер запущен: {WEB_URL}")
    else:
        print("Сервер не запущен.")

    print("Ollama запущена." if ollama_ready() else "Ollama не запущена.")


def open_web() -> None:
    if not read_pid():
        start()
    subprocess.run(["open", WEB_URL], check=False)


def terminal() -> None:
    ensure_ollama()
    subprocess.run(["uv", "run", "lm-agent"], cwd=ROOT, check=False)


def restart() -> None:
    stop()
    start()


def usage() -> None:
    print("Использование: python3 scripts/project.py <start|stop|restart|status|open|terminal>")


def main() -> int:
    actions = {
        "start": start,
        "stop": stop,
        "restart": restart,
        "status": status,
        "open": open_web,
        "terminal": terminal,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        usage()
        return 2
    try:
        actions[sys.argv[1]]()
        return 0
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

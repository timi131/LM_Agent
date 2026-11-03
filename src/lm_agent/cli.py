"""Интерактивный чат с агентом в терминале."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading

import ollama

from .agent import Agent, ToolEvent
from .config import Settings
from .toolkits import ALL, build_registry, make_context

HELP = "Команды: /reset — очистить историю, /tools — инструменты, /memory — память, /exit — выход"


def _short(value, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _quiet(fn):
    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001 — прогрев необязателен
            pass

    return run


def make_approver(auto: bool):
    def approve(name: str, args: dict) -> bool:
        if auto:
            return True
        if not sys.stdin.isatty():
            print(f"  ⛔ {name}: нужно подтверждение, но терминал не интерактивный — отклонено", file=sys.stderr)
            return False
        print(f"\n  ⚠️  Агент хочет выполнить {name}:", file=sys.stderr)
        for key, value in args.items():
            print(f"     {key}: {_short(value, 2000)}", file=sys.stderr)
        try:
            return input("     Разрешить? [y/N] ").strip().lower() in ("y", "yes", "д", "да")
        except (EOFError, KeyboardInterrupt):
            return False

    return approve


def build_agent(settings: Settings, verbose: bool = True, toolkits: tuple[str, ...] = ALL) -> Agent:
    client = ollama.Client(host=settings.host)
    registry, memory = build_registry(settings, client, make_approver(settings.auto_approve), toolkits)

    state = {"mid_line": False}  # напечатан ли кусок ответа без перевода строки

    def token(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        state["mid_line"] = not text.endswith("\n")

    def show(event: ToolEvent) -> None:
        if state["mid_line"]:
            print()
            state["mid_line"] = False
        if verbose:
            print(f"  🔧 {event.name}({_short(event.arguments, 120)}) → {_short(event.result)}", file=sys.stderr)

    return Agent(
        client=client, tools=registry, settings=settings, on_tool=show, on_token=token,
        context=make_context(settings, memory, toolkits),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lm-agent", description="LLM-агент на базе Ollama")
    parser.add_argument("prompt", nargs="*", help="разовый запрос; без него запускается интерактивный режим")
    parser.add_argument("-m", "--model", help="модель Ollama (по умолчанию из LM_AGENT_MODEL)")
    parser.add_argument("-q", "--quiet", action="store_true", help="не показывать вызовы инструментов")
    parser.add_argument(
        "-t", "--toolkits", default=os.getenv("LM_AGENT_TOOLKITS", ",".join(ALL)),
        help=f"наборы инструментов через запятую (по умолчанию все: {','.join(ALL)})",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение опасных действий")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.model:
        settings.model = args.model
    if args.yes:
        settings.auto_approve = True
    toolkits = tuple(t.strip() for t in args.toolkits.split(",") if t.strip())
    unknown = set(toolkits) - set(ALL)
    if unknown:
        parser.error(f"неизвестные наборы: {', '.join(sorted(unknown))}. Доступны: {', '.join(ALL)}")
    agent = build_agent(settings, verbose=not args.quiet, toolkits=toolkits)

    try:
        if args.prompt:
            agent.ask(" ".join(args.prompt))  # ответ печатается по мере генерации
            print()
            return 0

        threading.Thread(target=_quiet(agent.warmup), daemon=True).start()  # пока пользователь печатает
        print(f"LM Agent · модель {settings.model} · {settings.host}\n{HELP}")
        if settings.auto_approve:
            print("⚠️  Подтверждения отключены: опасные действия выполняются без вопросов.")
        while True:
            try:
                prompt = input("\n› ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not prompt:
                continue
            if prompt in ("/exit", "/quit"):
                return 0
            if prompt == "/reset":
                agent.reset()
                print("История очищена (долговременная память сохранена).")
                continue
            if prompt == "/tools":
                print(", ".join(agent.tools.names()))
                continue
            if prompt == "/memory":
                print(agent.tools.call("list_memories", {}) if "list_memories" in agent.tools else "Память отключена")
                continue
            agent.ask(prompt)
            print()
    except ollama.ResponseError as e:
        print(f"Ошибка Ollama: {e.error}. Модель скачана? `ollama pull {settings.model}`", file=sys.stderr)
    except ConnectionError:
        print(f"Нет связи с Ollama по адресу {settings.host}. Запущен ли `ollama serve`?", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

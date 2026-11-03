"""Задачи по расписанию: в заданное время агент выполняет запрос и отправляет результат.

Задача — это обычный запрос к агенту («сводка по новым заявкам»), поэтому данные он берёт любым
инструментом: таблица, документы, API сервиса, сайт. Подтвердить опасное действие во время
задачи некому, поэтому такие действия отклоняются (если не включён LM_AGENT_AUTO_APPROVE).
Задачи хранятся в data_dir/schedule.json; результаты — там же (последние 10 у каждой задачи).
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .hub import Collector, Hub, current_channel, error_text

HISTORY = 10
TICK = 20  # как часто проверять, не пора ли запустить задачу, секунды

_DAYS = {
    "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 0,
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0,
}
_DAY_NAMES = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"]


# --- расписание: человеческий формат → cron ---

def parse_when(text: str) -> str:
    """«09:00», «каждый день в 9:30», «будни 09:00», «пн,ср 18:00», «выходные 10:00»,
    «каждый час», «каждые 15 минут» или cron из пяти полей → cron."""
    t = " ".join(str(text).lower().replace("ё", "е").split())
    if re.fullmatch(r"[\d*/,\-]+( [\d*/,\-]+){4}", t):
        cron_fields(t)  # проверка
        return t
    m = re.search(r"кажд\w* (\d+) мин", t) or re.search(r"every (\d+) min", t)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 59:
            raise ValueError("интервал в минутах должен быть от 1 до 59")
        return f"*/{n} * * * *"
    if re.search(r"кажд\w* час|every hour|hourly", t):
        return "0 * * * *"
    tm = re.search(r"(\d{1,2})[:.](\d{2})", t) or re.search(r"\bв (\d{1,2})\b(?! мин)", t)
    if not tm:
        raise ValueError(f"не понял время в «{text}». Примеры: 09:00, будни 09:00, пн,ср 18:30, каждые 30 минут")
    hour, minute = int(tm.group(1)), int(tm.group(2)) if tm.lastindex and tm.lastindex >= 2 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("время должно быть от 00:00 до 23:59")
    if re.search(r"будн|рабоч|weekday", t):
        dow = "1-5"
    elif re.search(r"выходн|weekend", t):
        dow = "6,0"
    else:
        days = sorted({_DAYS[d] for d in re.findall(r"\b(пн|вт|ср|чт|пт|сб|вс|mon|tue|wed|thu|fri|sat|sun)\b", t)})
        dow = ",".join(map(str, days)) if days else "*"
    return f"{minute} {hour} * * {dow}"


def _field(spec: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start, end = map(int, part.split("-"))
        else:
            start = end = int(part)
        if not (lo <= start <= hi and lo <= end <= hi) or step < 1:
            raise ValueError(f"значение вне диапазона {lo}–{hi}: {spec}")
        values.update(range(start, end + 1, step))
    return values


def cron_fields(cron: str) -> tuple[set[int], ...]:
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError("в cron должно быть 5 полей: минута час день месяц день_недели")
    minute, hour, dom, month, dow = parts
    dows = {d % 7 for d in _field(dow, 0, 7)}  # 7 — тоже воскресенье
    return _field(minute, 0, 59), _field(hour, 0, 23), _field(dom, 1, 31), _field(month, 1, 12), dows


def next_run(cron: str, after: datetime) -> datetime:
    minutes, hours, doms, months, dows = cron_fields(cron)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 370):
        if (t.minute in minutes and t.hour in hours and t.day in doms and t.month in months
                and (t.weekday() + 1) % 7 in dows):
            return t
        t += timedelta(minutes=1)
    raise ValueError("по этому расписанию задача никогда не запустится")


def describe(cron: str) -> str:
    minute, hour, dom, month, dow = cron.split()
    if minute.startswith("*/") and hour == "*":
        return f"каждые {minute[2:]} мин"
    if minute.isdigit() and hour == "*":
        return f"каждый час в :{int(minute):02d}"
    if minute.isdigit() and hour.isdigit() and dom == month == "*":
        at = f"{int(hour):02d}:{int(minute):02d}"
        if dow == "*":
            return f"ежедневно в {at}"
        if dow == "1-5":
            return f"по будням в {at}"
        if dow in ("6,0", "0,6"):
            return f"по выходным в {at}"
        days = sorted(cron_fields(cron)[4], key=lambda d: (d + 6) % 7)
        return f"{','.join(_DAY_NAMES[d] for d in days)} в {at}"
    return f"cron: {cron}"


# --- хранение и запуск ---

class Scheduler:
    def __init__(self, hub: Hub, path: Path) -> None:
        self.hub = hub
        self.path = path
        self.tasks: list[dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self.notifiers: list[Callable[[dict, str], None]] = []  # кому доставлять результаты (Telegram)
        self._lock = threading.RLock()
        self._running: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        now = datetime.now()
        for task in self.tasks:  # пропущенные, пока сервер был выключен, не догоняем
            task["next_run"] = next_run(task["cron"], now).isoformat(timespec="minutes")

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=1), encoding="utf-8")

    def get(self, task_id: str) -> dict | None:
        return next((t for t in self.tasks if t["id"] == str(task_id).lstrip("#")), None)

    def add(self, when: str, prompt: str, name: str = "", chat_id: int | None = None) -> dict:
        if not prompt.strip():
            raise ValueError("пустой запрос задачи")
        cron = parse_when(when)
        task = {
            "id": uuid.uuid4().hex[:6], "name": name.strip() or prompt.strip()[:60], "cron": cron,
            "when": describe(cron), "prompt": prompt.strip(), "chat_id": chat_id, "enabled": True,
            "created": datetime.now().isoformat(timespec="seconds"),
            "next_run": next_run(cron, datetime.now()).isoformat(timespec="minutes"),
            "history": [],
        }
        with self._lock:
            self.tasks.append(task)
            self.save()
        return task

    def remove(self, task_id: str) -> bool:
        with self._lock:
            before = len(self.tasks)
            self.tasks = [t for t in self.tasks if t["id"] != str(task_id).lstrip("#")]
            self.save()
            return len(self.tasks) < before

    def set_enabled(self, task_id: str, enabled: bool) -> dict:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        with self._lock:
            task["enabled"] = bool(enabled)
            task["next_run"] = next_run(task["cron"], datetime.now()).isoformat(timespec="minutes")
            self.save()
        return task

    def run_task(self, task: dict) -> str:
        """Выполняет задачу (синхронно) и доставляет результат."""
        task_id = task["id"]
        with self._lock:
            if task_id in self._running:
                return "Задача уже выполняется"
            self._running.add(task_id)
        prompt = (
            f"Это задача по расписанию «{task['name']}». Пользователя рядом нет: "
            f"выполни запрос сам и дай понятный итог, без вопросов к пользователю.\n\n{task['prompt']}"
        )
        started = datetime.now()
        collector = Collector(self.hub.settings.auto_approve)
        try:
            text = self.hub.ask(f"schedule-{task_id}", prompt, collector, fresh=True)
            ok = True
        except Exception as e:  # noqa: BLE001
            text, ok = f"Не удалось выполнить: {error_text(e, self.hub.settings)}", False
        finally:
            with self._lock:
                self._running.discard(task_id)
        with self._lock:
            tool_events = [e for e in collector.events if e.get("type") == "tool"]
            tools = [{"name": e["name"], "arguments": e["arguments"]} for e in tool_events]
            files = [m.group(1) for e in tool_events if (m := re.search(r"Файл готов: (.+?) \(\d", e.get("result", "")))]
            task["history"] = ([{"at": started.isoformat(timespec="seconds"), "ok": ok, "text": text, "tools": tools, "files": files}]
                               + task.get("history", []))[:HISTORY]
            task["last_run"] = started.isoformat(timespec="seconds")
            self.save()
        for notify in list(self.notifiers):
            try:
                notify(task, text)
            except Exception:  # noqa: BLE001 — недоставка в Telegram не должна ронять планировщик
                pass
        return text

    def run_in_background(self, task: dict) -> None:
        threading.Thread(target=self.run_task, args=(task,), daemon=True, name=f"task-{task['id']}").start()

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        for task in list(self.tasks):
            if not task.get("enabled") or task["id"] in self._running:
                continue
            if datetime.fromisoformat(task["next_run"]) <= now:
                with self._lock:
                    task["next_run"] = next_run(task["cron"], now).isoformat(timespec="minutes")
                    self.save()
                self.run_in_background(task)

    def start(self) -> None:
        def loop():
            while not self._stop.wait(TICK):
                self.tick()

        self._thread = threading.Thread(target=loop, daemon=True, name="lm-agent-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def register_tools(scheduler: Scheduler) -> None:
    registry = scheduler.hub.registry

    def explain(args: dict) -> str:
        try:
            return f"{describe(parse_when(args.get('when', '')))}: {args.get('prompt', '')}"
        except ValueError as e:
            return f"расписание не распознано: {e}"

    @registry.register(confirm=True, explain=explain)
    def schedule_task(when: str, prompt: str, name: str = "") -> str:
        """Ставит задачу по расписанию: в заданное время агент выполнит запрос и пришлёт результат.

        when: когда — "09:00", "будни 09:00", "пн,ср 18:30", "выходные 10:00", "каждые 30 минут" или cron
        prompt: что сделать — обычными словами, как просил пользователь (не SQL и не код: при каждом запуске агент сам посмотрит свежие данные), например "Сводка по таблице заявок: сколько всего, сколько открытых и в ожидании, сколько без ответственного"
        name: короткое название задачи
        """
        channel = current_channel.get()
        task = scheduler.add(when, prompt, name, chat_id=getattr(channel, "chat_id", None))
        return f"Задача #{task['id']} «{task['name']}» поставлена: {task['when']}. Первый запуск: {task['next_run'].replace('T', ' ')}"

    @registry.register
    def list_scheduled() -> str:
        """Показывает задачи по расписанию: номер, когда, что делает, последний запуск."""
        if not scheduler.tasks:
            return "Задач по расписанию нет"
        lines = []
        for t in scheduler.tasks:
            state = "" if t.get("enabled") else " (выключена)"
            last = f", последний запуск {t['last_run'].replace('T', ' ')}" if t.get("last_run") else ""
            lines.append(f"#{t['id']} «{t['name']}» — {t['when']}{state}; следующий {t['next_run'].replace('T', ' ')}{last}")
        return "\n".join(lines)

    @registry.register(confirm=True)
    def delete_scheduled(task_id: str) -> str:
        """Удаляет задачу по расписанию.

        task_id: номер задачи из list_scheduled, например "a1b2c3"
        """
        return "Удалена" if scheduler.remove(task_id) else f"Задача {task_id} не найдена"

from datetime import datetime

import pytest

from lm_agent import Settings
from lm_agent.hub import Hub
from lm_agent.scheduler import Scheduler, describe, next_run, parse_when, register_tools


@pytest.mark.parametrize("text, cron", [
    ("09:00", "0 9 * * *"),
    ("каждое утро в 9", "0 9 * * *"),
    ("каждый день в 9:30", "30 9 * * *"),
    ("будни 09:00", "0 9 * * 1-5"),
    ("по выходным в 10:15", "15 10 * * 6,0"),
    ("пн,ср 18:30", "30 18 * * 1,3"),
    ("каждые 15 минут", "*/15 * * * *"),
    ("каждый час", "0 * * * *"),
    ("0 8 1 * *", "0 8 1 * *"),
])
def test_parse_when(text, cron):
    assert parse_when(text) == cron


def test_parse_when_errors():
    with pytest.raises(ValueError):
        parse_when("когда-нибудь")
    with pytest.raises(ValueError):
        parse_when("25:00")


def test_next_run_and_describe():
    monday_10 = datetime(2026, 11, 2, 10, 0)  # понедельник
    assert next_run("0 9 * * 1-5", monday_10) == datetime(2026, 11, 3, 9, 0)
    assert next_run("0 9 * * 6,0", monday_10) == datetime(2026, 11, 7, 9, 0)
    assert next_run("*/15 * * * *", datetime(2026, 11, 2, 10, 7)) == datetime(2026, 11, 2, 10, 15)
    assert describe("0 9 * * 1-5") == "по будням в 09:00"
    assert describe("30 18 * * 1,3") == "пн,ср в 18:30"
    assert describe("*/15 * * * *") == "каждые 15 мин"


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def chat(self, messages, stream=False, **kw):
        self.prompts.append(messages[-1]["content"])
        msg = {"message": {"role": "assistant", "content": self.answers.pop(0)}}
        return iter([msg]) if stream else msg


@pytest.fixture
def make(tmp_path):
    def factory(*answers):
        settings = Settings(workspace=tmp_path / "ws", docs_dir=tmp_path / "docs", data_dir=tmp_path / "data",
                            services_file=tmp_path / "s.json", file_roots=[tmp_path])
        hub = Hub(settings, ("basic",), FakeClient(*answers))
        scheduler = Scheduler(hub, tmp_path / "data" / "schedule.json")
        register_tools(scheduler)
        return hub, scheduler

    return factory


def test_task_runs_when_due_and_notifies(make, tmp_path):
    hub, scheduler = make("Сводка: 5 новых заявок")
    delivered = []
    scheduler.notifiers.append(lambda task, text: delivered.append((task["name"], text)))
    task = scheduler.add("09:00", "Сводка по заявкам за вчера", "Утренняя сводка")
    scheduler.tick(datetime.fromisoformat(task["next_run"]))  # «наступило» время
    for _ in range(100):  # задача выполняется в фоне
        if delivered:
            break
        import time; time.sleep(0.02)
    assert delivered == [("Утренняя сводка", "Сводка: 5 новых заявок")]
    assert "задача по расписанию «Утренняя сводка»" in hub.client.prompts[0]
    assert task["history"][0]["ok"] and task["last_run"]
    # сохранено на диск и переживает перезапуск
    again = Scheduler(hub, tmp_path / "data" / "schedule.json")
    assert again.tasks[0]["name"] == "Утренняя сводка" and again.tasks[0]["history"]


def test_disabled_task_does_not_run(make):
    hub, scheduler = make()
    task = scheduler.add("каждые 5 минут", "пинг")
    scheduler.set_enabled(task["id"], False)
    scheduler.tick(datetime.fromisoformat(task["next_run"]))
    assert hub.client.prompts == []


def test_schedule_tool_requires_confirmation(make):
    hub, scheduler = make()
    out = hub.registry.call("schedule_task", {"when": "будни 09:00", "prompt": "сводка"})
    assert out.startswith("Отклонено") and scheduler.tasks == []  # без подтверждения — не ставится

    hub.registry.approver = lambda name, args: "по будням в 09:00: сводка" in args["что это"]
    assert "по будням в 09:00" in hub.registry.call("schedule_task", {"when": "будни 09:00", "prompt": "сводка"})
    assert "сводка" in hub.registry.call("list_scheduled", {})

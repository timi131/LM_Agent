import threading
import time

import pytest

from lm_agent import Settings
from lm_agent.hub import Hub
from lm_agent.scheduler import Scheduler, register_tools
from lm_agent.telegram import TelegramBot, split_text, to_html


class FakeTelegram:
    """Подменяет Bot API: копит отправленное, отдаёт заранее заданные обновления."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.next_id = 100

    def __call__(self, method, params):
        if method == "getFile":
            return {"file_path": "voice/file_1.oga"}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        self.sent.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def texts(self, method="sendMessage"):
        return [p.get("text", "") for m, p in self.sent if m == method]

    def wait_for(self, predicate, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)

    def chat(self, stream=False, **kw):
        r = self.responses.pop(0)
        return iter([r]) if stream else r


def answer(text):
    return {"message": {"role": "assistant", "content": text}}


def tool_call(name, **args):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}}


@pytest.fixture
def make(tmp_path):
    def factory(*responses, allowed=("42",), transcriber=None):
        settings = Settings(workspace=tmp_path / "ws", docs_dir=tmp_path / "d", data_dir=tmp_path / "data",
                            services_file=tmp_path / "s.json", file_roots=[tmp_path])
        hub = Hub(settings, ("basic", "shell", "files"), FakeClient(*responses))
        scheduler = Scheduler(hub, tmp_path / "data" / "schedule.json")
        register_tools(scheduler)
        api = FakeTelegram()
        bot = TelegramBot(hub, "TOKEN", set(allowed), scheduler, api=api,
                          transcriber=transcriber, download=lambda path: b"OggS-fake-audio")
        return bot, api, scheduler

    return factory


def message(text, user_id=42, username="georgy"):
    return {"update_id": 1, "message": {"chat": {"id": user_id}, "from": {"id": user_id, "username": username}, "text": text}}


def test_stranger_gets_only_his_id(make):
    bot, api, _ = make(allowed=("42",))
    api.updates = [message("привет", user_id=7, username="stranger")]
    bot.poll_once()
    assert "Нет доступа. Ваш Telegram id: 7" in api.texts()[0]


def test_allowed_by_username(make):
    bot, api, _ = make(answer("Привет!"), allowed=("@Georgy",))
    api.updates = [message("привет", user_id=5, username="georgy")]
    bot.poll_once()
    assert api.wait_for(lambda: "Привет!" in api.texts())


def test_chat_with_approval_buttons(make):
    bot, api, _ = make(tool_call("run_command", command="echo hi"), answer("Вывод: **hi**"))
    api.updates = [message("запусти echo")]
    bot.poll_once()
    assert api.wait_for(lambda: any("Агент хочет выполнить run_command" in t for t in api.texts()))
    keyboard = next(p["reply_markup"] for m, p in api.sent if m == "sendMessage" and "reply_markup" in p)
    allow = keyboard["inline_keyboard"][0][0]["callback_data"]
    api.updates = [{"update_id": 2, "callback_query": {"id": "q1", "data": allow, "from": {"id": 42},
                                                       "message": {"chat": {"id": 42}, "message_id": 5}}}]
    bot.poll_once()
    assert api.wait_for(lambda: any("<b>hi</b>" in t for t in api.texts()))
    final = [t for t in api.texts() if "<b>hi</b>" in t][0]
    assert final.startswith("🔧 run_command")  # какими инструментами пользовался


def test_scheduled_result_delivered_to_chat(make):
    bot, api, scheduler = make(answer("5 новых заявок"))
    task = scheduler.add("09:00", "сводка", "Утро", chat_id=42)
    scheduler.run_task(task)
    assert any("🗓 <b>Утро</b>" in t and "5 новых заявок" in t for t in api.texts())


def test_formatting_helpers():
    assert to_html("**жирный** и `код` <тег>") == "<b>жирный</b> и <code>код</code> &lt;тег&gt;"
    assert to_html("```\nx < 1\n```") == "<pre>x &lt; 1</pre>"
    parts = split_text("строка\n" * 1000, limit=100)
    assert all(len(p) <= 100 for p in parts) and "".join(parts).count("строка") == 1000


class FakeTranscriber:
    def __init__(self, text):
        self.text = text
        self.files = []

    def transcribe(self, path):
        self.files.append((path.suffix, path.read_bytes()))
        return self.text


def voice(duration=3, user_id=42):
    return {"update_id": 3, "message": {"chat": {"id": user_id}, "from": {"id": user_id},
                                        "voice": {"file_id": "F1", "duration": duration, "file_size": 5000}}}


def test_voice_message_transcribed_and_answered(make):
    tr = FakeTranscriber("сколько будет два плюс два")
    bot, api, _ = make(answer("Четыре"), transcriber=tr)
    api.updates = [voice()]
    bot.poll_once()
    assert api.wait_for(lambda: "Четыре" in api.texts())
    assert tr.files == [(".oga", b"OggS-fake-audio")]  # скачанный файл передан распознаванию
    edits = api.texts("editMessageText")
    assert "🎤 сколько будет два плюс два" in edits  # пользователь видит, что бот услышал
    assert bot.hub.agent("tg-42").messages[1]["content"] == "сколько будет два плюс два"


def test_voice_edge_cases(make):
    bot, api, _ = make(transcriber=FakeTranscriber(""))
    api.updates = [voice(duration=3600)]
    bot.poll_once()
    assert api.wait_for(lambda: any("Слишком длинное" in t for t in api.texts()))
    api.updates = [voice()]
    bot.poll_once()
    assert api.wait_for(lambda: any("Не разобрал речь" in t for t in api.texts("editMessageText")))

    bot, api, _ = make(transcriber=None)
    api.updates = [voice()]
    bot.poll_once()
    assert api.wait_for(lambda: any("Голосовые выключены" in t for t in api.texts()))


def test_created_report_sent_as_file(make, tmp_path):
    call = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
        "name": "create_document", "arguments": {"name": "Отчёт за сентябрь", "content": "# Итог\n\nВсё хорошо", "format": "md"}}}]}}
    bot, api, _ = make(call, answer("Готово"))
    from lm_agent.toolkits import reports  # create_document — из набора reports
    reports.register(bot.hub.registry, bot.hub.settings.workspace.resolve())
    sent = []
    bot._upload = lambda chat_id, path: sent.append((chat_id, path.name, path.read_text()))
    api.updates = [message("сделай отчёт")]
    bot.poll_once()
    assert api.wait_for(lambda: sent)
    assert sent == [(42, "Отчёт за сентябрь.md", "# Итог\n\nВсё хорошо")]  # имя с пробелами

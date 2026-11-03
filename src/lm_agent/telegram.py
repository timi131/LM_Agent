"""Telegram-бот: тот же агент в Telegram (long polling, без внешнего адреса и вебхуков).

Доступ — только пользователям из TELEGRAM_ALLOWED_USERS (числовой id или @username):
у агента есть shell и файлы, поэтому чужим бот не отвечает, а лишь сообщает их id.
Опасные действия подтверждаются кнопками «Разрешить / Отклонить» прямо в чате.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from .hub import Busy, Hub, error_text

APPROVAL_TIMEOUT = 300
MAX_MESSAGE = 4000  # лимит Telegram — 4096 символов
MAX_VOICE_SECONDS = 600
MAX_DOWNLOAD = 20 * 1024 * 1024  # больше Bot API скачать не даёт
HELP = (
    "Я — агент. Пишите задачу текстом или голосовым: найти в интернете, посчитать по таблице, "
    "поставить задачу по расписанию («каждое утро в 9 присылай сводку по заявкам»).\n\n"
    "/reset — начать диалог заново\n/tasks — задачи по расписанию\n/id — мой Telegram id"
)


def to_html(text: str) -> str:
    """Markdown из ответа модели → безопасный HTML, который понимает Telegram."""
    blocks = re.split(r"```(?:\w+)?\n?(.*?)```", text, flags=re.S)
    out = []
    for i, block in enumerate(blocks):
        if i % 2:
            out.append(f"<pre>{html.escape(block.strip())}</pre>")
            continue
        s = html.escape(block)
        s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", s, flags=re.M)
        out.append(s)
    return "".join(out)


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + [text] if text else parts


class TelegramChannel:
    """Канал одного запроса из Telegram: статус с вызовами инструментов и подтверждения кнопками."""

    def __init__(self, bot: "TelegramBot", chat_id: int) -> None:
        self.bot, self.chat_id = bot, chat_id
        self.tools: list[str] = []
        self.status_id: int | None = None

    def send(self, payload: dict) -> None:
        if payload.get("type") != "tool":
            return
        self.tools.append(payload["name"])
        text = "⏳ Работаю…\n" + "\n".join(f"🔧 {name}" for name in self.tools[-15:])
        if self.status_id is None:
            self.status_id = self.bot.send(self.chat_id, text, plain=True)
        else:
            self.bot.api("editMessageText", chat_id=self.chat_id, message_id=self.status_id, text=text)

    def approve(self, name: str, arguments: dict) -> bool:
        lines = [f"⚠️ <b>Агент хочет выполнить {html.escape(name)}</b>"]
        for key, value in arguments.items():
            shown = str(value)
            lines.append(f"<b>{html.escape(str(key))}</b>: <code>{html.escape(shown[:1500])}</code>")
        return self.bot.ask_approval(self.chat_id, "\n".join(lines))


class TelegramBot:
    def __init__(
        self, hub: Hub, token: str, allowed: set[str], scheduler: Any = None,
        api: Callable[[str, dict], Any] | None = None, proxy: str | None = None,
        transcriber: Any = None, download: Callable[[str], bytes] | None = None,
    ) -> None:
        self.hub = hub
        self.token = token
        self.allowed = {a.strip().lstrip("@").lower() for a in allowed if a.strip()}
        self.scheduler = scheduler
        self._http = None if api else httpx.Client(timeout=40, proxy=proxy or None)
        self._api = api or self._real_api
        self._download = download or self._real_download
        self.transcriber = transcriber  # распознавание голосовых (speech.Transcriber); None — выключено
        self._pending: dict[str, tuple[threading.Event, list[bool]]] = {}
        self._stop = threading.Event()
        self.offset = 0
        if scheduler is not None:
            scheduler.notifiers.append(self.deliver)

    # --- Bot API ---

    def _real_api(self, method: str, params: dict) -> Any:
        resp = self._http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"Telegram API: HTTP {resp.status_code}"))
        return data.get("result")

    def _real_download(self, file_path: str) -> bytes:
        resp = self._http.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}")
        resp.raise_for_status()
        return resp.content

    def api(self, method: str, **params: Any) -> Any:
        try:
            return self._api(method, params)
        except Exception:  # noqa: BLE001 — сбой одного вызова (сеть, «message is not modified») не роняет бота
            return None

    def send(self, chat_id: int, text: str, plain: bool = False, **extra: Any) -> int | None:
        """Отправляет текст (с разбивкой на части); возвращает id последнего сообщения."""
        message_id = None
        for part in split_text(text or "(пустой ответ)"):
            result = None
            if not plain:
                result = self.api("sendMessage", chat_id=chat_id, text=to_html(part), parse_mode="HTML",
                                  disable_web_page_preview=True, **extra)
            if result is None:  # HTML не принят — отправим как есть
                result = self.api("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True, **extra)
            if result:
                message_id = result.get("message_id")
        return message_id

    # --- доступ ---

    def is_allowed(self, user: dict) -> bool:
        return str(user.get("id")) in self.allowed or (user.get("username") or "").lower() in self.allowed

    # --- подтверждения ---

    def ask_approval(self, chat_id: int, text_html: str) -> bool:
        request_id = uuid.uuid4().hex[:12]
        event, decision = threading.Event(), [False]
        self._pending[request_id] = (event, decision)
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Разрешить", "callback_data": f"ap:{request_id}:1"},
            {"text": "✖️ Отклонить", "callback_data": f"ap:{request_id}:0"},
        ]]}
        self.api("sendMessage", chat_id=chat_id, text=text_html, parse_mode="HTML", reply_markup=keyboard)
        answered = event.wait(APPROVAL_TIMEOUT)
        self._pending.pop(request_id, None)
        if not answered:
            self.send(chat_id, "⌛ Время на ответ истекло — действие отклонено", plain=True)
        return answered and decision[0]

    def _on_callback(self, query: dict) -> None:
        data = query.get("data", "")
        if not self.is_allowed(query.get("from", {})) or not data.startswith("ap:"):
            self.api("answerCallbackQuery", callback_query_id=query["id"], text="Нет доступа")
            return
        _, request_id, allow = data.split(":")
        item = self._pending.get(request_id)
        verdict = "✅ Разрешено" if allow == "1" else "✖️ Отклонено"
        if item:
            item[1][0] = allow == "1"
            item[0].set()
        self.api("answerCallbackQuery", callback_query_id=query["id"], text=verdict if item else "Запрос уже неактуален")
        msg = query.get("message") or {}
        if msg:
            self.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                     reply_markup={"inline_keyboard": [[{"text": verdict, "callback_data": "done"}]]})

    # --- сообщения ---

    def _on_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        user = message.get("from", {})
        text = (message.get("text") or "").strip()
        if not self.is_allowed(user):
            self.send(chat_id, f"Нет доступа. Ваш Telegram id: {user.get('id')}. "
                               "Попросите администратора добавить его в TELEGRAM_ALLOWED_USERS.", plain=True)
            return
        media = message.get("voice") or message.get("audio") or message.get("video_note")
        if media and not text:
            threading.Thread(target=self._voice, args=(chat_id, media), daemon=True, name=f"tg-voice-{chat_id}").start()
            return
        if not text:
            self.send(chat_id, "Пока понимаю текст и голосовые.", plain=True)
            return
        command = text.split()[0].split("@")[0].lower()
        if command in ("/start", "/help"):
            self.send(chat_id, HELP, plain=True)
        elif command == "/id":
            self.send(chat_id, f"Ваш Telegram id: {user.get('id')}", plain=True)
        elif command == "/reset":
            self.hub.reset(f"tg-{chat_id}")
            self.send(chat_id, "Начали заново. Долговременная память сохранена.", plain=True)
        elif command == "/tasks":
            listing = self.hub.registry.call("list_scheduled", {}) if "list_scheduled" in self.hub.registry else "Расписание отключено"
            self.send(chat_id, listing, plain=True)
        else:
            threading.Thread(target=self._chat, args=(chat_id, text), daemon=True, name=f"tg-{chat_id}").start()

    def _voice(self, chat_id: int, media: dict) -> None:
        """Голосовое → текст (локальный Whisper) → обычный запрос к агенту."""
        if self.transcriber is None:
            self.send(chat_id, "Голосовые выключены: нужен пакет faster-whisper, а LM_AGENT_WHISPER_MODEL не должен быть off.", plain=True)
            return
        if media.get("duration", 0) > MAX_VOICE_SECONDS:
            self.send(chat_id, f"Слишком длинное голосовое: понимаю до {MAX_VOICE_SECONDS // 60} минут.", plain=True)
            return
        if media.get("file_size", 0) > MAX_DOWNLOAD:
            self.send(chat_id, "Файл больше 20 МБ — Telegram не даёт ботам скачать такой.", plain=True)
            return
        status = self.send(chat_id, "🎧 Слушаю…", plain=True)
        self.api("sendChatAction", chat_id=chat_id, action="typing")
        path = None
        try:
            info = self._api("getFile", {"file_id": media["file_id"]})
            data = self._download(info["file_path"])
            suffix = Path(info["file_path"]).suffix or ".oga"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                f.write(data)
                path = Path(f.name)
            text = self.transcriber.transcribe(path)
        except Exception as e:  # noqa: BLE001
            self._edit_or_send(chat_id, status, f"⚠️ Не удалось распознать голосовое: {type(e).__name__}: {e}")
            return
        finally:
            if path:
                path.unlink(missing_ok=True)
        if not text:
            self._edit_or_send(chat_id, status, "🤷 Не разобрал речь — попробуйте ещё раз или напишите текстом.")
            return
        self._edit_or_send(chat_id, status, f"🎤 {text}")
        self._chat(chat_id, text)

    def _edit_or_send(self, chat_id: int, message_id: int | None, text: str) -> None:
        if message_id is None or self.api("editMessageText", chat_id=chat_id, message_id=message_id, text=text) is None:
            self.send(chat_id, text, plain=True)

    def _chat(self, chat_id: int, text: str) -> None:
        session = f"tg-{chat_id}"
        if self.hub.busy(session):
            self.send(chat_id, "Я ещё отвечаю на прошлое сообщение — подождите немного.", plain=True)
            return
        done = threading.Event()

        def typing():
            while not done.is_set():
                self.api("sendChatAction", chat_id=chat_id, action="typing")
                done.wait(4)

        threading.Thread(target=typing, daemon=True).start()
        channel = TelegramChannel(self, chat_id)
        try:
            answer = self.hub.ask(session, text, channel)
        except Busy as e:
            answer = str(e)
        except Exception as e:  # noqa: BLE001
            answer = f"⚠️ {error_text(e, self.hub.settings)}"
        finally:
            done.set()
        if channel.status_id is not None:
            self.api("deleteMessage", chat_id=chat_id, message_id=channel.status_id)
            answer = "🔧 " + ", ".join(dict.fromkeys(channel.tools)) + "\n\n" + answer
        self.send(chat_id, answer)

    # --- результаты задач по расписанию ---

    def deliver(self, task: dict, text: str) -> None:
        targets = [task["chat_id"]] if task.get("chat_id") else self.default_chats()
        for chat_id in targets:
            self.send(chat_id, f"🗓 **{task['name']}**\n\n{text}")

    def default_chats(self) -> list[int]:
        """Числовые id из списка разрешённых: личный чат с ботом имеет тот же id, что и пользователь."""
        return [int(a) for a in self.allowed if a.lstrip("-").isdigit()]

    # --- цикл ---

    def poll_once(self, timeout: int = 25) -> None:
        updates = self._api("getUpdates", {"offset": self.offset, "timeout": timeout,
                                           "allowed_updates": ["message", "callback_query"]}) or []
        for update in updates:
            self.offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    self._on_callback(update["callback_query"])
                elif "message" in update:
                    self._on_message(update["message"])
            except Exception:  # noqa: BLE001 — одно кривое сообщение не должно останавливать бота
                pass

    def run_forever(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = 1
            except Exception:  # noqa: BLE001 — нет сети/VPN: ждём и пробуем снова
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def start(self) -> threading.Thread:
        me = self.api("getMe")
        if me:
            self.username = me.get("username")
        thread = threading.Thread(target=self.run_forever, daemon=True, name="lm-agent-telegram")
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def from_env(hub: Hub, scheduler: Any = None) -> TelegramBot | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    allowed = set(os.getenv("TELEGRAM_ALLOWED_USERS", "").replace(";", ",").split(","))
    return TelegramBot(
        hub, token, allowed, scheduler, proxy=os.getenv("TELEGRAM_PROXY") or None,
        transcriber=hub.transcriber,
    )


def main(argv: list[str] | None = None) -> int:
    """Только бот и расписание, без веб-чата — например, на сервере."""
    import argparse

    from .config import Settings
    from .scheduler import Scheduler, register_tools
    from .toolkits import ALL

    parser = argparse.ArgumentParser(prog="lm-agent-bot", description="Telegram-бот LM Agent с задачами по расписанию")
    parser.add_argument("-m", "--model")
    parser.add_argument("-t", "--toolkits", default=os.getenv("LM_AGENT_TOOLKITS", ",".join(ALL)))
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if "LM_AGENT_BROWSER_HEADLESS" not in os.environ:
        settings.browser_headless = True
    if args.model:
        settings.model = args.model
    hub = Hub(settings, tuple(t.strip() for t in args.toolkits.split(",") if t.strip()))
    scheduler = Scheduler(hub, settings.data_dir / "schedule.json")
    register_tools(scheduler)
    bot = from_env(hub, scheduler)
    if bot is None:
        parser.error("задайте TELEGRAM_BOT_TOKEN (и TELEGRAM_ALLOWED_USERS) в .env")
    if not bot.allowed:
        print("⚠️  TELEGRAM_ALLOWED_USERS пуст — бот никому не будет отвечать, только сообщит id.")
    scheduler.start()
    bot.start()
    print(f"Бот @{getattr(bot, 'username', '?')} запущен · модель {settings.model} · Ctrl+C — выход")
    hub.warmup()
    if hub.transcriber is not None:
        try:
            hub.transcriber._load()
        except Exception:  # noqa: BLE001
            pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0

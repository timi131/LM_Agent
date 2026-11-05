"""Цикл агента: модель → вызовы инструментов → результаты → модель … → итоговый ответ."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import Settings
from .tools import ToolRegistry


class ChatClient(Protocol):
    def chat(self, *, model: str, messages: list[dict], tools: list[dict], options: dict) -> Any: ...


# Защита от зацикливания: тот же вызов с теми же аргументами.
MAX_REPEATS = 2
LOOP_WARNING = (
    "\n⚠️ Ты уже делал ровно этот вызов. Повторять его бессмысленно: измени аргументы "
    "(например, исправь запрос) или дай пользователю ответ по тому, что уже известно."
)
LOOP_STOP = (
    "Не выполнено: этот вызов с теми же аргументами уже был несколько раз. "
    "Смени подход или ответь пользователю по тому, что уже известно."
)

# Маленькие модели любят написать «Теперь я создам документ» и закончить ответ, ничего не вызвав.
# Смотрим только последнюю фразу: в статьях и инструкциях такие слова встречаются и в середине готового ответа.
# Не считаем обещанием обращение к клиенту («затем нажмите») и сообщение о сделанном («я создал», «теперь создан»).
PROMISE = re.compile(
    r"\b((теперь|сейчас|далее|давайте|затем|приступаю|итак)[,\s]+(я\s+)?|(я|мы)\s+(сейчас\s+|тогда\s+)?)"
    r"(созда|сдела|сформиру|выполн|откро|найд|ищу|поищ|посмотр|прокрут|сохран|запиш|выгруж|посчита|отправ|"
    r"постав|подготов|оформл|провер|перейд|нажм|введ|загруж|попробу|повтор)\w*"
    r"(?<!те)(?<!л)(?<!ла)(?<!ло)(?<!ли)(?<!н)(?<!на)(?<!но)(?<!ны)\b",
    re.I,
)
# Просьба подождать — только отдельной фразой: «подождите до следующего дня» — это совет, а не обещание.
WAITING = re.compile(
    r"^(пожалуйста,?\s*)?(подождите|минутку|одну секунду|секундочку)(,?\s+(немного|минутку|секунду|пожалуйста))*[.!…]*$",
    re.I,
)
MAX_NUDGES = 2
NUDGE = "Продолжай: сделай это сейчас — вызови нужный инструмент, а не описывай словами."

KB_PREFIX = (
    "Статьи из базы знаний, найденные по вопросу пользователя. Если статья отвечает на вопрос — ответь по ней "
    "своими словами, сразу, без вызова инструментов и ничего не добавляя от себя. Если в статье «Требует проверки: да» — "
    "добавь, что ответ нужно уточнить у специалиста поддержки. Если в статье есть «Уточнить» — попроси эти данные. "
    "Если статьи не про это — не используй их.\n\n"
)

# Результаты этих инструментов — снимки состояния (страницы браузера). Актуален только последний,
# старые занимают контекст, поэтому их сокращаем.
SNAPSHOT_TOOLS = ("browser_",)
STALE_NOTE = "\n…(устаревший снимок страницы сокращён; актуальный — ниже или через browser_snapshot)"


@dataclass
class ToolEvent:
    name: str
    arguments: dict
    result: str


def is_promise(text: str) -> bool:
    """Закончила ли модель ответ обещанием что-то сделать, вместо того чтобы сделать."""
    last = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())[-1]
    return bool(PROMISE.search(last) or WAITING.search(last))


def _as_dict(obj: Any) -> dict:
    """Ответ ollama — pydantic-модель; тестовые клиенты могут вернуть обычный dict."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return obj


@dataclass
class Agent:
    client: ChatClient
    tools: ToolRegistry
    settings: Settings
    on_tool: Callable[[ToolEvent], None] | None = None
    # Потоковый вывод: вызывается с каждым кусочком текста по мере генерации.
    on_token: Callable[[str], None] | None = None
    # Дополнительный контекст для системного промпта (например, долговременная память).
    context: Callable[[], str] | None = None
    # Журнал: log(событие, данные) — запросы, вызовы инструментов, ответы и причина остановки.
    log: Callable[[str, dict], None] | None = None
    # Автоматический поиск в базе знаний перед каждым вопросом: вопрос → текст статей или "".
    retriever: Callable[[str], str] | None = None
    stop_reason: str = ""
    messages: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        system = self.settings.system_prompt
        extra = self.context() if self.context else ""
        if extra:
            system += "\n\n" + extra
        self.messages = [{"role": "system", "content": system}]

    def ask(self, prompt: str) -> str:
        self._attach_knowledge(prompt)
        self.messages.append({"role": "user", "content": prompt})
        options = {"temperature": self.settings.temperature, "num_ctx": self.settings.num_ctx, **self.settings.options}

        repeats: dict[str, int] = {}
        nudges = 0
        self._log("user", text=prompt[:500])
        for step in range(self.settings.max_steps):
            self._compact_snapshots()
            request = dict(model=self.settings.model, messages=self.messages, tools=self.tools.schemas(), options=options)
            message = self._stream(request) if self.on_token else _as_dict(self.client.chat(**request)).get("message", {})
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []

            self.messages.append({"role": "assistant", "content": content, **({"tool_calls": tool_calls} if tool_calls else {})})
            if not tool_calls:
                if nudges < MAX_NUDGES and self.tools.names() and is_promise(content):
                    nudges += 1  # подталкиваем не больше MAX_NUDGES раз: дальше пусть отвечает как есть
                    self.stop_reason = "nudge"
                    self._log("nudge", text=content[-200:])
                    self.messages.append({"role": "user", "content": NUDGE})
                    continue
                self.stop_reason = "answer"
                self._log("answer", text=content[:500], steps=step + 1)
                return content

            for call in tool_calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                key = name + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
                repeats[key] = repeats.get(key, 0) + 1
                if repeats[key] > MAX_REPEATS:
                    # маленькие модели любят повторять один и тот же неудачный вызов до упора
                    result = LOOP_STOP
                else:
                    result = self.tools.call(name, args)
                    if repeats[key] == MAX_REPEATS:
                        result += LOOP_WARNING
                self._log("tool", name=name, arguments=args, result=result[:300])
                if self.on_tool:
                    self.on_tool(ToolEvent(name, args if isinstance(args, dict) else {"raw": args}, result))
                self.messages.append({"role": "tool", "content": result, "tool_name": name})

        self.stop_reason = "step_limit"
        self._log("step_limit", steps=self.settings.max_steps)
        return f"Остановлено: превышен лимит шагов ({self.settings.max_steps}) без итогового ответа."

    def _log(self, event: str, **data: Any) -> None:
        if self.log:
            try:
                self.log(event, data)
            except Exception:  # noqa: BLE001 — журнал не должен ломать ответ
                pass

    def _attach_knowledge(self, prompt: str) -> None:
        """Сами ищем в базе знаний: маленькая модель среди десятков инструментов часто не догадывается искать."""
        if not self.retriever:
            return
        # статьи к прошлым вопросам больше не нужны — они только занимают контекст
        self.messages = [m for m in self.messages if not (m["role"] == "user" and m["content"].startswith(KB_PREFIX))]
        try:
            found = self.retriever(prompt)
        except Exception as e:  # noqa: BLE001 — без базы агент всё равно может ответить
            self._log("retrieve_error", error=f"{type(e).__name__}: {e}")
            return
        self._log("retrieve", found=bool(found), text=found[:300])
        if found:
            # сообщением перед вопросом, а не системным: системный текст стоит до описаний инструментов,
            # и любое его изменение сбрасывает кэш Ollama (+15–20 с на чтение инструментов к каждому ответу)
            self.messages.append({"role": "user", "content": KB_PREFIX + found})

    def warmup(self) -> None:
        """Заранее прогоняет системный промпт и описания инструментов через модель, чтобы Ollama
        закэшировала этот общий префикс: иначе первый ответ ждёт его чтения (~15–20 с на 7B)."""
        self.client.chat(
            model=self.settings.model,
            messages=[self.messages[0], {"role": "user", "content": "."}],
            tools=self.tools.schemas(),
            options={"temperature": self.settings.temperature, "num_ctx": self.settings.num_ctx, "num_predict": 1},
        )

    def _stream(self, request: dict) -> dict:
        content, tool_calls = [], []
        for chunk in self.client.chat(**request, stream=True):
            msg = _as_dict(chunk).get("message", {})
            if msg.get("content"):
                content.append(msg["content"])
                self.on_token(msg["content"])
            tool_calls.extend(msg.get("tool_calls") or [])
        return {"content": "".join(content), "tool_calls": tool_calls}

    def _compact_snapshots(self) -> None:
        snapshots = [
            m for m in self.messages
            if m["role"] == "tool" and m.get("tool_name", "").startswith(SNAPSHOT_TOOLS)
            and not m["content"].endswith(STALE_NOTE)
        ]
        for m in snapshots[:-1]:
            m["content"] = "\n".join(m["content"].splitlines()[:2]) + STALE_NOTE

# LM Agent

Локальный LLM-агент на базе [Ollama](https://ollama.com): модель сама решает, когда вызвать инструмент (tool calling), получает результат и продолжает рассуждение до итогового ответа. Модель работает локально; в сеть агент выходит только через инструменты (поиск, страницы, API, почта).

## Быстрый старт

```bash
ollama pull qwen2.5:7b
ollama pull embeddinggemma     # для поиска по документам
uv sync
cp .env.example .env           # и заполните нужное
uv run lm-agent                # интерактивный чат
uv run lm-agent "Сколько будет sqrt(2) * 10?"   # разовый запрос
uv run lm-agent -t basic,web   # только выбранные наборы инструментов
uv run lm-agent -m llama3.1:8b # другая модель
```

Команды в чате: `/tools`, `/memory`, `/reset` (очищает диалог, но не память), `/exit`.

## Что умеет

| Набор | Инструменты | Подтверждение |
|---|---|---|
| `basic` | `get_current_time`, `calculate` | — |
| `files` | `list_files`, `find_files`, `read_file`, `write_file` | запись вне `workspace/` |
| `web` | `web_search` (DuckDuckGo, без ключа), `fetch_url` | — |
| `shell` | `run_command` | **каждый запуск** |
| `services` | `list_services`, `call_service`, `http_request` | POST/PUT/PATCH/DELETE |
| `mail` | `read_emails` (IMAP), `send_email` (SMTP) | каждое письмо |
| `docs` | `search_documents`, `index_documents` | — |
| `memory` | `remember`, `list_memories`, `forget` | — |

### Подтверждения

Перед опасным действием агент показывает, что именно собирается сделать, и спрашивает `Разрешить? [y/N]`. Если терминал не интерактивный (скрипт, cron), такие действия отклоняются. Флаг `-y` или `LM_AGENT_AUTO_APPROVE=1` отключает вопросы. Включайте это, только если доверяете модели и всем источникам, которые она читает: текст веб-страницы или письма может подтолкнуть модель выполнить чужую команду.

### Файлы

Относительные пути считаются от `workspace/`. Абсолютные пути и `~/…` разрешены только внутри папок из `LM_AGENT_FILE_ROOTS` (по умолчанию — домашняя папка). Чтение в этих папках идёт без вопросов, запись вне `workspace/` требует подтверждения.

### Внешние сервисы (CRM, API)

Скопируйте `services.example.json` в `services.json` и опишите сервисы:

```json
{
  "okocrm": {
    "description": "OkoCRM: сделки, контакты, задачи",
    "base_url": "https://api.okocrm.com/v2",
    "headers": {"Authorization": "Bearer ${OKOCRM_TOKEN}"}
  }
}
```

`${VAR}` подставляется из окружения или `.env` на стороне агента, поэтому модель токенов не видит. Адрес `base_url` в примере нужно сверить с документацией сервиса. Для разовых запросов к любому API есть `http_request`.

### Почта

Заполните в `.env` поля `EMAIL_ADDRESS`, `EMAIL_PASSWORD` (пароль приложения, а не основной), `IMAP_HOST` и `SMTP_HOST`. Письма читаются без пометки «прочитано».

### Поиск по документам

Положите файлы в `docs/` (или укажите `LM_AGENT_DOCS`). Поддерживаются `.md .txt .pdf .docx .html .csv .json` и исходный код. Индекс строится на эмбеддингах `embeddinggemma` и хранится в `~/.lm_agent/doc_index.json`. При каждом поиске пересчитываются только новые и изменённые файлы.

### Память

`remember` сохраняет факты в `~/.lm_agent/memory.json`. При каждом запуске они попадают в системный промпт. Просмотреть память можно через `/memory`, удалить запись — попросив агента «забудь #N».

## Настройки

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | адрес сервера Ollama |
| `LM_AGENT_MODEL` | `qwen2.5:7b` | модель (нужна поддержка tools) |
| `LM_AGENT_EMBED_MODEL` | `embeddinggemma` | модель эмбеддингов для документов |
| `LM_AGENT_WORKSPACE` | `./workspace` | рабочая папка агента |
| `LM_AGENT_FILE_ROOTS` | `~` | где разрешено работать с файлами (через `:`) |
| `LM_AGENT_DOCS` | `./docs` | папка документов для поиска |
| `LM_AGENT_DATA` | `~/.lm_agent` | память и индекс |
| `LM_AGENT_SERVICES` | `./services.json` | описание внешних сервисов |
| `LM_AGENT_TOOLKITS` | все | включённые наборы инструментов |
| `LM_AGENT_AUTO_APPROVE` | `0` | не спрашивать подтверждений |
| `LM_AGENT_MAX_STEPS` | `8` | лимит шагов «модель → инструмент» на один запрос |
| `LM_AGENT_NUM_CTX` | `16384` | окно контекста модели в токенах |
| `LM_AGENT_COMMAND_TIMEOUT` | `60` | таймаут команды shell, секунды |
| `LM_AGENT_TEMPERATURE` | `0.2` | температура генерации |
| `LM_AGENT_SYSTEM_PROMPT` | см. `config.py` | системный промпт |

## Выбор модели и ограничения

- `qwen2.5:7b` справляется с цепочками инструментов, памятью и поиском по документам.
- `llama3.1:8b` вызывает все инструменты разом и подставляет пустые значения. Годится только для простых запросов с одним инструментом.
- Модель на 7B параметров в веб-задачах часто довольствуется сниппетами поиска и отвечает по своим устаревшим знаниям. Надёжнее дать ей конкретную ссылку («открой …») или взять модель крупнее (например, `qwen2.5:14b`).
- Чем меньше инструментов, тем точнее модель их выбирает. Для узких задач включайте только нужные наборы через `-t`.

## Свой инструмент

Подойдёт обычная функция с аннотациями типов: схема для модели строится из сигнатуры и докстринга.

```python
import ollama
from lm_agent import Agent, Settings
from lm_agent.toolkits import build_registry

settings = Settings.from_env()
client = ollama.Client(host=settings.host)
tools, memory = build_registry(settings, client, enabled=("basic", "memory"))

@tools.register
def get_weather(city: str) -> str:
    """Возвращает погоду в городе.

    city: название города
    """
    return f"В {city} +5°C, облачно"

# @tools.register(confirm=True) — спрашивать пользователя перед каждым вызовом
# (approver передаётся в build_registry; без него такие вызовы отклоняются)

agent = Agent(client=client, tools=tools, settings=settings, context=memory.as_prompt)
print(agent.ask("Какая погода в Казани?"))
```

## Структура

```
src/lm_agent/
  agent.py            цикл агента
  tools.py            реестр инструментов, JSON-схемы, подтверждения
  config.py           настройки
  cli.py              терминальный интерфейс
  toolkits/           basic, files, web, shell, services, mail, docs, memory
tests/                тесты (без реальной модели и сети)
```

## Тесты

```bash
uv run pytest
```

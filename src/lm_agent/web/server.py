"""Веб-интерфейс: чат в браузере (WebSocket) и HTTP API.

Агент синхронный, поэтому каждый запрос выполняется в отдельном потоке. Подтверждения опасных
действий уходят во вкладку браузера и ждут ответа пользователя; без вкладки (HTTP API) действуют
правила LM_AGENT_AUTO_APPROVE.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import ollama
from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import telegram
from ..agent import Agent
from ..config import Settings
from ..hub import Busy, Collector, Hub, error_text
from ..scheduler import Scheduler, register_tools
from ..toolkits import ALL

STATIC = Path(__file__).parent / "static"
APPROVAL_TIMEOUT = 300
UPLOAD_LIMIT = 50 * 1024 * 1024


class Channel:
    """Куда агент отправляет события и у кого спрашивает подтверждение — вкладка браузера."""

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self.ws, self.loop = ws, loop
        self.closed = False
        self._pending: dict[str, tuple[threading.Event, list[bool]]] = {}

    def send(self, payload: dict) -> None:
        """Вызывается из потока агента."""
        if self.closed:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.ws.send_json(payload), self.loop).result(timeout=10)
        except Exception:  # noqa: BLE001 — вкладку закрыли; агент доработает молча
            self.closed = True

    def approve(self, name: str, arguments: dict) -> bool:
        request_id = uuid.uuid4().hex
        event, decision = threading.Event(), [False]
        self._pending[request_id] = (event, decision)
        self.send({"type": "approval", "id": request_id, "name": name, "arguments": arguments})
        answered = event.wait(APPROVAL_TIMEOUT)
        self._pending.pop(request_id, None)
        if not answered:
            self.send({"type": "approval_expired", "id": request_id})
        return answered and decision[0]

    def resolve(self, request_id: str, allow: bool) -> None:
        item = self._pending.get(request_id)
        if item:
            item[1][0] = bool(allow)
            item[0].set()

    def close(self) -> None:
        self.closed = True
        for event, _ in list(self._pending.values()):
            event.set()  # решение остаётся False — действие отклоняется


def _same_origin(headers: Any) -> bool:
    """Защита от запросов с чужих сайтов, открытых в том же браузере."""
    origin = headers.get("origin")
    return not origin or urlparse(origin).netloc == headers.get("host")


def _history(agent: Agent) -> list[dict]:
    out: list[dict] = []
    pending_args: list[dict] = []  # аргументы вызовов ассистента — для следующих по порядку role=tool
    for m in agent.messages:
        if m["role"] == "assistant":
            pending_args = [c.get("function", {}).get("arguments") or {} for c in m.get("tool_calls") or []]
            if m.get("content"):
                out.append({"role": "assistant", "content": m["content"]})
        elif m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "tool":
            args = pending_args.pop(0) if pending_args else {}
            out.append({"role": "tool", "name": m.get("tool_name", ""), "arguments": args, "content": m["content"]})
    return out


class ChatRequest(BaseModel):
    message: str
    session: str = "api"


class Fact(BaseModel):
    fact: str


class TaskIn(BaseModel):
    when: str
    prompt: str
    name: str = ""


class TaskPatch(BaseModel):
    enabled: bool


def create_app(
    settings: Settings | None = None, toolkits: tuple[str, ...] = ALL, client: Any = None,
    background: bool = True,
) -> FastAPI:
    """background=False — без прогрева, планировщика и Telegram (для тестов)."""
    settings = settings or Settings.from_env()
    token = os.getenv("LM_AGENT_WEB_TOKEN", "")
    hub = Hub(settings, toolkits, client)
    registry, memory, browser = hub.registry, hub.memory, hub.browser
    scheduler = Scheduler(hub, settings.data_dir / "schedule.json")
    register_tools(scheduler)
    bot = telegram.from_env(hub, scheduler) if background else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background:
            threading.Thread(target=hub.warmup, daemon=True, name="lm-agent-warmup").start()
            scheduler.start()
            if bot:
                bot.start()
        yield
        scheduler.stop()
        if bot:
            bot.stop()

    app = FastAPI(title="LM Agent", docs_url="/api/docs-ui", redoc_url=None, lifespan=lifespan)
    app.state.hub, app.state.scheduler, app.state.bot = hub, scheduler, bot

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request.headers):
            return JSONResponse({"detail": "запрос с чужого сайта отклонён"}, status_code=403)
        if token and request.url.path.startswith("/api/"):
            given = request.headers.get("x-token") or request.query_params.get("token")
            if given != token:
                return JSONResponse({"detail": "нужен токен"}, status_code=401)
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/info")
    def info():
        return {
            "model": settings.model,
            "host": settings.host,
            "toolkits": list(toolkits),
            "tools": registry.names(),
            "auto_approve": settings.auto_approve,
            "docs_dir": str(settings.docs_dir.resolve()),
            "memory": memory is not None,
            "docs": "docs" in toolkits,
            "browser": browser is not None,
            "telegram": bool(bot),
        }

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        collector = Collector(settings.auto_approve)
        try:
            answer = await asyncio.to_thread(hub.ask, req.session, req.message, collector)
        except Busy as e:
            raise HTTPException(409, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, error_text(e, settings)) from e
        return {"answer": answer, "tools": collector.events}

    @app.post("/api/sessions/{session_id}/reset")
    def reset(session_id: str):
        hub.reset(session_id)
        return {"ok": True}

    # --- расписание ---

    def task_view(t: dict) -> dict:
        return {k: t.get(k) for k in ("id", "name", "when", "cron", "prompt", "enabled", "next_run", "last_run", "history", "chat_id")}

    @app.get("/api/schedule")
    def list_tasks():
        return [task_view(t) for t in scheduler.tasks]

    @app.post("/api/schedule")
    def add_task(task: TaskIn):
        try:
            return task_view(scheduler.add(task.when, task.prompt, task.name))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.patch("/api/schedule/{task_id}")
    def patch_task(task_id: str, patch: TaskPatch):
        try:
            return task_view(scheduler.set_enabled(task_id, patch.enabled))
        except KeyError as e:
            raise HTTPException(404, "Задача не найдена") from e

    @app.delete("/api/schedule/{task_id}")
    def delete_task(task_id: str):
        if not scheduler.remove(task_id):
            raise HTTPException(404, "Задача не найдена")
        return {"ok": True}

    @app.post("/api/schedule/{task_id}/run")
    async def run_task(task_id: str):
        task = scheduler.get(task_id)
        if task is None:
            raise HTTPException(404, "Задача не найдена")
        text = await asyncio.to_thread(scheduler.run_task, task)
        return {"text": text, "task": task_view(task)}

    # --- память ---

    def need_memory():
        if memory is None:
            raise HTTPException(404, "Набор memory отключён")
        return memory

    @app.get("/api/memory")
    def list_memory():
        return need_memory().items

    @app.post("/api/memory")
    def add_memory(fact: Fact):
        if not fact.fact.strip():
            raise HTTPException(400, "Пустой факт")
        return {"id": need_memory().add(fact.fact)}

    @app.delete("/api/memory/{memory_id}")
    def delete_memory(memory_id: int):
        if not need_memory().remove(memory_id):
            raise HTTPException(404, "Запись не найдена")
        return {"ok": True}

    # --- документы ---

    docs_dir = settings.docs_dir

    def doc_path(name: str) -> Path:
        root = docs_dir.resolve()
        target = (root / name).resolve()
        if root not in target.parents:
            raise HTTPException(400, "Недопустимое имя файла")
        return target

    @app.get("/api/docs")
    def list_docs():
        if not docs_dir.exists():
            return []
        return [
            {"name": str(p.relative_to(docs_dir)), "size": p.stat().st_size}
            for p in sorted(docs_dir.rglob("*"))
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(docs_dir).parts)
        ]

    @app.post("/api/docs")
    async def upload_docs(files: list[UploadFile]):
        docs_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            name = Path(f.filename or "").name
            if not name or name.startswith("."):
                raise HTTPException(400, f"Недопустимое имя файла: {f.filename!r}")
            data = await f.read()
            if len(data) > UPLOAD_LIMIT:
                raise HTTPException(413, f"{name}: файл больше {UPLOAD_LIMIT // 1024 // 1024} МБ")
            doc_path(name).write_bytes(data)
            saved.append(name)
        return {"saved": saved}

    @app.delete("/api/docs/{name:path}")
    def delete_doc(name: str):
        target = doc_path(name)
        if not target.is_file():
            raise HTTPException(404, "Файл не найден")
        target.unlink()
        return {"ok": True}

    @app.post("/api/docs/reindex")
    async def reindex():
        if "index_documents" not in registry:
            raise HTTPException(404, "Набор docs отключён")
        return {"result": await asyncio.to_thread(registry.call, "index_documents", {})}

    # --- живая панель браузера агента ---

    @app.websocket("/ws/browser")
    async def ws_browser(ws: WebSocket):
        if browser is None or not _same_origin(ws.headers) or (token and ws.query_params.get("token") != token):
            await ws.close(code=1008)
            return
        await ws.accept()
        loop = asyncio.get_running_loop()
        latest: dict[str, Any] = {}
        ready = asyncio.Event()

        def on_frame(data: bytes, meta: dict) -> None:  # из потока браузера
            latest["frame"] = (data, meta)  # промежуточные кадры отбрасываются, если сеть не успевает
            loop.call_soon_threadsafe(ready.set)

        async def sender() -> None:
            while True:
                await ready.wait()
                ready.clear()
                data, meta = latest.pop("frame")
                await ws.send_json({"type": "meta", **meta})
                await ws.send_bytes(data)

        await ws.send_json({"type": "status", **browser.status()})
        browser.add_viewer(on_frame)
        send_task = asyncio.create_task(sender())
        try:
            while True:
                ev = await ws.receive_json()
                kind = ev.pop("type", "")
                try:
                    await asyncio.to_thread(browser.user_action, kind, **ev)
                except Exception as e:  # noqa: BLE001
                    await ws.send_json({"type": "error", "text": error_text(e, settings)})
        except WebSocketDisconnect:
            pass
        finally:
            browser.remove_viewer(on_frame)
            send_task.cancel()

    # --- чат в браузере ---

    @app.websocket("/ws")
    async def ws_chat(ws: WebSocket):
        if not _same_origin(ws.headers) or (token and ws.query_params.get("token") != token):
            await ws.close(code=1008)
            return
        await ws.accept()
        session_id = ws.query_params.get("session") or uuid.uuid4().hex
        agent = hub.agent(session_id)
        channel = Channel(ws, asyncio.get_running_loop())
        await ws.send_json({"type": "history", "messages": _history(agent), "busy": hub.busy(session_id)})

        async def run(text: str) -> None:
            await ws.send_json({"type": "busy", "value": True})
            try:
                answer = await asyncio.to_thread(hub.ask, session_id, text, channel)
                payload = {"type": "answer", "text": answer}
            except Exception as e:  # noqa: BLE001
                payload = {"type": "error", "text": error_text(e, settings)}
            if not channel.closed:
                await ws.send_json(payload)
                await ws.send_json({"type": "busy", "value": False})

        tasks: set[asyncio.Task] = set()
        try:
            while True:
                data = await ws.receive_json()
                kind = data.get("type")
                if kind == "approve":
                    channel.resolve(str(data.get("id")), bool(data.get("allow")))
                elif kind == "message":
                    text = str(data.get("text", "")).strip()
                    if not text:
                        continue
                    if hub.busy(session_id):
                        await ws.send_json({"type": "error", "text": "Агент ещё отвечает — дождитесь ответа"})
                        continue
                    task = asyncio.create_task(run(text))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                elif kind == "reset":
                    if hub.busy(session_id):
                        await ws.send_json({"type": "error", "text": "Нельзя очистить чат, пока агент отвечает"})
                        continue
                    hub.reset(session_id)
                    await ws.send_json({"type": "history", "messages": [], "busy": False})
        except WebSocketDisconnect:
            pass
        finally:
            channel.close()

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(prog="lm-agent-web", description="Веб-чат LM Agent")
    parser.add_argument("--host", default=os.getenv("LM_AGENT_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("LM_AGENT_WEB_PORT", "8000")))
    parser.add_argument("-m", "--model", help="модель Ollama")
    parser.add_argument("-t", "--toolkits", default=os.getenv("LM_AGENT_TOOLKITS", ",".join(ALL)))
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждений (опасно)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if "LM_AGENT_BROWSER_HEADLESS" not in os.environ:
        # в веб-чате браузер виден в живой панели — отдельное окно Chrome не нужно
        settings.browser_headless = True
    if args.model:
        settings.model = args.model
    if args.yes:
        settings.auto_approve = True
    toolkits = tuple(t.strip() for t in args.toolkits.split(",") if t.strip())
    unknown = set(toolkits) - set(ALL)
    if unknown:
        parser.error(f"неизвестные наборы: {', '.join(sorted(unknown))}. Доступны: {', '.join(ALL)}")

    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.getenv("LM_AGENT_WEB_TOKEN"):
        print(
            f"⚠️  Сервер будет доступен по сети ({args.host}), а у агента есть shell и доступ к файлам.\n"
            "   Задайте LM_AGENT_WEB_TOKEN или запускайте на 127.0.0.1."
        )
    print(f"LM Agent · модель {settings.model} · откройте http://{args.host}:{args.port}")
    uvicorn.run(create_app(settings, toolkits), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

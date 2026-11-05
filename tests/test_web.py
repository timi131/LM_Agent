import pytest
from fastapi.testclient import TestClient

from lm_agent import Settings
from lm_agent.web.server import create_app


class FakeClient:
    """Чат по сценарию + простые эмбеддинги для документов."""

    def __init__(self, responses=()):
        self.responses = list(responses)

    def chat(self, stream=False, **kwargs):
        response = self.responses.pop(0)
        if not stream:
            return response
        # как Ollama: текст приходит кусочками, вызовы инструментов — целиком
        msg = response["message"]
        chunks = [{"message": {"role": "assistant", "content": w}} for w in msg.get("content", "").split(" ") if w]
        chunks = [{"message": {"role": "assistant", "content": c["message"]["content"] + " "}} for c in chunks]
        if msg.get("tool_calls"):
            chunks.append({"message": {"role": "assistant", "content": "", "tool_calls": msg["tool_calls"]}})
        return iter(chunks or [{"message": {"role": "assistant", "content": ""}}])

    def embed(self, model, input):
        return {"embeddings": [[len(t) % 7 + 1.0, 1.0] for t in input]}


def tool_call(name, **arguments):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}}


def answer(text):
    return {"message": {"role": "assistant", "content": text}}


@pytest.fixture
def make(tmp_path):
    def factory(responses=(), **overrides):
        settings = Settings(
            workspace=tmp_path / "ws", docs_dir=tmp_path / "docs", data_dir=tmp_path / "data",
            services_file=tmp_path / "services.json", file_roots=[tmp_path], **overrides,
        )
        return TestClient(create_app(settings, client=FakeClient(responses), background=False))

    return factory


def receive_until(ws, kind):
    events = []
    while True:
        ev = ws.receive_json()
        events.append(ev)
        if ev["type"] == kind:
            return events


def test_index_and_info(make):
    client = make()
    assert "REDSMS Агент" in client.get("/").text
    info = client.get("/api/info").json()
    assert "run_command" in info["tools"] and info["auto_approve"] is False


def test_ws_chat_with_approval(make):
    client = make([tool_call("run_command", command="echo hi"), answer("Готово")])
    with client.websocket_connect("/ws?session=s1") as ws:
        assert ws.receive_json() == {"type": "history", "messages": [], "busy": False}
        ws.send_json({"type": "message", "text": "запусти echo"})
        approval = receive_until(ws, "approval")[-1]
        assert approval["name"] == "run_command" and approval["arguments"] == {"command": "echo hi"}
        ws.send_json({"type": "approve", "id": approval["id"], "allow": True})
        events = receive_until(ws, "answer")
        tool = next(e for e in events if e["type"] == "tool")
        assert "stdout:\nhi" in tool["result"]
        assert events[-1]["text"].strip() == "Готово"
        assert any(e["type"] == "delta" for e in events)  # ответ шёл потоком

    # история сессии восстанавливается при переподключении
    with client.websocket_connect("/ws?session=s1") as ws:
        history = ws.receive_json()["messages"]
        assert [m["role"] for m in history] == ["user", "tool", "assistant"]
        assert history[1]["arguments"] == {"command": "echo hi"}


def test_ws_denied(make):
    client = make([tool_call("run_command", command="rm -rf /tmp/x"), answer("Ок, не буду")])
    with client.websocket_connect("/ws?session=s2") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "удали"})
        approval = receive_until(ws, "approval")[-1]
        ws.send_json({"type": "approve", "id": approval["id"], "allow": False})
        tool = next(e for e in receive_until(ws, "answer") if e["type"] == "tool")
        assert tool["result"].startswith("Отклонено")


def test_http_chat_denies_dangerous_by_default(make):
    client = make([tool_call("run_command", command="echo hi"), answer("Не смог")])
    data = client.post("/api/chat", json={"message": "запусти", "session": "api"}).json()
    assert data["answer"].strip() == "Не смог"
    assert all(e["type"] == "tool" for e in data["tools"])  # кусочки текста в HTTP-ответ не попадают
    assert data["tools"][0]["result"].startswith("Отклонено")


def test_cross_origin_blocked(make):
    client = make()
    r = client.post("/api/memory", json={"fact": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_token(make, monkeypatch):
    monkeypatch.setenv("LM_AGENT_WEB_TOKEN", "s3cret")
    client = make()
    assert client.get("/api/info").status_code == 401
    assert client.get("/api/info", headers={"X-Token": "s3cret"}).status_code == 200


def test_memory_api(make):
    client = make()
    new_id = client.post("/api/memory", json={"fact": "Отвечай кратко"}).json()["id"]
    assert client.get("/api/memory").json()[0]["text"] == "Отвечай кратко"
    assert client.delete(f"/api/memory/{new_id}").status_code == 200
    assert client.get("/api/memory").json() == []


def test_docs_api(make):
    client = make()
    r = client.post("/api/docs", files=[("files", ("hr.md", "Отпуск за 14 дней".encode(), "text/markdown"))])
    assert r.json() == {"saved": ["hr.md"]}
    assert client.get("/api/docs").json()[0]["name"] == "hr.md"
    assert "Файлов: 1" in client.post("/api/docs/reindex").json()["result"]
    assert client.delete("/api/docs/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert client.delete("/api/docs/hr.md").status_code == 200
    assert client.get("/api/docs").json() == []


def test_live_browser_panel(make, tmp_path):
    page = tmp_path / "p.html"
    page.write_text("<html><body style='margin:0'><button id=b style='width:300px;height:100px' "
                    "onclick=\"document.title='нажато'\">Жми</button></body></html>")
    client = make(browser_headless=True)
    with client.websocket_connect("/ws/browser") as ws:
        assert ws.receive_json()["type"] == "status"
        ws.send_json({"type": "navigate", "url": page.as_uri()})
        meta = ws.receive_json()
        if meta.get("type") == "error":
            pytest.skip(f"браузер недоступен: {meta['text']}")
        assert meta["type"] == "meta" and meta["url"].endswith("p.html")
        assert ws.receive_bytes()[:2] == b"\xff\xd8"  # JPEG-кадр
        ws.send_json({"type": "click", "x": 50, "y": 50})
        for _ in range(10):  # ждём кадр с новым заголовком
            ev = ws.receive_json()
            ws.receive_bytes()
            if ev.get("title") == "нажато":
                break
        assert ev["title"] == "нажато"


def test_schedule_api(make):
    client = make([answer("Итог: всё хорошо")])
    bad = client.post("/api/schedule", json={"when": "когда-нибудь", "prompt": "x"})
    assert bad.status_code == 400 and "не понял время" in bad.json()["detail"]
    task = client.post("/api/schedule", json={"when": "будни 09:00", "prompt": "сводка", "name": "Утро"}).json()
    assert task["when"] == "по будням в 09:00" and task["enabled"]
    assert client.patch(f"/api/schedule/{task['id']}", json={"enabled": False}).json()["enabled"] is False
    run = client.post(f"/api/schedule/{task['id']}/run").json()
    assert run["text"].strip() == "Итог: всё хорошо" and run["task"]["history"][0]["ok"]
    assert client.delete(f"/api/schedule/{task['id']}").status_code == 200
    assert client.get("/api/schedule").json() == []


def test_transcribe_api(make):
    client = make()

    class FakeTranscriber:
        def transcribe(self, path):
            assert path.suffix == ".webm" and path.read_bytes() == b"webm-audio"
            return "привет агент"

    client.app.state.hub.transcriber = FakeTranscriber()
    r = client.post("/api/transcribe", files={"audio": ("voice.webm", b"webm-audio", "audio/webm")})
    assert r.json() == {"text": "привет агент"}
    assert client.post("/api/transcribe", files={"audio": ("voice.webm", b"", "audio/webm")}).status_code == 400

    client.app.state.hub.transcriber = None
    r = client.post("/api/transcribe", files={"audio": ("voice.webm", b"x", "audio/webm")})
    assert r.status_code == 503 and client.get("/api/info").json()["voice"] is False


def test_download_created_file(make, tmp_path):
    client = make()
    report = tmp_path / "ws" / "reports" / "Отчёт.docx"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"PK-docx")
    r = client.get("/api/files/reports/Отчёт.docx")
    assert r.status_code == 200 and r.content == b"PK-docx" and "attachment" in r.headers["content-disposition"]
    secret = tmp_path / "secret.txt"
    secret.write_text("пароль")
    assert client.get("/api/files/../secret.txt").status_code == 404
    assert client.get("/api/files/..%2F..%2Fsecret.txt").status_code == 404


def test_agent_log_written(make, tmp_path):
    client = make([tool_call("calculate", expression="2+2"), answer("4")])
    client.post("/api/chat", json={"message": "сколько 2+2", "session": "s-log"})
    events = client.app.state.hub.log.tail()
    assert [e["event"] for e in events] == ["user", "tool", "answer"]
    assert events[1]["name"] == "calculate" and events[1]["result"] == "4" and events[0]["session"] == "s-log"

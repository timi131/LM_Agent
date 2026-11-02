import pytest
from fastapi.testclient import TestClient

from lm_agent import Settings
from lm_agent.web.server import create_app


class FakeClient:
    """Чат по сценарию + простые эмбеддинги для документов."""

    def __init__(self, responses=()):
        self.responses = list(responses)

    def chat(self, **kwargs):
        return self.responses.pop(0)

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
        return TestClient(create_app(settings, client=FakeClient(responses)))

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
    assert "LM Agent" in client.get("/").text
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
        assert events[-1]["text"] == "Готово"

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
    assert data["answer"] == "Не смог"
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

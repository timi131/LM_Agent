import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lm_agent.toolkits.vscode import VSCodeBridge, register
from lm_agent.tools import ToolRegistry


class BridgeHandler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self.seen.append(("GET", self.path, self.headers.get("Authorization"), None))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "workspaces": ["project"]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.path == "/select_project":
            result = {"path": "/tmp/project"}
        elif self.path == "/select_analysis":
            result = {"path": "/tmp/source"}
        elif self.path == "/list":
            result = {"files": ["src/", "main.py"], "truncated": False}
        elif self.path == "/read":
            result = {"content": "print('hello')", "truncated": False}
        else:
            result = {"path": body.get("path", ""), "line": 7, "column": 2}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, *_):
        pass


@pytest.fixture
def vscode_registry(tmp_path):
    BridgeHandler.seen = []
    server = HTTPServer(("127.0.0.1", 0), BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry = ToolRegistry()
    register(registry, f"http://127.0.0.1:{server.server_port}", tmp_path / "vscode_token")
    yield registry, tmp_path / "vscode_token"
    server.shutdown()
    thread.join()
    server.server_close()


def test_vscode_tools_use_authenticated_local_bridge(vscode_registry):
    registry, token_file = vscode_registry
    assert "VS Code подключён" in registry.call("vscode_status", {})
    assert "7:2" in registry.call("vscode_open_file", {"path": "src/main.py", "line": 7, "column": 2})
    assert token_file.read_text().strip()
    assert token_file.stat().st_mode & 0o077 == 0
    assert all(call[2] == f"Bearer {token_file.read_text().strip()}" for call in BridgeHandler.seen)


def test_vscode_edits_and_terminal_require_confirmation(vscode_registry):
    registry, _ = vscode_registry
    assert "Отклонено" in registry.call("vscode_edit_file", {"path": "main.py", "content": "new"})
    assert "Отклонено" in registry.call("vscode_create_file", {"path": "new.py", "content": "new"})
    assert "Отклонено" in registry.call("vscode_run_terminal", {"command": "pytest"})
    assert not BridgeHandler.seen

    registry.approver = lambda name, args: True
    assert "Файл проекта сохранён" in registry.call("vscode_edit_file", {"path": "main.py", "content": "new"})
    assert "Файл проекта создан" in registry.call("vscode_create_file", {"path": "new.py", "content": "new"})
    assert "Команда отправлена" in registry.call("vscode_run_terminal", {"command": "pytest"})
    assert [call[1] for call in BridgeHandler.seen] == ["/edit", "/create", "/terminal"]


def test_vscode_folder_selection_and_read_tools(vscode_registry):
    registry, _ = vscode_registry
    assert "Папка проекта выбрана: /tmp/project" in registry.call("vscode_select_project_folder", {})
    assert "Папка для анализа выбрана: /tmp/source" in registry.call("vscode_select_analysis_folder", {})
    assert json.loads(registry.call("vscode_list_files", {"path": ".", "scope": "analysis"})) == {
        "files": ["src/", "main.py"], "truncated": False,
    }
    assert json.loads(registry.call("vscode_read_file", {"path": "main.py", "scope": "analysis"})) == {
        "content": "print('hello')", "truncated": False,
    }


def test_vscode_bridge_rejects_non_loopback_urls(tmp_path):
    for url in ("https://127.0.0.1:8765", "http://example.com:8765", "http://127.0.0.1"):
        with pytest.raises(ValueError, match="localhost URL"):
            VSCodeBridge(url, tmp_path / "token")

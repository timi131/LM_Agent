"""Каталог и клиенты моделей для Ollama и OpenAI-compatible API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import httpx


class OpenAICompatibleClient:
    """Минимальный клиент chat completions с tool calling и потоком текста."""

    def __init__(self, base_url: str, api_key: str = "", model: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def chat(self, *, model: str, messages: list[dict], tools: list[dict], options: dict,
             stream: bool = False, **_kwargs) -> dict | Iterator[dict]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "temperature": options.get("temperature", 0.2),
            "stream": stream,
        }
        if "num_ctx" in options:
            payload["max_tokens"] = 4096

        if not stream:
            response = httpx.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=120,
            )
            if response.status_code >= 400:
                raise ConnectionError(f"Внешняя модель вернула HTTP {response.status_code}: {response.text[:500]}")
            return self._message(response.json())

        return self._stream(headers, payload)

    def _stream(self, headers: dict, payload: dict) -> Iterator[dict]:
        tool_calls: dict[int, dict] = {}
        with httpx.stream(
            "POST", f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=120,
        ) as response:
            if response.status_code >= 400:
                raise ConnectionError(f"Внешняя модель вернула HTTP {response.status_code}: {response.text[:500]}")
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield {"message": {"role": "assistant", "content": content}}
                for call in delta.get("tool_calls") or []:
                    index = call.get("index", 0)
                    target = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    target["id"] = call.get("id") or target["id"]
                    function = call.get("function") or {}
                    target["function"]["name"] += function.get("name") or ""
                    target["function"]["arguments"] += function.get("arguments") or ""

        if tool_calls:
            yield {"message": {"role": "assistant", "content": "", "tool_calls": self._normalize_tool_calls(tool_calls)}}

    def _message(self, data: dict) -> dict:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        normalized = self._normalize_tool_calls({
            i: call for i, call in enumerate(message.get("tool_calls") or [])
        })
        return {"message": {
            "role": message.get("role", "assistant"),
            "content": message.get("content") or "",
            **({"tool_calls": normalized} if normalized else {}),
        }}

    @staticmethod
    def _normalize_tool_calls(calls: dict[int, dict]) -> list[dict]:
        normalized = []
        for index in sorted(calls):
            call = calls[index]
            raw = call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {"raw": raw}
            normalized.append({"id": call.get("id", ""), "type": "function",
                               "function": {"name": call["function"]["name"], "arguments": arguments}})
        return normalized


class ExternalModelStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path.chmod(0o600)

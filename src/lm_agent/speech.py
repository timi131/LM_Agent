"""Распознавание речи локально через faster-whisper (Whisper на CPU, без интернета).

Модель скачивается один раз в data_dir/whisper. Размеры: tiny / base / small / medium / large-v3:
чем больше, тем точнее и медленнее. small — хороший баланс для русского языка.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

# Новый CDN HuggingFace (Xet) отвечает 403 через часть VPN — качаем модель обычным способом.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


class Transcriber:
    def __init__(self, model_size: str, models_dir: Path, language: str | None = None) -> None:
        self.model_size = model_size
        self.models_dir = models_dir
        self.language = language or None  # None — определить язык автоматически
        self._model = None
        self._lock = threading.Lock()  # модель одна, распознаём по очереди

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self.models_dir.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8", download_root=str(self.models_dir),
            )
        return self._model

    def transcribe(self, audio: Path) -> str:
        with self._lock:
            model = self._load()
            segments, _info = model.transcribe(
                str(audio), language=self.language, vad_filter=True, beam_size=5,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

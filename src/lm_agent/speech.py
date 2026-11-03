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

SAMPLE_RATE = 16_000  # Whisper ждёт моно 16 кГц


# Подсказка распознаванию: слова, которые часто звучат в работе. Whisper пишет их правильно,
# а не «редсмс» или «око срм». Свою задайте в LM_AGENT_WHISPER_PROMPT («-» — без подсказки).
DEFAULT_PROMPT = (
    "Разговор о сервисе рассылок REDSMS. SMS, рассылка, заявка, ответственный, статус, "
    "OkoCRM, Telegram, личный кабинет, API, имя отправителя, Мегафон, МТС, Билайн, Т2."
)


class Transcriber:
    def __init__(
        self, model_size: str, models_dir: Path, language: str | None = "ru", prompt: str | None = DEFAULT_PROMPT,
    ) -> None:
        self.model_size = model_size
        self.models_dir = models_dir
        # Русский по умолчанию: на коротких фразах автоопределение языка часто ошибается.
        # "auto" или None — определять язык самому.
        self.language = None if (language or "auto").lower() == "auto" else language
        self.prompt = prompt or None
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
        samples = decode_audio(audio)
        with self._lock:
            model = self._load()
            segments, _info = model.transcribe(
                samples, language=self.language, vad_filter=True, beam_size=5,
                initial_prompt=self.prompt,
                # без этого Whisper на длинной записи подхватывает свои же ошибки и начинает повторяться
                condition_on_previous_text=False,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()


def decode_audio(path: Path, rate: int = SAMPLE_RATE):
    """Любой аудиофайл (голосовые Telegram — Ogg/Opus) → моно float32 с частотой 16 кГц.

    Декодируем сами: встроенный декодер faster-whisper 1.2 не совместим с PyAV 19+
    (передаёт удалённый параметр metadata_errors)."""
    import av
    import numpy as np

    chunks = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # хвост из буфера ресемплера
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def make_transcriber(settings) -> Transcriber | None:
    """Распознавание, если установлен faster-whisper и оно не выключено (LM_AGENT_WHISPER_MODEL=off).
    Модель загружается при первом голосовом, не при старте."""
    size = os.getenv("LM_AGENT_WHISPER_MODEL", "small").strip()
    if size.lower() in ("", "off", "0", "none"):
        return None
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return None
    prompt = os.getenv("LM_AGENT_WHISPER_PROMPT", DEFAULT_PROMPT).strip()
    return Transcriber(
        size, settings.data_dir / "whisper",
        language=os.getenv("LM_AGENT_WHISPER_LANGUAGE", "").strip() or "ru",
        prompt=None if prompt in ("", "-") else prompt,
    )

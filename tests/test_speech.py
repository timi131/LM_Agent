import math

import pytest

av = pytest.importorskip("av")

from lm_agent.speech import SAMPLE_RATE, decode_audio  # noqa: E402


def make_voice(path, seconds=1.0, rate=48000):
    """Синусоида в Ogg/Opus — как голосовое Telegram."""
    import numpy as np

    out = av.open(str(path), "w", format="ogg")
    stream = out.add_stream("libopus", rate=rate)
    stream.layout = "mono"
    t = np.arange(int(rate * seconds)) / rate
    samples = (np.sin(2 * math.pi * 440 * t) * 0.3 * 32767).astype("int16").reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
    frame.sample_rate = rate
    for packet in stream.encode(frame):
        out.mux(packet)
    for packet in stream.encode(None):
        out.mux(packet)
    out.close()


def test_decode_telegram_voice(tmp_path):
    path = tmp_path / "voice.oga"
    make_voice(path, seconds=1.0)
    samples = decode_audio(path)
    assert samples.dtype.name == "float32"
    assert abs(len(samples) / SAMPLE_RATE - 1.0) < 0.1  # длительность сохранилась, частота 16 кГц
    assert 0.1 < abs(samples).max() <= 1.0  # сигнал есть и нормирован


def test_transcriber_defaults(monkeypatch, tmp_path):
    from lm_agent.config import Settings
    from lm_agent.speech import DEFAULT_PROMPT, make_transcriber

    settings = Settings(data_dir=tmp_path)
    for name in ("LM_AGENT_WHISPER_MODEL", "LM_AGENT_WHISPER_LANGUAGE", "LM_AGENT_WHISPER_PROMPT"):
        monkeypatch.delenv(name, raising=False)
    tr = make_transcriber(settings)
    assert tr.language == "ru" and tr.prompt == DEFAULT_PROMPT and tr.model_size == "small"
    monkeypatch.setenv("LM_AGENT_WHISPER_LANGUAGE", "auto")
    monkeypatch.setenv("LM_AGENT_WHISPER_PROMPT", "-")
    monkeypatch.setenv("LM_AGENT_WHISPER_MODEL", "large-v3-turbo")
    tr = make_transcriber(settings)
    assert tr.language is None and tr.prompt is None and tr.model_size == "large-v3-turbo"
    monkeypatch.setenv("LM_AGENT_WHISPER_MODEL", "off")
    assert make_transcriber(settings) is None

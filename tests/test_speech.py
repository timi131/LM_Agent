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

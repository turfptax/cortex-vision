"""Tests for the WASAPI loopback + mic audio capture module (v0.4.0).

sounddevice is mocked throughout — these tests don't open a real audio
device. The pipeline integration test in test_live_pipeline.py exercises
the orchestrator's audio path with the same mocks.
"""
from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# _LinearResampler — pure-numpy, no mocks needed
# ---------------------------------------------------------------------------

def test_resampler_passes_through_when_rates_match():
    from cortex_vision.audio.loopback import _LinearResampler

    r = _LinearResampler(src_rate=16000, src_channels=1, dst_rate=16000, dst_channels=1)
    sig = np.linspace(-1, 1, 1600, dtype=np.float32)
    out = r.resample(sig.reshape(-1, 1))
    assert out.shape == (1600,)
    assert np.allclose(out, sig, atol=1e-3)


def test_resampler_downsamples_48k_to_16k():
    from cortex_vision.audio.loopback import _LinearResampler

    r = _LinearResampler(src_rate=48000, src_channels=1, dst_rate=16000, dst_channels=1)
    # 1 second of 48kHz audio
    sig = np.sin(np.linspace(0, 2 * np.pi * 440, 48000, dtype=np.float32))
    out = r.resample(sig.reshape(-1, 1))
    # Should produce ~16000 samples
    assert 15500 <= out.shape[0] <= 16500


def test_resampler_downmixes_stereo_to_mono():
    from cortex_vision.audio.loopback import _LinearResampler

    r = _LinearResampler(src_rate=16000, src_channels=2, dst_rate=16000, dst_channels=1)
    # Stereo: left=1.0, right=-1.0 -> mono should be 0
    stereo = np.zeros((100, 2), dtype=np.float32)
    stereo[:, 0] = 1.0
    stereo[:, 1] = -1.0
    out = r.resample(stereo)
    assert out.shape == (100,)
    assert np.allclose(out, 0.0)


def test_resampler_handles_empty_input():
    from cortex_vision.audio.loopback import _LinearResampler

    r = _LinearResampler(src_rate=48000, src_channels=1, dst_rate=16000, dst_channels=1)
    out = r.resample(np.zeros((0, 1), dtype=np.float32))
    assert out.shape == (0,)


# ---------------------------------------------------------------------------
# _resolve_device — translates user-facing spec into sounddevice index
# ---------------------------------------------------------------------------

def _fake_sd_query_devices(devices):
    """Return a callable that mocks sd.query_devices() for the given list."""
    def query(idx=None):
        if idx is None:
            return devices
        return devices[idx]
    return query


def test_resolve_device_default_loopback_when_none():
    from cortex_vision.audio.loopback import _resolve_device

    fake_sd = MagicMock()
    fake_sd.default.device = (0, 5)               # input=0, output=5
    fake_sd.query_devices = _fake_sd_query_devices([])

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        idx, loopback = _resolve_device(None)

    assert idx == 5
    assert loopback is True


def test_resolve_device_int_is_input():
    from cortex_vision.audio.loopback import _resolve_device

    fake_sd = MagicMock()

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        idx, loopback = _resolve_device(2)

    assert idx == 2
    assert loopback is False


def test_resolve_device_string_match_input():
    from cortex_vision.audio.loopback import _resolve_device

    fake_sd = MagicMock()
    fake_sd.query_devices = _fake_sd_query_devices([
        {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Yeti Microphone", "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
    ])

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        idx, loopback = _resolve_device("Yeti")

    assert idx == 1
    assert loopback is False


def test_resolve_device_string_match_output_uses_loopback():
    from cortex_vision.audio.loopback import _resolve_device

    fake_sd = MagicMock()
    fake_sd.query_devices = _fake_sd_query_devices([
        {"name": "Yeti Microphone", "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Speakers (Realtek)", "max_input_channels": 0, "max_output_channels": 2},
    ])

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        idx, loopback = _resolve_device("Speakers")

    assert idx == 1
    assert loopback is True


def test_resolve_device_no_match_raises():
    from cortex_vision.audio.loopback import _resolve_device

    fake_sd = MagicMock()
    fake_sd.query_devices = _fake_sd_query_devices([
        {"name": "Yeti Microphone", "max_input_channels": 2, "max_output_channels": 0},
    ])

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        with pytest.raises(ValueError, match="No audio device matches"):
            _resolve_device("does-not-exist")


# ---------------------------------------------------------------------------
# list_input_devices — discovery for the picker
# ---------------------------------------------------------------------------

def test_list_input_devices_includes_default_output_loopback():
    from cortex_vision.audio.loopback import list_input_devices

    all_devices = [
        {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "default_samplerate": 44100, "max_output_channels": 0},
        {"name": "Yeti Microphone", "max_input_channels": 2, "default_samplerate": 48000, "max_output_channels": 0},
        {"name": "Speakers (Realtek)", "max_input_channels": 0, "default_samplerate": 48000, "max_output_channels": 2},
        {"name": "ignored output", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100},
        {"name": "Some Random Mic", "max_input_channels": 1, "default_samplerate": 16000, "max_output_channels": 0},
    ]

    fake_sd = MagicMock()
    fake_sd.default.device = (0, 2)        # default output is index 2 = Speakers (Realtek)

    def query(idx=None):
        if idx is None:
            return all_devices
        return all_devices[idx]

    fake_sd.query_devices = query

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        devices = list_input_devices()

    # Should include: 1 desktop loopback + 3 real input devices = 4
    assert len(devices) == 4
    # First entry is the desktop loopback sentinel
    assert devices[0].index == -1
    assert devices[0].is_default_output_loopback is True
    assert "Speakers" in devices[0].name
    # Then real inputs
    assert any("Yeti" in d.name for d in devices)


def test_list_input_devices_when_sounddevice_missing():
    """Graceful return when sounddevice doesn't exist (non-Windows or
    bundling failed)."""
    from cortex_vision.audio.loopback import list_input_devices

    # Importing sounddevice will raise — list returns empty
    real_import = __import__

    def fake_import(name, *args, **kw):
        if name == "sounddevice":
            raise ImportError("no sounddevice in this build")
        return real_import(name, *args, **kw)

    with patch("builtins.__import__", side_effect=fake_import):
        devices = list_input_devices()

    assert devices == []


# ---------------------------------------------------------------------------
# AudioCapture — full lifecycle with sounddevice mocked
# ---------------------------------------------------------------------------

class _FakeStream:
    """Mock sounddevice.InputStream with a callback we can drive."""
    def __init__(self, *, callback, samplerate, channels, **kwargs):
        self._callback = callback
        self.samplerate = samplerate
        self.channels = channels
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        pass

    # Test helper — simulate a buffer of audio coming in
    def feed_audio(self, frames: np.ndarray):
        # frames shape: (n_samples, n_channels) float32
        self._callback(frames, frames.shape[0], None, None)


def test_audio_capture_writes_wav(tmp_path):
    from cortex_vision.audio import loopback

    fake_sd = MagicMock()
    fake_sd.default.device = (0, 5)
    fake_sd.query_devices.return_value = {
        "name": "Speakers", "max_output_channels": 2, "default_samplerate": 48000.0,
    }

    stream_holder: dict = {}

    def fake_stream_ctor(**kw):
        s = _FakeStream(**kw)
        stream_holder["stream"] = s
        return s

    fake_sd.InputStream = fake_stream_ctor
    fake_sd.WasapiSettings = MagicMock(return_value="wasapi-settings-marker")

    out_wav = tmp_path / "audio.wav"
    captured_levels: list[tuple[float, float]] = []

    cap = loopback.AudioCapture(
        out_path=out_wav,
        device=None,                                    # default loopback
        on_level=lambda r, p: captured_levels.append((r, p)),
    )

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        cap.open()
        # Simulate ~1 second of 48kHz stereo audio = 48000 frames
        # We feed it as 10 chunks of 4800 each
        for _ in range(10):
            audio = np.random.uniform(-0.5, 0.5, (4800, 2)).astype(np.float32)
            stream_holder["stream"].feed_audio(audio)
        cap.close()

    assert out_wav.exists()
    # WAV should be playable as 16kHz mono 16-bit
    with wave.open(str(out_wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        # ~1s of audio -> ~16000 frames (resampler trims a small tail)
        assert 14000 <= w.getnframes() <= 17000

    # Level callback fired at least once
    assert len(captured_levels) >= 1
    rms, peak = captured_levels[0]
    assert 0 <= rms <= 1
    assert 0 <= peak <= 1


def test_audio_capture_close_is_idempotent(tmp_path):
    from cortex_vision.audio import loopback

    fake_sd = MagicMock()
    fake_sd.default.device = (0, 5)
    fake_sd.query_devices.return_value = {
        "name": "Mic", "max_input_channels": 1, "default_samplerate": 16000.0,
        "max_output_channels": 0,
    }
    fake_sd.InputStream = lambda **kw: _FakeStream(**kw)
    fake_sd.WasapiSettings = MagicMock()

    cap = loopback.AudioCapture(out_path=tmp_path / "audio.wav", device=2)

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        cap.open()
        cap.close()
        cap.close()                                     # second close — must not raise
        cap.close()


def test_audio_capture_open_failure_cleans_up(tmp_path):
    from cortex_vision.audio import loopback

    fake_sd = MagicMock()
    fake_sd.default.device = (0, 5)
    fake_sd.query_devices.return_value = {
        "name": "Mic", "max_input_channels": 1, "default_samplerate": 16000.0,
        "max_output_channels": 0,
    }

    def boom(**kw):
        raise RuntimeError("device busy")

    fake_sd.InputStream = boom
    fake_sd.WasapiSettings = MagicMock()

    out_wav = tmp_path / "audio.wav"
    cap = loopback.AudioCapture(out_path=out_wav, device=2)

    with patch.dict("sys.modules", {"sounddevice": fake_sd}):
        with pytest.raises(RuntimeError, match="device busy"):
            cap.open()

    # WAV file should NOT exist — we cleaned up the partial open
    assert not out_wav.exists()
    assert cap.is_running is False

"""Real-time audio capture for live mode.

Two source types, picked at session start:

  1. Desktop audio (default) — WASAPI loopback grabs whatever's playing
     through your default Windows output device. No VB-Audio CABLE or
     Stereo Mix required; sounddevice's `WasapiSettings(loopback=True)`
     handles it natively. Captures the system mix exactly as you hear it.

  2. Microphone — pick a specific input device by name or index. Same
     callback path, just an InputStream without the loopback flag.

Output:
  - Continuous append to a single 16 kHz mono WAV file at <session_dir>/audio.wav
  - Periodic RMS callback (~10 Hz) for the live audio level meter
  - Lifecycle: open() -> running -> close() -> finalized WAV on disk

Why a file, not in-memory buffer:
  Live sessions can run for hours. 16 kHz mono = 32 KB/s = ~115 MB/hour.
  Files survive crashes (we can re-transcribe from disk). Memory buffers
  are just easier to lose.

The actual transcription happens in `pipeline/live.py` after Stop, by
running whisper.cpp on the finalized WAV via the existing v0.3.5
`transcribe_file()` chain. No live-stream transcription on this path —
post-process for better quality and lower CPU pressure.
"""
from __future__ import annotations

import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)


# How often we emit an RMS-level event. 10 Hz is smooth enough for a UI
# meter without flooding the WS queue.
LEVEL_INTERVAL_S = 0.1

# Target audio format. 16 kHz mono matches what whisper.cpp expects, so we
# don't need a separate ffmpeg resample on the recorded file.
SAMPLE_RATE = 16000
CHANNELS = 1


@dataclass
class AudioDevice:
    """One available input device. Returned by list_input_devices()."""
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float
    is_default_output_loopback: bool = False


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def list_input_devices() -> list[AudioDevice]:
    """Return audio capture sources usable by WasapiLoopbackCapture.

    Includes:
      - All real input devices (microphones, line-ins, USB capture cards)
      - The default Windows output device, exposed as a loopback target

    Used by the /api/video/live/audio-devices endpoint to populate the
    "Audio source" dropdown in the LiveMode picker.
    """
    try:
        import sounddevice as sd
    except Exception as e:                                # noqa: BLE001
        logger.warning("sounddevice not importable: %s", e)
        return []

    out: list[AudioDevice] = []

    # Default output device, exposed as loopback (entry index = -1 sentinel
    # so the caller can recognize "use default loopback" without naming a
    # specific device — Windows reassigns indices when devices change)
    try:
        default_output_idx = sd.default.device[1]
        default_output = sd.query_devices(default_output_idx)
        out.append(AudioDevice(
            index=-1,
            name=f"Desktop audio ({default_output['name']})",
            max_input_channels=default_output.get("max_output_channels", 2),
            default_samplerate=default_output.get("default_samplerate", 48000.0),
            is_default_output_loopback=True,
        ))
    except Exception:                                     # noqa: BLE001
        # Couldn't read default output — skip the desktop entry, still
        # return real mics
        pass

    # Real input devices
    try:
        for i, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                out.append(AudioDevice(
                    index=i,
                    name=dev["name"],
                    max_input_channels=dev["max_input_channels"],
                    default_samplerate=dev.get("default_samplerate", 0.0),
                ))
    except Exception:                                     # noqa: BLE001
        pass

    return out


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

class AudioCapture:
    """Continuous audio capture to a 16 kHz mono WAV file.

    Pick the source via `device`:
      - None  -> default WASAPI output, loopback (captures desktop audio)
      - int   -> sounddevice input index (microphone)
      - str   -> name substring match against the input device list

    Lifecycle:
        cap = AudioCapture(out_path=..., on_level=...)
        cap.open()    # starts capture
        ...
        cap.close()   # stops, finalizes WAV header
    """

    def __init__(
        self,
        out_path: Path,
        device: int | str | None = None,
        on_level: Callable[[float, float], None] | None = None,
    ) -> None:
        """
        Args:
            out_path: where to write the WAV file
            device: capture source — None for desktop loopback, int for
                input device index, str for substring match on device name
            on_level: optional callback(rms, peak) called ~10 Hz with
                normalized [0, 1] amplitudes for a UI level meter
        """
        self.out_path = Path(out_path)
        self.device = device
        self.on_level = on_level

        self._stream = None
        self._wav: wave.Wave_write | None = None
        self._lock = threading.Lock()
        self._running = False
        self._native_samplerate: int = 0
        self._native_channels: int = 0
        self._resampler: _LinearResampler | None = None
        self._last_level_emit: float = 0.0
        self._frames_written: int = 0
        self._error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Start the capture stream. Raises on failure.

        Channel-count negotiation: PortAudio's `paInvalidChannelCount` (-9998)
        fires when the count we request isn't valid for the device's current
        share mode. Different audio hardware reports `max_output_channels`
        wildly (8 for 7.1 surround, 6 for 5.1, 2 for stereo) but the input
        loopback typically only accepts the device's *current* mix-format
        channel count. We try a small list of common values in order:

            1. The device's reported max channels (most likely to work)
            2. 2 (stereo — by far the most common output config)
            3. 1 (mono — works for simple mic devices)

        First match wins. If all fail, raise with the last error.
        """
        if self._running:
            raise RuntimeError("AudioCapture already open")

        import sounddevice as sd

        # Resolve the device + decide loopback flag
        device_index, is_loopback = _resolve_device(self.device)

        # Read native format from the device
        info = sd.query_devices(device_index) if device_index is not None else sd.query_devices(sd.default.device[1])
        if is_loopback:
            native_channels = int(info.get("max_output_channels", 2)) or 2
        else:
            native_channels = int(info.get("max_input_channels", 1)) or 1
        self._native_samplerate = int(info.get("default_samplerate", 48000) or 48000)

        # Open the WAV file for writing
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.out_path), "wb")
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(2)        # 16-bit
        self._wav.setframerate(SAMPLE_RATE)

        # Build the kwargs once; only `channels` varies between attempts
        base_kwargs: dict = {
            "device": device_index,
            "samplerate": self._native_samplerate,
            "dtype": "float32",
            "callback": self._callback,
        }
        if is_loopback:
            try:
                base_kwargs["extra_settings"] = sd.WasapiSettings(loopback=True)
            except Exception:                             # noqa: BLE001
                # Older sounddevice / non-Windows — no loopback support
                logger.warning(
                    "WASAPI loopback not supported in this sounddevice build; "
                    "falling back to default input device"
                )

        # Channel-count attempts: native first, then common fallbacks. Dedup
        # while preserving order so we don't try the same count twice.
        candidates: list[int] = []
        for c in (native_channels, 2, 1):
            if c > 0 and c not in candidates:
                candidates.append(c)

        opened_channels: int | None = None
        last_error: Exception | None = None

        for channels in candidates:
            try:
                stream = sd.InputStream(**base_kwargs, channels=channels)
                stream.start()
                self._stream = stream
                opened_channels = channels
                break
            except Exception as e:                        # noqa: BLE001
                last_error = e
                logger.info(
                    "AudioCapture: channels=%d didn't work (%s) — trying next",
                    channels, e,
                )
                continue

        if opened_channels is None:
            # Clean up the half-opened WAV
            self._wav.close()
            self._wav = None
            try:
                self.out_path.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"Could not open audio stream with any of {candidates} channels "
                f"on device {device_index} (loopback={is_loopback}). "
                f"Last error: {last_error}"
            )

        # Build the resampler now that we know the actual channel count
        self._native_channels = opened_channels
        self._resampler = _LinearResampler(
            src_rate=self._native_samplerate,
            src_channels=opened_channels,
            dst_rate=SAMPLE_RATE,
            dst_channels=CHANNELS,
        )

        self._running = True
        logger.info(
            "audio capture started: device=%s native=%dHz x %dch -> 16kHz mono -> %s",
            device_index, self._native_samplerate, opened_channels, self.out_path,
        )

    def close(self) -> None:
        """Stop capture, finalize the WAV header. Idempotent."""
        with self._lock:
            self._running = False
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:                         # noqa: BLE001
                    logger.exception("error stopping audio stream")
                self._stream = None

            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:                         # noqa: BLE001
                    logger.exception("error closing WAV file")
                self._wav = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def duration_s(self) -> float:
        """Seconds of audio written to disk so far."""
        return self._frames_written / SAMPLE_RATE if self._frames_written else 0.0

    @property
    def native_samplerate(self) -> int:
        return self._native_samplerate

    @property
    def error(self) -> str | None:
        return self._error

    # ------------------------------------------------------------------
    # sounddevice callback (audio thread — keep fast)
    # ------------------------------------------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:
        """Called by sounddevice on every audio buffer. Must be non-blocking
        and fast. Resampling + WAV write happen here; if they're too slow,
        we'll get glitches. 16 kHz mono is light enough that this is fine
        even on modest CPUs."""
        if status:
            logger.debug("audio status flags: %s", status)

        # indata: shape (frames, channels), float32
        try:
            mono_16k = self._resampler.resample(indata)   # type: ignore[union-attr]
        except Exception as e:                            # noqa: BLE001
            self._error = f"resample failed: {e}"
            return

        # Write to WAV (convert float32 [-1, 1] to int16)
        with self._lock:
            if self._wav is not None:
                try:
                    int16 = np.clip(mono_16k * 32767.0, -32768, 32767).astype(np.int16)
                    self._wav.writeframes(int16.tobytes())
                    self._frames_written += len(int16)
                except Exception as e:                    # noqa: BLE001
                    self._error = f"wav write failed: {e}"
                    return

        # Emit level event ~10 Hz
        if self.on_level is not None:
            now = time.perf_counter()
            if now - self._last_level_emit >= LEVEL_INTERVAL_S:
                rms = float(np.sqrt(np.mean(mono_16k ** 2))) if mono_16k.size else 0.0
                peak = float(np.max(np.abs(mono_16k))) if mono_16k.size else 0.0
                try:
                    self.on_level(rms, peak)
                except Exception:                         # noqa: BLE001
                    logger.exception("on_level callback raised — ignoring")
                self._last_level_emit = now


# ---------------------------------------------------------------------------
# Internals — device resolution + resampling
# ---------------------------------------------------------------------------

def _resolve_device(spec: int | str | None) -> tuple[int | None, bool]:
    """Translate a user-facing device spec to (sd_index, is_loopback).

    None -> default output device, loopback=True
    int  -> input device index, loopback=False
    str  -> substring match on device name. If matches an output device,
            loopback=True; otherwise input device, loopback=False.
    """
    import sounddevice as sd

    if spec is None or spec == "" or spec == "desktop":
        # Default WASAPI output, loopback flag set in the InputStream call
        try:
            return sd.default.device[1], True
        except Exception:                                 # noqa: BLE001
            return None, True

    if isinstance(spec, int):
        return spec, False

    # String match — search both inputs and outputs
    needle = spec.lower()
    for i, dev in enumerate(sd.query_devices()):
        if needle in dev["name"].lower():
            if dev.get("max_input_channels", 0) > 0:
                return i, False
            if dev.get("max_output_channels", 0) > 0:
                return i, True

    raise ValueError(f"No audio device matches {spec!r}")


class _LinearResampler:
    """Tiny in-process resampler. Linear interpolation — adequate for
    speech-targeted ASR; no need for the SciPy-quality polyphase.

    Handles channel downmix to mono via simple average. Maintains internal
    state (a tail buffer) across calls so resampling stays seamless across
    audio buffer boundaries.
    """

    def __init__(self, src_rate: int, src_channels: int,
                 dst_rate: int, dst_channels: int) -> None:
        self.src_rate = src_rate
        self.src_channels = src_channels
        self.dst_rate = dst_rate
        self.dst_channels = dst_channels
        self.ratio = src_rate / dst_rate
        # Tail buffer for cross-call interpolation
        self._tail = np.zeros((0,), dtype=np.float32)

    def resample(self, indata: np.ndarray) -> np.ndarray:
        """Take a (frames, channels) float32 array; return (out_frames,) mono float32."""
        if indata.size == 0:
            return np.zeros((0,), dtype=np.float32)

        # Downmix to mono if needed
        if indata.ndim == 2 and indata.shape[1] > 1:
            mono = np.mean(indata, axis=1).astype(np.float32)
        elif indata.ndim == 2:
            mono = indata[:, 0].astype(np.float32)
        else:
            mono = indata.astype(np.float32)

        # Prepend tail
        full = np.concatenate([self._tail, mono])

        # Linear interpolation to dst_rate
        if self.src_rate == self.dst_rate:
            self._tail = np.zeros((0,), dtype=np.float32)
            return full

        # Compute output length so the next call's tail aligns
        n_in = len(full)
        n_out = int(n_in / self.ratio)
        if n_out <= 0:
            self._tail = full
            return np.zeros((0,), dtype=np.float32)

        # Sample indices (0 .. n_in-1) at the destination rate
        x_out = np.arange(n_out, dtype=np.float64) * self.ratio
        x_in = np.arange(n_in, dtype=np.float64)
        out = np.interp(x_out, x_in, full).astype(np.float32)

        # Save the tail beyond what we consumed for next time
        consumed = int(np.ceil(x_out[-1])) + 1 if n_out > 0 else 0
        self._tail = full[consumed:].copy() if consumed < n_in else np.zeros((0,), dtype=np.float32)

        return out

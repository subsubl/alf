"""
Bluetooth Mic Capture (VAD / PTT / Duplex / Wake Word) + WebRTC Audio DSP Front-End
+ Local Earcons + Follow-Up Conversation Window + Stream Watchdog + 20Hz Viseme Lip-Sync
for Raspberry Pi Zero 2 W (inspired by rhasspy/wyoming-satellite).
"""

from __future__ import annotations

import asyncio
from array import array
import collections
import logging
import math
from pathlib import Path
import queue
import struct
import sys
import threading
import time
import wave
from typing import Awaitable, Callable, Deque, Dict, List, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None

try:
    import webrtcvad
except Exception:  # pragma: no cover
    webrtcvad = None

try:
    from webrtc_noise_gain import AudioProcessor as _WebRTCAudioProcessor  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    _WebRTCAudioProcessor = None

try:
    import openwakeword  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    openwakeword = None

logger = logging.getLogger(__name__)

SendAudioFrameFn = Callable[[bytes], Awaitable[bool]]
SpeechStateFn = Callable[[bool], Awaitable[None]]
VisemeCallback = Callable[[int], Awaitable[None]]
PlaybackDoneCallback = Callable[[], Awaitable[None]]
BargeInCallback = Callable[[], Awaitable[None]]
WakeDetectedCallback = Callable[[], Awaitable[None]]
StreamStatusCallback = Callable[[bool], Awaitable[None]]
WakeDetectorFn = Callable[[bytes], bool]

_VALID_MIC_MODES = frozenset({"vad", "ptt", "duplex", "wake"})


def _normalize_mic_mode(mode: str) -> str:
    norm = (mode or "vad").strip().lower()
    return norm if norm in _VALID_MIC_MODES else "vad"


def synthesize_earcon_pcm(kind: str, sample_rate: int = 16000) -> bytes:
    """
    Generate a crisp, low-latency 16-bit mono PCM earcon chime (wyoming-satellite style).
    - 'awake': ascending two-tone chime (D5 -> A5, ~140ms)
    - 'done': gentle descending confirmation blip (G5 -> C5, ~100ms)
    - 'error': soft low double-pulse (E4 -> C4, ~160ms)
    """
    sr = max(8000, int(sample_rate))
    if kind == "awake":
        segments = [(587.33, 0.065, 0.28), (880.00, 0.075, 0.32)]
    elif kind == "done":
        segments = [(783.99, 0.045, 0.22), (523.25, 0.055, 0.20)]
    else:  # "error"
        segments = [(329.63, 0.075, 0.28), (261.63, 0.085, 0.28)]

    samples = array("h")
    two_pi = 2.0 * math.pi
    for freq, dur_s, amp in segments:
        n = max(1, int(sr * dur_s))
        for i in range(n):
            # Raised-cosine (Hann) fade-in/out window to eliminate speaker clicks
            env = 0.5 * (1.0 - math.cos(two_pi * i / max(1, n - 1)))
            val = int(32767.0 * amp * env * math.sin(two_pi * freq * (i / sr)))
            samples.append(max(-32768, min(32767, val)))

    if sys.byteorder != "little":  # pragma: no cover
        samples.byteswap()
    return samples.tobytes()


def load_wav_pcm16_mono(path: str | Path, target_rate: int = 16000) -> Optional[bytes]:
    """Load a custom 16-bit mono WAV file for --awake-wav / --done-wav earcons."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        with wave.open(str(p), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        if sampwidth != 2 or not raw:
            return None
        arr = array("h")
        arr.frombytes(raw[: len(raw) - (len(raw) % 2)])
        if sys.byteorder != "little":  # pragma: no cover
            arr.byteswap()
        if channels > 1:
            mono = array("h", (arr[i] for i in range(0, len(arr) - channels + 1, channels)))
            arr = mono
        if rate != target_rate and len(arr) > 1:
            ratio = target_rate / float(rate)
            out_len = max(1, int(len(arr) * ratio))
            resampled = array(
                "h",
                (arr[min(len(arr) - 1, int(i / ratio))] for i in range(out_len)),
            )
            arr = resampled
        if sys.byteorder != "little":  # pragma: no cover
            arr.byteswap()
        return arr.tobytes()
    except Exception as exc:
        logger.warning("Failed to load earcon WAV %s: %s", path, exc)
        return None


class AudioPreprocessor:
    """
    Wyoming-Satellite style audio front-end:
    - Uses `webrtc_noise_gain.AudioProcessor` when installed.
    - Falls back to a pure-Python/NumPy 80Hz high-pass rumble filter, adaptive spectral noise gate
      (levels 0..4), and Automatic Gain Control (0..31 dBFS) with soft limiting.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        noise_suppression_level: int = 0,
        auto_gain_dbfs: int = 0,
    ) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.noise_suppression_level = max(0, min(4, int(noise_suppression_level)))
        self.auto_gain_dbfs = max(0, min(31, int(auto_gain_dbfs)))
        self._hp_prev_in: float = 0.0
        self._hp_prev_out: float = 0.0
        self._noise_floor_rms: float = 180.0
        self._current_gain: float = 1.0
        self._native_proc = None

        if (
            _WebRTCAudioProcessor is not None
            and self.sample_rate == 16000
            and (self.noise_suppression_level > 0 or self.auto_gain_dbfs > 0)
        ):
            try:
                self._native_proc = _WebRTCAudioProcessor(
                    self.auto_gain_dbfs,
                    self.noise_suppression_level,
                )
            except Exception:  # pragma: no cover
                self._native_proc = None

    @property
    def enabled(self) -> bool:
        return self.noise_suppression_level > 0 or self.auto_gain_dbfs > 0

    def process(self, pcm_bytes: bytes) -> bytes:
        if not self.enabled or len(pcm_bytes) < 2:
            return pcm_bytes

        usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if usable <= 0:
            return pcm_bytes

        # 1. Native webrtc-noise-gain path (10ms = 320B subframes at 16kHz)
        if self._native_proc is not None and usable % 320 == 0:  # pragma: no cover
            try:
                out = bytearray()
                for offset in range(0, usable, 320):
                    res = self._native_proc.Process10ms(pcm_bytes[offset : offset + 320])
                    out.extend(res.audio)
                return bytes(out)
            except Exception:
                pass

        # 2. Pure-Python / NumPy DSP fallback (80Hz High-Pass + NS Gate + AGC)
        arr = array("h")
        arr.frombytes(pcm_bytes[:usable])
        if sys.byteorder != "little":  # pragma: no cover
            arr.byteswap()

        n = len(arr)
        if n == 0:
            return pcm_bytes

        # First-order high-pass filter (~80Hz at 16kHz) removes DC offset & fan hum
        rc = 1.0 / (2.0 * math.pi * 80.0)
        dt = 1.0 / float(self.sample_rate)
        alpha = rc / (rc + dt)
        prev_x = self._hp_prev_in
        prev_y = self._hp_prev_out
        filtered = [0.0] * n
        sum_sq = 0.0
        peak = 0.0
        for i, x_int in enumerate(arr):
            x = float(x_int)
            y = alpha * (prev_y + x - prev_x)
            prev_x = x
            prev_y = y
            filtered[i] = y
            sum_sq += y * y
            ay = abs(y)
            if ay > peak:
                peak = ay
        self._hp_prev_in = prev_x
        self._hp_prev_out = prev_y

        rms = math.sqrt(sum_sq / n)

        # Noise Suppression (0..4): adaptive noise floor tracking + soft expander gate
        ns_gain = 1.0
        if self.noise_suppression_level > 0:
            if rms < self._noise_floor_rms * 1.4:
                self._noise_floor_rms = 0.90 * self._noise_floor_rms + 0.10 * max(20.0, rms)
            threshold = self._noise_floor_rms * (1.15 + 0.35 * self.noise_suppression_level)
            if rms < threshold and threshold > 1e-3:
                floor_atten = max(0.08, 1.0 - 0.21 * self.noise_suppression_level)
                ratio = rms / threshold
                ns_gain = floor_atten + (1.0 - floor_atten) * (ratio * ratio)

        # Automatic Gain Control (0..31 dBFS)
        target_gain = 1.0
        if self.auto_gain_dbfs > 0 and rms > 60.0:
            max_linear_gain = min(35.0, math.pow(10.0, self.auto_gain_dbfs / 20.0))
            desired_rms = 3200.0
            raw_gain = min(max_linear_gain, max(1.0, desired_rms / max(1.0, rms)))
            if peak * raw_gain > 30000.0 and peak > 1.0:
                raw_gain = min(raw_gain, 30000.0 / peak)
            # Smooth attack/release envelope
            smooth = 0.35 if raw_gain > self._current_gain else 0.65
            self._current_gain = smooth * self._current_gain + (1.0 - smooth) * raw_gain
            target_gain = self._current_gain

        total_gain = ns_gain * target_gain
        out_arr = array("h", (max(-32768, min(32767, int(v * total_gain))) for v in filtered))
        if sys.byteorder != "little":  # pragma: no cover
            out_arr.byteswap()
        return out_arr.tobytes()


class LocalWakeWordDetector:
    """
    Lightweight local wake-word detector for 'wake' mic mode:
    - Uses `wake_detector_fn` callback if supplied.
    - Uses `openwakeword` when installed on Raspberry Pi.
    - Provides a built-in energy-burst cadence fallback and manual `trigger()` for offline use.
    """

    def __init__(
        self,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
        detector_fn: Optional[WakeDetectorFn] = None,
    ) -> None:
        self.model_name = (model_name or "hey_jarvis").strip()
        self.threshold = max(0.05, min(0.99, float(threshold)))
        self._detector_fn = detector_fn
        self._oww_model = None
        self._manual_armed = False
        self._voiced_streak = 0

        if self._detector_fn is None and openwakeword is not None:  # pragma: no cover
            try:
                self._oww_model = openwakeword.Model(wakeword_models=[self.model_name])
            except Exception as exc:
                logger.warning("openwakeword init failed (%s); using cadence fallback", exc)
                self._oww_model = None

    def trigger(self) -> None:
        """Manually arm a wake detection for the next frame (or programmatic trigger)."""
        self._manual_armed = True

    def reset(self) -> None:
        self._manual_armed = False
        self._voiced_streak = 0

    def detect(self, pcm_20ms: bytes, has_voice: bool = False) -> bool:
        if self._manual_armed:
            self._manual_armed = False
            self._voiced_streak = 0
            return True

        if self._detector_fn is not None:
            try:
                return bool(self._detector_fn(pcm_20ms))
            except Exception:
                return False

        if self._oww_model is not None and np is not None:  # pragma: no cover
            try:
                samples = np.frombuffer(pcm_20ms, dtype=np.int16)
                preds = self._oww_model.predict(samples)
                if isinstance(preds, dict):
                    for score in preds.values():
                        if float(score) >= self.threshold:
                            return True
                return False
            except Exception:
                pass

        # Built-in fallback: sustained 3-frame voiced phrase onset (~60ms)
        if has_voice:
            self._voiced_streak += 1
            if self._voiced_streak >= 3:
                self._voiced_streak = 0
                return True
        else:
            self._voiced_streak = 0
        return False


class AudioSyncEngine:
    """Handles bidirectional 16kHz mono PCM audio and synchronized 20Hz mouth animation."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 100,
        vad_aggressiveness: int = 2,
        vad_silence_ms: int = 900,
        bt_latency_ms: int = 150,
        default_mic_mode: str = "vad",
        input_device: Optional[int | str] = None,
        output_device: Optional[int | str] = None,
        send_audio_frame: Optional[SendAudioFrameFn] = None,
        on_speech_state: Optional[SpeechStateFn] = None,
        on_viseme: Optional[VisemeCallback] = None,
        on_playback_done: Optional[PlaybackDoneCallback] = None,
        on_barge_in: Optional[BargeInCallback] = None,
        # Wyoming-Satellite inspired enhancements:
        vad_trigger_level: int = 1,
        max_turn_seconds: float = 30.0,
        noise_suppression_level: int = 0,
        auto_gain_dbfs: int = 0,
        earcons_enabled: bool = False,
        awake_wav: Optional[str | Path] = None,
        done_wav: Optional[str | Path] = None,
        error_wav: Optional[str | Path] = None,
        wake_word_model: str = "hey_jarvis",
        wake_threshold: float = 0.5,
        wake_refractory_ms: int = 2000,
        followup_window_ms: int = 0,
        wake_detector_fn: Optional[WakeDetectorFn] = None,
        on_wake_detected: Optional[WakeDetectedCallback] = None,
        stream_stall_timeout_s: float = 5.0,
        watchdog_interval_s: float = 0.0,
        on_stream_status: Optional[StreamStatusCallback] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.vad_silence_ms = vad_silence_ms
        self.bt_latency_ms = bt_latency_ms
        self.mic_mode = _normalize_mic_mode(default_mic_mode)
        self.input_device = input_device
        self.output_device = output_device

        self._send_audio_frame = send_audio_frame
        self._on_speech_state = on_speech_state
        self._on_viseme = on_viseme
        self._on_playback_done = on_playback_done
        self._on_barge_in = on_barge_in
        self._on_wake_detected = on_wake_detected
        self._on_stream_status = on_stream_status

        # Smart VAD & WebRTC DSP Front-End configuration
        self.vad_trigger_level = max(1, int(vad_trigger_level))
        self.max_turn_seconds = max(0.0, float(max_turn_seconds))
        self.preprocessor = AudioPreprocessor(
            sample_rate=self.sample_rate,
            noise_suppression_level=noise_suppression_level,
            auto_gain_dbfs=auto_gain_dbfs,
        )

        # Earcons configuration
        self.earcons_enabled = bool(earcons_enabled)
        self.awake_wav = awake_wav
        self.done_wav = done_wav
        self.error_wav = error_wav
        self._earcon_cache: Dict[str, bytes] = {}

        # Wake word & follow-up conversation window state
        self.wake_refractory_ms = max(0, int(wake_refractory_ms))
        self.followup_window_ms = max(0, int(followup_window_ms))
        self._wake_detector = LocalWakeWordDetector(
            model_name=wake_word_model,
            threshold=wake_threshold,
            detector_fn=wake_detector_fn,
        )
        self._last_wake_ts: float = -1e9
        self._followup_until_ts: float = 0.0

        # Bluetooth stream health watchdog
        self.stream_stall_timeout_s = max(0.0, float(stream_stall_timeout_s))
        self.watchdog_interval_s = max(0.0, float(watchdog_interval_s))
        self._last_mic_rx_ts: float = time.monotonic()
        self._mic_status_error_count: int = 0
        self._stream_recoveries: int = 0
        self._watchdog_task: Optional[asyncio.Task[None]] = None

        self.volume: float = 0.75
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

        # Capture state (protected by _tx_lock across PortAudio callback & asyncio threads)
        self._tx_lock = threading.Lock()
        self._in_stream = None
        self._capturing_turn = False
        self._ptt_active = False
        vad_level = max(0, min(3, int(vad_aggressiveness)))
        self._vad = webrtcvad.Vad(vad_level) if webrtcvad else None
        self._pre_roll: Deque[bytes] = collections.deque(maxlen=15)  # 15 * 20ms = 300ms
        self._vad_onset_count: int = 0
        self._tx_accum = bytearray()
        self._last_voice_ts = 0.0
        self._turn_start_ts = 0.0
        self._tx_chunk_bytes = max(2, int(self.sample_rate * 2 * (self.frame_ms / 1000.0)) & ~1)

        # Playback & viseme sync state
        self._out_stream = None
        self._play_queue: queue.Queue[bytes] = queue.Queue()
        self._play_remainder = bytearray()
        self._play_lock = threading.Lock()
        self._viseme_task: Optional[asyncio.Task[None]] = None
        self._viseme_gen: int = 0
        self._viseme_done_gen: int = -1
        self._tts_stream_ended: bool = False
        self._is_speaking = False

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    @property
    def is_capturing(self) -> bool:
        with self._tx_lock:
            return self._capturing_turn or self._ptt_active

    @property
    def is_in_followup_window(self) -> bool:
        return time.monotonic() < self._followup_until_ts

    @property
    def stream_recoveries(self) -> int:
        return self._stream_recoveries

    def arm_followup_window(self, now: Optional[float] = None) -> None:
        """Open the post-TTS follow-up conversation window so user can reply without wake word."""
        if self.followup_window_ms > 0:
            ts = time.monotonic() if now is None else now
            self._followup_until_ts = ts + (self.followup_window_ms / 1000.0)

    def clear_followup_window(self) -> None:
        self._followup_until_ts = 0.0

    def get_earcon_pcm(self, kind: str) -> bytes:
        """Return cached or freshly loaded/synthesized 16kHz PCM bytes for an earcon."""
        norm = kind.strip().lower()
        if norm in self._earcon_cache:
            return self._earcon_cache[norm]
        wav_path = None
        if norm == "awake":
            wav_path = self.awake_wav
        elif norm == "done":
            wav_path = self.done_wav
        elif norm == "error":
            wav_path = self.error_wav
        pcm = load_wav_pcm16_mono(wav_path, self.sample_rate) if wav_path else None
        if not pcm:
            pcm = synthesize_earcon_pcm(norm, self.sample_rate)
        self._earcon_cache[norm] = pcm
        return pcm

    def play_earcon(self, kind: str) -> bytes:
        """Enqueue a local earcon ('awake', 'done', 'error') for immediate speaker playback."""
        pcm = self.get_earcon_pcm(kind)
        if pcm:
            self._play_queue.put(pcm)
        return pcm

    def set_mic_mode(self, mode: str) -> None:
        norm = _normalize_mic_mode(mode)
        finish_turn = False
        with self._tx_lock:
            self.mic_mode = norm
            self._vad_onset_count = 0
            self._wake_detector.reset()
            if self._capturing_turn:
                self._capturing_turn = False
                self._ptt_active = False
                self._pre_roll.clear()
                finish_turn = True
        logger.info("Mic mode set to: %s", norm)
        if finish_turn and self._loop and not self._loop.is_closed() and self._loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is self._loop:
                self._loop.create_task(self._finish_vad_turn())
            else:
                asyncio.run_coroutine_threadsafe(self._finish_vad_turn(), self._loop)

    def set_volume(self, vol_percent: int) -> None:
        self.volume = max(0.0, min(1.0, vol_percent / 100.0))

    def _open_portaudio_streams(self) -> bool:
        """Open (or re-open) PortAudio input and output streams."""
        if sd is None:
            return False
        vad_block = int(self.sample_rate * 0.02)
        in_ok = False
        out_ok = False
        try:
            self._in_stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=vad_block,
                channels=1,
                dtype="int16",
                device=self.input_device,
                callback=self._on_mic_audio,
            )
            self._in_stream.start()
            in_ok = True
            logger.info("Bluetooth mic capture opened (%d Hz, 20ms blocks)", self.sample_rate)
        except Exception as exc:
            logger.error("Failed to open input audio device: %s", exc)
            self._in_stream = None

        try:
            self._out_stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                blocksize=int(self.sample_rate * 0.05),
                channels=1,
                dtype="int16",
                device=self.output_device,
                callback=self._on_speaker_audio,
            )
            self._out_stream.start()
            out_ok = True
            logger.info("Bluetooth speaker output opened (%d Hz)", self.sample_rate)
        except Exception as exc:
            logger.error("Failed to open output audio device: %s", exc)
            self._out_stream = None

        self._last_mic_rx_ts = time.monotonic()
        return in_ok and out_ok

    def _close_portaudio_streams(self) -> None:
        if self._in_stream:
            try:
                self._in_stream.stop()
                self._in_stream.close()
            except Exception:
                pass
            finally:
                self._in_stream = None
        if self._out_stream:
            try:
                self._out_stream.stop()
                self._out_stream.close()
            except Exception:
                pass
            finally:
                self._out_stream = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._last_mic_rx_ts = time.monotonic()
        if sd is None:
            logger.warning("sounddevice not installed; running in headless audio stub mode.")
            return

        self._open_portaudio_streams()
        if self.watchdog_interval_s > 0 and (self._watchdog_task is None or self._watchdog_task.done()):
            self._watchdog_task = asyncio.create_task(self._run_watchdog_loop())

    async def stop(self) -> None:
        self._running = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        self._watchdog_task = None
        self.stop_playback()
        self._close_portaudio_streams()

    async def _run_watchdog_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self.watchdog_interval_s)
                if not self._running:
                    break
                await self.check_and_heal_streams()
        except asyncio.CancelledError:
            pass

    async def check_and_heal_streams(self, now: Optional[float] = None) -> bool:
        """
        Inspect input/output PortAudio streams for stalls or BT disconnects and re-initialize if unhealthy.
        Returns True if a stream recovery attempt was triggered.
        """
        if not self._running or sd is None:
            return False

        ts = time.monotonic() if now is None else now
        in_s = self._in_stream
        out_s = self._out_stream

        missing = in_s is None or out_s is None
        closed = bool(getattr(in_s, "closed", False)) or bool(getattr(out_s, "closed", False))
        inactive = (
            (in_s is not None and getattr(in_s, "active", True) is False and getattr(in_s, "started", True) is False)
            or (out_s is not None and getattr(out_s, "active", True) is False and getattr(out_s, "started", True) is False)
        )
        stalled = self.stream_stall_timeout_s > 0 and (ts - self._last_mic_rx_ts) >= self.stream_stall_timeout_s

        if not (missing or closed or inactive or stalled):
            return False

        logger.warning(
            "Audio stream watchdog triggered (missing=%s, closed=%s, inactive=%s, stalled=%s); healing streams...",
            missing,
            closed,
            inactive,
            stalled,
        )
        self._close_portaudio_streams()
        ok = self._open_portaudio_streams()
        self._last_mic_rx_ts = ts
        if ok:
            self._stream_recoveries += 1
        if self._on_stream_status:
            try:
                await self._on_stream_status(ok)
            except Exception:
                pass
        return True

    async def toggle_ptt(self) -> bool:
        """Toggle Push-To-Talk recording; returns True if now listening."""
        now = time.monotonic()
        with self._tx_lock:
            self._ptt_active = not self._ptt_active
            active = self._ptt_active
            if active:
                self._capturing_turn = True
                self._last_voice_ts = now
                self._turn_start_ts = now
                self._vad_onset_count = 0
                self._pre_roll.clear()
                self._tx_accum.clear()
                ended_turn = False
            else:
                ended_turn = self._capturing_turn
                self._capturing_turn = False
                self._vad_onset_count = 0
                self._pre_roll.clear()

        if active:
            self.stop_playback()
            if self.earcons_enabled:
                self.play_earcon("awake")
            if self._on_speech_state:
                await self._on_speech_state(True)
        else:
            if ended_turn:
                if self.earcons_enabled:
                    self.play_earcon("done")
                await self._flush_tx_accum()
                if self._on_speech_state:
                    await self._on_speech_state(False)
        return active

    def _is_voice_frame(self, pcm_20ms: bytes) -> bool:
        if self._vad is not None:
            try:
                return self._vad.is_speech(pcm_20ms, self.sample_rate)
            except Exception:
                pass
        usable = len(pcm_20ms) - (len(pcm_20ms) % 2)
        if usable <= 0:
            return False
        mv = memoryview(pcm_20ms)[:usable]
        # RMS energy fallback when webrtcvad isn't available
        if np is not None:
            samples = np.frombuffer(mv, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        else:
            arr = array("h")
            arr.frombytes(mv)
            if sys.byteorder != "little":
                arr.byteswap()
            rms = math.sqrt(sum(s * s for s in arr) / len(arr)) if arr else 0.0
        return rms > 600.0

    def _dispatch_frames(self, frames_to_send: List[bytes]) -> None:
        loop = self._loop
        if not loop or loop.is_closed() or not self._send_audio_frame:
            return
        for frame in frames_to_send:
            asyncio.run_coroutine_threadsafe(self._send_audio_frame(frame), loop)

    def _on_mic_audio(self, indata: bytes, frames: int, time_info: object, status: object) -> None:
        if not self._running:
            return
        now = time.monotonic()
        self._last_mic_rx_ts = now
        if status:
            self._mic_status_error_count += 1

        raw_chunk = bytes(indata)
        chunk = self.preprocessor.process(raw_chunk) if self.preprocessor.enabled else raw_chunk

        with self._tx_lock:
            mode = self.mic_mode
            speaking = self._is_speaking
            if self._ptt_active:
                if self._capturing_turn:
                    self._tx_accum.extend(chunk)
                    ptt_frames: List[bytes] = []
                    while len(self._tx_accum) >= self._tx_chunk_bytes:
                        ptt_frames.append(bytes(self._tx_accum[: self._tx_chunk_bytes]))
                        del self._tx_accum[: self._tx_chunk_bytes]
                else:
                    ptt_frames = []
                ptt_handled = True
            elif speaking and mode != "duplex":
                return
            else:
                ptt_handled = False
                ptt_frames = []

        if ptt_handled:
            if ptt_frames:
                self._dispatch_frames(ptt_frames)
            return

        if mode == "ptt":
            return

        # Auto VAD / Full-Duplex VAD / Wake-Word mode
        has_voice = self._is_voice_frame(chunk)
        frames_to_send: List[bytes] = []
        notify_speech_start = False
        notify_wake = False
        trigger_barge_in = False
        trigger_finish = False

        with self._tx_lock:
            if not self._running:
                return
            mode = self.mic_mode
            speaking = self._is_speaking
            if self._ptt_active:
                if self._capturing_turn:
                    self._tx_accum.extend(chunk)
                    while len(self._tx_accum) >= self._tx_chunk_bytes:
                        frames_to_send.append(bytes(self._tx_accum[: self._tx_chunk_bytes]))
                        del self._tx_accum[: self._tx_chunk_bytes]
            elif mode == "ptt" or (speaking and mode != "duplex"):
                return
            elif not self._capturing_turn:
                self._pre_roll.append(chunk)
                should_start_turn = False

                if mode == "wake" and now >= self._followup_until_ts:
                    # Wake-word gated mode (outside post-TTS follow-up window)
                    in_refractory = (now - self._last_wake_ts) * 1000.0 < self.wake_refractory_ms
                    if not in_refractory and self._wake_detector.detect(chunk, has_voice=has_voice):
                        self._last_wake_ts = now
                        should_start_turn = True
                        notify_wake = True
                else:
                    # Standard VAD, Duplex VAD, or Wake mode inside active follow-up window
                    if has_voice:
                        self._vad_onset_count += 1
                        if self._vad_onset_count >= self.vad_trigger_level:
                            should_start_turn = True
                    else:
                        self._vad_onset_count = 0

                if should_start_turn:
                    self._capturing_turn = True
                    self._vad_onset_count = 0
                    self._followup_until_ts = 0.0
                    self._last_voice_ts = now
                    self._turn_start_ts = now
                    self._tx_accum.clear()
                    for pre in self._pre_roll:
                        self._tx_accum.extend(pre)
                    self._pre_roll.clear()
                    while len(self._tx_accum) >= self._tx_chunk_bytes:
                        frames_to_send.append(bytes(self._tx_accum[: self._tx_chunk_bytes]))
                        del self._tx_accum[: self._tx_chunk_bytes]
                    notify_speech_start = True
                    if speaking and mode == "duplex":
                        trigger_barge_in = True
            else:
                self._tx_accum.extend(chunk)
                while len(self._tx_accum) >= self._tx_chunk_bytes:
                    frames_to_send.append(bytes(self._tx_accum[: self._tx_chunk_bytes]))
                    del self._tx_accum[: self._tx_chunk_bytes]
                if has_voice:
                    self._last_voice_ts = now

                silence_exceeded = (not has_voice) and (
                    (now - self._last_voice_ts) * 1000.0 >= self.vad_silence_ms
                )
                max_turn_exceeded = (
                    self.max_turn_seconds > 0.0
                    and (now - self._turn_start_ts) >= self.max_turn_seconds
                )
                if silence_exceeded or max_turn_exceeded:
                    self._capturing_turn = False
                    self._vad_onset_count = 0
                    trigger_finish = True

        loop = self._loop
        if trigger_barge_in and loop and not loop.is_closed():
            loop.call_soon_threadsafe(self.stop_playback)
            if self._on_barge_in:
                asyncio.run_coroutine_threadsafe(self._on_barge_in(), loop)
        if notify_speech_start:
            if self.earcons_enabled:
                self.play_earcon("awake")
            if notify_wake and loop and not loop.is_closed() and self._on_wake_detected:
                asyncio.run_coroutine_threadsafe(self._on_wake_detected(), loop)
            if loop and not loop.is_closed() and self._on_speech_state:
                asyncio.run_coroutine_threadsafe(self._on_speech_state(True), loop)
        if frames_to_send:
            self._dispatch_frames(frames_to_send)
        if trigger_finish and loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._finish_vad_turn(), loop)

    def _queue_tx_bytes(self, pcm_bytes: bytes) -> None:
        frames_to_send: List[bytes] = []
        with self._tx_lock:
            self._tx_accum.extend(pcm_bytes)
            while len(self._tx_accum) >= self._tx_chunk_bytes:
                frames_to_send.append(bytes(self._tx_accum[: self._tx_chunk_bytes]))
                del self._tx_accum[: self._tx_chunk_bytes]
        if frames_to_send:
            self._dispatch_frames(frames_to_send)

    async def _flush_tx_accum(self) -> None:
        with self._tx_lock:
            pending = bytes(self._tx_accum)
            self._tx_accum.clear()
        if pending and self._send_audio_frame:
            await self._send_audio_frame(pending)

    async def _finish_vad_turn(self) -> None:
        if self.earcons_enabled:
            self.play_earcon("done")
        await self._flush_tx_accum()
        if self._on_speech_state:
            await self._on_speech_state(False)

    def _on_speaker_audio(self, outdata: bytearray, frames: int, time_info: object, status: object) -> None:
        needed_bytes = frames * 2
        vol = self.volume
        with self._play_lock:
            while len(self._play_remainder) < needed_bytes:
                try:
                    nxt = self._play_queue.get_nowait()
                    self._play_remainder.extend(nxt)
                except queue.Empty:
                    break

            avail = min(needed_bytes, len(self._play_remainder))
            avail -= avail % 2
            if avail > 0:
                raw = bytes(self._play_remainder[:avail])
                del self._play_remainder[:avail]
                if vol < 0.99:
                    if np is not None:
                        arr = np.clip(
                            np.frombuffer(raw, dtype=np.int16).astype(np.float32) * vol,
                            -32768,
                            32767,
                        ).astype(np.int16)
                        raw = arr.tobytes()
                    else:
                        pcm_arr = array("h")
                        pcm_arr.frombytes(raw)
                        if sys.byteorder != "little":
                            pcm_arr.byteswap()
                        for i, s in enumerate(pcm_arr):
                            pcm_arr[i] = max(-32768, min(32767, int(s * vol)))
                        if sys.byteorder != "little":
                            pcm_arr.byteswap()
                        raw = pcm_arr.tobytes()
                outdata[:avail] = raw
                if avail < needed_bytes:
                    outdata[avail:needed_bytes] = b"\x00" * (needed_bytes - avail)
            else:
                outdata[:needed_bytes] = b"\x00" * needed_bytes

    def feed_tts_audio(self, pcm_bytes: bytes) -> None:
        """Push incoming Spixi VoIP TTS audio frames to the Bluetooth speaker queue."""
        if pcm_bytes:
            self._play_queue.put(pcm_bytes)

    def _cancel_task(self, task: asyncio.Task[None]) -> None:
        if not task.done():
            task.cancel()

    async def _emit_mouth_zero_if_gen(self, gen: int) -> None:
        if self._viseme_gen == gen and self._on_viseme:
            try:
                await self._on_viseme(0)
            except Exception:
                pass

    def stop_playback(self) -> None:
        """Interrupt active TTS audio playback and stop viseme animation."""
        self._viseme_gen += 1
        stop_gen = self._viseme_gen
        self._tts_stream_ended = True
        with self._play_lock:
            while not self._play_queue.empty():
                try:
                    self._play_queue.get_nowait()
                except queue.Empty:
                    break
            self._play_remainder.clear()

        task = self._viseme_task
        self._viseme_task = None
        self._is_speaking = False

        if task and not task.done():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if self._loop and not self._loop.is_closed() and self._loop.is_running() and running_loop is not self._loop:
                self._loop.call_soon_threadsafe(self._cancel_task, task)
            else:
                task.cancel()

        if self._on_viseme:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is not None and running_loop.is_running():
                running_loop.create_task(self._emit_mouth_zero_if_gen(stop_gen))
            elif self._loop and not self._loop.is_closed() and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._emit_mouth_zero_if_gen(stop_gen), self._loop)

    def start_viseme_pacemaker(self, envelope: List[int]) -> None:
        """Start 20Hz mouth animation synchronized with BT speaker latency."""
        self._viseme_gen += 1
        gen = self._viseme_gen
        self._tts_stream_ended = False
        if self._viseme_task and not self._viseme_task.done():
            self._viseme_task.cancel()
        self._is_speaking = True
        with self._tx_lock:
            if not self._capturing_turn:
                self._pre_roll.clear()
        self._viseme_task = asyncio.create_task(self._run_viseme_loop(envelope, gen))

    async def _run_viseme_loop(self, envelope: List[int], gen: int) -> None:
        cancelled = False
        try:
            if self.bt_latency_ms > 0:
                await asyncio.sleep(self.bt_latency_ms / 1000.0)

            for val in envelope:
                if self._viseme_gen != gen or not self._is_speaking:
                    break
                if self._on_viseme:
                    await self._on_viseme(val)
                await asyncio.sleep(0.05)  # 20Hz pacemaker
        except asyncio.CancelledError:
            cancelled = True
        finally:
            if self._viseme_gen == gen:
                self._is_speaking = False
                if self._on_viseme:
                    try:
                        await self._on_viseme(0)
                    except Exception:
                        pass
                if not cancelled:
                    self._viseme_done_gen = gen
                    self.arm_followup_window()
                    if self._on_playback_done:
                        try:
                            await self._on_playback_done()
                        except Exception:
                            pass

    async def finish_tts_stream(self, interrupted: bool = False) -> None:
        """Handle tts_end cleanly without truncating buffered audio/visemes on normal end."""
        if interrupted:
            self.stop_playback()
            self._viseme_done_gen = self._viseme_gen
            if self._on_viseme:
                try:
                    await self._on_viseme(0)
                except Exception:
                    pass
            if self._on_playback_done:
                try:
                    await self._on_playback_done()
                except Exception:
                    pass
            return

        self._tts_stream_ended = True
        if self._viseme_task is None or self._viseme_task.done():
            self._is_speaking = False
            if self._viseme_done_gen == self._viseme_gen:
                return
            self._viseme_done_gen = self._viseme_gen
            self.arm_followup_window()
            if self._on_viseme:
                try:
                    await self._on_viseme(0)
                except Exception:
                    pass
            if self._on_playback_done:
                try:
                    await self._on_playback_done()
                except Exception:
                    pass

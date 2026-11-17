"""
Bluetooth Mic Capture (VAD / PTT) + Speaker Playback + 20Hz Viseme Lip-Sync Pacemaker
for Raspberry Pi Zero 2 W.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import queue
import threading
import time
from typing import Awaitable, Callable, Deque, List, Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None

try:
    import webrtcvad
except ImportError:  # pragma: no cover
    webrtcvad = None

logger = logging.getLogger(__name__)

SendAudioFrameFn = Callable[[bytes], Awaitable[bool]]
SpeechStateFn = Callable[[bool], Awaitable[None]]
VisemeCallback = Callable[[int], Awaitable[None]]
PlaybackDoneCallback = Callable[[], Awaitable[None]]


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
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.vad_silence_ms = vad_silence_ms
        self.bt_latency_ms = bt_latency_ms
        self.mic_mode = default_mic_mode
        self.input_device = input_device
        self.output_device = output_device

        self._send_audio_frame = send_audio_frame
        self._on_speech_state = on_speech_state
        self._on_viseme = on_viseme
        self._on_playback_done = on_playback_done

        self.volume: float = 0.75
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

        # Capture state
        self._in_stream = None
        self._capturing_turn = False
        self._ptt_active = False
        self._vad = webrtcvad.Vad(vad_aggressiveness) if webrtcvad else None
        self._pre_roll: Deque[bytes] = collections.deque(maxlen=15)  # 15 * 20ms = 300ms
        self._tx_accum = bytearray()
        self._last_voice_ts = 0.0
        self._tx_chunk_bytes = int(self.sample_rate * 2 * (self.frame_ms / 1000.0))

        # Playback & viseme sync state
        self._out_stream = None
        self._play_queue: queue.Queue[bytes] = queue.Queue()
        self._play_remainder = bytearray()
        self._play_lock = threading.Lock()
        self._viseme_task: Optional[asyncio.Task[None]] = None
        self._is_speaking = False

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    def set_mic_mode(self, mode: str) -> None:
        self.mic_mode = "ptt" if mode.lower() == "ptt" else "vad"
        logger.info("Mic mode set to: %s", self.mic_mode)

    def set_volume(self, vol_percent: int) -> None:
        self.volume = max(0.0, min(1.0, vol_percent / 100.0))

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        if sd is None:
            logger.warning("sounddevice not installed; running in headless audio stub mode.")
            return

        # Open 20ms-frame input stream (320 samples @ 16kHz) for WebRTC VAD compatibility
        vad_block = int(self.sample_rate * 0.02)
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
            logger.info("Bluetooth mic capture opened (%d Hz, 20ms blocks)", self.sample_rate)
        except Exception as exc:
            logger.error("Failed to open input audio device: %s", exc)

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
            logger.info("Bluetooth speaker output opened (%d Hz)", self.sample_rate)
        except Exception as exc:
            logger.error("Failed to open output audio device: %s", exc)

    async def stop(self) -> None:
        self._running = False
        self.stop_playback()
        if self._in_stream:
            self._in_stream.stop()
            self._in_stream.close()
            self._in_stream = None
        if self._out_stream:
            self._out_stream.stop()
            self._out_stream.close()
            self._out_stream = None

    async def toggle_ptt(self) -> bool:
        """Toggle Push-To-Talk recording; returns True if now listening."""
        self._ptt_active = not self._ptt_active
        if self._ptt_active:
            self.stop_playback()
            self._capturing_turn = True
            self._tx_accum.clear()
            if self._on_speech_state:
                await self._on_speech_state(True)
        else:
            if self._capturing_turn:
                self._capturing_turn = False
                await self._flush_tx_accum()
                if self._on_speech_state:
                    await self._on_speech_state(False)
        return self._ptt_active

    def _is_voice_frame(self, pcm_20ms: bytes) -> bool:
        if self._vad is not None:
            try:
                return self._vad.is_speech(pcm_20ms, self.sample_rate)
            except Exception:
                pass
        # RMS energy fallback when webrtcvad isn't available
        samples = np.frombuffer(pcm_20ms, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        return rms > 600.0

    def _on_mic_audio(self, indata: bytes, frames: int, time_info: object, status: object) -> None:
        if not self._running or self._is_speaking:
            return
        chunk = bytes(indata)
        now = time.monotonic()

        if self.mic_mode == "ptt":
            if self._ptt_active and self._capturing_turn:
                self._queue_tx_bytes(chunk)
            return

        # Auto VAD mode
        has_voice = self._is_voice_frame(chunk)
        if not self._capturing_turn:
            self._pre_roll.append(chunk)
            if has_voice:
                self._capturing_turn = True
                self._last_voice_ts = now
                self._tx_accum.clear()
                for pre in self._pre_roll:
                    self._queue_tx_bytes(pre)
                self._pre_roll.clear()
                if self._loop and self._on_speech_state:
                    asyncio.run_coroutine_threadsafe(self._on_speech_state(True), self._loop)
        else:
            self._queue_tx_bytes(chunk)
            if has_voice:
                self._last_voice_ts = now
            elif (now - self._last_voice_ts) * 1000.0 >= self.vad_silence_ms:
                self._capturing_turn = False
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._finish_vad_turn(), self._loop)

    def _queue_tx_bytes(self, pcm_bytes: bytes) -> None:
        self._tx_accum.extend(pcm_bytes)
        while len(self._tx_accum) >= self._tx_chunk_bytes:
            frame = bytes(self._tx_accum[: self._tx_chunk_bytes])
            del self._tx_accum[: self._tx_chunk_bytes]
            if self._loop and self._send_audio_frame:
                asyncio.run_coroutine_threadsafe(self._send_audio_frame(frame), self._loop)

    async def _flush_tx_accum(self) -> None:
        if self._tx_accum and self._send_audio_frame:
            await self._send_audio_frame(bytes(self._tx_accum))
            self._tx_accum.clear()

    async def _finish_vad_turn(self) -> None:
        await self._flush_tx_accum()
        if self._on_speech_state:
            await self._on_speech_state(False)

    def _on_speaker_audio(self, outdata: bytearray, frames: int, time_info: object, status: object) -> None:
        needed_bytes = frames * 2
        with self._play_lock:
            while len(self._play_remainder) < needed_bytes:
                try:
                    nxt = self._play_queue.get_nowait()
                    self._play_remainder.extend(nxt)
                except queue.Empty:
                    break

            avail = min(needed_bytes, len(self._play_remainder))
            if avail > 0:
                raw = bytes(self._play_remainder[:avail])
                del self._play_remainder[:avail]
                if self.volume < 0.99:
                    arr = (np.frombuffer(raw, dtype=np.int16).astype(np.float32) * self.volume).astype(np.int16)
                    raw = arr.tobytes()
                outdata[:avail] = raw
                if avail < needed_bytes:
                    outdata[avail:needed_bytes] = b"\x00" * (needed_bytes - avail)
            else:
                outdata[:needed_bytes] = b"\x00" * needed_bytes

    def feed_tts_audio(self, pcm_bytes: bytes) -> None:
        """Push incoming Spixi VoIP TTS audio frames to the Bluetooth speaker queue."""
        if pcm_bytes:
            self._play_queue.put(pcm_bytes)

    def stop_playback(self) -> None:
        """Interrupt active TTS audio playback and stop viseme animation."""
        with self._play_lock:
            while not self._play_queue.empty():
                try:
                    self._play_queue.get_nowait()
                except queue.Empty:
                    break
            self._play_remainder.clear()
        if self._viseme_task and not self._viseme_task.done():
            self._viseme_task.cancel()
        self._is_speaking = False

    def start_viseme_pacemaker(self, envelope: List[int]) -> None:
        """Start 20Hz mouth animation synchronized with BT speaker latency."""
        if self._viseme_task and not self._viseme_task.done():
            self._viseme_task.cancel()
        self._is_speaking = True
        self._viseme_task = asyncio.create_task(self._run_viseme_loop(envelope))

    async def _run_viseme_loop(self, envelope: List[int]) -> None:
        try:
            if self.bt_latency_ms > 0:
                await asyncio.sleep(self.bt_latency_ms / 1000.0)

            for val in envelope:
                if not self._is_speaking:
                    break
                if self._on_viseme:
                    await self._on_viseme(val)
                await asyncio.sleep(0.05)  # 20Hz pacemaker
        except asyncio.CancelledError:
            pass
        finally:
            self._is_speaking = False
            if self._on_viseme:
                await self._on_viseme(0)
            if self._on_playback_done:
                await self._on_playback_done()

"""
Bluetooth Mic Capture (VAD / PTT) + Speaker Playback + 20Hz Viseme Lip-Sync Pacemaker
for Raspberry Pi Zero 2 W.
"""

from __future__ import annotations

import asyncio
from array import array
import collections
import logging
import math
import queue
import sys
import threading
import time
from typing import Awaitable, Callable, Deque, List, Optional

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

logger = logging.getLogger(__name__)

SendAudioFrameFn = Callable[[bytes], Awaitable[bool]]
SpeechStateFn = Callable[[bool], Awaitable[None]]
VisemeCallback = Callable[[int], Awaitable[None]]
PlaybackDoneCallback = Callable[[], Awaitable[None]]
BargeInCallback = Callable[[], Awaitable[None]]

_VALID_MIC_MODES = frozenset({"vad", "ptt", "duplex"})


def _normalize_mic_mode(mode: str) -> str:
    norm = (mode or "vad").strip().lower()
    return norm if norm in _VALID_MIC_MODES else "vad"


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
        self._tx_accum = bytearray()
        self._last_voice_ts = 0.0
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

    def set_mic_mode(self, mode: str) -> None:
        norm = _normalize_mic_mode(mode)
        finish_turn = False
        with self._tx_lock:
            self.mic_mode = norm
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

    async def toggle_ptt(self) -> bool:
        """Toggle Push-To-Talk recording; returns True if now listening."""
        with self._tx_lock:
            self._ptt_active = not self._ptt_active
            active = self._ptt_active
            if active:
                self._capturing_turn = True
                self._last_voice_ts = time.monotonic()
                self._pre_roll.clear()
                self._tx_accum.clear()
                ended_turn = False
            else:
                ended_turn = self._capturing_turn
                self._capturing_turn = False
                self._pre_roll.clear()

        if active:
            self.stop_playback()
            if self._on_speech_state:
                await self._on_speech_state(True)
        else:
            if ended_turn:
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
        chunk = bytes(indata)
        now = time.monotonic()

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

        # Auto VAD / Full-Duplex VAD mode
        has_voice = self._is_voice_frame(chunk)
        frames_to_send: List[bytes] = []
        notify_speech_start = False
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
                if has_voice:
                    self._capturing_turn = True
                    self._last_voice_ts = now
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
                elif (now - self._last_voice_ts) * 1000.0 >= self.vad_silence_ms:
                    self._capturing_turn = False
                    trigger_finish = True

        loop = self._loop
        if trigger_barge_in and loop and not loop.is_closed():
            loop.call_soon_threadsafe(self.stop_playback)
            if self._on_barge_in:
                asyncio.run_coroutine_threadsafe(self._on_barge_in(), loop)
        if notify_speech_start and loop and not loop.is_closed() and self._on_speech_state:
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

"""OpenAI-compatible STT / TTS client and 20Hz viseme envelope generator."""

from __future__ import annotations

import asyncio
from array import array
import inspect
import io
import logging
import math
import sys
import wave
from typing import Any, Iterator, List, Optional, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def _pcm16_bytes_to_array(pcm_bytes: bytes) -> array:
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    arr = array("h")
    if usable > 0:
        arr.frombytes(pcm_bytes[:usable])
        if sys.byteorder != "little":
            arr.byteswap()
    return arr


def _array_to_pcm16_bytes(arr: array) -> bytes:
    if sys.byteorder != "little":
        out = array("h", arr)
        out.byteswap()
        return out.tobytes()
    return arr.tobytes()


def _downmix_pcm16_to_mono(pcm_bytes: bytes, channels: int) -> bytes:
    if channels <= 1:
        usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
        return pcm_bytes[:usable]
    usable = len(pcm_bytes) - (len(pcm_bytes) % (2 * channels))
    if usable <= 0:
        return b""
    if np is not None:
        samples = np.frombuffer(pcm_bytes[:usable], dtype="<i2").astype(np.float32)
        frames = samples.reshape(-1, channels)
        mono = np.mean(frames, axis=1)
        return np.clip(np.round(mono), -32768, 32767).astype("<i2").tobytes()

    arr = _pcm16_bytes_to_array(pcm_bytes[:usable])
    mono_arr = array("h")
    for i in range(0, len(arr), channels):
        total = sum(arr[i : i + channels])
        avg = int(round(total / float(channels)))
        if avg < -32768:
            avg = -32768
        elif avg > 32767:
            avg = 32767
        mono_arr.append(avg)
    return _array_to_pcm16_bytes(mono_arr)


async def _read_binary_response(resp: Any) -> bytes:
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)
    if hasattr(resp, "aread") and callable(resp.aread):
        data = await resp.aread()
        return bytes(data)
    if hasattr(resp, "read") and callable(resp.read):
        data = resp.read()
        if inspect.isawaitable(data):
            data = await data
        return bytes(data)
    if hasattr(resp, "content"):
        return bytes(resp.content)
    return b""


def pcm16_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw signed 16-bit little-endian PCM bytes in an in-memory WAV container."""
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive, got {sample_rate}")
    if channels <= 0:
        raise ValueError(f"channels must be positive, got {channels}")
    usable = len(pcm_bytes) - (len(pcm_bytes) % (2 * channels))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes[:usable])
    return buf.getvalue()


def resample_pcm16(pcm_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linearly resample 16-bit signed mono PCM between sample rates."""
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"Sample rates must be positive (src={src_rate}, dst={dst_rate})")

    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return b""
    pcm_clean = pcm_bytes[:usable]
    if src_rate == dst_rate:
        return pcm_clean

    if np is not None:
        samples = np.frombuffer(pcm_clean, dtype="<i2").astype(np.float32)
        if samples.size == 0:
            return b""
        duration = samples.size / float(src_rate)
        dst_len = max(1, int(round(duration * dst_rate)))
        src_x = np.linspace(0.0, 1.0, samples.size, endpoint=False)
        dst_x = np.linspace(0.0, 1.0, dst_len, endpoint=False)
        resampled = np.interp(dst_x, src_x, samples)
        return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()

    src_arr = _pcm16_bytes_to_array(pcm_clean)
    n_src = len(src_arr)
    if n_src == 0:
        return b""
    duration = n_src / float(src_rate)
    dst_len = max(1, int(round(duration * dst_rate)))
    dst_arr = array("h")
    if n_src == 1:
        val = src_arr[0]
        dst_arr.extend([val] * dst_len)
        return _array_to_pcm16_bytes(dst_arr)

    for j in range(dst_len):
        pos = (j * n_src) / float(dst_len)
        if pos >= n_src - 1:
            sample_val = src_arr[-1]
        else:
            idx = int(pos)
            frac = pos - idx
            interp = src_arr[idx] + frac * (src_arr[idx + 1] - src_arr[idx])
            sample_val = int(interp)
        if sample_val < -32768:
            sample_val = -32768
        elif sample_val > 32767:
            sample_val = 32767
        dst_arr.append(sample_val)
    return _array_to_pcm16_bytes(dst_arr)


def compute_viseme_envelope(pcm_bytes: bytes, sample_rate: int = 16000, fps: int = 20) -> List[int]:
    """Compute normalized 0..100 mouth-open RMS amplitude values at `fps` Hz."""
    if sample_rate <= 0 or fps <= 0:
        raise ValueError(f"sample_rate and fps must be positive (rate={sample_rate}, fps={fps})")

    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return []
    pcm_clean = pcm_bytes[:usable]
    step = max(1, sample_rate // fps)

    rms_vals: List[float] = []
    if np is not None:
        samples = np.frombuffer(pcm_clean, dtype="<i2").astype(np.float32) / 32768.0
        for i in range(0, samples.size, step):
            chunk = samples[i : i + step]
            rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
            rms_vals.append(rms)
    else:
        arr = _pcm16_bytes_to_array(pcm_clean)
        n = len(arr)
        for i in range(0, n, step):
            chunk = arr[i : i + step]
            if chunk:
                sq_sum = sum((s / 32768.0) ** 2 for s in chunk)
                rms = math.sqrt(sq_sum / len(chunk))
            else:
                rms = 0.0
            rms_vals.append(rms)

    if not rms_vals:
        return []

    peak = max(max(rms_vals), 0.08)
    envelopes = [min(100, max(0, int((r / peak) * 100.0))) for r in rms_vals]
    return envelopes


def split_audio_frames(pcm_bytes: bytes, sample_rate: int = 16000, frame_ms: int = 100) -> List[bytes]:
    """Split 16-bit mono PCM into fixed-duration frames for Spixi VoIP appData streaming."""
    bytes_per_frame = int(sample_rate * 2 * (frame_ms / 1000.0))
    bytes_per_frame -= bytes_per_frame % 2
    if bytes_per_frame <= 0:
        bytes_per_frame = 3200
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    pcm_clean = pcm_bytes[:usable]
    return [pcm_clean[i : i + bytes_per_frame] for i in range(0, len(pcm_clean), bytes_per_frame)]


def iter_audio_frames(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    frame_ms: int = 100,
    cancel_event: Optional[asyncio.Event] = None,
) -> Iterator[bytes]:
    """Yield fixed-duration PCM16 frames, stopping early if `cancel_event` is set."""
    for frame in split_audio_frames(pcm_bytes, sample_rate=sample_rate, frame_ms=frame_ms):
        if cancel_event is not None and cancel_event.is_set():
            break
        yield frame


class SpeechProcessor:
    """Handles OpenAI-compatible STT transcription and TTS synthesis on the AI Server."""

    def __init__(
        self,
        stt_base_url: str,
        stt_api_key: str,
        stt_model: str,
        tts_base_url: str,
        tts_api_key: str,
        tts_model: str,
        tts_voice: str,
        stt_client: Any = None,
        tts_client: Any = None,
    ) -> None:
        if stt_client is not None:
            self.stt_client = stt_client
        else:
            if AsyncOpenAI is None:
                raise RuntimeError("openai package is required when stt_client is not provided")
            self.stt_client = AsyncOpenAI(base_url=stt_base_url, api_key=stt_api_key)
        self.stt_model = stt_model

        if tts_client is not None:
            self.tts_client = tts_client
        else:
            if AsyncOpenAI is None:
                raise RuntimeError("openai package is required when tts_client is not provided")
            self.tts_client = AsyncOpenAI(base_url=tts_base_url, api_key=tts_api_key)
        self.tts_model = tts_model
        self.tts_voice = tts_voice

    async def transcribe_pcm(
        self,
        pcm_bytes: bytes,
        sample_rate: int = 16000,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> str:
        """Send captured user audio to OpenAI-compatible /v1/audio/transcriptions."""
        if cancel_event is not None and cancel_event.is_set():
            return ""
        if sample_rate <= 0:
            return ""

        usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
        min_bytes = max(2, int(sample_rate * 2 * 0.05))
        if usable < min_bytes:  # < 50ms
            return ""

        wav_bytes = pcm16_to_wav_bytes(pcm_bytes[:usable], sample_rate=sample_rate, channels=1)
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = "speech.wav"

        try:
            resp = await self.stt_client.audio.transcriptions.create(
                model=self.stt_model,
                file=audio_file,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("STT transcription failed: %s", exc)
            return ""

        if cancel_event is not None and cancel_event.is_set():
            return ""
        if isinstance(resp, str):
            return resp.strip()
        return (getattr(resp, "text", None) or "").strip()

    async def synthesize_pcm_16k(
        self,
        text: str,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Tuple[bytes, List[int]]:
        """
        Synthesize speech via OpenAI-compatible /v1/audio/speech and normalize
        to 16kHz 16-bit mono PCM + 20Hz viseme amplitude envelope.
        """
        if not text or not text.strip():
            return b"", []
        if cancel_event is not None and cancel_event.is_set():
            return b"", []

        try:
            resp = await self.tts_client.audio.speech.create(
                model=self.tts_model,
                voice=self.tts_voice,
                input=text,
                response_format="pcm",
            )
            raw_bytes = await _read_binary_response(resp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TTS synthesis failed: %s", exc)
            return b"", []

        if cancel_event is not None and cancel_event.is_set():
            return b"", []
        if not raw_bytes:
            return b"", []

        # Handle custom OpenAI-compatible TTS servers that return WAV even when pcm is requested
        src_rate = 24000  # OpenAI TTS raw PCM default is 24kHz mono 16-bit
        if raw_bytes[:4] == b"RIFF":
            try:
                with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
                    if wf.getsampwidth() != 2:
                        logger.warning("Unsupported WAV sample width: %d", wf.getsampwidth())
                        return b"", []
                    src_rate = wf.getframerate()
                    n_channels = wf.getnchannels()
                    pcm_raw = wf.readframes(wf.getnframes())
                    if n_channels > 1:
                        pcm_raw = _downmix_pcm16_to_mono(pcm_raw, n_channels)
            except (wave.Error, EOFError, ValueError) as exc:
                logger.warning("Invalid WAV payload from TTS: %s", exc)
                return b"", []
        else:
            pcm_raw = raw_bytes

        if src_rate <= 0:
            return b"", []

        pcm_16k = resample_pcm16(pcm_raw, src_rate=src_rate, dst_rate=16000)
        envelope = compute_viseme_envelope(pcm_16k, sample_rate=16000, fps=20)
        return pcm_16k, envelope

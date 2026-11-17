"""OpenAI-compatible STT / TTS client and 20Hz viseme envelope generator."""

from __future__ import annotations

import io
import logging
import wave
from typing import List, Tuple

import numpy as np
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def pcm16_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw signed 16-bit little-endian PCM bytes in an in-memory WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def resample_pcm16(pcm_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linearly resample 16-bit signed mono PCM between sample rates."""
    if src_rate == dst_rate or not pcm_bytes:
        return pcm_bytes

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return b""

    duration = samples.size / float(src_rate)
    dst_len = max(1, int(round(duration * dst_rate)))
    src_x = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    dst_x = np.linspace(0.0, 1.0, dst_len, endpoint=False)
    resampled = np.interp(dst_x, src_x, samples)
    return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()


def compute_viseme_envelope(pcm_bytes: bytes, sample_rate: int = 16000, fps: int = 20) -> List[int]:
    """Compute normalized 0..100 mouth-open RMS amplitude values at `fps` Hz."""
    if not pcm_bytes:
        return []

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    step = max(1, sample_rate // fps)
    rms_vals: List[float] = []

    for i in range(0, samples.size, step):
        chunk = samples[i : i + step]
        rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
        rms_vals.append(rms)

    if not rms_vals:
        return []

    peak = max(max(rms_vals), 0.08)
    envelopes = [min(100, max(0, int((r / peak) * 100.0))) for r in rms_vals]
    return envelopes


def split_audio_frames(pcm_bytes: bytes, sample_rate: int = 16000, frame_ms: int = 100) -> List[bytes]:
    """Split 16-bit mono PCM into fixed-duration frames for Spixi VoIP appData streaming."""
    bytes_per_frame = int(sample_rate * 2 * (frame_ms / 1000.0))
    if bytes_per_frame <= 0:
        bytes_per_frame = 3200
    return [pcm_bytes[i : i + bytes_per_frame] for i in range(0, len(pcm_bytes), bytes_per_frame)]


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
    ) -> None:
        self.stt_client = AsyncOpenAI(base_url=stt_base_url, api_key=stt_api_key)
        self.stt_model = stt_model

        self.tts_client = AsyncOpenAI(base_url=tts_base_url, api_key=tts_api_key)
        self.tts_model = tts_model
        self.tts_voice = tts_voice

    async def transcribe_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """Send captured user audio to OpenAI-compatible /v1/audio/transcriptions."""
        if len(pcm_bytes) < 1600:  # < 50ms
            return ""

        wav_bytes = pcm16_to_wav_bytes(pcm_bytes, sample_rate=sample_rate, channels=1)
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = "speech.wav"

        resp = await self.stt_client.audio.transcriptions.create(
            model=self.stt_model,
            file=audio_file,
        )
        return (resp.text or "").strip()

    async def synthesize_pcm_16k(self, text: str) -> Tuple[bytes, List[int]]:
        """
        Synthesize speech via OpenAI-compatible /v1/audio/speech and normalize
        to 16kHz 16-bit mono PCM + 20Hz viseme amplitude envelope.
        """
        if not text.strip():
            return b"", []

        resp = await self.tts_client.audio.speech.create(
            model=self.tts_model,
            voice=self.tts_voice,
            input=text,
            response_format="pcm",
        )
        raw_bytes = resp.read()

        # Handle custom OpenAI-compatible TTS servers that return WAV even when pcm is requested
        src_rate = 24000  # OpenAI TTS raw PCM default is 24kHz mono 16-bit
        if raw_bytes[:4] == b"RIFF":
            with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
                src_rate = wf.getframerate()
                pcm_raw = wf.readframes(wf.getnframes())
        else:
            pcm_raw = raw_bytes

        pcm_16k = resample_pcm16(pcm_raw, src_rate=src_rate, dst_rate=16000)
        envelope = compute_viseme_envelope(pcm_16k, sample_rate=16000, fps=20)
        return pcm_16k, envelope

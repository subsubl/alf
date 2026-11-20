"""
ESP32-S3 Full-Screen Image & Video Streamer (Tier 2):
- Converts PNG / JPEG / WebP / GIF / PPM / raw RGB888 / raw RGB565 into 120x120 RGB565 frames
  (zoomed 2x to 240x240 full round LCD on the ESP32-S3).
- Streams live video (HTTP MJPEG, animated GIF, or MP4/RTSP/HTTP via `ffmpeg`) over the
  921,600-baud USB serial bridge (`EspSerialBridge.send_image_rgb565`).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from pathlib import Path
import re
import shutil
import struct
import time
from typing import Any, Awaitable, Callable, Dict, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

try:
    from PIL import Image  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

logger = logging.getLogger("pi_hub.media")

DEFAULT_MEDIA_W = 120
DEFAULT_MEDIA_H = 120
MAX_MEDIA_BYTES = 10 * 1024 * 1024


def rgb888_to_rgb565(
    rgb_bytes: bytes,
    src_w: int,
    src_h: int,
    dst_w: int = DEFAULT_MEDIA_W,
    dst_h: int = DEFAULT_MEDIA_H,
) -> bytes:
    """Center-crop to square, resize (nearest-neighbor), and pack little-endian RGB565."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        raise ValueError("Invalid image dimensions")
    if len(rgb_bytes) < src_w * src_h * 3:
        raise ValueError("RGB888 buffer smaller than src_w * src_h * 3")

    side = min(src_w, src_h)
    x0 = (src_w - side) // 2
    y0 = (src_h - side) // 2

    out = bytearray(dst_w * dst_h * 2)
    pos = 0
    for dy in range(dst_h):
        sy = y0 + (dy * side) // dst_h
        row_off = sy * src_w * 3
        for dx in range(dst_w):
            sx = x0 + (dx * side) // dst_w
            idx = row_off + sx * 3
            r = rgb_bytes[idx]
            g = rgb_bytes[idx + 1]
            b = rgb_bytes[idx + 2]
            val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            struct.pack_into("<H", out, pos, val)
            pos += 2
    return bytes(out)


def _parse_ppm_p6(raw_bytes: bytes) -> Optional[tuple[int, int, bytes]]:
    """Minimal pure-Python binary PPM (P6) parser."""
    if not raw_bytes.startswith(b"P6"):
        return None
    m = re.match(
        rb"P6\s+(?:#[^\n]*\n\s*)*(\d+)\s+(?:#[^\n]*\n\s*)*(\d+)\s+(?:#[^\n]*\n\s*)*(\d+)\s",
        raw_bytes,
    )
    if not m:
        return None
    w = int(m.group(1))
    h = int(m.group(2))
    maxval = int(m.group(3))
    if maxval != 255 or w <= 0 or h <= 0:
        return None
    pixels = raw_bytes[m.end() :]
    if len(pixels) < w * h * 3:
        return None
    return w, h, pixels[: w * h * 3]


def decode_image_to_rgb565(
    raw_bytes: bytes,
    target_w: int = DEFAULT_MEDIA_W,
    target_h: int = DEFAULT_MEDIA_H,
) -> bytes:
    """Decode raw RGB565, PPM (P6), or PNG/JPEG/GIF/WebP (via Pillow) into RGB565."""
    expected_rgb565 = target_w * target_h * 2
    if len(raw_bytes) == expected_rgb565 and not raw_bytes.startswith(
        (b"\x89PNG", b"\xff\xd8", b"GIF8", b"P6", b"RIFF")
    ):
        return raw_bytes

    ppm = _parse_ppm_p6(raw_bytes)
    if ppm is not None:
        pw, ph, rgb = ppm
        return rgb888_to_rgb565(rgb, pw, ph, target_w, target_h)

    if Image is not None:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            rgb_img = img.convert("RGB")
            sw, sh = rgb_img.size
            side = min(sw, sh)
            left = (sw - side) // 2
            top = (sh - side) // 2
            cropped = rgb_img.crop((left, top, left + side, top + side))
            resized = cropped.resize((target_w, target_h))
            return rgb888_to_rgb565(resized.tobytes(), target_w, target_h, target_w, target_h)

    raise ValueError(
        "Unsupported image format (install Pillow for PNG/JPEG, or provide PPM P6 / raw 120x120 RGB565)"
    )


async def fetch_media_bytes(source: str, max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
    """Load media bytes from data URI, local path, or HTTP(S) URL."""
    src = (source or "").strip()
    if not src:
        raise ValueError("Empty media source")

    if src.startswith("data:"):
        _, _, b64_part = src.partition(",")
        return base64.b64decode(b64_part)

    if src.startswith(("http://", "https://")):
        if aiohttp is None:
            raise RuntimeError("aiohttp is required to fetch HTTP/HTTPS media URLs")
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(src) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status} fetching media")
                data = await resp.content.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"Media exceeds {max_bytes} byte limit")
                return data

    p = Path(src)
    if not p.is_file():
        raise FileNotFoundError(f"Media file not found: {src}")
    data = p.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"Media exceeds {max_bytes} byte limit")
    return data


class EspMediaStreamer:
    """Coordinates full-screen image display and async video streaming to the ESP32-S3 LCD."""

    def __init__(
        self,
        esp_bridge: Any,
        fetch_fn: Optional[Callable[[str], Awaitable[bytes]]] = None,
    ) -> None:
        self.esp = esp_bridge
        self._fetch_fn = fetch_fn or fetch_media_bytes
        self._stream_task: Optional[asyncio.Task[None]] = None
        self.is_streaming: bool = False
        self.frames_sent: int = 0

    async def stop_stream(self, clear_lcd: bool = False) -> Dict[str, Any]:
        task = self._stream_task
        self._stream_task = None
        self.is_streaming = False
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if clear_lcd:
            return await self.esp.clear_media(wait_res=True)
        return {"ok": 1, "stopped": True}

    async def display_image(
        self,
        image_url: Optional[str] = None,
        image_b64: Optional[str] = None,
        media_path: Optional[str] = None,
        width: int = DEFAULT_MEDIA_W,
        height: int = DEFAULT_MEDIA_H,
    ) -> Dict[str, Any]:
        await self.stop_stream(clear_lcd=False)

        if image_b64:
            b64 = str(image_b64).strip()
            if b64.startswith("data:") and "," in b64:
                _, _, b64 = b64.partition(",")
            raw = base64.b64decode(b64)
        elif image_url:
            raw = await self._fetch_fn(str(image_url).strip())
        elif media_path:
            raw = await self._fetch_fn(str(media_path).strip())
        else:
            return {"ok": False, "error": "Provide 'image_url', 'image_b64', or 'media_path'"}

        rgb565 = decode_image_to_rgb565(raw, target_w=width, target_h=height)
        res = await self.esp.send_image_rgb565(rgb565, width=width, height=height, wait_res=True)
        if res.get("ok") == 1:
            self.frames_sent += 1
        return {"ok": res.get("ok") == 1, "width": width, "height": height, "esp_response": res}

    async def start_video_stream(
        self,
        video_url: str,
        fps: int = 5,
        duration_s: float = 10.0,
        width: int = DEFAULT_MEDIA_W,
        height: int = DEFAULT_MEDIA_H,
    ) -> Dict[str, Any]:
        src = (video_url or "").strip()
        if not src:
            return {"ok": False, "error": "Missing 'video_url'"}

        await self.stop_stream(clear_lcd=False)
        fps_clamped = max(1, min(10, int(fps)))
        dur_clamped = max(0.5, min(60.0, float(duration_s)))

        self.is_streaming = True
        self._stream_task = asyncio.create_task(
            self._run_video_loop(src, fps_clamped, dur_clamped, width, height)
        )
        return {
            "ok": True,
            "streaming": True,
            "video_url": src,
            "fps": fps_clamped,
            "duration_s": dur_clamped,
            "resolution": f"{width}x{height}",
        }

    async def _run_video_loop(
        self,
        source: str,
        fps: int,
        duration_s: float,
        width: int,
        height: int,
    ) -> None:
        deadline = time.monotonic() + duration_s
        interval = 1.0 / float(fps)
        frame_bytes = width * height * 2

        try:
            # 1. Animated GIF (local or fetched)
            if source.lower().endswith(".gif") and Image is not None:
                raw = await self._fetch_fn(source)
                with Image.open(io.BytesIO(raw)) as gif:
                    idx = 0
                    while time.monotonic() < deadline and self.is_streaming:
                        try:
                            gif.seek(idx)
                        except EOFError:
                            idx = 0
                            gif.seek(0)
                        rgb_img = gif.convert("RGB").resize((width, height))
                        pkt = rgb888_to_rgb565(rgb_img.tobytes(), width, height, width, height)
                        await self.esp.send_image_rgb565(pkt, width=width, height=height, wait_res=True)
                        self.frames_sent += 1
                        idx += 1
                        await asyncio.sleep(interval)
                return

            # 2. HTTP MJPEG multipart stream
            if aiohttp is not None and source.startswith(("http://", "https://")) and "mjpeg" in source.lower():
                timeout = aiohttp.ClientTimeout(total=None, sock_read=5.0)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(source) as resp:
                        buf = bytearray()
                        last_sent = 0.0
                        async for chunk in resp.content.iter_chunked(8192):
                            if time.monotonic() >= deadline or not self.is_streaming:
                                break
                            buf.extend(chunk)
                            while True:
                                soi = buf.find(b"\xff\xd8")
                                if soi < 0:
                                    if len(buf) > 65536:
                                        del buf[:-2]
                                    break
                                eoi = buf.find(b"\xff\xd9", soi + 2)
                                if eoi < 0:
                                    break
                                jpg = bytes(buf[soi : eoi + 2])
                                del buf[: eoi + 2]
                                now = time.monotonic()
                                if now - last_sent >= interval:
                                    last_sent = now
                                    pkt = decode_image_to_rgb565(jpg, target_w=width, target_h=height)
                                    await self.esp.send_image_rgb565(pkt, width=width, height=height, wait_res=True)
                                    self.frames_sent += 1
                return

            # 3. Generic MP4 / RTSP / HTTP video via ffmpeg -> raw rgb565le pipe
            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                logger.warning("ffmpeg not installed; cannot stream video source '%s'", source)
                return

            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source,
                "-t",
                f"{duration_s:.2f}",
                "-vf",
                vf,
                "-r",
                str(fps),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb565le",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                assert proc.stdout is not None
                while time.monotonic() < deadline and self.is_streaming:
                    raw_frame = await proc.stdout.readexactly(frame_bytes)
                    await self.esp.send_image_rgb565(raw_frame, width=width, height=height, wait_res=True)
                    self.frames_sent += 1
            except asyncio.IncompleteReadError:
                pass
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except Exception:
                        proc.kill()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ESP32 video stream error (%s): %s", source, exc)
        finally:
            self.is_streaming = False

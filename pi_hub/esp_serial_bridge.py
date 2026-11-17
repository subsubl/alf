"""Async USB Serial NDJSON bridge between Raspberry Pi Zero 2 W and ESP32-S3."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

import serial

logger = logging.getLogger(__name__)

EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class EspSerialBridge:
    """Thread-backed async NDJSON serial client with auto-reconnect and RPC correlation."""

    def __init__(self, port: str = "/dev/ttyACM0", baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._ser: Optional[serial.Serial] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tx_lock = threading.Lock()
        self._pending: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._event_handler: Optional[EventHandler] = None

    def set_event_handler(self, handler: EventHandler) -> None:
        self._event_handler = handler

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._thread = threading.Thread(target=self._io_loop, daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._running = False
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def _io_loop(self) -> None:
        while self._running:
            try:
                with serial.Serial(self.port, self.baudrate, timeout=0.2) as ser:
                    self._ser = ser
                    logger.info("Connected to ESP32-S3 on %s @ %d", self.port, self.baudrate)
                    while self._running:
                        raw = ser.readline()
                        if not raw:
                            continue
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if self._loop:
                            asyncio.run_coroutine_threadsafe(self._handle_msg(msg), self._loop)
            except Exception as exc:
                self._ser = None
                logger.warning("ESP32 serial connection lost (%s); retrying in 2s...", exc)
                time.sleep(2.0)

    async def _handle_msg(self, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("t")
        if msg_type == "res":
            req_id = str(msg.get("id", ""))
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif msg_type == "evt" and self._event_handler:
            await self._event_handler(msg)

    async def send(self, payload: Dict[str, Any]) -> None:
        if not self._ser or not self._ser.is_open:
            return
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._tx_lock:
            if self._ser and self._ser.is_open:
                self._ser.write(line)
                self._ser.flush()

    async def request(self, payload: Dict[str, Any], timeout: float = 3.0) -> Dict[str, Any]:
        if not self._loop:
            return {"ok": 0, "error": "Serial bridge not started"}

        req_id = str(payload.get("id") or uuid.uuid4().hex[:8])
        payload["id"] = req_id
        fut: asyncio.Future[Dict[str, Any]] = self._loop.create_future()
        self._pending[req_id] = fut

        await self.send(payload)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"ok": 0, "error": "ESP32 serial timeout"}

    async def set_state(self, emo: str, sub: Optional[str] = None, mic: Optional[int] = None) -> None:
        pkt: Dict[str, Any] = {"t": "state", "emo": emo}
        if sub is not None:
            pkt["sub"] = sub[:180]
        if mic is not None:
            pkt["mic"] = int(mic)
        await self.send(pkt)

    async def set_sync(self, emo: Optional[str], sub: Optional[str], mouth: int) -> None:
        pkt: Dict[str, Any] = {"t": "sync", "mouth": int(mouth)}
        if emo:
            pkt["emo"] = emo
        if sub is not None:
            pkt["sub"] = sub[:180]
        await self.send(pkt)

    async def set_mouth(self, mouth: int) -> None:
        await self.send({"t": "mouth", "v": int(mouth)})

    async def set_debug(
        self,
        quixi: Optional[str] = None,
        voip: Optional[str] = None,
        srv_ms: Optional[int] = None,
        bt_spk: Optional[str] = None,
    ) -> None:
        pkt: Dict[str, Any] = {"t": "dbg"}
        if quixi is not None:
            pkt["quixi"] = quixi
        if voip is not None:
            pkt["voip"] = voip
        if srv_ms is not None:
            pkt["srv_ms"] = int(srv_ms)
        if bt_spk is not None:
            pkt["bt_spk"] = bt_spk
        await self.send(pkt)

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

ESP_RX_LINE_MAX = 512

EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class EspSerialBridge:
    """Thread-backed async NDJSON serial client with auto-reconnect and RPC correlation."""

    def __init__(self, port: str = "/dev/ttyACM0", baudrate: int = 921600) -> None:
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
        if self._running and self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._io_loop, name="esp-serial-io", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._running = False
        ser = self._ser
        self._ser = None
        if ser and getattr(ser, "is_open", False):
            try:
                ser.close()
            except Exception:
                pass
        for req_id in list(self._pending.keys()):
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result({"ok": 0, "error": "Serial bridge stopped"})
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    async def pause_for_flash(self) -> bool:
        """Temporarily release the serial port so esptool can flash the ESP32-S3 over USB."""
        was_running = self._running
        if was_running:
            await self.stop()
        return was_running

    async def resume_after_flash(self, should_restart: bool = True) -> None:
        """Re-acquire the serial port at 921600 baud after esptool completes."""
        if should_restart and not self._running:
            await self.start()
            await asyncio.sleep(0.05)

    async def hardware_reset(self) -> Dict[str, Any]:
        """Pulse CH343 DTR/RTS to reboot the ESP32-S3 and verify post-boot ping."""
        with self._tx_lock:
            ser = self._ser
            if ser and getattr(ser, "is_open", False):
                try:
                    ser.dtr = False
                    ser.rts = True
                    time.sleep(0.08)
                    ser.rts = False
                except Exception as exc:
                    logger.warning("DTR/RTS pulse failed: %s", exc)
        await asyncio.sleep(0.15)
        res = await self.request({"t": "ping"}, timeout=2.5)
        return {"ok": res.get("ok") == 1, "action": "reset", "ping": res}

    def _io_loop(self) -> None:
        while self._running:
            try:
                with serial.Serial(self.port, self.baudrate, timeout=0.2) as ser:
                    self._ser = ser
                    if hasattr(ser, "reset_input_buffer"):
                        try:
                            ser.reset_input_buffer()
                        except Exception:
                            pass
                    logger.info("Connected to ESP32-S3 on %s @ %d", self.port, self.baudrate)
                    while self._running and self._ser is ser and getattr(ser, "is_open", False):
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
                        if not isinstance(msg, dict):
                            continue
                        if self._loop and not self._loop.is_closed():
                            asyncio.run_coroutine_threadsafe(self._handle_msg(msg), self._loop)
            except Exception as exc:
                logger.warning("ESP32 serial connection lost (%s); retrying in 2s...", exc)
            finally:
                self._ser = None
            if self._running:
                for _ in range(20):
                    if not self._running:
                        break
                    time.sleep(0.1)

    async def _handle_msg(self, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("t")
        if msg_type == "res":
            req_id = msg.get("id")
            if req_id is not None:
                fut = self._pending.pop(str(req_id), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        elif msg_type == "evt" and self._event_handler:
            try:
                await self._event_handler(msg)
            except Exception:
                logger.exception("ESP event handler failed")

    async def send(self, payload: Dict[str, Any]) -> bool:
        if not self._ser or not getattr(self._ser, "is_open", False):
            return False

        pkt = dict(payload)
        line = (json.dumps(pkt, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
            "utf-8", errors="replace"
        )
        if len(line) >= ESP_RX_LINE_MAX and isinstance(pkt.get("sub"), str):
            sub_val = pkt["sub"]
            while sub_val and len(line) >= ESP_RX_LINE_MAX:
                excess = len(line) - (ESP_RX_LINE_MAX - 1)
                trim = max(1, excess // 4)
                sub_val = sub_val[:-trim]
                pkt["sub"] = sub_val
                line = (json.dumps(pkt, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
                    "utf-8", errors="replace"
                )

        if len(line) >= ESP_RX_LINE_MAX:
            logger.warning("Dropping oversized ESP32 serial frame (%d bytes >= %d)", len(line), ESP_RX_LINE_MAX)
            return False

        with self._tx_lock:
            ser = self._ser
            if not ser or not getattr(ser, "is_open", False):
                return False
            try:
                ser.write(line)
                ser.flush()
                return True
            except Exception as exc:
                logger.warning("ESP32 serial write failed: %s", exc)
                self._ser = None
                try:
                    ser.close()
                except Exception:
                    pass
                return False

    async def request(self, payload: Dict[str, Any], timeout: float = 3.0) -> Dict[str, Any]:
        if not self._loop or not self._running:
            return {"ok": 0, "error": "Serial bridge not started"}

        pkt = dict(payload)
        req_id = str(pkt.get("id") or uuid.uuid4().hex[:8])
        pkt["id"] = req_id
        fut: asyncio.Future[Dict[str, Any]] = self._loop.create_future()
        self._pending[req_id] = fut

        try:
            sent = await self.send(pkt)
            if not sent:
                return {"ok": 0, "error": "ESP32 serial not connected"}
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": 0, "error": "ESP32 serial timeout"}
        finally:
            self._pending.pop(req_id, None)

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
        ip: Optional[str] = None,
        ixi: Optional[str] = None,
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
        if ip is not None:
            pkt["ip"] = str(ip)[:47]
        if ixi is not None:
            pkt["ixi"] = str(ixi)[:95]
        await self.send(pkt)

    async def set_net_info(
        self,
        ip: Optional[str] = None,
        ixi: Optional[str] = None,
        wait_res: bool = False,
        timeout: float = 3.0,
    ) -> Dict[str, Any]:
        """Push Raspberry Pi DHCP IP address and QuIXI Ixian wallet address to the ESP32-S3 Network screen."""
        pkt: Dict[str, Any] = {"t": "net"}
        if ip is not None:
            pkt["ip"] = str(ip)[:47]
        if ixi is not None:
            pkt["ixi"] = str(ixi)[:95]
        if len(pkt) == 1:
            return {"ok": 1}
        if wait_res:
            return await self.request(pkt, timeout=timeout)
        ok = await self.send(pkt)
        return {"ok": 1 if ok else 0}

    async def set_cfg(
        self,
        bright: Optional[int] = None,
        vol: Optional[int] = None,
        mode: Optional[str] = None,
        ble_en: Optional[bool | int] = None,
        sleep_s: Optional[int] = None,
        wait_res: bool = True,
        timeout: float = 3.0,
    ) -> Dict[str, Any]:
        pkt: Dict[str, Any] = {"t": "cfg"}
        if bright is not None:
            pkt["bright"] = max(10, min(100, int(bright)))
        if vol is not None:
            pkt["vol"] = max(0, min(100, int(vol)))
        if mode is not None:
            pkt["mode"] = str(mode).strip().lower()
        if ble_en is not None:
            pkt["ble_en"] = 1 if ble_en else 0
        if sleep_s is not None:
            pkt["sleep_s"] = int(sleep_s)
        if len(pkt) == 1:
            return {"ok": 1}
        if wait_res:
            return await self.request(pkt, timeout=timeout)
        ok = await self.send(pkt)
        return {"ok": 1 if ok else 0}

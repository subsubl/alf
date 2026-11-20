"""
Raspberry Pi Zero 2 W Companion Hub Web GUI (Tier 2):
- Full QuIXI Account Creation, Wallet Management, Contact / Friend Request Manager,
  Encrypted Spixi Chat, VoIP Call Controls, and Universal QuIXI RPC Explorer.
- Live ESP32-S3 Round Face Mirror, 921600-Baud Serial Monitor, QMI8658 6-Axis IMU,
  32-Device BLE Radar, Allow-Listed GPIO Matrix + Vbat ADC, and Display Settings.
- Wyoming-Satellite Audio DSP, Wake Word, Earcon & Bluetooth Stream Watchdog Controls.

Zero external web framework dependencies (built on Python's native asyncio HTTP/1.1 + SSE + RFC 6455 WebSockets).
Zero-Trust compliant: holds zero AI provider API keys and masks walletPassword in all GET responses.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import struct
import time
import urllib.parse
import uuid
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from common_ixian import QuIXIError

logger = logging.getLogger("pi_hub.web_gui")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ALLOWLIST_PINS: List[int] = [15, 16, 17, 18, 21, 33, 34, 35, 36, 37]
VALID_EMOTIONS: Tuple[str, ...] = (
    "IDLE",
    "LISTENING",
    "THINKING",
    "SPEAKING",
    "HAPPY",
    "SAD",
    "SURPRISED",
    "SLEEPING",
    "ALERT",
)


class PiWebGUI:
    """
    Self-contained Web Control Center for the Raspberry Pi Zero 2 W Hub (Tier 2).
    """

    def __init__(
        self,
        hub: Any,
        host: str = "127.0.0.1",
        port: int = 8082,
        auth_token: Optional[str] = None,
        ixian_cfg_path: Optional[Path] = None,
    ) -> None:
        self.hub = hub
        self.host = (host or "127.0.0.1").strip() or "127.0.0.1"
        self.port = int(port)
        self.auth_token = (auth_token or "").strip()
        self.ixian_cfg_path = Path(
            ixian_cfg_path or os.getenv("QUIXI_CFG_PATH", "/opt/quixi/ixian.cfg")
        )

        self._server: Optional[asyncio.AbstractServer] = None
        self.bound_port: int = self.port

        # Live ESP32-S3 mirror & sensor telemetry cache
        self.face_state: Dict[str, Any] = {
            "emo": "IDLE",
            "sub": "Ready!",
            "mouth": 0,
            "updated_at": time.time(),
        }
        self.esp_telemetry: Dict[str, Any] = {
            "uptime_ms": 0,
            "last_ping_rtt_ms": 0,
            "imu": {},
            "ble": [],
            "vbat_mv": 0,
            "gpio_pins": {str(p): {"pin": p, "dir": "in", "pull": "none", "val": 0} for p in ALLOWLIST_PINS},
            "cfg": {"bright": 100, "vol": 75, "mode": "vad", "ble_en": 1, "sleep_s": 120},
        }
        self.esp_event_log: Deque[Dict[str, Any]] = collections.deque(maxlen=80)
        self.quixi_event_log: Deque[Dict[str, Any]] = collections.deque(maxlen=80)

        self._ws_clients: Set[asyncio.StreamWriter] = set()
        self._sse_queues: Set[asyncio.Queue[Dict[str, Any]]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> int:
        if self._server is not None:
            return self.bound_port
        self._server = await asyncio.start_server(
            self._handle_connection, host=self.host, port=self.port
        )
        sockets = getattr(self._server, "sockets", None) or []
        if sockets:
            sockname = sockets[0].getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                self.bound_port = int(sockname[1])
        logger.info("Pi Hub Web GUI listening on http://%s:%d", self.host, self.bound_port)
        return self.bound_port

    async def stop(self) -> None:
        for writer in list(self._ws_clients):
            try:
                writer.close()
            except Exception:
                pass
        self._ws_clients.clear()
        self._sse_queues.clear()

        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    # ------------------------------------------------------------------
    # Real-Time Event Broadcasts (ESP32 & QuIXI -> Browser)
    # ------------------------------------------------------------------

    def broadcast_event(self, event_type: str, data: Dict[str, Any]) -> None:
        pkt = {"type": event_type, "ts": time.time(), "data": data}
        for q in list(self._sse_queues):
            try:
                q.put_nowait(pkt)
            except Exception:
                pass

        if self._ws_clients:
            raw_json = json.dumps(pkt, default=str).encode("utf-8")
            frame = self._encode_ws_frame(raw_json, opcode=0x1)
            for writer in list(self._ws_clients):
                if writer.is_closing():
                    self._ws_clients.discard(writer)
                    continue
                try:
                    writer.write(frame)
                except Exception:
                    self._ws_clients.discard(writer)

    def update_face_state(
        self,
        emo: Optional[str] = None,
        sub: Optional[str] = None,
        mouth: Optional[int] = None,
    ) -> None:
        if emo:
            self.face_state["emo"] = str(emo).strip().upper()
        if sub is not None:
            self.face_state["sub"] = str(sub)[:180]
        if mouth is not None:
            self.face_state["mouth"] = max(0, min(100, int(mouth)))
        self.face_state["updated_at"] = time.time()
        self.broadcast_event("face_state", dict(self.face_state))

    def record_esp_event(self, evt: Dict[str, Any]) -> None:
        entry = {"ts": time.time(), "event": evt}
        self.esp_event_log.append(entry)
        src = evt.get("src")
        if src == "imu":
            self.esp_telemetry["imu"] = {**self.esp_telemetry.get("imu", {}), **evt}
        elif src == "ble" and isinstance(evt.get("ble"), list):
            self.esp_telemetry["ble"] = list(evt["ble"])
        elif src == "cfg":
            for k in ("bright", "vol", "mode", "ble_en", "sleep_s"):
                if k in evt:
                    self.esp_telemetry["cfg"][k] = evt[k]
        self.broadcast_event("esp_event", entry)

    def record_quixi_event(self, topic: str, payload: Dict[str, Any], summary: str = "") -> None:
        entry = {
            "id": uuid.uuid4().hex[:8],
            "ts": time.time(),
            "topic": topic,
            "sender": str(payload.get("sender", "")),
            "summary": summary,
            "payload": payload,
        }
        self.quixi_event_log.append(entry)
        self.broadcast_event("quixi_event", entry)

    # ------------------------------------------------------------------
    # QuIXI Account / ixian.cfg Provisioning Helper
    # ------------------------------------------------------------------

    def read_ixian_cfg_masked(self) -> Dict[str, Any]:
        """Read local ixian.cfg with walletPassword masked."""
        res: Dict[str, Any] = {
            "path": str(self.ixian_cfg_path),
            "exists": self.ixian_cfg_path.is_file(),
            "apiPort": 8001,
            "mqDriver": "mqtt",
            "mqHost": "127.0.0.1",
            "mqPort": 1883,
            "networkType": "mainnet",
            "walletPasswordSet": False,
            "walletPasswordMasked": "",
        }
        if not self.ixian_cfg_path.is_file():
            return res
        try:
            text = self.ixian_cfg_path.read_text(encoding="utf-8")
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = [p.strip() for p in line.split("=", 1)]
                if k == "apiPort" and v.isdigit():
                    res["apiPort"] = int(v)
                elif k == "mqDriver":
                    res["mqDriver"] = v
                elif k == "mqHost":
                    res["mqHost"] = v
                elif k == "mqPort" and v.isdigit():
                    res["mqPort"] = int(v)
                elif k == "networkType":
                    res["networkType"] = v
                elif k == "walletPassword":
                    is_placeholder = not v or "REPLACE" in v.upper()
                    res["walletPasswordSet"] = not is_placeholder
                    res["walletPasswordMasked"] = "***" if not is_placeholder else ""
        except Exception as exc:
            res["error"] = str(exc)
        return res

    def write_ixian_cfg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Provision or update ixian.cfg for QuIXI account creation with chmod 600."""
        api_port = int(params.get("apiPort", 8001))
        mq_host = str(params.get("mqHost", "127.0.0.1")).strip() or "127.0.0.1"
        mq_port = int(params.get("mqPort", 1883))
        net_type = str(params.get("networkType", "mainnet")).strip().lower()
        if net_type not in ("mainnet", "testnet", "regtest"):
            net_type = "mainnet"

        raw_pw = str(params.get("walletPassword", "")).strip()
        if not raw_pw or "***" in raw_pw:
            # Preserve existing password if present
            existing_pw = ""
            if self.ixian_cfg_path.is_file():
                for line in self.ixian_cfg_path.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("walletPassword") and "=" in line:
                        existing_pw = line.split("=", 1)[1].strip()
                        break
            raw_pw = existing_pw or "TamagciWalletPass_" + uuid.uuid4().hex[:12]

        content = (
            "# QuIXI Pi Hub Configuration (Managed by PiWebGUI)\n"
            f"apiPort = {api_port}\n"
            f"apiBinds = http://127.0.0.1:{api_port}/\n"
            "addPeer = 127.0.0.1\n"
            "mqDriver = mqtt\n"
            f"mqHost = {mq_host}\n"
            f"mqPort = {mq_port}\n"
            f"networkType = {net_type}\n"
            f"walletPassword = {raw_pw}\n"
        )
        self.ixian_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.ixian_cfg_path.write_text(content, encoding="utf-8")
        try:
            os.chmod(self.ixian_cfg_path, 0o600)
        except Exception:
            pass
        return self.read_ixian_cfg_masked()

    # ------------------------------------------------------------------
    # Authentication & Programmatic Request Dispatcher
    # ------------------------------------------------------------------

    def _is_request_authorized(self, headers: Dict[str, str], query_params: Dict[str, str]) -> bool:
        if not self.auth_token:
            return True
        auth_hdr = headers.get("authorization", "").strip()
        if auth_hdr.lower().startswith("bearer ") and auth_hdr[7:].strip() == self.auth_token:
            return True
        if headers.get("x-api-key", "").strip() == self.auth_token:
            return True
        if query_params.get("token", "").strip() == self.auth_token:
            return True
        return False

    def get_status_snapshot(self) -> Dict[str, Any]:
        audio = getattr(self.hub, "audio", None)
        voip = getattr(self.hub, "voip", None)
        quixi = getattr(self.hub, "quixi", None)
        esp = getattr(self.hub, "esp", None)
        return {
            "ok": True,
            "server_ixian_address": getattr(self.hub, "server_address", ""),
            "face": dict(self.face_state),
            "esp": {
                "port": getattr(esp, "port", "/dev/ttyACM0"),
                "baudrate": getattr(esp, "baudrate", 921600),
                "connected": bool(getattr(esp, "_ser", None) is not None),
                "telemetry": self.esp_telemetry,
                "recent_events": list(self.esp_event_log)[-25:],
            },
            "quixi": {
                "rest_url": getattr(quixi, "rest_url", "http://127.0.0.1:8001"),
                "mqtt_host": getattr(quixi, "mqtt_host", "127.0.0.1"),
                "mqtt_port": getattr(quixi, "mqtt_port", 1883),
                "cfg": self.read_ixian_cfg_masked(),
                "recent_events": list(self.quixi_event_log)[-25:],
            },
            "voip": {
                "in_call": bool(getattr(voip, "is_in_call", False)),
                "contact": getattr(voip, "current_contact", None),
                "codec": getattr(voip, "current_codec", None),
                "session_id": (
                    voip.current_session_id.hex()
                    if getattr(voip, "current_session_id", None)
                    else None
                ),
            },
            "audio": {
                "mic_mode": getattr(audio, "mic_mode", "vad"),
                "volume": int(round(getattr(audio, "volume", 0.75) * 100)),
                "is_speaking": bool(getattr(audio, "is_speaking", False)),
                "is_capturing": bool(getattr(audio, "is_capturing", False)),
                "in_followup_window": bool(getattr(audio, "is_in_followup_window", False)),
                "stream_recoveries": int(getattr(audio, "stream_recoveries", 0)),
                "vad_trigger_level": int(getattr(audio, "vad_trigger_level", 1)),
                "noise_suppression_level": int(
                    getattr(getattr(audio, "preprocessor", None), "noise_suppression_level", 0)
                ),
                "auto_gain_dbfs": int(
                    getattr(getattr(audio, "preprocessor", None), "auto_gain_dbfs", 0)
                ),
                "earcons_enabled": bool(getattr(audio, "earcons_enabled", False)),
            },
            "allowlist_pins": ALLOWLIST_PINS,
            "valid_emotions": list(VALID_EMOTIONS),
        }

    async def handle_api_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        parsed = urllib.parse.urlsplit(path)
        route = parsed.path.rstrip("/") or "/"
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if not self._is_request_authorized(hdrs, query):
            return 401, {"ok": False, "error": "Unauthorized: valid token required"}

        payload = body if isinstance(body, dict) else {}

        # --------------------------------------------------------------
        # 1. Overall Pi Hub Snapshot
        # --------------------------------------------------------------
        if method == "GET" and route == "/api/status":
            return 200, self.get_status_snapshot()

        # --------------------------------------------------------------
        # 2. QuIXI Account, Wallet, Contacts & RPC Endpoints
        # --------------------------------------------------------------
        if method == "GET" and route == "/api/quixi/overview":
            overview: Dict[str, Any] = {"ok": True, "cfg": self.read_ixian_cfg_masked()}
            for key, fn in (
                ("status", self.hub.quixi.status),
                ("wallet", self.hub.quixi.my_wallet),
                ("balance", self.hub.quixi.get_balance),
                ("nick", self.hub.quixi.get_nick),
                ("contacts", self.hub.quixi.contacts),
            ):
                try:
                    res = await fn()
                    overview[key] = res.get("result", res) if isinstance(res, dict) else res
                except Exception as exc:
                    overview[key] = {"error": str(exc)}
            return 200, overview

        if route == "/api/quixi/account/provision":
            if method == "GET":
                return 200, {"ok": True, "cfg": self.read_ixian_cfg_masked()}
            if method == "POST":
                cfg = self.write_ixian_cfg(payload)
                self.broadcast_event("quixi_cfg_updated", cfg)
                return 200, {"ok": True, "cfg": cfg}

        if method == "POST" and route == "/api/quixi/account/new_address":
            base_addr = str(payload.get("base_address") or "").strip() or None
            try:
                res = await self.hub.quixi.generate_new_address(base_addr)
                self.broadcast_event("quixi_new_address", res)
                return 200, {"ok": True, "address": res.get("result", res), "raw": res}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if route == "/api/quixi/account/nick":
            try:
                if method == "GET":
                    res = await self.hub.quixi.get_nick()
                    return 200, {"ok": True, "nick": res.get("result", res)}
                if method == "POST":
                    nick = str(payload.get("nick") or "").strip()
                    if not nick:
                        return 400, {"ok": False, "error": "Missing 'nick'"}
                    res = await self.hub.quixi.set_nick(nick)
                    return 200, {"ok": True, "nick": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "GET" and route == "/api/quixi/account/wallet":
            try:
                w = await self.hub.quixi.view_wallet()
                b = await self.hub.quixi.get_balance()
                return 200, {
                    "ok": True,
                    "wallet": w.get("result", w),
                    "balance": b.get("result", b),
                }
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "GET" and route == "/api/quixi/account/backup":
            try:
                res = await self.hub.quixi.backup_wallet()
                return 200, {"ok": True, "backup": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "GET" and route == "/api/quixi/contacts":
            search = query.get("search", "").strip() or None
            try:
                res = await self.hub.quixi.contacts(search=search)
                return 200, {"ok": True, "contacts": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and route == "/api/quixi/contacts/add":
            addr = str(payload.get("address") or "").strip()
            if not addr:
                return 400, {"ok": False, "error": "Missing 'address'"}
            try:
                res = await self.hub.quixi.add_contact(addr)
                self.broadcast_event("contact_added", {"address": addr, "result": res})
                return 200, {"ok": True, "address": addr, "result": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and route == "/api/quixi/contacts/accept":
            addr = str(payload.get("address") or "").strip()
            if not addr:
                return 400, {"ok": False, "error": "Missing 'address'"}
            try:
                res = await self.hub.quixi.accept_contact(addr)
                self.broadcast_event("contact_accepted", {"address": addr, "result": res})
                return 200, {"ok": True, "address": addr, "result": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and route == "/api/quixi/contacts/remove":
            addr = str(payload.get("address") or "").strip()
            if not addr:
                return 400, {"ok": False, "error": "Missing 'address'"}
            try:
                res = await self.hub.quixi.remove_contact(addr)
                self.broadcast_event("contact_removed", {"address": addr, "result": res})
                return 200, {"ok": True, "address": addr, "result": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and route == "/api/quixi/pair_server":
            addr = str(payload.get("server_ixian_address") or payload.get("address") or "").strip()
            if not addr:
                return 400, {"ok": False, "error": "Missing 'server_ixian_address'"}
            self.hub.server_address = addr
            steps: Dict[str, Any] = {"server_ixian_address": addr}
            try:
                steps["addContact"] = await self.hub.quixi.add_contact(addr)
            except Exception as exc:
                steps["addContact_error"] = str(exc)
            try:
                steps["acceptContact"] = await self.hub.quixi.accept_contact(addr)
            except Exception as exc:
                steps["acceptContact_error"] = str(exc)
            if payload.get("dial_voip", True):
                try:
                    await self.hub.voip.initiate_call(addr)
                    steps["voip_dialed"] = True
                except Exception as exc:
                    steps["voip_error"] = str(exc)
            self.broadcast_event("server_paired", steps)
            return 200, {"ok": True, **steps}

        if method == "POST" and route == "/api/quixi/chat":
            addr = str(payload.get("address") or self.hub.server_address or "").strip()
            message = str(payload.get("message") or "").strip()
            channel = int(payload.get("channel", 0))
            if not addr or not message:
                return 400, {"ok": False, "error": "Both 'address' and 'message' are required"}
            try:
                res = await self.hub.quixi.send_chat_message(addr, message, channel=channel)
                self.record_quixi_event("Chat/out", {"sender": "self", "recipient": addr, "message": message}, summary=message)
                return 200, {"ok": True, "result": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and route == "/api/quixi/app_data":
            addr = str(payload.get("address") or self.hub.server_address or "").strip()
            protocol_id = str(payload.get("protocol_id") or "com.tamagci.agent").strip()
            data_val = payload.get("data", "")
            data_str = json.dumps(data_val) if isinstance(data_val, (dict, list)) else str(data_val)
            if not addr:
                return 400, {"ok": False, "error": "Missing 'address'"}
            try:
                res = await self.hub.quixi.send_app_data(addr, data_str, protocol_id=protocol_id)
                return 200, {"ok": True, "result": res.get("result", res)}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and route == "/api/quixi/voip":
            act = str(payload.get("action") or "dial").strip().lower()
            addr = str(payload.get("address") or self.hub.server_address or "").strip()
            try:
                if act in ("dial", "call", "test_call"):
                    if not addr:
                        return 400, {"ok": False, "error": "Missing target 'address' for VoIP call"}
                    sid = await self.hub.voip.initiate_call(addr)
                    return 200, {"ok": True, "action": "dial", "address": addr, "session_id": sid.hex()}
                if act in ("hangup", "end", "stop"):
                    await self.hub.voip.hangup_call()
                    return 200, {"ok": True, "action": "hangup"}
                return 400, {"ok": False, "error": f"Unknown voip action: {act}"}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and route == "/api/quixi/rpc":
            rpc_method = str(payload.get("method") or "").strip().lstrip("/")
            raw_params = payload.get("params")
            params_dict: Dict[str, str] = {}
            if isinstance(raw_params, dict):
                params_dict = {str(k): str(v) for k, v in raw_params.items() if v is not None}
            if not rpc_method:
                return 400, {"ok": False, "error": "Missing QuIXI 'method'"}
            try:
                res = await self.hub.quixi.call_rpc(rpc_method, params_dict)
                return 200, {"ok": True, "method": rpc_method, "response": res}
            except (QuIXIError, Exception) as exc:
                return 502, {"ok": False, "method": rpc_method, "error": str(exc)}

        # --------------------------------------------------------------
        # 3. ESP32-S3 Live Monitoring & Control Endpoints
        # --------------------------------------------------------------
        if method == "POST" and route == "/api/esp32/ping":
            t0 = time.monotonic()
            res = await self.hub.esp.request({"t": "ping"})
            rtt_ms = int((time.monotonic() - t0) * 1000)
            if isinstance(res, dict) and "up" in res:
                self.esp_telemetry["uptime_ms"] = int(res.get("up", 0))
            self.esp_telemetry["last_ping_rtt_ms"] = rtt_ms
            return 200, {"ok": res.get("ok") == 1, "rtt_ms": rtt_ms, "response": res}

        if method == "POST" and route == "/api/esp32/state":
            emo = str(payload.get("emotion") or payload.get("emo") or "IDLE").strip().upper()
            sub = payload.get("subtitle") if "subtitle" in payload else payload.get("sub")
            mouth = payload.get("mouth")
            await self.hub.esp.set_state(emo, sub)
            if mouth is not None:
                await self.hub.esp.set_mouth(int(mouth))
            self.update_face_state(
                emo=emo,
                sub=str(sub) if sub is not None else None,
                mouth=int(mouth) if mouth is not None else None,
            )
            return 200, {"ok": True, "face": dict(self.face_state)}

        if method == "POST" and route == "/api/esp32/cfg":
            cfg_kwargs: Dict[str, Any] = {}
            if "brightness" in payload or "bright" in payload:
                val = int(payload.get("brightness", payload.get("bright", 100)))
                cfg_kwargs["bright"] = max(10, min(100, val))
                self.esp_telemetry["cfg"]["bright"] = cfg_kwargs["bright"]
            if "volume" in payload or "vol" in payload:
                val = int(payload.get("volume", payload.get("vol", 75)))
                cfg_kwargs["vol"] = max(0, min(100, val))
                self.hub.audio.set_volume(cfg_kwargs["vol"])
                self.esp_telemetry["cfg"]["vol"] = cfg_kwargs["vol"]
            if "mic_mode" in payload or "mode" in payload:
                m = str(payload.get("mic_mode", payload.get("mode", "vad")))
                self.hub.audio.set_mic_mode(m)
                cfg_kwargs["mode"] = self.hub.audio.mic_mode
                self.esp_telemetry["cfg"]["mode"] = self.hub.audio.mic_mode
            if "ble_en" in payload:
                cfg_kwargs["ble_en"] = 1 if payload["ble_en"] else 0
                self.esp_telemetry["cfg"]["ble_en"] = cfg_kwargs["ble_en"]
            if "sleep_s" in payload:
                cfg_kwargs["sleep_s"] = int(payload["sleep_s"])
                self.esp_telemetry["cfg"]["sleep_s"] = cfg_kwargs["sleep_s"]
            res = await self.hub.esp.set_cfg(**cfg_kwargs, wait_res=bool(payload.get("wait_res", True)))
            return 200, {"ok": True, "applied": cfg_kwargs, "esp_response": res}

        if method == "POST" and route == "/api/esp32/imu":
            res = await self.hub.esp.request({"t": "imu_get"})
            if isinstance(res, dict) and res.get("ok") == 1:
                self.esp_telemetry["imu"] = dict(res)
                self.broadcast_event("imu_telemetry", dict(res))
            return 200, {"ok": res.get("ok") == 1, "imu": res}

        if method == "POST" and route == "/api/esp32/ble":
            res = await self.hub.esp.request({"t": "ble_get"})
            min_rssi = payload.get("min_rssi")
            if isinstance(res, dict) and isinstance(res.get("ble"), list):
                ble_list = list(res["ble"])
                if min_rssi is not None:
                    ble_list = [d for d in ble_list if int(d.get("rssi", -127)) >= int(min_rssi)]
                    res["ble"] = ble_list
                    res["cnt"] = len(ble_list)
                self.esp_telemetry["ble"] = ble_list
                self.broadcast_event("ble_telemetry", {"ble": ble_list})
            return 200, {"ok": res.get("ok") == 1, "ble": res.get("ble", []), "response": res}

        if method == "POST" and route == "/api/esp32/gpio":
            act = str(payload.get("action") or "get").strip().lower()
            if "pin" not in payload:
                return 400, {"ok": False, "error": "Missing 'pin'"}
            pin = int(payload["pin"])
            if act in ("cfg", "write", "set"):
                direction = str(payload.get("dir") or payload.get("mode") or "out").strip().lower()
                pull = str(payload.get("pull") or "none").strip().lower()
                val = int(payload.get("val", payload.get("state", 0)))
                res = await self.hub.esp.request(
                    {"t": "gpio_cfg", "pin": pin, "dir": direction, "pull": pull, "val": val}
                )
                if res.get("ok") == 1 and str(pin) in self.esp_telemetry["gpio_pins"]:
                    self.esp_telemetry["gpio_pins"][str(pin)] = {
                        "pin": pin,
                        "dir": direction,
                        "pull": pull,
                        "val": val,
                    }
                return 200, {"ok": res.get("ok") == 1, "response": res}
            else:
                res = await self.hub.esp.request({"t": "gpio_get", "pin": pin})
                if res.get("ok") == 1:
                    if str(pin) in self.esp_telemetry["gpio_pins"] and "val" in res:
                        self.esp_telemetry["gpio_pins"][str(pin)]["val"] = int(res["val"])
                    if "vbat_mv" in res:
                        self.esp_telemetry["vbat_mv"] = int(res["vbat_mv"])
                return 200, {"ok": res.get("ok") == 1, "response": res}

        if method == "POST" and route == "/api/esp32/raw":
            cmd = payload.get("cmd")
            if not isinstance(cmd, dict) or "t" not in cmd:
                return 400, {"ok": False, "error": "Expected 'cmd' JSON object with 't' field"}
            wait_res = bool(payload.get("wait_res", True))
            if wait_res:
                res = await self.hub.esp.request(dict(cmd))
                return 200, {"ok": True, "response": res}
            await self.hub.esp.send(dict(cmd))
            return 200, {"ok": True, "sent": cmd}

        if method == "GET" and route == "/api/esp32/firmware":
            fw_upd = getattr(self.hub, "firmware", None)
            if fw_upd is None:
                return 500, {"ok": False, "error": "Firmware updater not initialized"}
            source = query.get("source", "").strip() or None
            return 200, fw_upd.check_firmware(source)

        if method == "POST" and route == "/api/esp32/firmware/upload":
            fw_upd = getattr(self.hub, "firmware", None)
            if fw_upd is None:
                return 500, {"ok": False, "error": "Firmware updater not initialized"}
            b64_data = str(payload.get("firmware_b64") or payload.get("data_b64") or "").strip()
            if not b64_data:
                return 400, {"ok": False, "error": "Missing 'firmware_b64'"}
            try:
                raw_fw = base64.b64decode(b64_data)
            except Exception as exc:
                return 400, {"ok": False, "error": f"Invalid base64 payload: {exc}"}
            exp_sha = str(payload.get("sha256") or "").strip() or None
            fname = str(payload.get("filename") or "zephyr.bin").strip()
            res = fw_upd.stage_firmware(raw_fw, expected_sha256=exp_sha, filename=fname)
            if not res.get("ok"):
                return 400, res
            self.broadcast_event("esp32_firmware_staged", res)
            return 200, res

        if method == "POST" and route == "/api/esp32/firmware/flash":
            fw_upd = getattr(self.hub, "firmware", None)
            if fw_upd is None:
                return 500, {"ok": False, "error": "Firmware updater not initialized"}
            source = str(payload.get("source") or "").strip() or None
            exp_sha = str(payload.get("sha256") or "").strip() or None
            baud = int(payload.get("baudrate", 921600))
            offset = str(payload.get("flash_offset", "0x0"))
            self.broadcast_event("esp32_flash_started", {"source": source, "baudrate": baud})
            res = await fw_upd.flash_firmware(
                source=source,
                expected_sha256=exp_sha,
                baudrate=baud,
                flash_offset=offset,
            )
            self.broadcast_event("esp32_flash_completed", res)
            return (200 if res.get("ok") else 502), res

        if method == "POST" and route == "/api/esp32/firmware/reset":
            fw_upd = getattr(self.hub, "firmware", None)
            if fw_upd is None:
                return 500, {"ok": False, "error": "Firmware updater not initialized"}
            res = await fw_upd.reset_esp32()
            self.broadcast_event("esp32_reset", res)
            return 200, res

        # --------------------------------------------------------------
        # 4. Audio / Wyoming-Satellite Controls
        # --------------------------------------------------------------
        if method == "POST" and route == "/api/audio/control":
            act = str(payload.get("action") or "").strip().lower()
            if act == "ptt_toggle":
                listening = await self.hub.audio.toggle_ptt()
                return 200, {"ok": True, "listening": listening}
            if act == "stop_playback":
                await self.hub._trigger_barge_in(subtitle="Interrupted.")
                return 200, {"ok": True, "stopped": True}
            if act == "earcon":
                kind = str(payload.get("kind") or "awake").strip().lower()
                pcm = self.hub.audio.play_earcon(kind)
                return 200, {"ok": True, "kind": kind, "bytes": len(pcm)}
            if act == "trigger_wake":
                self.hub.audio._wake_detector.trigger()
                return 200, {"ok": True, "wake_armed": True}
            if act == "heal_streams":
                healed = await self.hub.audio.check_and_heal_streams()
                return 200, {
                    "ok": True,
                    "healed": healed,
                    "recoveries": self.hub.audio.stream_recoveries,
                }
            return 400, {"ok": False, "error": f"Unknown audio action: {act}"}

        return 404, {"ok": False, "error": f"Not found: {method} {route}"}

    # ------------------------------------------------------------------
    # Asyncio HTTP/1.1 + SSE + RFC 6455 WebSocket Transport
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            req_line_bytes = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not req_line_bytes:
                writer.close()
                return
            req_line = req_line_bytes.decode("latin-1", errors="replace").strip()
            parts = req_line.split(" ")
            if len(parts) < 2:
                writer.close()
                return
            method, path = parts[0].upper(), parts[1]

            headers: Dict[str, str] = {}
            while True:
                line_b = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if not line_b or line_b in (b"\r\n", b"\n"):
                    break
                line_s = line_b.decode("latin-1", errors="replace").strip()
                if ":" in line_s:
                    k, v = line_s.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if method == "OPTIONS":
                await self._write_http_response(writer, 204, b"", "text/plain")
                return

            parsed = urllib.parse.urlsplit(path)
            route = parsed.path.rstrip("/") or "/"
            query = dict(urllib.parse.parse_qsl(parsed.query))

            if route == "/api/ws" and headers.get("upgrade", "").lower() == "websocket":
                if not self._is_request_authorized(headers, query):
                    await self._write_http_response(
                        writer, 401, b'{"error":"Unauthorized"}', "application/json"
                    )
                    return
                await self._serve_websocket(reader, writer, headers)
                return

            if method == "GET" and route == "/api/events":
                if not self._is_request_authorized(headers, query):
                    await self._write_http_response(
                        writer, 401, b'{"error":"Unauthorized"}', "application/json"
                    )
                    return
                await self._serve_sse_events(writer)
                return

            if method == "GET" and route in ("/", "/index.html"):
                html_bytes = PI_DASHBOARD_HTML.encode("utf-8")
                await self._write_http_response(writer, 200, html_bytes, "text/html; charset=utf-8")
                return

            content_len = int(headers.get("content-length", "0") or "0")
            body_obj: Optional[Dict[str, Any]] = None
            if content_len > 0:
                raw_body = await asyncio.wait_for(reader.readexactly(content_len), timeout=15.0)
                try:
                    parsed_body = json.loads(raw_body.decode("utf-8", errors="replace"))
                    if isinstance(parsed_body, dict):
                        body_obj = parsed_body
                except Exception:
                    body_obj = None

            status_code, resp_dict = await self.handle_api_request(
                method, path, body=body_obj, headers=headers
            )
            resp_bytes = json.dumps(resp_dict, default=str).encode("utf-8")
            await self._write_http_response(writer, status_code, resp_bytes, "application/json")
        except Exception as exc:
            logger.debug("HTTP connection closed: %s", exc)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _write_http_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        reasons = {
            200: "OK",
            204: "No Content",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            502: "Bad Gateway",
            500: "Internal Server Error",
        }
        reason = reasons.get(status, "OK")
        hdr = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Headers: Authorization, Content-Type, X-API-Key\r\n"
            "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(hdr + body)
        await writer.drain()

    async def _serve_sse_events(self, writer: asyncio.StreamWriter) -> None:
        hdr = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("latin-1")
        writer.write(hdr)
        init_pkt = {"type": "init", "ts": time.time(), "data": self.get_status_snapshot()}
        writer.write(f"data: {json.dumps(init_pkt, default=str)}\n\n".encode("utf-8"))
        await writer.drain()

        q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._sse_queues.add(q)
        try:
            while not writer.is_closing():
                pkt = await q.get()
                writer.write(f"data: {json.dumps(pkt, default=str)}\n\n".encode("utf-8"))
                await writer.drain()
        except Exception:
            pass
        finally:
            self._sse_queues.discard(q)

    @staticmethod
    def _encode_ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        return bytes(header) + payload

    async def _serve_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: Dict[str, str],
    ) -> None:
        ws_key = headers.get("sec-websocket-key", "").strip()
        accept = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("latin-1")
        writer.write(handshake)
        await writer.drain()

        self._ws_clients.add(writer)
        init_raw = json.dumps(
            {"type": "init", "ts": time.time(), "data": self.get_status_snapshot()},
            default=str,
        ).encode("utf-8")
        writer.write(self._encode_ws_frame(init_raw, opcode=0x1))
        await writer.drain()

        try:
            while not writer.is_closing():
                b1, b2 = await reader.readexactly(2)
                opcode = b1 & 0x0F
                masked = bool(b2 & 0x80)
                length = b2 & 0x7F
                if length == 126:
                    (length,) = struct.unpack("!H", await reader.readexactly(2))
                elif length == 127:
                    (length,) = struct.unpack("!Q", await reader.readexactly(8))
                mask_key = await reader.readexactly(4) if masked else b""
                data = await reader.readexactly(length) if length > 0 else b""
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    writer.write(self._encode_ws_frame(data, opcode=0xA))
                    await writer.drain()
                    continue
                if masked and data:
                    data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
                if opcode == 0x1 and data:
                    try:
                        cmd = json.loads(data.decode("utf-8", errors="ignore"))
                        if isinstance(cmd, dict) and cmd.get("action") == "ping":
                            pong = json.dumps({"type": "pong", "ts": time.time()}).encode("utf-8")
                            writer.write(self._encode_ws_frame(pong, opcode=0x1))
                            await writer.drain()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self._ws_clients.discard(writer)


# ---------------------------------------------------------------------------
# Embedded Raspberry Pi Hub Web GUI (QuIXI Wallet/Contacts + ESP32-S3 Monitor)
# ---------------------------------------------------------------------------

PI_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Alf Pi Hub · QuIXI & ESP32-S3 Control Center</title>
<style>
  :root {
    --bg-0: #090b10;
    --bg-1: #111520;
    --bg-2: #181e2c;
    --bg-3: #222a3d;
    --border: #2b354d;
    --text-0: #f3f5fa;
    --text-1: #a8b3cf;
    --text-2: #6b7694;
    --accent: #38bdf8;
    --accent-2: #34d399;
    --amber: #fbbf24;
    --red: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background: var(--bg-0); color: var(--text-0);
    min-height: 100vh; display: flex; flex-direction: column;
  }
  header {
    background: var(--bg-1); border-bottom: 1px solid var(--border);
    padding: 12px 22px; display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 12px;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 1.05rem; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent-2); box-shadow: 0 0 10px var(--accent-2); }
  .badges { display: flex; flex-wrap: wrap; gap: 8px; }
  .badge {
    font-size: 0.74rem; padding: 4px 10px; border-radius: 99px;
    background: var(--bg-2); border: 1px solid var(--border); color: var(--text-1);
  }
  .badge.ok { border-color: rgba(52,211,153,0.45); color: var(--accent-2); }
  .grid-main {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(370px, 1fr));
    gap: 16px; padding: 18px 22px; flex: 1;
  }
  .col { display: flex; flex-direction: column; gap: 16px; }
  .card {
    background: var(--bg-1); border: 1px solid var(--border);
    border-radius: 12px; padding: 15px; display: flex; flex-direction: column; gap: 11px;
  }
  .card-hdr {
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid var(--border); padding-bottom: 8px;
  }
  .card-hdr h2 { font-size: 0.92rem; color: var(--accent); letter-spacing: 0.3px; }
  .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .row-between { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 0.82rem; }
  input[type="text"], input[type="password"], input[type="number"], select, textarea {
    background: var(--bg-2); border: 1px solid var(--border);
    color: var(--text-0); border-radius: 8px; padding: 7px 10px;
    font-size: 0.82rem; outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  .btn {
    background: var(--bg-3); color: var(--text-0);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 12px; font-size: 0.79rem; cursor: pointer; font-weight: 500;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn-primary {
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: #060911; border: none; font-weight: 600;
  }
  .btn-danger:hover { border-color: var(--red); color: var(--red); }
  .mono-box {
    font-family: ui-monospace, Consolas, monospace; font-size: 0.75rem;
    background: var(--bg-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 9px; color: var(--text-1);
    max-height: 185px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
  }
  .face-wrap {
    display: flex; align-items: center; gap: 16px;
    background: var(--bg-2); padding: 12px; border-radius: 10px; border: 1px solid var(--border);
  }
  #piFaceCanvas {
    width: 112px; height: 112px; border-radius: 50%;
    background: #04060b; border: 2px solid var(--accent); flex-shrink: 0;
  }
  .gpio-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; }
  .gpio-cell {
    background: var(--bg-2); border: 1px solid var(--border); border-radius: 7px;
    padding: 6px 9px; display: flex; align-items: center; justify-content: space-between;
    font-size: 0.76rem;
  }
  .contact-item {
    background: var(--bg-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 10px; display: flex; align-items: center; justify-content: space-between;
    gap: 8px; font-size: 0.78rem;
  }
  .contact-addr { font-family: ui-monospace, Consolas, monospace; font-size: 0.73rem; color: var(--accent); overflow: hidden; text-overflow: ellipsis; }
</style>
</head>
<body>
  <header>
    <div class="brand">
      <span class="dot" id="wsDot"></span>
      <span>Alf Pi Zero 2 W · QuIXI & ESP32-S3 Hub GUI</span>
    </div>
    <div class="badges">
      <span class="badge ok" id="quixiBadge">QuIXI: --</span>
      <span class="badge" id="voipBadge">VoIP: IDLE</span>
      <span class="badge" id="espBadge">ESP32: 921600 baud</span>
      <span class="badge" id="vbatBadge">Vbat: -- mV</span>
      <span class="badge" id="micBadge">Mic: vad</span>
    </div>
  </header>

  <div class="grid-main">
    <!-- COLUMN 1: QuIXI Account Creation, Wallet & Contacts / Friend Requests -->
    <div class="col">
      <section class="card">
        <div class="card-hdr">
          <h2>1 · QuIXI Account Creation & Wallet</h2>
          <button class="btn" onclick="refreshQuixiOverview()">Refresh Node</button>
        </div>
        <div class="row-between">
          <span>Nickname</span>
          <div class="row">
            <input type="text" id="qNick" placeholder="Alf-Pi-Hub" style="width:140px" />
            <button class="btn" onclick="setQuixiNick()">Set Nick</button>
          </div>
        </div>
        <div class="row-between">
          <span>Primary Wallet Address</span>
          <button class="btn btn-primary" onclick="generateNewAddress()">+ New Address</button>
        </div>
        <div class="mono-box" id="walletBox">Loading QuIXI wallet & node status...</div>
        <div class="row">
          <button class="btn" onclick="viewWalletDetails()">View Wallet</button>
          <button class="btn" onclick="backupWallet()">Export Backup</button>
        </div>
      </section>

      <section class="card">
        <div class="card-hdr">
          <h2>2 · QuIXI Node Provisioning (ixian.cfg)</h2>
          <span class="badge" id="cfgStatusBadge">chmod 600</span>
        </div>
        <div class="row-between">
          <span>Wallet Password</span>
          <input type="password" id="cfgWalletPw" placeholder="Set strong wallet password" style="width:190px" />
        </div>
        <div class="row-between">
          <span>Network Type</span>
          <select id="cfgNetType">
            <option value="mainnet">mainnet</option>
            <option value="testnet">testnet</option>
          </select>
        </div>
        <div class="row-between">
          <span>REST API / MQTT Ports</span>
          <div class="row">
            <input type="number" id="cfgApiPort" value="8001" style="width:80px" />
            <input type="number" id="cfgMqPort" value="1883" style="width:80px" />
          </div>
        </div>
        <button class="btn btn-primary" onclick="provisionIxianCfg()">Save / Provision ixian.cfg</button>
      </section>

      <section class="card">
        <div class="card-hdr">
          <h2>3 · Contacts, Friend Requests & AI Server Pairing</h2>
          <button class="btn" onclick="refreshContacts()">Refresh</button>
        </div>
        <div class="row">
          <input type="text" id="contactAddrInput" placeholder="Paste Ixian Wallet Address..." style="flex:1" />
        </div>
        <div class="row">
          <button class="btn btn-primary" onclick="addContact()">Send Friend Request</button>
          <button class="btn" onclick="acceptContact()">Accept Request</button>
          <button class="btn" onclick="pairAsServer()">Pair as AI Server & Dial</button>
        </div>
        <div id="contactsList" style="display:flex;flex-direction:column;gap:6px;"></div>
      </section>
    </div>

    <!-- COLUMN 2: Spixi VoIP, Chat, Universal QuIXI RPC & Wyoming Audio -->
    <div class="col">
      <section class="card">
        <div class="card-hdr">
          <h2>4 · Spixi VoIP Call & Encrypted P2P Chat</h2>
          <div class="row">
            <button class="btn btn-primary" onclick="voipAction('dial')">Dial Server</button>
            <button class="btn btn-danger" onclick="voipAction('hangup')">Hang Up</button>
          </div>
        </div>
        <div class="row">
          <input type="text" id="chatTargetInput" placeholder="Recipient Ixian Address (blank = AI Server)" style="flex:1" />
        </div>
        <div class="row">
          <input type="text" id="chatMsgInput" placeholder="Send encrypted Spixi chat message..." style="flex:1" />
          <button class="btn btn-primary" onclick="sendQuixiChat()">Send Chat</button>
        </div>
        <div class="mono-box" id="quixiEventsBox">QuIXI MQTT & Chat stream events will appear here...</div>
      </section>

      <section class="card">
        <div class="card-hdr">
          <h2>5 · Universal QuIXI REST API Explorer</h2>
          <button class="btn btn-primary" onclick="runCustomQuixiRpc()">Execute RPC</button>
        </div>
        <div class="row">
          <input type="text" id="rpcMethod" value="status" placeholder="Method (e.g. status, mywallet, contacts, getblock)" style="flex:1" />
        </div>
        <textarea id="rpcParams" rows="2" placeholder='{"address": "..."}'>{}</textarea>
        <div class="mono-box" id="rpcResultBox">Click "Execute RPC" to query any QuIXI endpoint.</div>
      </section>

      <section class="card">
        <div class="card-hdr">
          <h2>6 · Wyoming-Satellite Audio & Bluetooth Pipeline</h2>
          <button class="btn" onclick="audioCtrl('heal_streams')">Heal BT Streams</button>
        </div>
        <div class="row">
          <button class="btn btn-primary" onclick="audioCtrl('ptt_toggle')">Toggle PTT</button>
          <button class="btn btn-danger" onclick="audioCtrl('stop_playback')">Stop TTS</button>
          <button class="btn" onclick="audioCtrl('trigger_wake')">Trigger Wake</button>
        </div>
        <div class="row">
          <button class="btn" onclick="playEarcon('awake')">Chime: Awake</button>
          <button class="btn" onclick="playEarcon('done')">Chime: Done</button>
          <button class="btn" onclick="playEarcon('error')">Chime: Error</button>
        </div>
        <div class="mono-box" id="audioStatusBox"></div>
      </section>
    </div>

    <!-- COLUMN 3: ESP32-S3 Live Face Mirror, IMU, BLE Radar, GPIO & Serial -->
    <div class="col">
      <section class="card">
        <div class="card-hdr">
          <h2>7 · ESP32-S3 Live Round Face & Display Config</h2>
          <button class="btn" onclick="pingEsp32()">Ping ESP32</button>
        </div>
        <div class="face-wrap">
          <canvas id="piFaceCanvas" width="240" height="240"></canvas>
          <div style="flex:1;display:flex;flex-direction:column;gap:7px;">
            <div class="row">
              <select id="espEmoSelect">
                <option>IDLE</option><option>LISTENING</option><option>THINKING</option>
                <option>SPEAKING</option><option>HAPPY</option><option>SAD</option>
                <option>SURPRISED</option><option>SLEEPING</option><option>ALERT</option>
              </select>
              <button class="btn btn-primary" onclick="pushEspState()">Set Face</button>
            </div>
            <input type="text" id="espSubInput" placeholder="Live subtitle text..." />
            <div class="row-between">
              <span>Brightness</span>
              <input type="range" id="espBright" min="10" max="100" value="100" onchange="pushEspCfg()" />
            </div>
            <div class="row-between">
              <span>Volume</span>
              <input type="range" id="espVol" min="0" max="100" value="75" onchange="pushEspCfg()" />
            </div>
            <div class="row-between">
              <span>Mic Mode</span>
              <select id="espMicMode" onchange="pushEspCfg()">
                <option value="vad">Auto VAD</option>
                <option value="ptt">Tap PTT</option>
                <option value="duplex">Full Duplex</option>
                <option value="wake">Wake Word</option>
              </select>
            </div>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-hdr">
          <h2>8 · ESP32-S3 Sensors (QMI8658 IMU & BLE Radar)</h2>
          <div class="row">
            <button class="btn" onclick="pollEspImu()">Poll IMU</button>
            <button class="btn" onclick="pollEspBle()">Scan BLE</button>
          </div>
        </div>
        <div class="mono-box" id="espSensorsBox">Click "Poll IMU" or "Scan BLE" to read ESP32-S3 sensors.</div>
      </section>

      <section class="card">
        <div class="card-hdr">
          <h2>9 · Allow-Listed GPIO Matrix & Raw Serial NDJSON</h2>
          <button class="btn" onclick="readEspGpio(16)">Read GPIO 16 & Vbat</button>
        </div>
        <div class="gpio-grid" id="piGpioGrid"></div>
        <div class="row">
          <input type="text" id="rawSerialInput" value='{"t":"ping"}' style="flex:1;font-family:monospace;" />
          <button class="btn" onclick="sendRawEsp()">Send NDJSON</button>
        </div>
        <div class="mono-box" id="espSerialBox">ESP32 serial responses & events...</div>
      </section>

      <section class="card">
        <div class="card-hdr">
          <h2>10 · ESP32-S3 LCD Firmware Updater & Reflash</h2>
          <button class="btn" onclick="checkEspFirmware()">Check Firmware</button>
        </div>
        <div class="row">
          <input type="file" id="fwFileInput" accept=".bin" style="font-size:0.75rem;flex:1;" />
          <button class="btn" onclick="uploadEspFirmware()">Stage .bin</button>
        </div>
        <div class="row">
          <button class="btn btn-primary" onclick="flashEspFirmware()">Reflash ESP32-S3 LCD</button>
          <button class="btn" onclick="resetEsp32()">Hardware Reset ESP32</button>
        </div>
        <div class="mono-box" id="espFirmwareBox">Click "Check Firmware" to inspect staged zephyr.bin & esptool.</div>
      </section>
    </div>
  </div>

<script>
  const PINS = [15,16,17,18,21,33,34,35,36,37];
  let faceState = { emo: "IDLE", sub: "Ready!", mouth: 0 };
  let pinVals = {};
  let blinkTick = 0;

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts
    });
    return await res.json();
  }

  function initPiUI() {
    const grid = document.getElementById("piGpioGrid");
    PINS.forEach(p => {
      pinVals[p] = 0;
      const cell = document.createElement("div");
      cell.className = "gpio-cell";
      cell.innerHTML = `<span>GPIO ${p}</span>
        <div class="row">
          <button class="btn" id="gBtn_${p}" onclick="toggleEspGpio(${p})">LOW</button>
          <button class="btn" onclick="readEspGpio(${p})">In</button>
        </div>`;
      grid.appendChild(cell);
    });
    refreshAll();
    connectWS();
    requestAnimationFrame(drawPiFace);
  }

  async function refreshAll() {
    const s = await api("/api/status");
    if (s.ok) applyStatus(s);
    refreshQuixiOverview();
  }

  function applyStatus(s) {
    if (s.face) faceState = { ...faceState, ...s.face };
    if (s.voip) {
      document.getElementById("voipBadge").textContent =
        "VoIP: " + (s.voip.in_call ? ("IN_CALL (" + (s.voip.codec || "pcm") + ")") : "IDLE");
    }
    if (s.esp && s.esp.telemetry) {
      const t = s.esp.telemetry;
      if (t.vbat_mv) document.getElementById("vbatBadge").textContent = "Vbat: " + t.vbat_mv + " mV";
    }
    if (s.audio) {
      document.getElementById("micBadge").textContent = "Mic: " + s.audio.mic_mode;
      document.getElementById("audioStatusBox").textContent = JSON.stringify(s.audio, null, 2);
    }
    if (s.quixi && s.quixi.cfg) {
      document.getElementById("cfgNetType").value = s.quixi.cfg.networkType || "mainnet";
      document.getElementById("cfgApiPort").value = s.quixi.cfg.apiPort || 8001;
      document.getElementById("cfgMqPort").value = s.quixi.cfg.mqPort || 1883;
      document.getElementById("cfgStatusBadge").textContent =
        s.quixi.cfg.walletPasswordSet ? "Password Set (chmod 600)" : "Needs Password";
    }
  }

  async function refreshQuixiOverview() {
    const o = await api("/api/quixi/overview");
    if (!o.ok) return;
    document.getElementById("quixiBadge").textContent = "QuIXI: ONLINE";
    if (typeof o.nick === "string") document.getElementById("qNick").value = o.nick;
    document.getElementById("walletBox").textContent = JSON.stringify(
      { wallet: o.wallet, balance: o.balance, status: o.status },
      null,
      2
    );
    renderContactsList(o.contacts);
  }

  function renderContactsList(contacts) {
    const box = document.getElementById("contactsList");
    box.innerHTML = "";
    const arr = Array.isArray(contacts) ? contacts : [];
    arr.forEach(c => {
      const addr = typeof c === "string" ? c : (c.address || JSON.stringify(c));
      const div = document.createElement("div");
      div.className = "contact-item";
      div.innerHTML = `<span class="contact-addr" title="${addr}">${addr}</span>
        <div class="row">
          <button class="btn" onclick="acceptContact('${addr}')">Accept</button>
          <button class="btn btn-danger" onclick="removeContact('${addr}')">Remove</button>
        </div>`;
      box.appendChild(div);
    });
  }

  async function setQuixiNick() {
    const nick = document.getElementById("qNick").value.trim();
    if (!nick) return;
    await api("/api/quixi/account/nick", { method: "POST", body: JSON.stringify({ nick }) });
    refreshQuixiOverview();
  }

  async function generateNewAddress() {
    const r = await api("/api/quixi/account/new_address", { method: "POST", body: "{}" });
    document.getElementById("walletBox").textContent = JSON.stringify(r, null, 2);
    refreshQuixiOverview();
  }

  async function viewWalletDetails() {
    const r = await api("/api/quixi/account/wallet");
    document.getElementById("walletBox").textContent = JSON.stringify(r, null, 2);
  }

  async function backupWallet() {
    const r = await api("/api/quixi/account/backup");
    document.getElementById("walletBox").textContent = JSON.stringify(r, null, 2);
  }

  async function provisionIxianCfg() {
    const body = {
      walletPassword: document.getElementById("cfgWalletPw").value,
      networkType: document.getElementById("cfgNetType").value,
      apiPort: parseInt(document.getElementById("cfgApiPort").value, 10),
      mqPort: parseInt(document.getElementById("cfgMqPort").value, 10)
    };
    const r = await api("/api/quixi/account/provision", { method: "POST", body: JSON.stringify(body) });
    if (r.ok) {
      document.getElementById("cfgWalletPw").value = "";
      refreshAll();
    }
  }

  async function refreshContacts() {
    const r = await api("/api/quixi/contacts");
    if (r.ok) renderContactsList(r.contacts);
  }

  async function addContact() {
    const address = document.getElementById("contactAddrInput").value.trim();
    if (!address) return;
    await api("/api/quixi/contacts/add", { method: "POST", body: JSON.stringify({ address }) });
    refreshContacts();
  }

  async function acceptContact(addrArg) {
    const address = addrArg || document.getElementById("contactAddrInput").value.trim();
    if (!address) return;
    await api("/api/quixi/contacts/accept", { method: "POST", body: JSON.stringify({ address }) });
    refreshContacts();
  }

  async function removeContact(address) {
    await api("/api/quixi/contacts/remove", { method: "POST", body: JSON.stringify({ address }) });
    refreshContacts();
  }

  async function pairAsServer() {
    const address = document.getElementById("contactAddrInput").value.trim();
    if (!address) return;
    const r = await api("/api/quixi/pair_server", { method: "POST", body: JSON.stringify({ address }) });
    document.getElementById("quixiEventsBox").textContent = JSON.stringify(r, null, 2);
    refreshAll();
  }

  async function voipAction(action) {
    const address = document.getElementById("chatTargetInput").value.trim();
    const r = await api("/api/quixi/voip", { method: "POST", body: JSON.stringify({ action, address }) });
    document.getElementById("quixiEventsBox").textContent = JSON.stringify(r, null, 2);
    refreshAll();
  }

  async function sendQuixiChat() {
    const address = document.getElementById("chatTargetInput").value.trim();
    const message = document.getElementById("chatMsgInput").value.trim();
    if (!message) return;
    document.getElementById("chatMsgInput").value = "";
    const r = await api("/api/quixi/chat", { method: "POST", body: JSON.stringify({ address, message }) });
    document.getElementById("quixiEventsBox").textContent = JSON.stringify(r, null, 2);
  }

  async function runCustomQuixiRpc() {
    const method = document.getElementById("rpcMethod").value.trim();
    let params = {};
    try { params = JSON.parse(document.getElementById("rpcParams").value || "{}"); } catch (e) {}
    const r = await api("/api/quixi/rpc", { method: "POST", body: JSON.stringify({ method, params }) });
    document.getElementById("rpcResultBox").textContent = JSON.stringify(r, null, 2);
  }

  async function audioCtrl(action) {
    const r = await api("/api/audio/control", { method: "POST", body: JSON.stringify({ action }) });
    document.getElementById("audioStatusBox").textContent = JSON.stringify(r, null, 2);
    refreshAll();
  }

  async function playEarcon(kind) {
    await api("/api/audio/control", { method: "POST", body: JSON.stringify({ action: "earcon", kind }) });
  }

  async function pingEsp32() {
    const r = await api("/api/esp32/ping", { method: "POST", body: "{}" });
    document.getElementById("espSerialBox").textContent = JSON.stringify(r, null, 2);
  }

  async function pushEspState() {
    const emotion = document.getElementById("espEmoSelect").value;
    const subtitle = document.getElementById("espSubInput").value;
    const r = await api("/api/esp32/state", { method: "POST", body: JSON.stringify({ emotion, subtitle }) });
    if (r.face) faceState = { ...faceState, ...r.face };
  }

  async function pushEspCfg() {
    const brightness = parseInt(document.getElementById("espBright").value, 10);
    const volume = parseInt(document.getElementById("espVol").value, 10);
    const mic_mode = document.getElementById("espMicMode").value;
    await api("/api/esp32/cfg", { method: "POST", body: JSON.stringify({ brightness, volume, mic_mode }) });
    refreshAll();
  }

  async function pollEspImu() {
    const r = await api("/api/esp32/imu", { method: "POST", body: "{}" });
    document.getElementById("espSensorsBox").textContent = JSON.stringify(r.imu || r, null, 2);
  }

  async function pollEspBle() {
    const r = await api("/api/esp32/ble", { method: "POST", body: "{}" });
    document.getElementById("espSensorsBox").textContent = JSON.stringify(r.ble || r, null, 2);
  }

  async function toggleEspGpio(pin) {
    const nextVal = pinVals[pin] ? 0 : 1;
    pinVals[pin] = nextVal;
    document.getElementById("gBtn_" + pin).textContent = nextVal ? "HIGH" : "LOW";
    const r = await api("/api/esp32/gpio", {
      method: "POST",
      body: JSON.stringify({ action: "write", pin, dir: "out", pull: "none", val: nextVal })
    });
    document.getElementById("espSerialBox").textContent = JSON.stringify(r, null, 2);
  }

  async function readEspGpio(pin) {
    const r = await api("/api/esp32/gpio", { method: "POST", body: JSON.stringify({ action: "get", pin }) });
    document.getElementById("espSerialBox").textContent = JSON.stringify(r, null, 2);
    if (r.response && r.response.vbat_mv) {
      document.getElementById("vbatBadge").textContent = "Vbat: " + r.response.vbat_mv + " mV";
    }
  }

  async function sendRawEsp() {
    let cmd = {};
    try { cmd = JSON.parse(document.getElementById("rawSerialInput").value); } catch (e) { return; }
    const r = await api("/api/esp32/raw", { method: "POST", body: JSON.stringify({ cmd, wait_res: true }) });
    document.getElementById("espSerialBox").textContent = JSON.stringify(r, null, 2);
  }

  async function checkEspFirmware() {
    const r = await api("/api/esp32/firmware");
    document.getElementById("espFirmwareBox").textContent = JSON.stringify(r, null, 2);
  }

  async function uploadEspFirmware() {
    const inp = document.getElementById("fwFileInput");
    if (!inp.files || !inp.files[0]) return;
    const file = inp.files[0];
    const buf = await file.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binStr = "";
    for (let i = 0; i < bytes.byteLength; i++) binStr += String.fromCharCode(bytes[i]);
    const b64 = btoa(binStr);
    const r = await api("/api/esp32/firmware/upload", {
      method: "POST",
      body: JSON.stringify({ filename: file.name, firmware_b64: b64 })
    });
    document.getElementById("espFirmwareBox").textContent = JSON.stringify(r, null, 2);
  }

  async function flashEspFirmware() {
    document.getElementById("espFirmwareBox").textContent = "Flashing ESP32-S3 LCD via esptool (releasing serial port)...";
    const r = await api("/api/esp32/firmware/flash", { method: "POST", body: "{}" });
    document.getElementById("espFirmwareBox").textContent = JSON.stringify(r, null, 2);
  }

  async function resetEsp32() {
    const r = await api("/api/esp32/firmware/reset", { method: "POST", body: "{}" });
    document.getElementById("espFirmwareBox").textContent = JSON.stringify(r, null, 2);
  }

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/ws`);
    const dot = document.getElementById("wsDot");
    ws.onopen = () => { dot.style.background = "#34d399"; };
    ws.onclose = () => { dot.style.background = "#f87171"; setTimeout(connectWS, 2500); };
    ws.onmessage = (ev) => {
      try {
        const pkt = JSON.parse(ev.data);
        if (pkt.type === "init") applyStatus(pkt.data);
        else if (pkt.type === "face_state") faceState = { ...faceState, ...pkt.data };
        else if (pkt.type === "esp_event") {
          document.getElementById("espSerialBox").textContent = JSON.stringify(pkt.data, null, 2);
        } else if (pkt.type === "quixi_event") {
          document.getElementById("quixiEventsBox").textContent = JSON.stringify(pkt.data, null, 2);
        }
      } catch (e) {}
    };
  }

  function drawPiFace() {
    const c = document.getElementById("piFaceCanvas");
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, 240, 240);
    const emo = faceState.emo || "IDLE";
    const col = emo === "ALERT" ? "#f87171" : (emo === "HAPPY" || emo === "LISTENING" ? "#34d399" : "#38bdf8");
    ctx.strokeStyle = col;
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.arc(120, 120, 114, 0, Math.PI * 2);
    ctx.stroke();

    blinkTick = (blinkTick + 1) % 170;
    const eyeH = (emo === "SLEEPING" || blinkTick > 163) ? 4 : 24;
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.roundRect(64, 88 - eyeH / 2, 26, eyeH, 10);
    ctx.roundRect(150, 88 - eyeH / 2, 26, eyeH, 10);
    ctx.fill();

    const mOpen = Math.max(4, (faceState.mouth || 0) * 0.34);
    ctx.beginPath();
    ctx.roundRect(92, 154 - mOpen / 2, 56, mOpen, 10);
    ctx.fill();
    requestAnimationFrame(drawPiFace);
  }

  window.addEventListener("DOMContentLoaded", initPiUI);
</script>
</body>
</html>
"""

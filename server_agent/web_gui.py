"""
Open WebUI-Inspired Control Center, Live Companion Mirror, Hardware Workbench,
and OpenAI-Compatible API Bridge (/v1/models, /v1/chat/completions) for Tier 3 (server_agent).

Zero external web framework dependencies: runs natively on Python's built-in asyncio
TCP server with HTTP/1.1, Server-Sent Events (SSE), and RFC 6455 WebSockets.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
import struct
import time
import urllib.parse
import uuid
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from server_agent.agent_router import ALLOWLIST_PINS, VALID_EMOTIONS, build_default_system_prompt
from server_agent.mcp_server import MCP_TOOLS, AlfMCPServer

logger = logging.getLogger("server_agent.web_gui")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def mask_secret(val: Optional[str]) -> str:
    """Mask an API key so raw provider secrets are never exposed over GET /api/settings."""
    raw = (val or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "***"
    return f"{raw[:3]}***{raw[-3:]}"


DEFAULT_PERSONAS: List[Dict[str, Any]] = [
    {
        "id": "alf-companion",
        "name": "Alf — Playful Desk Companion",
        "companion_name": "Alf",
        "default_emotion": "HAPPY",
        "tts_voice": "alloy",
        "description": "Warm, witty embodied desk companion with expressive face animations and sensor awareness.",
        "system_prompt": build_default_system_prompt("Alf"),
    },
    {
        "id": "alf-engineer",
        "name": "Alf — Hardware Lab Assistant",
        "companion_name": "Alf-Lab",
        "default_emotion": "IDLE",
        "tts_voice": "echo",
        "description": "Precision electronics & GPIO bench assistant focused on pin states, Vbat ADC, IMU, and BLE diagnostics.",
        "system_prompt": (
            build_default_system_prompt("Alf-Lab")
            + " Focus on precise hardware diagnostics, GPIO pin states, battery voltage (mV), "
            "IMU accelerometer/gyroscope telemetry, and BLE RSSI measurements."
        ),
    },
    {
        "id": "alf-sentry",
        "name": "Alf — BLE & Motion Sentry",
        "companion_name": "Alf-Sentry",
        "default_emotion": "ALERT",
        "tts_voice": "onyx",
        "description": "Proactive room sentry monitoring BLE device presence, IMU vibration/tamper, and status alerts.",
        "system_prompt": (
            build_default_system_prompt("Alf-Sentry")
            + " Act as a vigilant room & desk sentry. Proactively check BLE radar and IMU motion sensors "
            "when asked about activity, and use ALERT or SURPRISED emotions when anomalies are detected."
        ),
    },
]


class ServerWebGUI:
    """
    Self-contained Open WebUI-style dashboard & OpenAI-compatible bridge attached to ServerCompanionService.
    """

    def __init__(
        self,
        service: Any,
        host: Optional[str] = None,
        port: Optional[int] = None,
        auth_token: Optional[str] = None,
    ) -> None:
        self.service = service
        self.host = (host or os.getenv("WEB_UI_HOST", "127.0.0.1")).strip() or "127.0.0.1"
        self.port = int(port if port is not None else os.getenv("WEB_UI_PORT", "8080"))
        self.auth_token = (
            auth_token if auth_token is not None else os.getenv("WEB_UI_TOKEN", "")
        ).strip()

        self._server: Optional[asyncio.AbstractServer] = None
        self.bound_port: int = self.port

        # Personas / Modelfiles registry
        self.personas: Dict[str, Dict[str, Any]] = {
            p["id"]: dict(p) for p in DEFAULT_PERSONAS
        }
        self.active_persona_id: str = "alf-companion"

        # Live companion state mirror (synchronized with ESP32-S3 + Pi Hub)
        self.face_state: Dict[str, Any] = {
            "emo": "IDLE",
            "sub": "Ready! Speak or chat below.",
            "mouth": 0,
            "envelope": [],
            "updated_at": time.time(),
        }
        self.telemetry: Dict[str, Any] = {
            "gpio_pins": {str(p): {"pin": p, "dir": "in", "val": 0} for p in ALLOWLIST_PINS},
            "vbat_mv": 0,
            "imu": {},
            "ble": [],
            "settings": {"brightness": 100, "volume": 75, "mic_mode": "vad"},
        }
        self.chat_feed: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        self.tool_log: Deque[Dict[str, Any]] = collections.deque(maxlen=60)

        # Connected real-time browser clients (WebSocket writers & SSE queues)
        self._ws_clients: Set[asyncio.StreamWriter] = set()
        self._sse_queues: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self.mcp = AlfMCPServer(self.service, self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> int:
        """Start the async TCP HTTP/WebSocket/SSE server and return the bound port."""
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
        logger.info("Alf Open WebUI Control Center listening on http://%s:%d", self.host, self.bound_port)
        return self.bound_port

    async def stop(self) -> None:
        """Stop the HTTP server and close active WebSocket/SSE streams."""
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
    # Real-Time Event Bus & Telemetry Hooks
    # ------------------------------------------------------------------

    def broadcast_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Push a structured real-time event to all connected WebSocket and SSE browser clients."""
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
        envelope: Optional[List[int]] = None,
    ) -> None:
        if emo:
            self.face_state["emo"] = str(emo).strip().upper()
        if sub is not None:
            self.face_state["sub"] = str(sub)[:180]
        if mouth is not None:
            self.face_state["mouth"] = max(0, min(100, int(mouth)))
        if envelope is not None:
            self.face_state["envelope"] = list(envelope)
        self.face_state["updated_at"] = time.time()
        self.broadcast_event("face_state", dict(self.face_state))

    def record_chat_turn(
        self,
        role: str,
        content: str,
        source: str = "voice",
        emotion: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:10],
            "role": role,
            "content": content,
            "source": source,
            "emotion": emotion or self.face_state.get("emo", "IDLE"),
            "tool_calls": tool_calls or [],
            "ts": time.time(),
        }
        self.chat_feed.append(entry)
        self.broadcast_event("chat_message", entry)
        return entry

    def record_tool_execution(
        self,
        tool: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        duration_ms: int = 0,
    ) -> Dict[str, Any]:
        entry = {
            "id": uuid.uuid4().hex[:8],
            "tool": tool,
            "args": args,
            "result": result,
            "duration_ms": duration_ms,
            "ts": time.time(),
        }
        self.tool_log.append(entry)

        # Update cached hardware telemetry from tool responses
        if tool == "set_companion_emotion" and isinstance(args, dict):
            self.update_face_state(emo=args.get("emotion"), sub=args.get("subtitle"))
        elif tool == "gpio_configure_and_write" and isinstance(args, dict):
            pin_key = str(args.get("pin", ""))
            if pin_key in self.telemetry["gpio_pins"]:
                self.telemetry["gpio_pins"][pin_key] = {
                    "pin": int(args["pin"]),
                    "dir": str(args.get("mode", "out")),
                    "pull": str(args.get("pull", "none")),
                    "val": int(args.get("state", 0)),
                }
        elif tool == "gpio_read_pin" and isinstance(args, dict) and isinstance(result, dict):
            pin_key = str(args.get("pin", ""))
            if pin_key in self.telemetry["gpio_pins"] and "val" in result:
                self.telemetry["gpio_pins"][pin_key]["val"] = int(result.get("val", 0))
            if "vbat_mv" in result:
                self.telemetry["vbat_mv"] = int(result.get("vbat_mv", 0))
        elif tool == "read_imu_motion" and isinstance(result, dict) and result.get("ok") == 1:
            self.telemetry["imu"] = dict(result)
        elif tool == "scan_ble_environment" and isinstance(result, dict) and isinstance(result.get("ble"), list):
            self.telemetry["ble"] = list(result["ble"])
        elif tool == "update_companion_settings" and isinstance(args, dict):
            for k in ("brightness", "volume", "mic_mode"):
                if k in args and args[k] is not None:
                    self.telemetry["settings"][k] = args[k]

        self.broadcast_event("tool_execution", entry)
        return entry

    # ------------------------------------------------------------------
    # Authentication & Programmatic Request Dispatcher
    # ------------------------------------------------------------------

    def _is_request_authorized(self, headers: Dict[str, str], query_params: Dict[str, str]) -> bool:
        if not self.auth_token:
            return True
        auth_hdr = headers.get("authorization", "").strip()
        if auth_hdr.lower().startswith("bearer "):
            cand = auth_hdr[7:].strip()
            if cand == self.auth_token:
                return True
        api_key_hdr = headers.get("x-api-key", "").strip()
        if api_key_hdr and api_key_hdr == self.auth_token:
            return True
        q_tok = query_params.get("token", "").strip()
        if q_tok and q_tok == self.auth_token:
            return True
        return False

    def get_status_snapshot(self) -> Dict[str, Any]:
        voip = getattr(self.service, "voip", None)
        brain = getattr(self.service, "brain", None)
        speech = getattr(self.service, "speech", None)
        return {
            "ok": True,
            "companion_name": getattr(brain, "companion_name", "Alf") if brain else "Alf",
            "active_persona_id": self.active_persona_id,
            "persona": self.personas.get(self.active_persona_id),
            "face": dict(self.face_state),
            "voip": {
                "in_call": bool(getattr(voip, "is_in_call", False)),
                "contact": getattr(voip, "current_contact", None),
                "session_id": (
                    voip.current_session_id.hex()
                    if getattr(voip, "current_session_id", None)
                    else None
                ),
            },
            "pi_address": getattr(self.service, "pi_address", ""),
            "models": {
                "llm_model": getattr(brain, "model", ""),
                "stt_model": getattr(speech, "stt_model", ""),
                "tts_model": getattr(speech, "tts_model", ""),
                "tts_voice": getattr(speech, "tts_voice", ""),
            },
            "telemetry": self.telemetry,
            "allowlist_pins": ALLOWLIST_PINS,
            "valid_emotions": list(VALID_EMOTIONS),
            "chat_feed": list(self.chat_feed),
            "tool_log": list(self.tool_log),
            "history_count": len(getattr(brain, "history", []) or []),
        }

    def get_masked_settings(self) -> Dict[str, Any]:
        brain = getattr(self.service, "brain", None)
        speech = getattr(self.service, "speech", None)
        quixi = getattr(self.service, "quixi", None)
        llm_client = getattr(brain, "client", None)
        stt_client = getattr(speech, "stt_client", None)
        tts_client = getattr(speech, "tts_client", None)
        mqtt_pw = getattr(quixi, "mqtt_password", None) or os.getenv("MQTT_PASSWORD", "")
        return {
            "ok": True,
            "companion_name": getattr(brain, "companion_name", "Alf"),
            "system_prompt": getattr(brain, "system_prompt", ""),
            "pi_ixian_address": getattr(self.service, "pi_address", ""),
            "quixi_rest_url": str(getattr(quixi, "rest_url", "") or os.getenv("QUIXI_REST_URL", "http://127.0.0.1:8001")),
            "mqtt_host": str(getattr(quixi, "mqtt_host", "") or os.getenv("MQTT_HOST", "127.0.0.1")),
            "mqtt_port": int(getattr(quixi, "mqtt_port", 0) or os.getenv("MQTT_PORT", "1883") or 1883),
            "mqtt_username": str(getattr(quixi, "mqtt_username", "") or os.getenv("MQTT_USERNAME", "")),
            "mqtt_password_masked": mask_secret(mqtt_pw),
            "llm_base_url": str(getattr(llm_client, "base_url", "") or os.getenv("LLM_BASE_URL", "")),
            "llm_api_key_masked": mask_secret(
                getattr(llm_client, "api_key", None) or os.getenv("LLM_API_KEY", "")
            ),
            "llm_model": getattr(brain, "model", ""),
            "stt_base_url": str(getattr(stt_client, "base_url", "") or os.getenv("STT_BASE_URL", "")),
            "stt_api_key_masked": mask_secret(
                getattr(stt_client, "api_key", None) or os.getenv("STT_API_KEY", "")
            ),
            "stt_model": getattr(speech, "stt_model", ""),
            "tts_base_url": str(getattr(tts_client, "base_url", "") or os.getenv("TTS_BASE_URL", "")),
            "tts_api_key_masked": mask_secret(
                getattr(tts_client, "api_key", None) or os.getenv("TTS_API_KEY", "")
            ),
            "tts_model": getattr(speech, "tts_model", ""),
            "tts_voice": getattr(speech, "tts_voice", ""),
        }

    def apply_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        brain = getattr(self.service, "brain", None)
        speech = getattr(self.service, "speech", None)
        quixi = getattr(self.service, "quixi", None)
        if "pi_ixian_address" in payload and payload["pi_ixian_address"] is not None:
            self.service.pi_address = str(payload["pi_ixian_address"]).strip()
        if quixi is not None:
            if "quixi_rest_url" in payload and payload["quixi_rest_url"]:
                quixi.rest_url = str(payload["quixi_rest_url"]).strip().rstrip("/")
            if "mqtt_host" in payload and payload["mqtt_host"]:
                quixi.mqtt_host = str(payload["mqtt_host"]).strip()
            if "mqtt_port" in payload and payload["mqtt_port"] not in (None, ""):
                try:
                    quixi.mqtt_port = max(1, min(65535, int(payload["mqtt_port"])))
                except (TypeError, ValueError):
                    pass
            if "mqtt_username" in payload and payload["mqtt_username"] is not None:
                quixi.mqtt_username = str(payload["mqtt_username"]).strip() or None
            if "mqtt_password" in payload and payload["mqtt_password"]:
                raw_mpw = str(payload["mqtt_password"]).strip()
                if "***" not in raw_mpw:
                    quixi.mqtt_password = raw_mpw
        if brain is not None:
            if "llm_model" in payload and payload["llm_model"]:
                brain.model = str(payload["llm_model"]).strip()
            if "companion_name" in payload and payload["companion_name"]:
                brain.companion_name = str(payload["companion_name"]).strip()
            if "system_prompt" in payload and payload["system_prompt"]:
                brain.system_prompt = str(payload["system_prompt"]).strip()
            if "llm_base_url" in payload and payload["llm_base_url"] and hasattr(brain, "client"):
                brain.client.base_url = str(payload["llm_base_url"]).strip()
            if "llm_api_key" in payload and payload["llm_api_key"] and hasattr(brain, "client"):
                raw_k = str(payload["llm_api_key"]).strip()
                if "***" not in raw_k:
                    brain.client.api_key = raw_k
        if speech is not None:
            if "stt_model" in payload and payload["stt_model"]:
                speech.stt_model = str(payload["stt_model"]).strip()
            if "tts_model" in payload and payload["tts_model"]:
                speech.tts_model = str(payload["tts_model"]).strip()
            if "tts_voice" in payload and payload["tts_voice"]:
                speech.tts_voice = str(payload["tts_voice"]).strip()
        self.broadcast_event("settings_updated", self.get_masked_settings())
        return self.get_masked_settings()

    def select_persona(self, persona_id: str) -> Dict[str, Any]:
        pid = (persona_id or "").strip()
        if pid not in self.personas:
            raise KeyError(f"Unknown persona: {pid}")
        p = self.personas[pid]
        self.active_persona_id = pid
        brain = getattr(self.service, "brain", None)
        speech = getattr(self.service, "speech", None)
        if brain is not None:
            brain.companion_name = p.get("companion_name", "Alf")
            brain.system_prompt = p.get("system_prompt", build_default_system_prompt("Alf"))
            brain.last_emotion = p.get("default_emotion", "HAPPY")
        if speech is not None and p.get("tts_voice"):
            speech.tts_voice = str(p["tts_voice"])
        self.update_face_state(emo=p.get("default_emotion", "HAPPY"), sub=f"Persona: {p['name']}")
        return p

    def upsert_persona(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        pid = str(spec.get("id") or "").strip().lower().replace(" ", "-")
        if not pid:
            pid = f"alf-{uuid.uuid4().hex[:6]}"
        name = str(spec.get("name") or pid).strip()
        comp_name = str(spec.get("companion_name") or name.split(" ")[0] or "Alf").strip()
        emo = str(spec.get("default_emotion") or "HAPPY").strip().upper()
        if emo not in VALID_EMOTIONS:
            emo = "HAPPY"
        voice = str(spec.get("tts_voice") or "alloy").strip()
        prompt = str(spec.get("system_prompt") or build_default_system_prompt(comp_name)).strip()
        persona = {
            "id": pid,
            "name": name,
            "companion_name": comp_name,
            "default_emotion": emo,
            "tts_voice": voice,
            "description": str(spec.get("description") or "Custom companion persona.").strip(),
            "system_prompt": prompt,
        }
        self.personas[pid] = persona
        if spec.get("activate", True):
            self.select_persona(pid)
        return persona

    async def handle_api_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Programmatic & HTTP JSON dispatcher for all /api/* and non-streaming /v1/* endpoints.
        Returns (http_status_code, response_dict).
        """
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        parsed = urllib.parse.urlsplit(path)
        route = parsed.path.rstrip("/") or "/"
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if not self._is_request_authorized(hdrs, query):
            return 401, {"ok": False, "error": "Unauthorized: valid WEB_UI_TOKEN required"}

        payload = body if isinstance(body, dict) else {}

        # 1. Status & Live Mirror Snapshot
        if method == "GET" and route == "/api/status":
            return 200, self.get_status_snapshot()

        # 2. Conversation History
        if route == "/api/history":
            brain = getattr(self.service, "brain", None)
            if method == "GET":
                return 200, {
                    "ok": True,
                    "history": list(getattr(brain, "history", []) or []),
                    "chat_feed": list(self.chat_feed),
                }
            if method == "DELETE":
                if brain is not None:
                    brain.reset()
                self.chat_feed.clear()
                self.broadcast_event("history_cleared", {})
                return 200, {"ok": True, "history": []}

        # 3. Personas / Modelfiles Workspace
        if route == "/api/personas":
            if method == "GET":
                return 200, {
                    "ok": True,
                    "active_persona_id": self.active_persona_id,
                    "personas": list(self.personas.values()),
                }
            if method == "POST":
                p = self.upsert_persona(payload)
                return 200, {"ok": True, "persona": p, "active_persona_id": self.active_persona_id}

        if method == "POST" and route == "/api/personas/select":
            pid = str(payload.get("persona_id") or payload.get("id") or "").strip()
            try:
                p = self.select_persona(pid)
                return 200, {"ok": True, "persona": p, "active_persona_id": self.active_persona_id}
            except KeyError as exc:
                return 404, {"ok": False, "error": str(exc)}

        # 4. Dynamic Provider / Model Settings (masked secrets!)
        if route == "/api/settings":
            if method == "GET":
                return 200, self.get_masked_settings()
            if method == "POST":
                return 200, self.apply_settings(payload)

        if method == "GET" and route == "/api/pairing":
            addr = ""
            quixi = getattr(self.service, "quixi", None)
            if quixi is not None and hasattr(quixi, "my_wallet"):
                try:
                    w_res = await quixi.my_wallet()
                    raw_w = w_res.get("result", w_res) if isinstance(w_res, dict) else w_res
                    if isinstance(raw_w, dict) and raw_w:
                        first_key = next(iter(raw_w.keys()))
                        if first_key and first_key not in ("result", "error"):
                            addr = str(first_key).strip()
                    elif isinstance(raw_w, list) and raw_w:
                        addr = str(raw_w[0]).strip()
                    elif isinstance(raw_w, str):
                        addr = raw_w.strip()
                except Exception:
                    pass
            return 200, {
                "ok": True,
                "server_ixian_address": addr,
                "pi_ixian_address": getattr(self.service, "pi_address", ""),
            }

        # 5. Barge-In / Interrupt Active Speech
        if method == "POST" and route == "/api/barge_in":
            target = self.service._resolve_rpc_target() or self.service.pi_address
            cancelled = self.service._cancel_active_tts(target)
            self.update_face_state(emo="IDLE", sub="Interrupted.", mouth=0)
            return 200, {"ok": True, "cancelled": cancelled}

        # 6. Direct Hardware Workbench Tool Execution
        if method == "POST" and route == "/api/hardware/tool":
            tool_name = str(payload.get("tool") or "").strip()
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            if not tool_name:
                return 400, {"ok": False, "error": "Missing 'tool' name"}
            t0 = time.monotonic()
            res = await self.service._call_pi_tool_rpc(tool_name, args)
            dur_ms = int((time.monotonic() - t0) * 1000)
            entry = self.record_tool_execution(tool_name, args, res, duration_ms=dur_ms)
            return 200, {"ok": True, "tool": tool_name, "result": res, "entry": entry}

        # 6b. Remote ESP32-S3 Firmware Check / Reset / Reflash via Pi Hub
        if method == "POST" and route == "/api/hardware/firmware":
            fw_act = str(payload.get("action") or payload.get("firmware_action") or "check").strip().lower()
            rpc_args: Dict[str, Any] = {"firmware_action": fw_act}
            if payload.get("source") or payload.get("firmware_source"):
                rpc_args["firmware_source"] = str(payload.get("source") or payload.get("firmware_source"))
            if payload.get("sha256") or payload.get("firmware_sha256"):
                rpc_args["firmware_sha256"] = str(payload.get("sha256") or payload.get("firmware_sha256"))
            t0 = time.monotonic()
            res = await self.service._call_pi_tool_rpc("update_companion_settings", rpc_args)
            dur_ms = int((time.monotonic() - t0) * 1000)
            entry = self.record_tool_execution("update_companion_settings", rpc_args, res, duration_ms=dur_ms)
            return 200, {"ok": bool(res.get("ok", False)), "action": fw_act, "result": res, "entry": entry}

        # 7. Interactive Web Chat Turn (with optional physical companion speech output)
        if method == "POST" and route == "/api/chat":
            user_text = str(payload.get("message") or payload.get("prompt") or "").strip()
            if not user_text:
                return 400, {"ok": False, "error": "Empty message"}
            if payload.get("persona_id") and str(payload["persona_id"]) in self.personas:
                self.select_persona(str(payload["persona_id"]))
            speak_on_companion = bool(payload.get("speak_on_companion", False))
            res = await self.service.run_web_chat_turn(
                user_text,
                speak_on_companion=speak_on_companion,
                source="web_gui",
            )
            return 200, res

        # 8. OpenAI-Compatible /v1/models (for external Open WebUI connections)
        if method == "GET" and route == "/v1/models":
            now_int = int(time.time())
            models = [
                {
                    "id": p["id"],
                    "object": "model",
                    "created": now_int,
                    "owned_by": "tamagci-alf",
                    "name": p["name"],
                }
                for p in self.personas.values()
            ]
            return 200, {"object": "list", "data": models}

        # 9. OpenAI-Compatible /v1/chat/completions (non-streaming JSON path)
        if method == "POST" and route == "/v1/chat/completions":
            return await self._handle_openai_chat_completion_json(payload, hdrs)

        # 10. Model Context Protocol (MCP) Server Endpoint
        if method == "GET" and route == "/mcp/tools":
            return 200, {"ok": True, "tools": MCP_TOOLS}

        if method == "POST" and route == "/mcp":
            resp = await self.mcp.handle_jsonrpc(payload)
            return 200, resp

        return 404, {"ok": False, "error": f"Not found: {method} {route}"}

    async def _handle_openai_chat_completion_json(
        self, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, Any]]:
        model_id = str(payload.get("model") or self.active_persona_id).strip()
        if model_id in self.personas and model_id != self.active_persona_id:
            self.select_persona(model_id)

        messages = payload.get("messages")
        user_text = ""
        if isinstance(messages, list):
            for m in reversed(messages):
                if isinstance(m, dict) and m.get("role") == "user":
                    content = m.get("content")
                    if isinstance(content, str):
                        user_text = content.strip()
                    elif isinstance(content, list):
                        parts = [
                            str(p.get("text", ""))
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        user_text = " ".join(parts).strip()
                    break
        if not user_text:
            return 400, {"error": {"message": "No user message found in messages[]", "type": "invalid_request_error"}}

        speak = bool(
            payload.get("speak_on_companion", False)
            or headers.get("x-alf-speak", "").strip().lower() in ("1", "true", "yes")
        )
        turn_res = await self.service.run_web_chat_turn(
            user_text, speak_on_companion=speak, source="openai_api"
        )
        reply_text = turn_res["reply"]
        comp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        return 200, {
            "id": comp_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply_text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(user_text.split()),
                "completion_tokens": len(reply_text.split()),
                "total_tokens": len(user_text.split()) + len(reply_text.split()),
            },
            "alf_meta": {
                "emotion": turn_res["emotion"],
                "tool_calls": turn_res["tool_calls"],
            },
        }

    # ------------------------------------------------------------------
    # Raw Asyncio HTTP/1.1 + SSE + RFC 6455 WebSocket Transport
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

            # CORS Preflight
            if method == "OPTIONS":
                await self._write_http_response(writer, 204, b"", "text/plain")
                return

            parsed = urllib.parse.urlsplit(path)
            route = parsed.path.rstrip("/") or "/"
            query = dict(urllib.parse.parse_qsl(parsed.query))

            # RFC 6455 WebSocket Upgrade (/api/ws)
            if route == "/api/ws" and headers.get("upgrade", "").lower() == "websocket":
                if not self._is_request_authorized(headers, query):
                    await self._write_http_response(
                        writer, 401, b'{"error":"Unauthorized"}', "application/json"
                    )
                    return
                await self._serve_websocket(reader, writer, headers)
                return

            # Server-Sent Events (/api/events)
            if method == "GET" and route == "/api/events":
                if not self._is_request_authorized(headers, query):
                    await self._write_http_response(
                        writer, 401, b'{"error":"Unauthorized"}', "application/json"
                    )
                    return
                await self._serve_sse_events(writer)
                return

            # Dashboard Single-Page App (GET /)
            if method == "GET" and route in ("/", "/index.html"):
                html_bytes = DASHBOARD_HTML.encode("utf-8")
                await self._write_http_response(writer, 200, html_bytes, "text/html; charset=utf-8")
                return

            # Read JSON body if Content-Length is present
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

            # OpenAI /v1/chat/completions streaming SSE mode (`stream: true`)
            if (
                method == "POST"
                and route == "/v1/chat/completions"
                and isinstance(body_obj, dict)
                and body_obj.get("stream") is True
            ):
                if not self._is_request_authorized(headers, query):
                    await self._write_http_response(
                        writer, 401, b'{"error":"Unauthorized"}', "application/json"
                    )
                    return
                await self._serve_openai_sse_stream(writer, body_obj, headers)
                return

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
            500: "Internal Server Error",
        }
        reason = reasons.get(status, "OK")
        hdr = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Headers: Authorization, Content-Type, X-API-Key, X-Alf-Speak\r\n"
            "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(hdr + body)
        await writer.drain()

    async def _serve_openai_sse_stream(
        self,
        writer: asyncio.StreamWriter,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> None:
        status_code, res = await self._handle_openai_chat_completion_json(payload, headers)
        if status_code != 200:
            await self._write_http_response(
                writer, status_code, json.dumps(res).encode("utf-8"), "application/json"
            )
            return

        hdr = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(hdr)
        await writer.drain()

        comp_id = res["id"]
        created = res["created"]
        model_id = res["model"]
        reply_text = res["choices"][0]["message"]["content"]

        # Stream role chunk followed by word tokens and [DONE]
        words = reply_text.split(" ")
        for idx, word in enumerate(words):
            token = word if idx == 0 else f" {word}"
            chunk_obj = {
                "id": comp_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": token} if idx == 0 else {"content": token},
                        "finish_reason": None,
                    }
                ],
            }
            writer.write(f"data: {json.dumps(chunk_obj)}\n\n".encode("utf-8"))
            await writer.drain()

        stop_chunk = {
            "id": comp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        writer.write(f"data: {json.dumps(stop_chunk)}\n\ndata: [DONE]\n\n".encode("utf-8"))
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
        # Send immediate initial snapshot
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
                if opcode == 0x8:  # Close
                    break
                if opcode == 0x9:  # Ping -> Pong
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
# Embedded Open WebUI-Inspired Dark-Mode Single-Page Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Alf · Open WebUI Companion Control Center</title>
<style>
  :root {
    --bg-0: #090a0f;
    --bg-1: #11131c;
    --bg-2: #181b28;
    --bg-3: #222638;
    --border: #2a2f45;
    --text-0: #f3f4f8;
    --text-1: #a9b1d6;
    --text-2: #6c7393;
    --accent: #38bdf8;
    --accent-2: #818cf8;
    --green: #34d399;
    --amber: #fbbf24;
    --red: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background: var(--bg-0);
    color: var(--text-0);
    height: 100vh;
    display: grid;
    grid-template-columns: 270px 1fr 340px;
    overflow: hidden;
  }
  aside, main { height: 100vh; overflow-y: auto; }
  .sidebar {
    background: var(--bg-1);
    border-right: 1px solid var(--border);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    font-weight: 700; font-size: 1.08rem; letter-spacing: 0.2px;
  }
  .brand-dot {
    width: 11px; height: 11px; border-radius: 50%;
    background: var(--green); box-shadow: 0 0 10px var(--green);
  }
  .section-lbl {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.9px;
    color: var(--text-2); font-weight: 600; margin-top: 6px;
  }
  .persona-card {
    background: var(--bg-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; cursor: pointer;
    transition: all 0.15s ease;
  }
  .persona-card:hover, .persona-card.active {
    border-color: var(--accent);
    background: rgba(56, 189, 248, 0.08);
  }
  .persona-card h4 { font-size: 0.86rem; margin-bottom: 3px; }
  .persona-card p { font-size: 0.74rem; color: var(--text-1); line-height: 1.35; }
  .btn {
    background: var(--bg-3); color: var(--text-0);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 11px; font-size: 0.8rem; cursor: pointer;
    transition: all 0.15s ease; font-weight: 500;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn-primary {
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: #06080e; border: none; font-weight: 600;
  }
  .btn-primary:hover { opacity: 0.92; color: #06080e; }
  .btn-danger:hover { border-color: var(--red); color: var(--red); }

  /* Center Chat + Live Face Mirror */
  .center-pane {
    display: flex; flex-direction: column; height: 100vh;
    background: var(--bg-0);
  }
  .face-hero {
    background: radial-gradient(circle at 50% 40%, #172038 0%, var(--bg-1) 75%);
    border-bottom: 1px solid var(--border);
    padding: 14px 22px;
    display: flex; align-items: center; gap: 22px;
  }
  #faceCanvas {
    width: 128px; height: 128px; border-radius: 50%;
    background: #04060b;
    border: 3px solid #29324e;
    box-shadow: 0 0 24px rgba(56, 189, 248, 0.18);
    flex-shrink: 0;
  }
  .face-meta { flex: 1; display: flex; flex-direction: column; gap: 8px; }
  .badge-row { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; }
  .badge {
    font-size: 0.72rem; padding: 3px 9px; border-radius: 99px;
    background: var(--bg-2); border: 1px solid var(--border); color: var(--text-1);
  }
  .badge.ok { border-color: rgba(52,211,153,0.45); color: var(--green); }
  .live-sub {
    font-size: 0.95rem; color: var(--text-0);
    background: rgba(17, 19, 28, 0.75); border: 1px solid var(--border);
    border-radius: 10px; padding: 9px 13px; min-height: 38px;
  }
  .emo-pills { display: flex; flex-wrap: wrap; gap: 5px; }
  .emo-pill {
    font-size: 0.68rem; padding: 3px 8px; border-radius: 6px;
    background: var(--bg-2); border: 1px solid var(--border);
    color: var(--text-1); cursor: pointer;
  }
  .emo-pill:hover { border-color: var(--accent); color: var(--accent); }

  /* Chat Feed */
  .chat-feed {
    flex: 1; overflow-y: auto; padding: 18px 24px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .msg {
    max-width: 82%; padding: 11px 15px; border-radius: 13px;
    line-height: 1.48; font-size: 0.9rem;
  }
  .msg.user {
    align-self: flex-end;
    background: linear-gradient(135deg, #1e3a5f, #27325c);
    border: 1px solid #354c7c;
  }
  .msg.assistant {
    align-self: flex-start;
    background: var(--bg-1);
    border: 1px solid var(--border);
  }
  .msg-hdr {
    font-size: 0.7rem; color: var(--text-2); margin-bottom: 4px;
    display: flex; justify-content: space-between; gap: 12px;
  }
  details.tool-card {
    margin-top: 8px; background: var(--bg-2); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 10px; font-size: 0.76rem;
  }
  details.tool-card summary {
    cursor: pointer; color: var(--accent); font-weight: 600;
  }
  details.tool-card pre {
    margin-top: 6px; white-space: pre-wrap; word-break: break-all;
    color: var(--text-1); font-family: ui-monospace, Consolas, monospace;
  }

  /* Composer */
  .composer {
    border-top: 1px solid var(--border); background: var(--bg-1);
    padding: 12px 20px; display: flex; align-items: center; gap: 10px;
  }
  .composer input[type="text"] {
    flex: 1; background: var(--bg-2); border: 1px solid var(--border);
    color: var(--text-0); border-radius: 10px; padding: 10px 14px;
    font-size: 0.9rem; outline: none;
  }
  .composer input[type="text"]:focus { border-color: var(--accent); }
  .toggle-lbl {
    font-size: 0.76rem; color: var(--text-1);
    display: flex; align-items: center; gap: 5px; cursor: pointer;
    user-select: none;
  }

  /* Right Drawer: Hardware Workbench */
  .workbench {
    background: var(--bg-1); border-left: 1px solid var(--border);
    padding: 16px; display: flex; flex-direction: column; gap: 14px;
  }
  .card {
    background: var(--bg-2); border: 1px solid var(--border);
    border-radius: 11px; padding: 12px; display: flex; flex-direction: column; gap: 9px;
  }
  .card h3 { font-size: 0.83rem; color: var(--accent); letter-spacing: 0.3px; }
  .ctrl-row {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 0.78rem; gap: 8px;
  }
  .ctrl-row input[type="range"], .ctrl-row select, .ctrl-row input[type="text"] {
    background: var(--bg-1); border: 1px solid var(--border);
    color: var(--text-0); border-radius: 6px; padding: 4px 7px; font-size: 0.76rem;
  }
  .gpio-grid {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px;
  }
  .gpio-item {
    background: var(--bg-1); border: 1px solid var(--border);
    border-radius: 7px; padding: 6px 8px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 0.74rem;
  }
  .kv-box {
    font-family: ui-monospace, Consolas, monospace; font-size: 0.73rem;
    color: var(--text-1); background: var(--bg-1); padding: 7px;
    border-radius: 7px; border: 1px solid var(--border);
  }
</style>
</head>
<body>
  <!-- Left Sidebar: Personas & OpenAI Bridge Info -->
  <aside class="sidebar">
    <div class="brand">
      <span class="brand-dot" id="wsDot"></span>
      <span>Alf · Control Center</span>
    </div>
    <div class="section-lbl">Workspace Personas</div>
    <div id="personaList" style="display:flex;flex-direction:column;gap:8px;"></div>

    <div class="section-lbl">Model & Runtime Settings</div>
    <div class="card">
      <div class="ctrl-row">
        <span>LLM</span>
        <input type="text" id="cfgLlmModel" style="width:130px" />
      </div>
      <div class="ctrl-row">
        <span>Voice</span>
        <input type="text" id="cfgTtsVoice" style="width:130px" />
      </div>
      <div class="ctrl-row">
        <span>Pi Wallet</span>
        <input type="text" id="cfgPiAddr" style="width:130px" />
      </div>
      <div class="ctrl-row">
        <span>QuIXI URL</span>
        <input type="text" id="cfgQuixiUrl" style="width:130px" />
      </div>
      <div class="ctrl-row">
        <span>MQTT Host</span>
        <input type="text" id="cfgMqttHost" style="width:130px" />
      </div>
      <div class="ctrl-row">
        <span>MQTT Port</span>
        <input type="text" id="cfgMqttPort" style="width:130px" />
      </div>
      <button class="btn btn-primary" onclick="saveRuntimeSettings()">Apply Runtime Config</button>
      <button class="btn" style="margin-top:6px;" onclick="copyServerIxianAddress()">Copy Server Ixian Address</button>
    </div>

    <div class="section-lbl">Open WebUI &amp; MCP Bridge</div>
    <div class="kv-box">
      <div><b>Base URL:</b> /v1</div>
      <div><b>Models:</b> GET /v1/models</div>
      <div><b>Chat:</b> POST /v1/chat/completions</div>
      <div><b>MCP:</b> POST /mcp (22 tools)</div>
    </div>

    <div style="margin-top:auto;display:flex;gap:8px;">
      <button class="btn" style="flex:1" onclick="clearHistory()">Clear Chat</button>
      <button class="btn btn-danger" onclick="triggerBargeIn()">Stop TTS</button>
    </div>
  </aside>

  <!-- Center Pane: Live 240x240 Virtual Face + Chat Stream -->
  <main class="center-pane">
    <section class="face-hero">
      <canvas id="faceCanvas" width="240" height="240"></canvas>
      <div class="face-meta">
        <div class="badge-row">
          <span class="badge ok" id="emoBadge">EMO: IDLE</span>
          <span class="badge" id="voipBadge">VoIP: IDLE</span>
          <span class="badge" id="modelBadge">Model: gpt-4o-mini</span>
          <span class="badge" id="vbatBadge">Vbat: -- mV</span>
        </div>
        <div class="live-sub" id="liveSub">Ready! Speak or chat below.</div>
        <div class="emo-pills" id="emoPills"></div>
      </div>
    </section>

    <section class="chat-feed" id="chatFeed"></section>

    <form class="composer" onsubmit="sendChat(event)">
      <input type="text" id="chatInput" placeholder="Message Alf (can call GPIO, IMU, BLE & face tools)..." autocomplete="off" />
      <label class="toggle-lbl" title="Synthesize TTS and speak aloud on the physical Pi + ESP32">
        <input type="checkbox" id="speakToggle" />
        Speak on Companion
      </label>
      <button type="submit" class="btn btn-primary">Send</button>
    </form>
  </main>

  <!-- Right Drawer: Interactive Hardware Workbench -->
  <aside class="workbench">
    <div class="section-lbl">Companion Valves & Audio</div>
    <div class="card">
      <h3>Display & Microphone Mode</h3>
      <div class="ctrl-row">
        <span>Brightness</span>
        <input type="range" id="hwBright" min="10" max="100" value="100" onchange="pushCompanionSettings()" />
      </div>
      <div class="ctrl-row">
        <span>Speaker Vol</span>
        <input type="range" id="hwVol" min="0" max="100" value="75" onchange="pushCompanionSettings()" />
      </div>
      <div class="ctrl-row">
        <span>Mic Mode</span>
        <select id="hwMicMode" onchange="pushCompanionSettings()">
          <option value="vad">Auto VAD</option>
          <option value="ptt">Tap PTT</option>
          <option value="duplex">Full Duplex</option>
          <option value="wake">Wake Word</option>
        </select>
      </div>
    </div>

    <div class="card">
      <div class="ctrl-row">
        <h3>Allow-Listed GPIO Matrix</h3>
        <button class="btn" onclick="readPin(16)">Read Pin 16 & Vbat</button>
      </div>
      <div class="gpio-grid" id="gpioGrid"></div>
    </div>

    <div class="card">
      <div class="ctrl-row">
        <h3>QMI8658 6-Axis IMU</h3>
        <button class="btn" onclick="pollImu()">Poll IMU</button>
      </div>
      <div class="kv-box" id="imuBox">Click "Poll IMU" to read 3-axis accel/gyro & temp.</div>
    </div>

    <div class="card">
      <div class="ctrl-row">
        <h3>BLE Environment Radar</h3>
        <button class="btn" onclick="scanBle()">Scan BLE</button>
      </div>
      <div class="kv-box" id="bleBox">Click "Scan BLE" to list nearby advertisers.</div>
    </div>

    <div class="card">
      <div class="ctrl-row">
        <h3>ESP32-S3 LCD Firmware</h3>
        <button class="btn" onclick="remoteFirmwareAction('check')">Check</button>
      </div>
      <div class="ctrl-row">
        <button class="btn btn-primary" style="flex:1" onclick="remoteFirmwareAction('flash')">Reflash ESP32-S3</button>
        <button class="btn" onclick="remoteFirmwareAction('reset')">Reset</button>
      </div>
      <div class="kv-box" id="fwBox">Remote firmware check, reset, or USB esptool reflash via Pi Hub.</div>
    </div>
  </aside>

<script>
  const EMOTIONS = ["IDLE","LISTENING","THINKING","SPEAKING","HAPPY","SAD","SURPRISED","SLEEPING","ALERT"];
  const PINS = [15,16,17,18,21,33,34,35,36,37];
  let faceState = { emo: "IDLE", sub: "Ready!", mouth: 0, envelope: [] };
  let pinStates = {};
  let blinkPhase = 0;

  function initUI() {
    const pills = document.getElementById("emoPills");
    EMOTIONS.forEach(e => {
      const b = document.createElement("button");
      b.className = "emo-pill";
      b.textContent = e;
      b.onclick = () => invokeTool("set_companion_emotion", { emotion: e, subtitle: "Emotion: " + e });
      pills.appendChild(b);
    });

    const grid = document.getElementById("gpioGrid");
    PINS.forEach(pin => {
      pinStates[pin] = 0;
      const d = document.createElement("div");
      d.className = "gpio-item";
      d.innerHTML = `<span>GPIO ${pin}</span>
        <button class="btn" id="pinBtn_${pin}" onclick="togglePin(${pin})">LOW</button>`;
      grid.appendChild(d);
    });

    fetchStatus();
    connectWebSocket();
    requestAnimationFrame(drawFaceLoop);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts
    });
    return await res.json();
  }

  async function fetchStatus() {
    const s = await api("/api/status");
    if (!s.ok) return;
    applySnapshot(s);
    const cfg = await api("/api/settings");
    if (cfg.ok) {
      document.getElementById("cfgLlmModel").value = cfg.llm_model || "";
      document.getElementById("cfgTtsVoice").value = cfg.tts_voice || "";
      document.getElementById("cfgPiAddr").value = cfg.pi_ixian_address || "";
      document.getElementById("cfgQuixiUrl").value = cfg.quixi_rest_url || "http://127.0.0.1:8001";
      document.getElementById("cfgMqttHost").value = cfg.mqtt_host || "127.0.0.1";
      document.getElementById("cfgMqttPort").value = cfg.mqtt_port || 1883;
    }
  }

  function applySnapshot(s) {
    if (s.face) {
      faceState = { ...faceState, ...s.face };
      updateFaceBadges();
    }
    if (s.voip) {
      document.getElementById("voipBadge").textContent = "VoIP: " + (s.voip.in_call ? "IN_CALL" : "IDLE");
    }
    if (s.models) {
      document.getElementById("modelBadge").textContent = "Model: " + (s.models.llm_model || "--");
    }
    if (s.telemetry && s.telemetry.vbat_mv) {
      document.getElementById("vbatBadge").textContent = "Vbat: " + s.telemetry.vbat_mv + " mV";
    }
    renderPersonas(s.active_persona_id);
    if (Array.isArray(s.chat_feed)) {
      const feed = document.getElementById("chatFeed");
      feed.innerHTML = "";
      s.chat_feed.forEach(appendChatMessage);
    }
  }

  async function renderPersonas(activeId) {
    const res = await api("/api/personas");
    if (!res.ok) return;
    const box = document.getElementById("personaList");
    box.innerHTML = "";
    res.personas.forEach(p => {
      const div = document.createElement("div");
      div.className = "persona-card" + (p.id === (activeId || res.active_persona_id) ? " active" : "");
      div.innerHTML = `<h4>${p.name}</h4><p>${p.description || ""}</p>`;
      div.onclick = async () => {
        await api("/api/personas/select", { method: "POST", body: JSON.stringify({ persona_id: p.id }) });
        fetchStatus();
      };
      box.appendChild(div);
    });
  }

  function updateFaceBadges() {
    document.getElementById("emoBadge").textContent = "EMO: " + (faceState.emo || "IDLE");
    document.getElementById("liveSub").textContent = faceState.sub || "";
  }

  function appendChatMessage(entry) {
    const feed = document.getElementById("chatFeed");
    const div = document.createElement("div");
    div.className = "msg " + (entry.role === "user" ? "user" : "assistant");
    let toolsHtml = "";
    if (Array.isArray(entry.tool_calls) && entry.tool_calls.length > 0) {
      toolsHtml = entry.tool_calls.map(tc => `
        <details class="tool-card">
          <summary>⚡ ${tc.tool}(${JSON.stringify(tc.args || {})})</summary>
          <pre>${JSON.stringify(tc.result || {}, null, 2)}</pre>
        </details>
      `).join("");
    }
    div.innerHTML = `
      <div class="msg-hdr">
        <span>${entry.role.toUpperCase()} · ${entry.source || "chat"}</span>
        <span>${entry.emotion || ""}</span>
      </div>
      <div>${(entry.content || "").replace(/</g, "&lt;")}</div>
      ${toolsHtml}
    `;
    feed.appendChild(div);
    feed.scrollTop = feed.scrollHeight;
  }

  async function sendChat(ev) {
    ev.preventDefault();
    const inp = document.getElementById("chatInput");
    const text = inp.value.trim();
    if (!text) return;
    inp.value = "";
    const speak = document.getElementById("speakToggle").checked;
    await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, speak_on_companion: speak })
    });
  }

  async function invokeTool(tool, args) {
    const res = await api("/api/hardware/tool", {
      method: "POST",
      body: JSON.stringify({ tool, args })
    });
    return res.result || {};
  }

  async function togglePin(pin) {
    const nextVal = pinStates[pin] ? 0 : 1;
    pinStates[pin] = nextVal;
    const btn = document.getElementById("pinBtn_" + pin);
    btn.textContent = nextVal ? "HIGH" : "LOW";
    await invokeTool("gpio_configure_and_write", { pin, mode: "out", pull: "none", state: nextVal });
  }

  async function readPin(pin) {
    const r = await invokeTool("gpio_read_pin", { pin });
    if (r.vbat_mv) {
      document.getElementById("vbatBadge").textContent = "Vbat: " + r.vbat_mv + " mV";
    }
  }

  async function pollImu() {
    const r = await invokeTool("read_imu_motion", {});
    document.getElementById("imuBox").textContent = JSON.stringify(r, null, 2);
  }

  async function scanBle() {
    const r = await invokeTool("scan_ble_environment", {});
    document.getElementById("bleBox").textContent = JSON.stringify(r.ble || r, null, 2);
  }

  async function remoteFirmwareAction(action) {
    document.getElementById("fwBox").textContent = "Running remote ESP32 firmware action: " + action + "...";
    const r = await api("/api/hardware/firmware", {
      method: "POST",
      body: JSON.stringify({ action })
    });
    document.getElementById("fwBox").textContent = JSON.stringify(r.result || r, null, 2);
  }

  async function pushCompanionSettings() {
    const brightness = parseInt(document.getElementById("hwBright").value, 10);
    const volume = parseInt(document.getElementById("hwVol").value, 10);
    const mic_mode = document.getElementById("hwMicMode").value;
    await invokeTool("update_companion_settings", { brightness, volume, mic_mode });
  }

  async function saveRuntimeSettings() {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        llm_model: document.getElementById("cfgLlmModel").value,
        tts_voice: document.getElementById("cfgTtsVoice").value,
        pi_ixian_address: document.getElementById("cfgPiAddr").value,
        quixi_rest_url: document.getElementById("cfgQuixiUrl").value,
        mqtt_host: document.getElementById("cfgMqttHost").value,
        mqtt_port: parseInt(document.getElementById("cfgMqttPort").value || "1883", 10)
      })
    });
    fetchStatus();
  }

  async function copyServerIxianAddress() {
    const r = await api("/api/pairing");
    const addr = (r.server_ixian_address || "").trim();
    if (!addr) {
      document.getElementById("toolOut").textContent = "Server QuIXI wallet address unavailable (is QuIXI running?).";
      return;
    }
    try {
      await navigator.clipboard.writeText(addr);
      document.getElementById("toolOut").textContent = `Copied Server Ixian Address:\n${addr}`;
    } catch {
      window.prompt("Copy Server Ixian Address:", addr);
    }
  }

  async function clearHistory() {
    await api("/api/history", { method: "DELETE" });
    document.getElementById("chatFeed").innerHTML = "";
  }

  async function triggerBargeIn() {
    await api("/api/barge_in", { method: "POST" });
  }

  function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/ws`);
    const dot = document.getElementById("wsDot");
    ws.onopen = () => { dot.style.background = "#34d399"; };
    ws.onclose = () => {
      dot.style.background = "#f87171";
      setTimeout(connectWebSocket, 2500);
    };
    ws.onmessage = (ev) => {
      try {
        const pkt = JSON.parse(ev.data);
        if (pkt.type === "init") applySnapshot(pkt.data);
        else if (pkt.type === "face_state") {
          faceState = { ...faceState, ...pkt.data };
          updateFaceBadges();
          if (Array.isArray(pkt.data.envelope) && pkt.data.envelope.length > 0) {
            playEnvelopeAnimation(pkt.data.envelope);
          }
        } else if (pkt.type === "chat_message") {
          appendChatMessage(pkt.data);
        }
      } catch (e) {}
    };
  }

  function playEnvelopeAnimation(env) {
    let i = 0;
    const timer = setInterval(() => {
      if (i >= env.length) {
        faceState.mouth = 0;
        clearInterval(timer);
        return;
      }
      faceState.mouth = env[i++];
    }, 50);
  }

  function drawFaceLoop() {
    const c = document.getElementById("faceCanvas");
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, 240, 240);

    // Outer ring glow
    const emo = faceState.emo || "IDLE";
    const colorMap = {
      IDLE: "#38bdf8", LISTENING: "#34d399", THINKING: "#a78bfa",
      SPEAKING: "#38bdf8", HAPPY: "#34d399", SAD: "#60a5fa",
      SURPRISED: "#fbbf24", SLEEPING: "#64748b", ALERT: "#f87171"
    };
    const col = colorMap[emo] || "#38bdf8";

    ctx.strokeStyle = col;
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.arc(120, 120, 114, 0, Math.PI * 2);
    ctx.stroke();

    // Eyes
    blinkPhase = (blinkPhase + 1) % 180;
    const eyeH = (emo === "SLEEPING" || blinkPhase > 173) ? 4 : (emo === "SURPRISED" ? 32 : 24);
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.roundRect(64, 88 - eyeH / 2, 26, eyeH, 10);
    ctx.roundRect(150, 88 - eyeH / 2, 26, eyeH, 10);
    ctx.fill();

    // Mouth (viseme lip-sync)
    const mouthOpen = Math.max(4, (faceState.mouth || 0) * 0.34);
    ctx.beginPath();
    if (emo === "HAPPY") {
      ctx.arc(120, 148, 28, 0, Math.PI, false);
      ctx.stroke();
    } else if (emo === "SAD") {
      ctx.arc(120, 168, 26, Math.PI, 0, false);
      ctx.stroke();
    } else {
      ctx.roundRect(92, 152 - mouthOpen / 2, 56, mouthOpen, 12);
      ctx.fill();
    }
    requestAnimationFrame(drawFaceLoop);
  }

  window.addEventListener("DOMContentLoaded", initUI);
</script>
</body>
</html>
"""

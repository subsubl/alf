"""
Model Context Protocol (MCP) Server for the Alf AI Companion (Tier 3):
Exposes EVERY hardware, LCD media, firmware, Spixi P2P, persona, chat, and MQTT/server
function as standard MCP tools (`2024-11-05` JSON-RPC 2.0):
  - Embedded HTTP MCP endpoint (`POST /mcp`, `GET /mcp/tools`) inside `ServerWebGUI` (:8080)
  - Standalone stdio MCP transport (`python -m server_agent.mcp_server`)

Zero-Trust compliant: runs strictly on Tier 3 (AI Server); all hardware/Spixi calls are
authenticated over Ixian P2P to the paired Raspberry Pi Hub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from server_agent.agent_router import AGENT_TOOLS, ALLOWLIST_PINS, VALID_EMOTIONS

logger = logging.getLogger("server_agent.mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_INFO = {"name": "alf-companion-mcp", "version": "0.4.2"}


def _agent_tool_schema(name: str) -> Dict[str, Any]:
    for t in AGENT_TOOLS:
        fn = t.get("function") or {}
        if fn.get("name") == name:
            return dict(fn.get("parameters") or {"type": "object", "properties": {}})
    return {"type": "object", "properties": {}}


MCP_TOOLS: List[Dict[str, Any]] = [
    # --- 1. ESP32-S3 LCD Face, Full-Screen Image & Video Streaming ---
    {
        "name": "set_companion_emotion",
        "description": "Set the ESP32-S3 animated face expression and subtitle, or control full-screen image/video display.",
        "inputSchema": _agent_tool_schema("set_companion_emotion"),
    },
    {
        "name": "display_lcd_image",
        "description": "Display a full-screen image (HTTP URL, data URI, base64 PNG/JPEG/PPM/RGB565, or file path) on the ESP32-S3 240x240 round LCD.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "HTTP/HTTPS URL or data URI of the image."},
                "image_b64": {"type": "string", "description": "Base64-encoded PNG/JPEG/PPM/RGB565 image bytes."},
                "media_path": {"type": "string", "description": "Optional Pi-local image file path."},
            },
        },
    },
    {
        "name": "stream_lcd_video",
        "description": "Stream a live video (MP4, RTSP, HTTP MJPEG, or animated GIF) full-screen onto the ESP32-S3 240x240 round LCD.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_url": {"type": "string", "description": "Video/MJPEG/GIF URL or Pi-local path."},
                "fps": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "duration_s": {"type": "number", "minimum": 0.5, "maximum": 60.0, "default": 10.0},
            },
            "required": ["video_url"],
        },
    },
    {
        "name": "clear_lcd_media",
        "description": "Stop any active video stream and clear the ESP32-S3 full-screen media overlay back to the animated face.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- 2. ESP32-S3 Hardware GPIO, BLE & IMU Sensors ---
    {
        "name": "gpio_configure_and_write",
        "description": f"Configure and write an allow-listed ESP32-S3 GPIO pin ({ALLOWLIST_PINS}).",
        "inputSchema": _agent_tool_schema("gpio_configure_and_write"),
    },
    {
        "name": "gpio_read_pin",
        "description": f"Read digital input state of an allow-listed ESP32-S3 GPIO pin ({ALLOWLIST_PINS}) and Vbat ADC voltage (mV).",
        "inputSchema": _agent_tool_schema("gpio_read_pin"),
    },
    {
        "name": "scan_ble_environment",
        "description": "Scan nearby Bluetooth Low Energy devices via the ESP32-S3 BLE passive observer.",
        "inputSchema": _agent_tool_schema("scan_ble_environment"),
    },
    {
        "name": "read_imu_motion",
        "description": "Read the ESP32-S3 QMI8658 6-axis IMU (accel, gyro, temp, gesture, orientation angle).",
        "inputSchema": _agent_tool_schema("read_imu_motion"),
    },
    # --- 3. Companion Settings & Live USB Firmware Updater ---
    {
        "name": "update_companion_settings",
        "description": "Update ESP32/Pi brightness, speaker volume, mic mode (vad/ptt/duplex/wake), BLE toggle, or sleep timeout.",
        "inputSchema": _agent_tool_schema("update_companion_settings"),
    },
    {
        "name": "check_esp32_firmware",
        "description": "Inspect staged zephyr.bin ESP32-S3 firmware SHA-256 and esptool readiness on the Raspberry Pi.",
        "inputSchema": {
            "type": "object",
            "properties": {"firmware_source": {"type": "string"}},
        },
    },
    {
        "name": "flash_esp32_firmware",
        "description": "Safely pause serial bridge and reflash the ESP32-S3 LCD over USB from the Raspberry Pi using esptool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "firmware_source": {"type": "string"},
                "firmware_sha256": {"type": "string"},
            },
        },
    },
    {
        "name": "reset_esp32_firmware",
        "description": "Perform a hardware DTR/RTS reset on the ESP32-S3 over USB.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- 4. Spixi Friends, Direct Voice Calls & Dictated Messaging ---
    {
        "name": "list_spixi_contacts",
        "description": "List saved Spixi friend contacts (address and nickname) on the Raspberry Pi QuIXI node.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_spixi_contact",
        "description": "Add a trusted Spixi friend contact with an optional nickname on the Raspberry Pi QuIXI node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Ixian wallet address of the Spixi friend."},
                "nickname": {"type": "string", "description": "Friendly nickname (e.g. 'Alice')."},
            },
            "required": ["address"],
        },
    },
    {
        "name": "call_spixi_contact",
        "description": "Start a Spixi VoIP voice call from the Raspberry Pi to a Spixi friend by nickname or Ixian address.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_name": {"type": "string", "description": "Nickname or Ixian address (e.g. 'Alice')."},
            },
            "required": ["contact_name"],
        },
    },
    {
        "name": "hangup_spixi_call",
        "description": "End the currently active Spixi VoIP call on the Raspberry Pi.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_spixi_message",
        "description": "Send an encrypted Spixi text message from the Raspberry Pi to a named Spixi friend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_name": {"type": "string", "description": "Nickname or Ixian address (e.g. 'Alice')."},
                "message": {"type": "string", "description": "Message text to send."},
            },
            "required": ["contact_name", "message"],
        },
    },
    # --- 5. Server Chat, Personas, Status & MQTT/Runtime Config ---
    {
        "name": "chat_with_companion",
        "description": "Send a text prompt through the server AgentBrain LLM (with hardware tool calling and optional TTS playback on the companion).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "speak_on_companion": {"type": "boolean", "default": False},
            },
            "required": ["message"],
        },
    },
    {
        "name": "list_personas",
        "description": "List available companion workspace personas and the active persona ID.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "select_persona",
        "description": "Switch the active companion persona (e.g. 'alf-companion', 'alf-engineer', 'alf-sentry').",
        "inputSchema": {
            "type": "object",
            "properties": {"persona_id": {"type": "string"}},
            "required": ["persona_id"],
        },
    },
    {
        "name": "get_companion_status",
        "description": "Return live companion status snapshot (face emotion, VoIP state, telemetry, model, Pi address).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_server_settings",
        "description": "Update server runtime settings (LLM model, TTS voice, Pi Ixian address, QuIXI REST URL, MQTT host/port).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "llm_model": {"type": "string"},
                "tts_voice": {"type": "string"},
                "pi_ixian_address": {"type": "string"},
                "quixi_rest_url": {"type": "string"},
                "mqtt_host": {"type": "string"},
                "mqtt_port": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
        },
    },
]

_MCP_TOOL_NAMES = frozenset(t["name"] for t in MCP_TOOLS)


class AlfMCPServer:
    """JSON-RPC 2.0 Model Context Protocol server dispatching to ServerCompanionService & ServerWebGUI."""

    def __init__(self, service: Any, gui: Any = None) -> None:
        self.service = service
        self.gui = gui

    async def _rpc_to_pi(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.monotonic()
        res = await self.service._call_pi_tool_rpc(tool_name, args)
        dur_ms = int((time.monotonic() - t0) * 1000)
        if self.gui is not None and hasattr(self.gui, "record_tool_execution"):
            self.gui.record_tool_execution(tool_name, args, res, duration_ms=dur_ms)
        return res

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = dict(arguments) if isinstance(arguments, dict) else {}
        tool = (name or "").strip()
        if tool not in _MCP_TOOL_NAMES:
            raise KeyError(f"Unknown MCP tool: {tool}")

        # 1. Direct Pi Hub RPC tools
        if tool in (
            "set_companion_emotion",
            "gpio_configure_and_write",
            "gpio_read_pin",
            "scan_ble_environment",
            "read_imu_motion",
            "update_companion_settings",
        ):
            return await self._rpc_to_pi(tool, args)

        # 2. LCD Media convenience tools
        if tool == "display_lcd_image":
            return await self._rpc_to_pi(
                "set_companion_emotion",
                {
                    "emotion": "HAPPY",
                    "display_mode": "image",
                    **{k: args[k] for k in ("image_url", "image_b64", "media_path") if k in args},
                },
            )
        if tool == "stream_lcd_video":
            return await self._rpc_to_pi(
                "set_companion_emotion",
                {
                    "emotion": "HAPPY",
                    "display_mode": "video",
                    "video_url": args.get("video_url", ""),
                    "fps": args.get("fps", 5),
                    "duration_s": args.get("duration_s", 10.0),
                },
            )
        if tool == "clear_lcd_media":
            return await self._rpc_to_pi(
                "set_companion_emotion", {"emotion": "IDLE", "display_mode": "clear"}
            )

        # 3. Firmware convenience tools
        if tool == "check_esp32_firmware":
            return await self._rpc_to_pi(
                "update_companion_settings",
                {"firmware_action": "check", **{k: args[k] for k in ("firmware_source",) if k in args}},
            )
        if tool == "flash_esp32_firmware":
            return await self._rpc_to_pi(
                "update_companion_settings",
                {
                    "firmware_action": "flash",
                    **{k: args[k] for k in ("firmware_source", "firmware_sha256") if k in args},
                },
            )
        if tool == "reset_esp32_firmware":
            return await self._rpc_to_pi("update_companion_settings", {"firmware_action": "reset"})

        # 4. Spixi Friends, Calls & Dictated Messaging convenience tools
        if tool == "list_spixi_contacts":
            return await self._rpc_to_pi("update_companion_settings", {"spixi_action": "list_contacts"})
        if tool == "add_spixi_contact":
            return await self._rpc_to_pi(
                "update_companion_settings",
                {
                    "spixi_action": "add_contact",
                    "address": args.get("address", ""),
                    "nickname": args.get("nickname", ""),
                },
            )
        if tool == "call_spixi_contact":
            return await self._rpc_to_pi(
                "update_companion_settings",
                {"spixi_action": "call", "contact_name": args.get("contact_name", "")},
            )
        if tool == "hangup_spixi_call":
            return await self._rpc_to_pi("update_companion_settings", {"spixi_action": "hangup"})
        if tool == "send_spixi_message":
            return await self._rpc_to_pi(
                "update_companion_settings",
                {
                    "spixi_action": "message",
                    "contact_name": args.get("contact_name", ""),
                    "spixi_message": args.get("message", args.get("spixi_message", "")),
                },
            )

        # 5. Server-side Chat, Personas, Status & Settings tools
        if tool == "chat_with_companion":
            msg = str(args.get("message") or "").strip()
            if not msg:
                return {"ok": False, "error": "Missing 'message'"}
            speak = bool(args.get("speak_on_companion", False))
            return await self.service.run_web_chat_turn(msg, speak_on_companion=speak, source="mcp")

        if tool == "list_personas":
            if self.gui is None:
                return {"ok": False, "error": "GUI workspace not attached"}
            return {
                "ok": True,
                "active_persona_id": self.gui.active_persona_id,
                "personas": list(self.gui.personas.values()),
            }

        if tool == "select_persona":
            if self.gui is None:
                return {"ok": False, "error": "GUI workspace not attached"}
            p = self.gui.select_persona(str(args.get("persona_id") or ""))
            return {"ok": True, "persona": p}

        if tool == "get_companion_status":
            if self.gui is not None:
                return self.gui.get_status_snapshot()
            return {"ok": True, "pi_address": getattr(self.service, "pi_address", "")}

        if tool == "update_server_settings":
            if self.gui is None:
                return {"ok": False, "error": "GUI workspace not attached"}
            return self.gui.apply_settings(args)

        return {"ok": False, "error": f"Unhandled tool: {tool}"}

    async def handle_jsonrpc(self, req: Dict[str, Any]) -> Dict[str, Any]:
        req_id = req.get("id")
        method = str(req.get("method") or "").strip()
        params = req.get("params") if isinstance(req.get("params"), dict) else {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": MCP_SERVER_INFO,
                },
            }

        if method in ("notifications/initialized", "initialized"):
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}

        if method == "tools/call":
            tool_name = str(params.get("name") or "").strip()
            tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                res = await self.call_tool(tool_name, tool_args)
                is_err = isinstance(res, dict) and res.get("ok") is False
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(res, default=str)}],
                        "structuredContent": res,
                        "isError": is_err,
                    },
                }
            except KeyError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": str(exc)},
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


async def run_stdio_server() -> None:
    """Run standalone stdio MCP server backed by ServerCompanionService."""
    from server_agent.main_server import ServerCompanionService

    svc = ServerCompanionService()
    mcp = AlfMCPServer(svc, getattr(svc, "gui", None))
    await svc.quixi.start()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            raw = line.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as exc:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
                continue
            resp = await mcp.handle_jsonrpc(req)
            if req.get("id") is not None:
                sys.stdout.write(json.dumps(resp, default=str) + "\n")
                sys.stdout.flush()
    finally:
        await svc.quixi.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_stdio_server())

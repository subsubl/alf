"""
Raspberry Pi Zero 2 W Companion Hub:
- Bridges USB Serial NDJSON to Waveshare ESP32-S3 ("face", touch, IMU, BLE, GPIOs)
- Runs local Spixi VoIPManager.cs client over QuIXI (REST + MQTT)
- Streams Bluetooth mic audio to AI Server and plays synced TTS audio + subtitles/visemes
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common_ixian import QuIXIClient, SpixiVoIPSessionManager
from common_ixian.quixi_client import _extract_b64_bytes
from pi_hub.audio_sync_engine import AudioSyncEngine
from pi_hub.esp_serial_bridge import EspSerialBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pi_hub")

AGENT_PROTOCOL_ID = "com.tamagci.agent"


class PiCompanionHub:
    """Main orchestrator daemon running on Raspberry Pi Zero 2 W."""

    def __init__(self, config_path: Path) -> None:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self.server_address: str = cfg["quixi"]["server_ixian_address"].strip()
        ser_cfg = cfg["serial"]
        q_cfg = cfg["quixi"]
        aud_cfg = cfg["audio"]

        self.esp = EspSerialBridge(
            port=ser_cfg["port"],
            baudrate=int(ser_cfg.get("baudrate", 115200)),
        )
        self.quixi = QuIXIClient(
            rest_url=q_cfg["rest_url"],
            mqtt_host=q_cfg["mqtt_host"],
            mqtt_port=int(q_cfg.get("mqtt_port", 1883)),
        )
        self.audio = AudioSyncEngine(
            sample_rate=int(aud_cfg.get("sample_rate", 16000)),
            frame_ms=int(aud_cfg.get("frame_ms", 100)),
            vad_aggressiveness=int(aud_cfg.get("vad_aggressiveness", 2)),
            vad_silence_ms=int(aud_cfg.get("vad_silence_ms", 900)),
            bt_latency_ms=int(aud_cfg.get("bt_latency_ms", 150)),
            default_mic_mode=str(aud_cfg.get("default_mic_mode", "vad")),
            input_device=aud_cfg.get("input_device"),
            output_device=aud_cfg.get("output_device"),
            send_audio_frame=self._send_mic_voip_frame,
            on_speech_state=self._on_speech_capture_state,
            on_viseme=self._on_viseme_frame,
            on_playback_done=self._on_playback_done,
        )
        self.voip = SpixiVoIPSessionManager(
            send_spixi_fn=self.quixi.send_spixi_message,
            supported_codecs=("pcm", "opus"),
            on_audio_data=self._on_voip_rx_audio,
            on_session_started=self._on_voip_started,
            on_session_ended=self._on_voip_ended,
        )
        self.quixi.attach_voip_manager(self.voip)
        self._last_ping_ts = 0.0

    async def start(self) -> None:
        self.esp.set_event_handler(self._on_esp_event)
        self.quixi.on_topic("AppProtocolData", self._on_app_protocol_data)
        self.quixi.on_topic("ChatStream", self._on_chat_stream)

        await self.esp.start()
        await self.quixi.start()
        await self.audio.start()

        await self.esp.set_debug(quixi="ONLINE", voip="DIALING", bt_spk="OK")
        await self.esp.set_state("IDLE", "Connecting to AI Server...")

        # Initiate persistent spixi.voip call with the AI Server via QuIXI
        if self.server_address and not self.server_address.startswith("1Replace"):
            try:
                await self.voip.initiate_call(self.server_address)
            except Exception as exc:
                logger.warning("Initial VoIP call initiation failed: %s", exc)

        try:
            while True:
                # Re-attempt call setup if not currently connected
                if (
                    not self.voip.is_in_call
                    and self.server_address
                    and not self.server_address.startswith("1Replace")
                ):
                    try:
                        await self.voip.initiate_call(self.server_address)
                    except Exception:
                        pass
                await asyncio.sleep(10.0)
        finally:
            await self.audio.stop()
            await self.quixi.stop()
            await self.esp.stop()

    async def _on_voip_started(self, contact: str, sid: bytes, codec: str) -> None:
        await self.esp.set_debug(quixi="ONLINE", voip="IN_CALL", bt_spk="OK")
        await self.esp.set_state("IDLE", "Ready! Speak or tap face.")

    async def _on_voip_ended(self, contact: str, sid: bytes, codec: str) -> None:
        await self.esp.set_debug(quixi="ONLINE", voip="IDLE")

    async def _send_mic_voip_frame(self, frame_bytes: bytes) -> bool:
        if self.voip.is_in_call:
            return await self.voip.send_audio_frame(frame_bytes)
        return False

    async def _on_speech_capture_state(self, active: bool) -> None:
        if active:
            await self.esp.set_state("LISTENING", "Listening...", mic=1)
        else:
            await self.esp.set_state("THINKING", "Thinking...", mic=0)
            if self.server_address:
                await self.quixi.send_app_data(
                    self.server_address,
                    json.dumps({"kind": "end_of_turn"}),
                    protocol_id=AGENT_PROTOCOL_ID,
                )

    async def _on_viseme_frame(self, value: int) -> None:
        await self.esp.set_mouth(value)

    async def _on_playback_done(self) -> None:
        await self.esp.set_state("IDLE")

    async def _on_voip_rx_audio(self, session_id: bytes, data: bytes, codec: str) -> None:
        """VoIPManager.onData equivalent: write incoming TTS audio to BT speaker."""
        self.audio.feed_tts_audio(data)

    async def _on_esp_event(self, evt: Dict[str, Any]) -> None:
        src = evt.get("src")
        act = evt.get("act")

        if src == "touch":
            if act == "ptt_toggle":
                await self.audio.toggle_ptt()
            elif act == "stop_speech":
                self.audio.stop_playback()
                await self.esp.set_state("IDLE", "Interrupted.")
        elif src == "cfg":
            if "mode" in evt:
                self.audio.set_mic_mode(str(evt["mode"]))
            if "vol" in evt:
                self.audio.set_volume(int(evt["vol"]))
        elif src == "dbg" and act == "ping":
            self._last_ping_ts = time.monotonic()
            if self.server_address:
                await self.quixi.send_app_data(
                    self.server_address,
                    json.dumps({"kind": "hw_event", "event": {"src": "dbg", "act": "ping", "ts": time.time()}}),
                    protocol_id=AGENT_PROTOCOL_ID,
                )
        elif src == "imu" and evt.get("gest") == "shake":
            if self.server_address:
                await self.quixi.send_app_data(
                    self.server_address,
                    json.dumps({"kind": "hw_event", "event": evt}),
                    protocol_id=AGENT_PROTOCOL_ID,
                )

    async def _on_chat_stream(self, topic: str, payload: Dict[str, Any]) -> None:
        """Display real-time streaming text chunks from QuIXI ChatStream."""
        try:
            raw = _extract_b64_bytes(payload)
            text = raw.decode("utf-8", errors="ignore").strip()
            if text:
                await self.esp.set_state("SPEAKING", text)
        except Exception:
            pass

    async def _on_app_protocol_data(self, topic: str, payload: Dict[str, Any]) -> None:
        sender = str(payload.get("sender", ""))
        try:
            raw = _extract_b64_bytes(payload)
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return

        kind = msg.get("kind")

        if kind == "tool_call":
            await self._execute_agent_tool(sender, msg)

        elif kind == "face_state":
            await self.esp.set_state(msg.get("emo", "IDLE"), msg.get("sub"))

        elif kind == "tts_meta":
            emo = msg.get("emo", "SPEAKING")
            text = msg.get("text", "")
            envelope = msg.get("envelope", [])
            await self.esp.set_sync(emo="SPEAKING", sub=text, mouth=envelope[0] if envelope else 30)
            self.audio.start_viseme_pacemaker(envelope)

        elif kind == "tts_chunk":
            pcm_b64 = msg.get("pcm_b64", "")
            if pcm_b64:
                self.audio.feed_tts_audio(base64.b64decode(pcm_b64))

        elif kind == "pong":
            rtt_ms = int((time.monotonic() - self._last_ping_ts) * 1000) if self._last_ping_ts else 0
            await self.esp.set_debug(quixi="ONLINE", srv_ms=rtt_ms)

    async def _execute_agent_tool(self, sender: str, msg: Dict[str, Any]) -> None:
        req_id = str(msg.get("id", ""))
        tool = str(msg.get("tool", ""))
        args = msg.get("args", {})
        result: Dict[str, Any] = {"ok": False}

        if tool == "set_companion_emotion":
            await self.esp.set_state(str(args.get("emotion", "HAPPY")), args.get("subtitle"))
            result = {"ok": True}

        elif tool == "gpio_configure_and_write":
            res = await self.esp.request(
                {
                    "t": "gpio_cfg",
                    "pin": int(args["pin"]),
                    "dir": str(args.get("mode", "out")),
                    "pull": "none",
                    "val": int(args.get("state", 0)),
                }
            )
            result = res

        elif tool == "gpio_read_pin":
            res = await self.esp.request({"t": "gpio_get", "pin": int(args["pin"])})
            result = res

        elif tool == "scan_ble_environment":
            res = await self.esp.request({"t": "ble_get"})
            min_rssi = args.get("min_rssi")
            if min_rssi is not None and isinstance(res.get("ble"), list):
                res["ble"] = [d for d in res["ble"] if int(d.get("rssi", -127)) >= int(min_rssi)]
                res["cnt"] = len(res["ble"])
            result = res

        elif tool == "read_imu_motion":
            result = await self.esp.request({"t": "imu_get"})

        elif tool == "update_companion_settings":
            if "volume" in args:
                self.audio.set_volume(int(args["volume"]))
            if "mic_mode" in args:
                self.audio.set_mic_mode(str(args["mic_mode"]))
            result = {"ok": True, "applied": args}

        else:
            result = {"ok": False, "error": f"Unknown tool: {tool}"}

        reply = json.dumps({"kind": "tool_res", "id": req_id, "result": result})
        await self.quixi.send_app_data(sender or self.server_address, reply, protocol_id=AGENT_PROTOCOL_ID)


if __name__ == "__main__":
    cfg_file = Path(__file__).resolve().parent / "config.yaml"
    hub = PiCompanionHub(cfg_file)
    asyncio.run(hub.start())

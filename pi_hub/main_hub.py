"""
Raspberry Pi Zero 2 W Companion Hub:
- Bridges USB Serial NDJSON to Waveshare ESP32-S3 ("face", touch, IMU, BLE, GPIOs)
- Runs local Spixi VoIPManager.cs client over QuIXI (REST + MQTT)
- Streams Bluetooth mic audio to AI Server and plays synced TTS audio + subtitles/visemes
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common_ixian import QuIXIClient, QuIXIError, SpixiVoIPSessionManager
from common_ixian.quixi_client import _extract_b64_bytes
from pi_hub.audio_sync_engine import AudioSyncEngine
from pi_hub.esp_serial_bridge import EspSerialBridge
from pi_hub.pi_web_gui import PiWebGUI

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

        self.server_address: str = str(cfg["quixi"].get("server_ixian_address", "") or "").strip()
        ser_cfg = cfg["serial"]
        q_cfg = cfg["quixi"]
        aud_cfg = cfg["audio"]
        self.allow_unauthenticated_dev: bool = bool(q_cfg.get("allow_unauthenticated_dev", False))

        self.esp = EspSerialBridge(
            port=ser_cfg["port"],
            baudrate=int(ser_cfg.get("baudrate", 921600)),
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
            on_barge_in=self._on_voice_barge_in,
            vad_trigger_level=int(aud_cfg.get("vad_trigger_level", 1)),
            max_turn_seconds=float(aud_cfg.get("max_turn_seconds", 30.0)),
            noise_suppression_level=int(aud_cfg.get("mic_noise_suppression", 0)),
            auto_gain_dbfs=int(aud_cfg.get("mic_auto_gain_dbfs", 0)),
            earcons_enabled=bool(aud_cfg.get("earcons_enabled", False)),
            awake_wav=aud_cfg.get("awake_wav"),
            done_wav=aud_cfg.get("done_wav"),
            error_wav=aud_cfg.get("error_wav"),
            wake_word_model=str(aud_cfg.get("wake_word_model", "hey_jarvis")),
            wake_threshold=float(aud_cfg.get("wake_threshold", 0.5)),
            wake_refractory_ms=int(aud_cfg.get("wake_refractory_ms", 2000)),
            followup_window_ms=int(aud_cfg.get("followup_window_ms", 0)),
            stream_stall_timeout_s=float(aud_cfg.get("stream_stall_timeout_s", 5.0)),
            watchdog_interval_s=float(aud_cfg.get("watchdog_interval_s", 0.0)),
            on_stream_status=self._on_audio_stream_status,
        )
        self.voip = SpixiVoIPSessionManager(
            send_spixi_fn=self.quixi.send_spixi_message,
            supported_codecs=("pcm", "opus"),
            on_audio_data=self._on_voip_rx_audio,
            on_session_started=self._on_voip_started,
            on_session_ended=self._on_voip_ended,
            on_request_error=self._on_voip_request_error,
            authorize_caller=self._is_authorized_server,
        )
        self.quixi.attach_voip_manager(self.voip)
        self._last_ping_ts = 0.0
        self._chat_stream_id: Optional[str] = None
        self._chat_stream_buf: str = ""
        self._esp_cfg: Dict[str, Any] = {}
        self._last_ble_scan: List[Dict[str, Any]] = []
        self._current_tts_turn_id: Optional[str] = None
        self._interrupted_turn_ids: Deque[str] = collections.deque(maxlen=16)
        self._superseded_chat_stream_ids: Deque[str] = collections.deque(maxlen=16)

        web_cfg = cfg.get("web_ui") if isinstance(cfg.get("web_ui"), dict) else {}
        self.web_ui_enabled: bool = bool(web_cfg.get("enabled", True))
        self.gui = PiWebGUI(
            self,
            host=str(web_cfg.get("host", "127.0.0.1") or "127.0.0.1"),
            port=int(web_cfg.get("port", 8082)),
            auth_token=web_cfg.get("auth_token"),
        )

    async def start(self) -> None:
        self.esp.set_event_handler(self._on_esp_event)
        self.quixi.on_topic("AppProtocolData", self._on_app_protocol_data)
        self.quixi.on_topic("ChatStream", self._on_chat_stream)
        self.quixi.on_topic("Chat", self._on_quixi_chat_event)
        self.quixi.on_topic("FriendStatusUpdate", self._on_quixi_friend_event)

        await self.esp.start()
        await self.quixi.start()
        await self.audio.start()
        if self.web_ui_enabled:
            await self.gui.start()

        await self.esp.set_debug(quixi="ONLINE", voip="DIALING", bt_spk="OK")
        await self.esp.set_state("IDLE", "Connecting to AI Server...")

        # Initiate persistent spixi.voip call with the AI Server via QuIXI
        if self._is_valid_server_address():
            try:
                await self.voip.initiate_call(self.server_address)
            except (QuIXIError, Exception) as exc:
                logger.warning("Initial VoIP call initiation failed: %s", exc)

        try:
            while True:
                # Re-attempt call setup if not currently connected
                if not self.voip.is_in_call and self._is_valid_server_address():
                    try:
                        await self.voip.initiate_call(self.server_address)
                    except (QuIXIError, Exception):
                        pass
                await asyncio.sleep(10.0)
        finally:
            await self.gui.stop()
            await self.audio.stop()
            await self.quixi.stop()
            await self.esp.stop()

    def _is_valid_server_address(self) -> bool:
        return bool(self.server_address and not self.server_address.startswith("1Replace"))

    def _is_authorized_server(self, sender: str) -> bool:
        sender = (sender or "").strip()
        if not self._is_valid_server_address():
            return bool(getattr(self, "allow_unauthenticated_dev", False))
        return bool(sender) and sender == self.server_address

    async def _send_agent_app_data(self, target_addr: str, payload: Dict[str, Any]) -> bool:
        target = (target_addr or self.server_address or "").strip()
        if not target or target.startswith("1Replace"):
            return False
        try:
            await self.quixi.send_app_data(
                target,
                json.dumps(payload),
                protocol_id=AGENT_PROTOCOL_ID,
            )
            return True
        except asyncio.CancelledError:
            raise
        except (QuIXIError, Exception) as exc:
            logger.warning("QuIXI send_app_data failed (%s): %s", payload.get("kind"), exc)
            return False

    async def _on_voip_started(self, contact: str, sid: bytes, codec: str) -> None:
        await self.esp.set_debug(quixi="ONLINE", voip="IN_CALL", bt_spk="OK")
        await self.esp.set_state("IDLE", "Ready! Speak or tap face.")

    async def _on_voip_ended(self, contact: str, sid: bytes, codec: str) -> None:
        await self.esp.set_debug(quixi="ONLINE", voip="IDLE")

    async def _on_voip_request_error(self, contact: str, sid: bytes, detail: str) -> None:
        logger.warning("VoIP request error from %s session=%s: %s", contact, sid.hex(), detail)
        if self.audio.earcons_enabled:
            self.audio.play_earcon("error")
        await self.esp.set_debug(quixi="ONLINE", voip="ERR")

    async def _on_audio_stream_status(self, ok: bool) -> None:
        await self.esp.set_debug(bt_spk="OK" if ok else "ERR")

    async def _send_mic_voip_frame(self, frame_bytes: bytes) -> bool:
        if self.voip.is_in_call:
            try:
                return await self.voip.send_audio_frame(frame_bytes)
            except asyncio.CancelledError:
                raise
            except (QuIXIError, Exception) as exc:
                logger.warning("VoIP send_audio_frame failed: %s", exc)
                return False
        return False

    async def _on_speech_capture_state(self, active: bool) -> None:
        if active:
            await self.esp.set_state("LISTENING", "Listening...", mic=1)
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(emo="LISTENING", sub="Listening...")
        else:
            await self.esp.set_state("THINKING", "Thinking...", mic=0)
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(emo="THINKING", sub="Thinking...")
            payload: Dict[str, Any] = {"kind": "end_of_turn"}
            if self.voip.current_session_id:
                payload["session_id"] = self.voip.current_session_id.hex()
            await self._send_agent_app_data(self.server_address, payload)

    def _is_turn_capture_blocking_playback(self) -> bool:
        return self.audio.is_capturing and (
            self.audio.mic_mode != "duplex" or self.audio._ptt_active
        )

    def _mark_active_turn_interrupted(self) -> None:
        if self._current_tts_turn_id and self._current_tts_turn_id not in self._interrupted_turn_ids:
            self._interrupted_turn_ids.append(self._current_tts_turn_id)
        self._current_tts_turn_id = None
        if self._chat_stream_id and self._chat_stream_id not in self._superseded_chat_stream_ids:
            self._superseded_chat_stream_ids.append(self._chat_stream_id)
        self._chat_stream_id = None
        self._chat_stream_buf = ""

    async def _trigger_barge_in(self, subtitle: Optional[str] = "Interrupted.") -> None:
        self._mark_active_turn_interrupted()
        self.audio.stop_playback()
        await self.esp.set_mouth(0)
        if subtitle is not None:
            await self.esp.set_state("IDLE", subtitle)
        if hasattr(self, "gui") and self.gui is not None:
            self.gui.update_face_state(emo="IDLE", sub=subtitle or "", mouth=0)
        await self._send_agent_app_data(
            self.server_address,
            {"kind": "barge_in", "act": "stop_speech"},
        )

    async def _on_voice_barge_in(self) -> None:
        await self._trigger_barge_in(subtitle=None)

    async def _on_viseme_frame(self, value: int) -> None:
        await self.esp.set_mouth(value)
        if hasattr(self, "gui") and self.gui is not None:
            self.gui.update_face_state(mouth=value)

    async def _on_playback_done(self) -> None:
        self._current_tts_turn_id = None
        if not self.audio.is_capturing:
            await self.esp.set_state("IDLE")
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(emo="IDLE", mouth=0)

    async def _on_voip_rx_audio(self, session_id: bytes, data: bytes, codec: str) -> None:
        """VoIPManager.onData equivalent: write incoming TTS audio to BT speaker."""
        if self._is_turn_capture_blocking_playback():
            return
        self.audio.feed_tts_audio(data)

    async def _on_quixi_chat_event(self, topic: str, payload: Dict[str, Any]) -> None:
        if hasattr(self, "gui") and self.gui is not None:
            raw = _extract_b64_bytes(payload)
            msg = raw.decode("utf-8", errors="ignore") if raw else ""
            self.gui.record_quixi_event(topic, payload, summary=msg)

    async def _on_quixi_friend_event(self, topic: str, payload: Dict[str, Any]) -> None:
        if hasattr(self, "gui") and self.gui is not None:
            self.gui.record_quixi_event(topic, payload, summary="FriendStatusUpdate")

    async def _on_esp_event(self, evt: Dict[str, Any]) -> None:
        if hasattr(self, "gui") and self.gui is not None:
            self.gui.record_esp_event(evt)
        src = evt.get("src")
        act = evt.get("act")

        if src == "touch":
            if act == "ptt_toggle":
                now_listening = await self.audio.toggle_ptt()
                if now_listening:
                    self._mark_active_turn_interrupted()
                    await self.esp.set_mouth(0)
                    await self._send_agent_app_data(
                        self.server_address,
                        {"kind": "barge_in", "act": "stop_speech"},
                    )
            elif act == "stop_speech":
                await self._trigger_barge_in(subtitle="Interrupted.")
        elif src == "cfg":
            if "mode" in evt:
                self.audio.set_mic_mode(str(evt["mode"]))
                self._esp_cfg["mode"] = self.audio.mic_mode
            if "vol" in evt:
                vol = int(evt["vol"])
                self.audio.set_volume(vol)
                self._esp_cfg["vol"] = max(0, min(100, vol))
            if "bright" in evt:
                self._esp_cfg["bright"] = max(10, min(100, int(evt["bright"])))
            if "ble_en" in evt:
                self._esp_cfg["ble_en"] = 1 if evt["ble_en"] else 0
            if "sleep_s" in evt:
                self._esp_cfg["sleep_s"] = int(evt["sleep_s"])
        elif src == "dbg":
            if act == "ping":
                self._last_ping_ts = time.monotonic()
                await self._send_agent_app_data(
                    self.server_address,
                    {"kind": "hw_event", "event": {"src": "dbg", "act": "ping", "ts": time.time()}},
                )
            elif act == "test_call":
                if self._is_valid_server_address() and not self.voip.is_in_call:
                    await self.esp.set_debug(quixi="ONLINE", voip="DIALING")
                    try:
                        await self.voip.initiate_call(self.server_address)
                    except (QuIXIError, Exception) as exc:
                        logger.warning("Test call initiation failed: %s", exc)
                        await self.esp.set_debug(quixi="ONLINE", voip="ERR")
        elif src == "imu":
            gest = str(evt.get("gest", "")).strip().lower()
            if gest in ("shake", "tilt", "pickup", "double_tap"):
                await self._send_agent_app_data(
                    self.server_address,
                    {"kind": "hw_event", "event": evt},
                )
        elif src == "ble" and isinstance(evt.get("ble"), list):
            self._last_ble_scan = list(evt["ble"])

    async def _on_chat_stream(self, topic: str, payload: Dict[str, Any]) -> None:
        """Display real-time streaming text chunks from QuIXI ChatStream."""
        sender = str(payload.get("sender", "")).strip()
        if not self._is_authorized_server(sender):
            return
        try:
            inner = payload.get("data")
            msg_id = inner.get("messageId") if isinstance(inner, dict) else None
            if isinstance(msg_id, str) and msg_id and msg_id in self._superseded_chat_stream_ids:
                return
            if self._is_turn_capture_blocking_playback():
                if isinstance(msg_id, str) and msg_id and msg_id not in self._superseded_chat_stream_ids:
                    self._superseded_chat_stream_ids.append(msg_id)
                return

            raw = _extract_b64_bytes(payload)
            text = raw.decode("utf-8", errors="ignore") if raw else ""
            if not text and isinstance(inner, dict) and isinstance(inner.get("message"), str):
                text = inner["message"]
            text = text.strip()
            if text.startswith("{"):
                try:
                    obj = json.loads(text)
                    if isinstance(obj, dict):
                        for key in ("message", "text", "chunk"):
                            if isinstance(obj.get(key), str):
                                text = obj[key].strip()
                                break
                except Exception:
                    pass

            is_stream_raw = inner.get("isStream") if isinstance(inner, dict) else None
            is_stream_end = is_stream_raw in (False, "false", "False", "0", 0)

            if isinstance(msg_id, str) and msg_id:
                if self._chat_stream_id != msg_id:
                    self._chat_stream_id = msg_id
                    self._chat_stream_buf = ""
                if text:
                    if not self._chat_stream_buf:
                        self._chat_stream_buf = text
                    elif self._chat_stream_buf[-1].isspace() or text[0].isspace():
                        self._chat_stream_buf += text
                    else:
                        self._chat_stream_buf += " " + text
                    self._chat_stream_buf = self._chat_stream_buf[-180:]
                display_text = self._chat_stream_buf
                if is_stream_end:
                    self._chat_stream_id = None
            else:
                display_text = text[-180:]

            if display_text:
                await self.esp.set_state("SPEAKING", display_text)
        except Exception:
            pass

    async def _on_app_protocol_data(self, topic: str, payload: Dict[str, Any]) -> None:
        sender = str(payload.get("sender", "")).strip()
        if not self._is_authorized_server(sender):
            return
        try:
            raw = _extract_b64_bytes(payload)
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        if not isinstance(msg, dict):
            return

        kind = msg.get("kind")

        if kind == "tool_call":
            await self._execute_agent_tool(sender, msg)

        elif kind == "tool_batch":
            await self._execute_agent_tool_batch(sender, msg)

        elif kind == "face_state":
            if self._is_turn_capture_blocking_playback():
                return
            await self.esp.set_state(str(msg.get("emo", "IDLE")), msg.get("sub"))

        elif kind == "tts_meta":
            turn_id = msg.get("turn_id")
            turn_id_str = str(turn_id) if turn_id is not None else None
            if turn_id_str and turn_id_str in self._interrupted_turn_ids:
                return
            if self._is_turn_capture_blocking_playback():
                if turn_id_str and turn_id_str not in self._interrupted_turn_ids:
                    self._interrupted_turn_ids.append(turn_id_str)
                return
            self._current_tts_turn_id = turn_id_str
            text = str(msg.get("text", ""))
            raw_env = msg.get("envelope", [])
            envelope = (
                [max(0, min(100, int(v))) for v in raw_env]
                if isinstance(raw_env, list)
                else []
            )
            await self.esp.set_sync(emo="SPEAKING", sub=text, mouth=envelope[0] if envelope else 30)
            self.audio.start_viseme_pacemaker(envelope)

        elif kind == "tts_chunk":
            turn_id = msg.get("turn_id")
            turn_id_str = str(turn_id) if turn_id is not None else None
            if turn_id_str and turn_id_str in self._interrupted_turn_ids:
                return
            if self._is_turn_capture_blocking_playback():
                if turn_id_str and turn_id_str not in self._interrupted_turn_ids:
                    self._interrupted_turn_ids.append(turn_id_str)
                return
            pcm_b64 = msg.get("pcm_b64", "")
            if pcm_b64:
                try:
                    self.audio.feed_tts_audio(base64.b64decode(pcm_b64))
                except Exception as exc:
                    logger.warning("Invalid tts_chunk base64: %s", exc)

        elif kind == "tts_end":
            turn_id = msg.get("turn_id")
            turn_id_str = str(turn_id) if turn_id is not None else None
            if turn_id_str and turn_id_str in self._interrupted_turn_ids:
                return
            if (
                self._current_tts_turn_id is not None
                and turn_id_str is not None
                and turn_id_str != self._current_tts_turn_id
            ):
                if turn_id_str not in self._interrupted_turn_ids:
                    self._interrupted_turn_ids.append(turn_id_str)
                return
            if self._current_tts_turn_id is None and not self.audio.is_speaking:
                if turn_id_str and turn_id_str not in self._interrupted_turn_ids:
                    self._interrupted_turn_ids.append(turn_id_str)
                return
            interrupted = bool(msg.get("interrupted", False))
            self._current_tts_turn_id = None
            if interrupted:
                if turn_id_str and turn_id_str not in self._interrupted_turn_ids:
                    self._interrupted_turn_ids.append(turn_id_str)
                self.audio.stop_playback()
                await self.esp.set_mouth(0)
                if not self.audio.is_capturing:
                    await self.esp.set_state("IDLE")
            else:
                await self.audio.finish_tts_stream(interrupted=False)

        elif kind == "pong":
            rtt_ms = int((time.monotonic() - self._last_ping_ts) * 1000) if self._last_ping_ts else 0
            await self.esp.set_debug(quixi="ONLINE", srv_ms=rtt_ms)

    async def _run_single_tool(self, tool: str, raw_args: Any) -> Dict[str, Any]:
        args = raw_args if isinstance(raw_args, dict) else {}
        try:
            if tool == "set_companion_emotion":
                await self.esp.set_state(str(args.get("emotion", "HAPPY")), args.get("subtitle"))
                return {"ok": True}

            if tool == "gpio_configure_and_write":
                pin = int(args["pin"])
                pull = str(args.get("pull", "none")).strip().lower()
                if pull not in ("none", "up", "down"):
                    pull = "none"
                mode_raw = args.get("mode")
                direction = str(mode_raw if mode_raw is not None else "out").strip().lower()
                return await self.esp.request(
                    {
                        "t": "gpio_cfg",
                        "pin": pin,
                        "dir": direction,
                        "pull": pull,
                        "val": int(args.get("state", 0)),
                    }
                )

            if tool == "gpio_read_pin":
                pin = int(args["pin"])
                if "pull" in args and args["pull"] is not None:
                    pull = str(args["pull"]).strip().lower()
                    if pull not in ("none", "up", "down"):
                        pull = "none"
                    cfg_res = await self.esp.request(
                        {
                            "t": "gpio_cfg",
                            "pin": pin,
                            "dir": "in",
                            "pull": pull,
                            "val": 0,
                        }
                    )
                    if cfg_res.get("ok") != 1:
                        return cfg_res
                return await self.esp.request({"t": "gpio_get", "pin": pin})

            if tool == "scan_ble_environment":
                res = await self.esp.request({"t": "ble_get"})
                min_rssi = args.get("min_rssi")
                if min_rssi is not None and isinstance(res.get("ble"), list):
                    res["ble"] = [d for d in res["ble"] if int(d.get("rssi", -127)) >= int(min_rssi)]
                    res["cnt"] = len(res["ble"])
                return res

            if tool == "read_imu_motion":
                return await self.esp.request({"t": "imu_get"})

            if tool == "update_companion_settings":
                applied: Dict[str, Any] = {}
                cfg_kwargs: Dict[str, Any] = {}
                if "brightness" in args and args["brightness"] is not None:
                    bright = max(10, min(100, int(args["brightness"])))
                    cfg_kwargs["bright"] = bright
                    applied["brightness"] = bright
                if "volume" in args and args["volume"] is not None:
                    vol = max(0, min(100, int(args["volume"])))
                    self.audio.set_volume(vol)
                    cfg_kwargs["vol"] = vol
                    applied["volume"] = vol
                if "mic_mode" in args and args["mic_mode"] is not None:
                    self.audio.set_mic_mode(str(args["mic_mode"]))
                    cfg_kwargs["mode"] = self.audio.mic_mode
                    applied["mic_mode"] = self.audio.mic_mode
                if cfg_kwargs:
                    esp_res = await self.esp.set_cfg(**cfg_kwargs, wait_res=True)
                    if esp_res.get("ok") != 1:
                        return {"ok": False, "error": esp_res.get("error", "ESP cfg failed"), "applied": applied}
                return {"ok": True, "applied": applied}

            return {"ok": False, "error": f"Unknown tool: {tool}"}
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    async def _execute_agent_tool(self, sender: str, msg: Dict[str, Any]) -> None:
        req_id = str(msg.get("id", ""))
        tool = str(msg.get("tool", ""))
        result = await self._run_single_tool(tool, msg.get("args"))
        await self._send_agent_app_data(
            sender or self.server_address,
            {"kind": "tool_res", "id": req_id, "result": result},
        )

    async def _execute_agent_tool_batch(self, sender: str, msg: Dict[str, Any]) -> None:
        batch_id = str(msg.get("id", ""))
        raw_calls = msg.get("calls")
        calls = raw_calls if isinstance(raw_calls, list) else []
        results: List[Dict[str, Any]] = []
        for idx, call in enumerate(calls):
            if not isinstance(call, dict):
                results.append({"id": f"call_{idx}", "result": {"ok": False, "error": "Invalid tool call item"}})
                continue
            cid = str(call.get("id") or f"call_{idx}")
            tool = str(call.get("tool", ""))
            res = await self._run_single_tool(tool, call.get("args"))
            results.append({"id": cid, "result": res})
        await self._send_agent_app_data(
            sender or self.server_address,
            {"kind": "tool_res", "id": batch_id, "results": results},
        )


if __name__ == "__main__":
    cfg_file = Path(__file__).resolve().parent / "config.yaml"
    hub = PiCompanionHub(cfg_file)
    asyncio.run(hub.start())

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
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common_ixian import QuIXIClient, QuIXIError, SpixiVoIPSessionManager
from common_ixian.quixi_client import _extract_b64_bytes
from pi_hub.audio_sync_engine import _REAL_NUMPY, AudioSyncEngine, np
from pi_hub.esp_firmware_updater import EspFirmwareUpdater
from pi_hub.esp_serial_bridge import EspSerialBridge
from pi_hub.pi_web_gui import PiWebGUI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pi_hub")

AGENT_PROTOCOL_ID = "com.tamagci.agent"


def detect_local_dhcp_ip() -> str:
    """Discover the Raspberry Pi's primary active LAN/Wi-Fi DHCP IPv4 address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("10.255.255.255", 1))
            ip = str(s.getsockname()[0])
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for cand in ips:
            if cand and not cand.startswith("127."):
                return cand
    except Exception:
        pass
    return "127.0.0.1"


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

        raw_friends = q_cfg.get("trusted_contacts", {})
        self.trusted_contacts: Dict[str, str] = {}
        if isinstance(raw_friends, dict):
            for k, v in raw_friends.items():
                addr_s = str(k).strip()
                if addr_s:
                    self.trusted_contacts[addr_s] = str(v or "").strip()
        elif isinstance(raw_friends, list):
            for item in raw_friends:
                if isinstance(item, dict):
                    addr_s = str(item.get("address", "")).strip()
                    if addr_s:
                        self.trusted_contacts[addr_s] = str(item.get("nickname", item.get("nick", "")) or "").strip()
                else:
                    addr_s = str(item).strip()
                    if addr_s:
                        self.trusted_contacts[addr_s] = ""

        self.auto_accept_friend_calls: bool = bool(q_cfg.get("auto_accept_friend_calls", True))
        self.display_friend_messages_on_esp: bool = bool(
            q_cfg.get("display_friend_messages_on_esp", True)
        )

        self.esp = EspSerialBridge(
            port=ser_cfg["port"],
            baudrate=int(ser_cfg.get("baudrate", 921600)),
        )
        self.firmware = EspFirmwareUpdater(self.esp)
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
            authorize_caller=self._is_authorized_voip_caller,
            can_preempt_caller=self._can_friend_preempt_call,
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
        self._pi_dhcp_ip: str = "127.0.0.1"
        self._pi_ixian_address: str = ""
        self._last_mouth_val: int = 0
        self._last_mouth_ts: float = 0.0
        self._last_net_push: tuple = ()
        self._last_contacts_sync: float = 0.0
        perf_cfg = cfg.get("performance") if isinstance(cfg.get("performance"), dict) else {}
        # Background housekeeping cadence (Pi Zero 2 W: fewer REST/serial round-trips)
        self._housekeeping_interval_s: float = max(
            2.0, float(perf_cfg.get("housekeeping_interval_s", 30.0))
        )
        self._contacts_sync_interval_s: float = max(
            5.0, float(perf_cfg.get("contacts_sync_interval_s", 90.0))
        )

        web_cfg = cfg.get("web_ui") if isinstance(cfg.get("web_ui"), dict) else {}
        self.web_ui_enabled: bool = bool(web_cfg.get("enabled", True))
        self.gui = PiWebGUI(
            self,
            host=str(web_cfg.get("host", "127.0.0.1") or "127.0.0.1"),
            port=int(web_cfg.get("port", 8082)),
            auth_token=web_cfg.get("auth_token"),
        )

    async def refresh_and_push_network_info(self, force: bool = True) -> Dict[str, str]:
        """Discover Pi DHCP IPv4 and QuIXI primary wallet address and push them to the ESP32-S3 Network screen.

        Periodic housekeeping passes ``force=False``: the wallet is only re-queried until it is known and the
        serial push is skipped when nothing changed (saves REST + USB traffic on the Pi Zero 2 W).
        """
        self._pi_dhcp_ip = detect_local_dhcp_ip()
        if force or not self._pi_ixian_address:
            try:
                w_res = await self.quixi.my_wallet()
                raw_w = w_res.get("result", w_res) if isinstance(w_res, dict) else w_res
                if isinstance(raw_w, dict) and raw_w:
                    first_key = next(iter(raw_w.keys()))
                    if first_key and first_key not in ("result", "error"):
                        self._pi_ixian_address = str(first_key).strip()
                elif isinstance(raw_w, list) and raw_w:
                    self._pi_ixian_address = str(raw_w[0]).strip()
                elif isinstance(raw_w, str) and raw_w.strip():
                    self._pi_ixian_address = raw_w.strip()
            except Exception:
                pass

        net_key = (self._pi_dhcp_ip, self._pi_ixian_address)
        if force or net_key != self._last_net_push:
            await self.esp.set_net_info(
                ip=self._pi_dhcp_ip,
                ixi=self._pi_ixian_address or None,
            )
            self._last_net_push = net_key
        net_snapshot = {
            "ip": self._pi_dhcp_ip,
            "ixi": self._pi_ixian_address,
        }
        if hasattr(self, "gui") and self.gui is not None:
            self.gui.esp_telemetry["net"] = net_snapshot
        return net_snapshot

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
        await self.refresh_and_push_network_info()
        await self.sync_quixi_contacts()

        # Initiate persistent spixi.voip call with the AI Server via QuIXI
        if self._is_valid_server_address():
            try:
                await self.voip.initiate_call(self.server_address)
            except (QuIXIError, Exception) as exc:
                logger.warning("Initial VoIP call initiation failed: %s", exc)

        try:
            while True:
                await self.refresh_and_push_network_info(force=False)
                now_mono = time.monotonic()
                if now_mono - self._last_contacts_sync >= self._contacts_sync_interval_s:
                    self._last_contacts_sync = now_mono
                    await self.sync_quixi_contacts()
                # Re-attempt call setup if not currently connected
                if not self.voip.is_in_call and self._is_valid_server_address():
                    try:
                        await self.voip.initiate_call(self.server_address)
                    except (QuIXIError, Exception):
                        pass
                # Poll quickly only while the AI Server session needs to be re-established
                needs_redial = not self.voip.is_in_call and self._is_valid_server_address()
                await asyncio.sleep(
                    min(10.0, self._housekeeping_interval_s) if needs_redial else self._housekeeping_interval_s
                )
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

    def add_trusted_friend(self, address: str, nickname: str = "") -> Dict[str, str]:
        """Register or update a trusted Spixi friend contact on the Pi Hub."""
        addr = (address or "").strip()
        if not addr:
            return dict(self.trusted_contacts)
        nick = (nickname or "").strip()
        if nick or addr not in self.trusted_contacts:
            self.trusted_contacts[addr] = nick
        return dict(self.trusted_contacts)

    def remove_trusted_friend(self, address: str) -> bool:
        """Remove a Spixi contact from the Pi Hub trusted friends registry."""
        addr = (address or "").strip()
        return self.trusted_contacts.pop(addr, None) is not None

    def get_contact_display_name(self, address: str) -> str:
        """Return a human-friendly nickname or short address for ESP32-S3 LCD subtitles."""
        addr = (address or "").strip()
        if not addr:
            return "Unknown"
        if addr == self.server_address:
            return "AI Server"
        nick = self.trusted_contacts.get(addr, "").strip()
        if nick:
            return nick
        return addr[:10] + (".." if len(addr) > 10 else "")

    def resolve_spixi_contact(self, query: str) -> Optional[tuple[str, str]]:
        """Resolve a Spixi contact by nickname (case-insensitive) or Ixian wallet address."""
        q = (query or "").strip()
        if not q:
            return None
        q_low = q.lower()
        # 1. Exact address match
        if q in self.trusted_contacts:
            return q, self.get_contact_display_name(q)
        # 2. Exact case-insensitive nickname match
        for addr, nick in self.trusted_contacts.items():
            if nick and nick.strip().lower() == q_low:
                return addr, nick.strip()
        # 3. Substring nickname or address prefix match
        for addr, nick in self.trusted_contacts.items():
            if (nick and q_low in nick.strip().lower()) or addr.lower().startswith(q_low):
                return addr, self.get_contact_display_name(addr)
        # 4. If query looks like a raw Ixian wallet address (starts with '1' and length >= 12)
        if q.startswith("1") and len(q) >= 12 and " " not in q:
            return q, self.get_contact_display_name(q)
        return None

    async def sync_quixi_contacts(self) -> Dict[str, str]:
        """Auto-sync local QuIXI /contacts into trusted_contacts so paired Spixi friends can call & message."""
        try:
            res = await self.quixi.contacts()
            raw = res.get("result", res) if isinstance(res, dict) else res
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        c_addr = str(item.get("address") or item.get("id") or "").strip()
                        c_nick = str(item.get("nick") or item.get("nickname") or item.get("name") or "").strip()
                        if c_addr and c_addr != self.server_address:
                            self.add_trusted_friend(c_addr, c_nick)
                    elif isinstance(item, str):
                        c_addr = item.strip()
                        if c_addr and c_addr != self.server_address:
                            self.add_trusted_friend(c_addr)
            elif isinstance(raw, dict):
                for k, v in raw.items():
                    c_addr = str(k).strip()
                    if c_addr and c_addr not in ("result", "error") and c_addr != self.server_address:
                        c_nick = str(v.get("nick", "") if isinstance(v, dict) else (v or "")).strip()
                        self.add_trusted_friend(c_addr, c_nick)
        except Exception:
            pass
        return dict(self.trusted_contacts)

    def _is_authorized_friend(self, sender: str) -> bool:
        sender = (sender or "").strip()
        return bool(sender) and sender != self.server_address and sender in self.trusted_contacts

    def _is_authorized_voip_caller(self, sender: str) -> bool:
        return self._is_authorized_server(sender) or (
            self.auto_accept_friend_calls and self._is_authorized_friend(sender)
        )

    def _can_friend_preempt_call(self, incoming_caller: str, current_contact: Optional[str]) -> bool:
        return bool(
            self.auto_accept_friend_calls
            and self._is_authorized_friend(incoming_caller)
            and (not current_contact or current_contact == self.server_address)
        )

    @property
    def is_in_friend_call(self) -> bool:
        return bool(
            self.voip.is_in_call
            and self.voip.current_contact
            and self._is_authorized_friend(self.voip.current_contact)
        )

    @property
    def is_dialing_or_in_friend_call(self) -> bool:
        return bool(
            self.voip.current_session_id is not None
            and self.voip.current_contact
            and self._is_authorized_friend(self.voip.current_contact)
        )

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
        if self._is_authorized_friend(contact):
            self._mark_active_turn_interrupted()
            self.audio.stop_playback()
            if self.audio.earcons_enabled:
                self.audio.play_earcon("awake")
            sub = f"Call: {self.get_contact_display_name(contact)}"
            await self.esp.set_debug(quixi="ONLINE", voip="FRIEND", bt_spk="OK")
            await self.esp.set_state("HAPPY", sub)
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(emo="HAPPY", sub=sub, mouth=0)
            return
        await self.esp.set_debug(quixi="ONLINE", voip="IN_CALL", bt_spk="OK")
        await self.esp.set_state("IDLE", "Ready! Speak or tap face.")

    async def _on_voip_ended(self, contact: str, sid: bytes, codec: str) -> None:
        await self.esp.set_debug(quixi="ONLINE", voip="IDLE")
        if self._is_authorized_friend(contact):
            await self.esp.set_mouth(0)
            await self.esp.set_state("IDLE", "Call ended.")
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(emo="IDLE", sub="Call ended.", mouth=0)

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
            if self.is_in_friend_call:
                sub = f"Call: {self.get_contact_display_name(self.voip.current_contact or '')}"
                await self.esp.set_state("LISTENING", sub, mic=1)
                if hasattr(self, "gui") and self.gui is not None:
                    self.gui.update_face_state(emo="LISTENING", sub=sub)
                return
            await self.esp.set_state("LISTENING", "Listening...", mic=1)
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(emo="LISTENING", sub="Listening...")
        else:
            if self.is_in_friend_call:
                sub = f"Call: {self.get_contact_display_name(self.voip.current_contact or '')}"
                await self.esp.set_state("HAPPY", sub, mic=0)
                if hasattr(self, "gui") and self.gui is not None:
                    self.gui.update_face_state(emo="HAPPY", sub=sub)
                return
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
            if self.is_dialing_or_in_friend_call:
                friend_name = self.get_contact_display_name(self.voip.current_contact or "")
                sub = f"Call: {friend_name}" if self.voip.is_in_call else f"Calling: {friend_name}..."
                await self.esp.set_state("HAPPY", sub)
                if hasattr(self, "gui") and self.gui is not None:
                    self.gui.update_face_state(emo="HAPPY", sub=sub, mouth=0)
                return
            await self.esp.set_state("IDLE")
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(emo="IDLE", mouth=0)

    @staticmethod
    def _estimate_pcm16_mouth_openness(data: bytes) -> int:
        if len(data) < 2:
            return 0
        count = min(len(data) // 2, 640)
        if _REAL_NUMPY and np is not None:
            s = np.frombuffer(data, dtype="<i2", count=count).astype(np.float32)
            rms = float(np.sqrt(np.dot(s, s) / count))
        else:
            samples = struct.unpack(f"<{count}h", data[: count * 2])
            rms = (sum(v * v for v in samples) / count) ** 0.5
        if rms < 120:
            return 0
        return max(10, min(100, int(rms / 65.0)))

    async def _on_voip_rx_audio(self, session_id: bytes, data: bytes, codec: str) -> None:
        """VoIPManager.onData equivalent: write incoming TTS or Spixi friend audio to BT speaker."""
        if not self.is_in_friend_call and self._is_turn_capture_blocking_playback():
            return
        self.audio.feed_tts_audio(data)
        if self.is_in_friend_call:
            mouth_val = self._estimate_pcm16_mouth_openness(data)
            # Throttle serial/WebSocket traffic: skip near-identical values within 60 ms
            now = time.monotonic()
            if (
                mouth_val != 0
                and abs(mouth_val - self._last_mouth_val) < 6
                and (now - self._last_mouth_ts) < 0.06
            ):
                return
            self._last_mouth_val = mouth_val
            self._last_mouth_ts = now
            await self.esp.set_mouth(mouth_val)
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(mouth=mouth_val)

    async def _on_quixi_chat_event(self, topic: str, payload: Dict[str, Any]) -> None:
        sender = str(payload.get("sender", "")).strip()
        raw = _extract_b64_bytes(payload)
        msg = raw.decode("utf-8", errors="ignore").strip() if raw else ""
        if not msg:
            inner = payload.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("message"), str):
                msg = inner["message"].strip()
            elif isinstance(payload.get("message"), str):
                msg = payload["message"].strip()

        if hasattr(self, "gui") and self.gui is not None:
            self.gui.record_quixi_event(topic, payload, summary=msg)

        if not msg or not self.display_friend_messages_on_esp:
            return
        if not (self._is_authorized_friend(sender) or self._is_authorized_server(sender)):
            return

        if self._is_authorized_friend(sender):
            sub_text = f"{self.get_contact_display_name(sender)}: {msg}"[:180]
        else:
            sub_text = msg[:180]

        if self.audio.earcons_enabled and not self.audio.is_speaking:
            self.audio.play_earcon("awake")
        await self.esp.set_state("HAPPY", sub_text)
        if hasattr(self, "gui") and self.gui is not None:
            self.gui.update_face_state(emo="HAPPY", sub=sub_text)

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
        elif src == "net":
            if act == "refresh":
                await self.refresh_and_push_network_info()

    async def _on_chat_stream(self, topic: str, payload: Dict[str, Any]) -> None:
        """Display real-time streaming text chunks from QuIXI ChatStream."""
        sender = str(payload.get("sender", "")).strip()
        is_friend = self._is_authorized_friend(sender) and self.display_friend_messages_on_esp
        if not (self._is_authorized_server(sender) or is_friend):
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
                if is_friend:
                    sub_text = f"{self.get_contact_display_name(sender)}: {display_text}"[:180]
                    await self.esp.set_state("HAPPY", sub_text)
                else:
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

            if tool in ("update_companion_settings", "flash_esp32_firmware"):
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
                if "ble_enabled" in args and args["ble_enabled"] is not None:
                    ble_en = 1 if args["ble_enabled"] else 0
                    cfg_kwargs["ble_en"] = ble_en
                    applied["ble_enabled"] = bool(ble_en)
                if "sleep_timeout_s" in args and args["sleep_timeout_s"] is not None:
                    sleep_s = int(args["sleep_timeout_s"])
                    cfg_kwargs["sleep_s"] = sleep_s
                    applied["sleep_timeout_s"] = sleep_s
                if cfg_kwargs:
                    esp_res = await self.esp.set_cfg(**cfg_kwargs, wait_res=True)
                    if esp_res.get("ok") != 1:
                        return {"ok": False, "error": esp_res.get("error", "ESP cfg failed"), "applied": applied}

                fw_act = str(
                    args.get("firmware_action")
                    or ("flash" if tool == "flash_esp32_firmware" else "")
                ).strip().lower()
                if fw_act:
                    fw_src = args.get("firmware_source")
                    fw_sha = args.get("firmware_sha256")
                    if fw_act == "check":
                        fw_res = self.firmware.check_firmware(fw_src)
                    elif fw_act == "reset":
                        fw_res = await self.firmware.reset_esp32()
                    elif fw_act in ("flash", "update", "reflash"):
                        fw_res = await self.firmware.flash_firmware(
                            source=str(fw_src) if fw_src is not None else None,
                            expected_sha256=str(fw_sha) if fw_sha is not None else None,
                        )
                    else:
                        return {"ok": False, "error": f"Unknown firmware_action: {fw_act}"}
                    applied["firmware"] = fw_res
                    return {"ok": bool(fw_res.get("ok", False)), "applied": applied, "firmware": fw_res}

                spixi_act = str(args.get("spixi_action") or "").strip().lower()
                if spixi_act:
                    await self.sync_quixi_contacts()
                    contact_query = str(
                        args.get("contact_name")
                        or args.get("contact")
                        or args.get("address")
                        or ""
                    ).strip()

                    if spixi_act in ("list", "list_contacts", "contacts"):
                        contacts_list = [
                            {"address": a, "nickname": n or self.get_contact_display_name(a)}
                            for a, n in self.trusted_contacts.items()
                        ]
                        spixi_res = {
                            "ok": True,
                            "action": "list_contacts",
                            "contacts": contacts_list,
                            "trusted_contacts": dict(self.trusted_contacts),
                        }
                        applied["spixi"] = spixi_res
                        return {"ok": True, "applied": applied, "spixi": spixi_res}

                    if spixi_act in ("hangup", "end", "stop"):
                        prev_contact = self.voip.current_contact
                        await self.voip.hangup_call()
                        spixi_res = {
                            "ok": True,
                            "action": "hangup",
                            "ended_contact": prev_contact,
                        }
                        applied["spixi"] = spixi_res
                        return {"ok": True, "applied": applied, "spixi": spixi_res}

                    if spixi_act in ("add", "add_contact"):
                        addr_arg = str(args.get("address") or contact_query).strip()
                        nick_arg = str(args.get("nickname") or args.get("contact_name") or "").strip()
                        if not addr_arg:
                            return {"ok": False, "error": "Missing Spixi contact address to add"}
                        self.add_trusted_friend(addr_arg, nick_arg if nick_arg != addr_arg else "")
                        try:
                            await self.quixi.add_contact(addr_arg)
                        except Exception:
                            pass
                        spixi_res = {
                            "ok": True,
                            "action": "add_contact",
                            "address": addr_arg,
                            "nickname": self.trusted_contacts.get(addr_arg, ""),
                        }
                        applied["spixi"] = spixi_res
                        return {"ok": True, "applied": applied, "spixi": spixi_res}

                    resolved = self.resolve_spixi_contact(contact_query)
                    if not resolved:
                        available = [
                            f"{n} ({a[:10]}..)" if n else a
                            for a, n in self.trusted_contacts.items()
                        ]
                        return {
                            "ok": False,
                            "error": f"Spixi contact '{contact_query}' not found",
                            "available_contacts": available,
                        }
                    target_addr, display_name = resolved

                    if spixi_act in ("call", "dial"):
                        self.add_trusted_friend(target_addr, display_name if display_name != target_addr else "")
                        try:
                            sid = await self.voip.initiate_call(target_addr, force=True)
                        except Exception as exc:
                            return {"ok": False, "error": f"Failed to start Spixi call to {display_name}: {exc}"}
                        dial_sub = f"Calling: {display_name}..."
                        await self.esp.set_debug(quixi="ONLINE", voip="FRIEND", bt_spk="OK")
                        await self.esp.set_state("HAPPY", dial_sub)
                        if hasattr(self, "gui") and self.gui is not None:
                            self.gui.update_face_state(emo="HAPPY", sub=dial_sub, mouth=0)
                        spixi_res = {
                            "ok": True,
                            "action": "call",
                            "contact_name": display_name,
                            "address": target_addr,
                            "session_id": sid.hex(),
                        }
                        applied["spixi"] = spixi_res
                        return {"ok": True, "applied": applied, "spixi": spixi_res}

                    if spixi_act in ("message", "chat", "send_message"):
                        msg_text = str(args.get("spixi_message") or args.get("message") or "").strip()
                        if not msg_text:
                            return {"ok": False, "error": "Missing 'spixi_message' text"}
                        try:
                            await self.quixi.send_chat_message(target_addr, msg_text)
                        except Exception as exc:
                            return {"ok": False, "error": f"Failed to send Spixi message to {display_name}: {exc}"}
                        spixi_res = {
                            "ok": True,
                            "action": "message",
                            "contact_name": display_name,
                            "address": target_addr,
                            "message": msg_text,
                        }
                        applied["spixi"] = spixi_res
                        return {"ok": True, "applied": applied, "spixi": spixi_res}

                    return {"ok": False, "error": f"Unknown spixi_action: {spixi_act}"}

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

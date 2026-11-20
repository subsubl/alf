"""
Server-side AI Companion Orchestrator:
- Connects to Server QuIXI (REST API + Mosquitto MQTT)
- Holds all OpenAI-compatible API keys in .env
- Implements Spixi VoIPManager.cs audio streaming via QuIXI
- Executes dynamic hardware tool calls over AppProtocolData
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common_ixian import QuIXIClient, SpixiVoIPSessionManager
from common_ixian.quixi_client import _extract_b64_bytes, is_quixi_ok
from server_agent.agent_router import AgentBrain
from server_agent.speech_processor import SpeechProcessor, iter_audio_frames
from server_agent.web_gui import ServerWebGUI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("server_agent")

AGENT_PROTOCOL_ID = "com.tamagci.agent"
MAX_RX_AUDIO_BYTES = int(os.getenv("MAX_RX_AUDIO_BYTES", str(16000 * 2 * 60)))
IMU_GESTURE_COOLDOWN_SEC = 3.0

_IMU_GESTURE_PROMPTS: Dict[str, str] = {
    "shake": "[System event: the user just shook your body playfully! React briefly.]",
    "tilt": "[System event: you were just tilted onto a new angle. React briefly if appropriate.]",
    "pickup": "[System event: the user just picked you up! Greet or react briefly.]",
    "double_tap": "[System event: the user double-tapped your housing! React briefly.]",
}


def _split_chat_stream_chunks(text: str, max_chunk_chars: int = 32) -> List[str]:
    """Split reply text into word-boundary chunks for incremental ChatStream updates."""
    stripped = (text or "").strip()
    if not stripped:
        return []
    if len(stripped) <= max_chunk_chars:
        return [stripped]

    chunks: List[str] = []
    current = ""
    for word in stripped.split(" "):
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chunk_chars:
            current = f"{current} {word}"
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


class ServerCompanionService:
    """Coordinates QuIXI P2P transport, Spixi VoIP audio turns, and OpenAI Agent logic."""

    def __init__(
        self,
        quixi_client: Any = None,
        speech_processor: Any = None,
        brain: Any = None,
    ) -> None:
        load_dotenv(Path(__file__).resolve().parent / ".env")

        self.pi_address = os.getenv("PI_IXIAN_ADDRESS", "").strip()
        self.allow_unauthenticated_dev = (
            os.getenv("ALLOW_UNAUTHENTICATED_DEV", "false").strip().lower()
            in {"1", "true", "yes"}
        )
        if not self.pi_address or self.pi_address.startswith("1Replace"):
            if self.allow_unauthenticated_dev:
                logger.warning(
                    "PI_IXIAN_ADDRESS is unset/placeholder; ALLOW_UNAUTHENTICATED_DEV=true permits unauthenticated callers."
                )
            else:
                logger.warning(
                    "PI_IXIAN_ADDRESS is unset/placeholder; server is fail-closed and will reject callers until configured."
                )

        self.quixi = quixi_client or QuIXIClient(
            rest_url=os.getenv("QUIXI_REST_URL", "http://127.0.0.1:8001"),
            mqtt_host=os.getenv("MQTT_HOST", "127.0.0.1"),
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        )

        self.speech = speech_processor or SpeechProcessor(
            stt_base_url=os.getenv("STT_BASE_URL", "https://api.openai.com/v1"),
            stt_api_key=os.getenv("STT_API_KEY", ""),
            stt_model=os.getenv("STT_MODEL", "whisper-1"),
            tts_base_url=os.getenv("TTS_BASE_URL", "https://api.openai.com/v1"),
            tts_api_key=os.getenv("TTS_API_KEY", ""),
            tts_model=os.getenv("TTS_MODEL", "tts-1"),
            tts_voice=os.getenv("TTS_VOICE", "alloy"),
        )

        self.brain = brain or AgentBrain(
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            companion_name=os.getenv("COMPANION_NAME", "Tamagci"),
            rpc_call_fn=self._call_pi_tool_rpc,
            rpc_batch_fn=self._call_pi_tool_batch_rpc,
        )

        self.voip = SpixiVoIPSessionManager(
            send_spixi_fn=self.quixi.send_spixi_message,
            supported_codecs=("pcm", "opus"),
            on_audio_data=self._on_voip_audio_data,
            on_session_started=self._on_voip_started,
            on_session_ended=self._on_voip_ended,
            on_request_error=self._on_voip_request_error,
            authorize_caller=self._is_authorized,
        )
        self.quixi.attach_voip_manager(self.voip)

        self._max_rx_audio_bytes: int = MAX_RX_AUDIO_BYTES
        self._rx_audio_buffers: Dict[bytes, bytearray] = {}
        self._session_senders: Dict[bytes, str] = {}
        self._sender_sessions: Dict[str, bytes] = {}
        self._rx_overflow_warned: Set[bytes] = set()
        self._pending_rpcs: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._turn_lock = asyncio.Lock()
        self._tts_cancel_events: Dict[str, Set[asyncio.Event]] = {}
        self._active_turn_target: Optional[str] = None
        self._last_imu_event_ts: Dict[str, float] = {}

        self.web_ui_enabled: bool = (
            os.getenv("WEB_UI_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
        )
        self.gui = ServerWebGUI(
            self,
            host=os.getenv("WEB_UI_HOST", "127.0.0.1"),
            port=int(os.getenv("WEB_UI_PORT", "8080")),
            auth_token=os.getenv("WEB_UI_TOKEN", ""),
        )

    def _is_authorized(self, sender: str) -> bool:
        sender = (sender or "").strip()
        if not self.pi_address or self.pi_address.startswith("1Replace"):
            return bool(getattr(self, "allow_unauthenticated_dev", False))
        return bool(sender) and sender == self.pi_address

    async def start(self) -> None:
        self.quixi.on_topic("AppProtocolData", self._on_app_protocol_data)
        self.quixi.on_topic("Chat", self._on_text_chat)
        await self.quixi.start()
        if self.web_ui_enabled:
            await self.gui.start()
        logger.info("Server Companion Service listening on QuIXI (MQTT + REST)...")

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.gui.stop()
            await self.quixi.stop()

    async def _on_voip_started(self, contact: str, sid: bytes, codec: str) -> None:
        contact = (contact or "").strip()
        self._session_senders[sid] = contact
        self._sender_sessions[contact] = sid
        self._rx_audio_buffers.setdefault(sid, bytearray())

    def _cleanup_voip_session(self, contact: str, sid: bytes) -> None:
        contact = (contact or "").strip()
        self._rx_audio_buffers.pop(sid, None)
        self._rx_overflow_warned.discard(sid)
        mapped_contact = self._session_senders.pop(sid, contact)
        if mapped_contact and self._sender_sessions.get(mapped_contact) == sid:
            self._sender_sessions.pop(mapped_contact, None)
        if mapped_contact:
            self._cancel_active_tts(mapped_contact)

    async def _on_voip_ended(self, contact: str, sid: bytes, codec: str) -> None:
        self._cleanup_voip_session(contact, sid)

    async def _on_voip_request_error(self, contact: str, sid: bytes, detail: str) -> None:
        logger.warning("VoIP request error from %s session=%s: %s", contact, sid.hex(), detail)
        self._cleanup_voip_session(contact, sid)

    def _register_cancel_event(
        self, target_addr: str, cancel_event: Optional[asyncio.Event] = None
    ) -> asyncio.Event:
        addr = (target_addr or "").strip()
        ev = cancel_event if cancel_event is not None else asyncio.Event()
        self._tts_cancel_events.setdefault(addr, set()).add(ev)
        return ev

    def _unregister_cancel_event(self, target_addr: str, cancel_event: asyncio.Event) -> None:
        addr = (target_addr or "").strip()
        events = self._tts_cancel_events.get(addr)
        if events is not None:
            events.discard(cancel_event)
            if not events:
                self._tts_cancel_events.pop(addr, None)

    def _cancel_active_tts(self, target_addr: str) -> bool:
        addr = (target_addr or "").strip()
        events = self._tts_cancel_events.get(addr)
        if not events:
            return False
        for ev in list(events):
            ev.set()
        return True

    async def _on_voip_audio_data(self, session_id: bytes, data: bytes, codec: str) -> None:
        """Buffer incoming Spixi VoIP audio frames sent from Pi Bluetooth Mic (session-scoped & capped)."""
        if not data:
            return

        sender = self._session_senders.get(session_id, "")
        if not sender and self.voip.current_session_id == session_id and self.voip.current_contact:
            sender = self.voip.current_contact.strip()
            self._session_senders[session_id] = sender
            self._sender_sessions[sender] = session_id

        if not self._is_authorized(sender):
            return

        buf = self._rx_audio_buffers.setdefault(session_id, bytearray())
        max_bytes = getattr(self, "_max_rx_audio_bytes", MAX_RX_AUDIO_BYTES)
        remaining = max_bytes - len(buf)
        if remaining <= 0:
            if session_id not in self._rx_overflow_warned:
                logger.warning(
                    "RX audio buffer cap (%d bytes) reached for session=%s; dropping excess audio",
                    max_bytes,
                    session_id.hex(),
                )
                self._rx_overflow_warned.add(session_id)
            return
        buf.extend(data[:remaining])

    def _pop_rx_audio_for_turn(self, sender: str, msg: Dict[str, Any]) -> bytes:
        sender = (sender or "").strip()
        sid: Optional[bytes] = None
        raw_sid = msg.get("session_id")
        if isinstance(raw_sid, str) and raw_sid.strip():
            try:
                parsed_sid = bytes.fromhex(raw_sid.strip())
                if sender and parsed_sid in self._session_senders and self._session_senders[parsed_sid] != sender:
                    logger.warning(
                        "Ignoring mismatched session_id=%s from %s (owned by %s)",
                        parsed_sid.hex(),
                        sender,
                        self._session_senders[parsed_sid],
                    )
                    return b""
                sid = parsed_sid
            except ValueError:
                sid = None

        if sid is None and sender:
            sid = self._sender_sessions.get(sender)

        if (
            sid is None
            and self.voip.current_session_id is not None
            and (not sender or (self.voip.current_contact or "").strip() == sender)
        ):
            sid = self.voip.current_session_id

        if sid is None and len(self._rx_audio_buffers) == 1:
            only_sid = next(iter(self._rx_audio_buffers))
            mapped = self._session_senders.get(only_sid)
            if not mapped or not sender or mapped == sender:
                sid = only_sid

        if sid is None:
            return b""

        buf = self._rx_audio_buffers.pop(sid, None)
        self._rx_overflow_warned.discard(sid)
        return bytes(buf) if buf else b""

    async def _on_app_protocol_data(self, topic: str, payload: Dict[str, Any]) -> None:
        sender = str(payload.get("sender", "")).strip()
        if not self._is_authorized(sender):
            return

        try:
            raw = _extract_b64_bytes(payload)
            msg = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            logger.warning("Invalid AppProtocolData JSON: %s", exc)
            return

        if not isinstance(msg, dict):
            return

        kind = msg.get("kind")
        target_addr = sender or self.pi_address

        if kind == "tool_res":
            req_id = str(msg.get("id", ""))
            fut = self._pending_rpcs.pop(req_id, None)
            if fut and not fut.done():
                if "results" in msg and "result" not in msg:
                    fut.set_result({"results": msg["results"]})
                else:
                    fut.set_result(msg.get("result", {}))

        elif kind in ("barge_in", "stop_speech") or msg.get("act") == "stop_speech":
            self._cancel_active_tts(target_addr)

        elif kind == "end_of_turn":
            audio_snapshot = self._pop_rx_audio_for_turn(sender, msg)
            cancel_event = self._register_cancel_event(target_addr)
            asyncio.create_task(
                self._handle_voice_turn(target_addr, audio_snapshot, cancel_event=cancel_event)
            )

        elif kind == "hw_event":
            # Proactive events from ESP32-S3 (e.g. IMU gestures, ping, or barge-in)
            evt = msg.get("event")
            if not isinstance(evt, dict):
                return

            if evt.get("act") == "stop_speech":
                self._cancel_active_tts(target_addr)
                return

            if evt.get("src") == "dbg" and evt.get("act") == "ping":
                try:
                    await self.quixi.send_app_data(
                        target_addr,
                        json.dumps({"kind": "pong", "ts": evt.get("ts", 0)}),
                        protocol_id=AGENT_PROTOCOL_ID,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to send pong: %s", exc)
            elif evt.get("src") == "imu":
                gest = str(evt.get("gest", "")).strip().lower()
                prompt = _IMU_GESTURE_PROMPTS.get(gest)
                if not prompt:
                    return
                if gest in ("tilt", "pickup"):
                    now = time.monotonic()
                    cd_key = f"{target_addr}:{gest}"
                    last_ts = self._last_imu_event_ts.get(cd_key, 0.0)
                    if (now - last_ts) < IMU_GESTURE_COOLDOWN_SEC:
                        return
                    self._last_imu_event_ts[cd_key] = now
                cancel_event = self._register_cancel_event(target_addr)
                asyncio.create_task(
                    self._handle_text_turn(target_addr, prompt, cancel_event=cancel_event)
                )

    async def _on_text_chat(self, topic: str, payload: Dict[str, Any]) -> None:
        """Also support direct text chats from a Spixi app contact or Pi."""
        sender = str(payload.get("sender", "")).strip()
        if not self._is_authorized(sender):
            return
        user_text = ""
        try:
            raw = _extract_b64_bytes(payload)
            if raw:
                user_text = raw.decode("utf-8", errors="ignore").strip()
            else:
                inner = payload.get("data")
                if isinstance(inner, dict) and isinstance(inner.get("message"), str):
                    user_text = inner["message"].strip()
        except Exception:
            return
        if user_text:
            await self._handle_text_turn(sender or self.pi_address, user_text)

    def _resolve_rpc_target(self) -> str:
        for candidate in (
            self.voip.current_contact,
            getattr(self, "_active_turn_target", None),
            self.pi_address,
        ):
            if candidate and not candidate.startswith("1Replace"):
                return candidate
        return ""

    async def _call_pi_tool_rpc(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self._resolve_rpc_target()
        if not target:
            return {"ok": False, "error": "No connected Pi Ixian address"}

        req_id = uuid.uuid4().hex[:10]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_rpcs[req_id] = fut

        rpc_payload = json.dumps(
            {"kind": "tool_call", "id": req_id, "tool": tool_name, "args": args}
        )
        is_flash = (
            tool_name == "flash_esp32_firmware"
            or (
                isinstance(args, dict)
                and str(args.get("firmware_action", "")).strip().lower() in ("flash", "update", "reflash")
            )
        )
        rpc_timeout = 90.0 if is_flash else 8.0
        try:
            await self.quixi.send_app_data(target, rpc_payload, protocol_id=AGENT_PROTOCOL_ID)
            return await asyncio.wait_for(fut, timeout=rpc_timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Hardware RPC timed out"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            self._pending_rpcs.pop(req_id, None)

    async def _call_pi_tool_batch_rpc(self, calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        target = self._resolve_rpc_target()
        if not target:
            return [{"ok": False, "error": "No connected Pi Ixian address"} for _ in calls]

        batch_id = uuid.uuid4().hex[:10]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_rpcs[batch_id] = fut

        serialized_calls = [
            {
                "id": str(c.get("id", f"call_{i}")),
                "tool": str(c.get("tool", "")),
                "args": c.get("args") if isinstance(c.get("args"), dict) else {},
            }
            for i, c in enumerate(calls)
        ]
        rpc_payload = json.dumps(
            {"kind": "tool_batch", "id": batch_id, "calls": serialized_calls}
        )
        try:
            await self.quixi.send_app_data(target, rpc_payload, protocol_id=AGENT_PROTOCOL_ID)
            timeout = min(16.0, max(8.0, 4.0 * len(calls)))
            res = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return [{"ok": False, "error": "Hardware batch RPC timed out"} for _ in calls]
        except Exception as exc:
            return [{"ok": False, "error": str(exc)} for _ in calls]
        finally:
            self._pending_rpcs.pop(batch_id, None)

        raw_results: Any = res.get("results")
        if raw_results is None and isinstance(res.get("result"), dict):
            raw_results = res["result"].get("results")
        if not isinstance(raw_results, list):
            return [{"ok": False, "error": "Invalid tool_batch response"} for _ in calls]

        by_id: Dict[str, Dict[str, Any]] = {}
        for item in raw_results:
            if isinstance(item, dict) and item.get("id"):
                by_id[str(item["id"])] = item.get("result", item) if "result" in item else item

        ordered: List[Dict[str, Any]] = []
        for idx, c in enumerate(serialized_calls):
            cid = c["id"]
            if cid in by_id and isinstance(by_id[cid], dict):
                ordered.append(by_id[cid])
            elif idx < len(raw_results) and isinstance(raw_results[idx], dict) and not raw_results[idx].get("id"):
                item = raw_results[idx]
                ordered.append(item.get("result", item) if "result" in item else item)
            else:
                ordered.append({"ok": False, "error": f"Missing result for {cid}"})
        return ordered

    async def _handle_voice_turn(
        self,
        target_addr: str,
        pcm_16k: bytes,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> None:
        target_addr = (target_addr or "").strip()
        cancel_event = self._register_cancel_event(target_addr, cancel_event)
        try:
            if cancel_event.is_set():
                logger.info("Voice turn cancelled before lock for %s", target_addr)
                return
            async with self._turn_lock:
                if cancel_event.is_set():
                    logger.info("Voice turn cancelled before STT for %s", target_addr)
                    return
                transcript = await self.speech.transcribe_pcm(
                    pcm_16k, sample_rate=16000, cancel_event=cancel_event
                )
                if cancel_event.is_set():
                    logger.info("Voice turn cancelled during STT for %s", target_addr)
                    return
                if not transcript:
                    try:
                        await self.quixi.send_app_data(
                            target_addr,
                            json.dumps({"kind": "face_state", "emo": "IDLE", "sub": "Didn't catch that."}),
                            protocol_id=AGENT_PROTOCOL_ID,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("Failed to send empty-transcript face_state: %s", exc)
                    return

                logger.info("User transcript: %s", transcript)
                await self._run_agent_and_reply(target_addr, transcript, cancel_event=cancel_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Voice turn failed: %s", exc)
            try:
                await self.quixi.send_app_data(
                    target_addr,
                    json.dumps({"kind": "face_state", "emo": "SAD", "sub": "Server error."}),
                    protocol_id=AGENT_PROTOCOL_ID,
                )
            except asyncio.CancelledError:
                raise
            except Exception as send_exc:
                logger.warning("Failed to send error face_state: %s", send_exc)
        finally:
            self._unregister_cancel_event(target_addr, cancel_event)

    async def _handle_text_turn(
        self,
        target_addr: str,
        user_text: str,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> None:
        target_addr = (target_addr or "").strip()
        cancel_event = self._register_cancel_event(target_addr, cancel_event)
        try:
            if cancel_event.is_set():
                logger.info("Text turn cancelled before lock for %s", target_addr)
                return
            async with self._turn_lock:
                if cancel_event.is_set():
                    logger.info("Text turn cancelled before LLM for %s", target_addr)
                    return
                await self._run_agent_and_reply(target_addr, user_text, cancel_event=cancel_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Text turn failed: %s", exc)
        finally:
            self._unregister_cancel_event(target_addr, cancel_event)

    async def _stream_chat_text(
        self,
        target_addr: str,
        reply_text: str,
        cancel_event: asyncio.Event,
    ) -> bool:
        chunks = _split_chat_stream_chunks(reply_text)
        if not chunks:
            return False

        stream_msg_id = base64.b64encode(uuid.uuid4().bytes).decode("ascii")
        seq = 0
        try:
            for chunk in chunks:
                if cancel_event.is_set():
                    return False
                res = await self.quixi.send_chat_stream_message(
                    target_addr,
                    chunk,
                    message_id_b64=stream_msg_id,
                    sequence=seq,
                    is_stream=True,
                )
                seq += 1
                if not is_quixi_ok(res):
                    return False

            if cancel_event.is_set():
                return False

            end_res = await self.quixi.send_chat_stream_message(
                target_addr,
                "",
                message_id_b64=stream_msg_id,
                sequence=seq,
                is_stream=False,
            )
            return is_quixi_ok(end_res)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ChatStream send failed: %s", exc)
            return False

    async def run_web_chat_turn(
        self,
        user_text: str,
        speak_on_companion: bool = False,
        source: str = "web_gui",
    ) -> Dict[str, Any]:
        """Execute a chat turn initiated from the Server Web GUI or OpenAI-compatible /v1/chat/completions."""
        target_addr = self._resolve_rpc_target() or self.pi_address
        can_speak = bool(
            speak_on_companion
            and target_addr
            and not target_addr.startswith("1Replace")
        )
        async with self._turn_lock:
            if can_speak:
                await self._run_agent_and_reply(target_addr, user_text, source=source)
                tool_calls = list(getattr(self.brain, "last_turn_tool_calls", []) or [])
                reply_text = ""
                for m in reversed(getattr(self.brain, "history", []) or []):
                    if isinstance(m, dict) and m.get("role") == "assistant" and not m.get("tool_calls"):
                        reply_text = str(m.get("content", ""))
                        break
                emotion = getattr(self.brain, "last_emotion", "HAPPY")
                return {
                    "ok": True,
                    "reply": reply_text,
                    "emotion": emotion,
                    "tool_calls": tool_calls,
                    "spoken": True,
                }

            prev_target = getattr(self, "_active_turn_target", None)
            self._active_turn_target = target_addr
            try:
                if hasattr(self, "gui") and self.gui is not None:
                    self.gui.record_chat_turn("user", user_text, source=source)
                    self.gui.update_face_state(emo="THINKING", sub="Thinking...")
                reply_text, emotion = await self.brain.process_user_turn(user_text)
                tool_calls = list(getattr(self.brain, "last_turn_tool_calls", []) or [])
                if hasattr(self, "gui") and self.gui is not None:
                    for tc in tool_calls:
                        self.gui.record_tool_execution(
                            tc.get("tool", ""),
                            tc.get("args") or {},
                            tc.get("result") or {},
                        )
                    self.gui.update_face_state(emo=emotion, sub=reply_text, mouth=0)
                    self.gui.record_chat_turn(
                        "assistant",
                        reply_text,
                        source=source,
                        emotion=emotion,
                        tool_calls=tool_calls,
                    )
                return {
                    "ok": True,
                    "reply": reply_text,
                    "emotion": emotion,
                    "tool_calls": tool_calls,
                    "spoken": False,
                }
            finally:
                self._active_turn_target = prev_target

    async def _run_agent_and_reply(
        self,
        target_addr: str,
        user_text: str,
        cancel_event: Optional[asyncio.Event] = None,
        source: str = "voice",
    ) -> None:
        target_addr = (target_addr or "").strip()
        prev_target = getattr(self, "_active_turn_target", None)
        self._active_turn_target = target_addr
        cancel_event = self._register_cancel_event(target_addr, cancel_event)

        try:
            if hasattr(self, "gui") and self.gui is not None:
                self.gui.record_chat_turn("user", user_text, source=source)
                self.gui.update_face_state(emo="THINKING", sub="Thinking...")

            reply_text, emotion = await self.brain.process_user_turn(user_text)
            if cancel_event.is_set():
                logger.info("Turn cancelled during LLM reasoning for %s", target_addr)
                return
            logger.info("Agent reply (%s): %s", emotion, reply_text)

            tool_calls = list(getattr(self.brain, "last_turn_tool_calls", []) or [])
            if hasattr(self, "gui") and self.gui is not None:
                for tc in tool_calls:
                    self.gui.record_tool_execution(
                        tc.get("tool", ""),
                        tc.get("args") or {},
                        tc.get("result") or {},
                    )
                self.gui.record_chat_turn(
                    "assistant",
                    reply_text,
                    source=source,
                    emotion=emotion,
                    tool_calls=tool_calls,
                )

            # 1. Stream live text subtitle over QuIXI ChatStream (SpixiMessageCode.CHAT_STREAM = 53)
            chat_stream_ok = await self._stream_chat_text(target_addr, reply_text, cancel_event)
            if cancel_event.is_set():
                logger.info("Turn cancelled during ChatStream for %s", target_addr)
                return

            # 2. Synthesize TTS PCM + 20Hz viseme envelope
            pcm_16k, envelope = await self.speech.synthesize_pcm_16k(
                reply_text, cancel_event=cancel_event
            )
            turn_id = uuid.uuid4().hex[:8]

            if cancel_event.is_set():
                logger.info("Turn cancelled before tts_meta for %s", target_addr)
                return

            if hasattr(self, "gui") and self.gui is not None:
                self.gui.update_face_state(
                    emo=emotion,
                    sub=reply_text,
                    mouth=envelope[0] if envelope else 0,
                    envelope=envelope,
                )

            if not pcm_16k:
                try:
                    await self.quixi.send_app_data(
                        target_addr,
                        json.dumps({"kind": "face_state", "emo": emotion, "sub": reply_text}),
                        protocol_id=AGENT_PROTOCOL_ID,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to send face_state: %s", exc)
                return

            # 3. Send synced subtitle + viseme envelope metadata via AppProtocolData
            meta_payload = json.dumps(
                {
                    "kind": "tts_meta",
                    "turn_id": turn_id,
                    "emo": emotion,
                    "text": reply_text,
                    "sample_rate": 16000,
                    "envelope": envelope,
                    "total_bytes": len(pcm_16k),
                    "chat_stream_ok": chat_stream_ok,
                }
            )
            await self.quixi.send_app_data(target_addr, meta_payload, protocol_id=AGENT_PROTOCOL_ID)

            # 4. Stream TTS audio frames over native Spixi VoIP appData (type 21)
            try:
                for frame in iter_audio_frames(
                    pcm_16k, sample_rate=16000, frame_ms=100, cancel_event=cancel_event
                ):
                    if cancel_event.is_set():
                        break
                    if self.voip.is_in_call and (self.voip.current_contact or "").strip() == target_addr:
                        await self.voip.send_audio_frame(frame)
                    else:
                        # Fallback if session was not initiated yet: send via AppProtocolData chunk
                        b64 = base64.b64encode(frame).decode("ascii")
                        await self.quixi.send_app_data(
                            target_addr,
                            json.dumps({"kind": "tts_chunk", "turn_id": turn_id, "pcm_b64": b64}),
                            protocol_id=AGENT_PROTOCOL_ID,
                        )
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=0.02)
                        break
                    except asyncio.TimeoutError:
                        pass
            finally:
                try:
                    await self.quixi.send_app_data(
                        target_addr,
                        json.dumps(
                            {
                                "kind": "tts_end",
                                "turn_id": turn_id,
                                "interrupted": cancel_event.is_set(),
                            }
                        ),
                        protocol_id=AGENT_PROTOCOL_ID,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Failed to send tts_end: %s", exc)
        finally:
            self._active_turn_target = prev_target
            self._unregister_cancel_event(target_addr, cancel_event)


if __name__ == "__main__":
    service = ServerCompanionService()
    asyncio.run(service.start())

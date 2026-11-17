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
import uuid
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common_ixian import QuIXIClient, SpixiVoIPSessionManager
from common_ixian.quixi_client import _extract_b64_bytes
from server_agent.agent_router import AgentBrain
from server_agent.speech_processor import SpeechProcessor, split_audio_frames

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("server_agent")

AGENT_PROTOCOL_ID = "com.tamagci.agent"


class ServerCompanionService:
    """Coordinates QuIXI P2P transport, Spixi VoIP audio turns, and OpenAI Agent logic."""

    def __init__(self) -> None:
        load_dotenv(Path(__file__).resolve().parent / ".env")

        self.pi_address = os.getenv("PI_IXIAN_ADDRESS", "").strip()
        self.quixi = QuIXIClient(
            rest_url=os.getenv("QUIXI_REST_URL", "http://127.0.0.1:8001"),
            mqtt_host=os.getenv("MQTT_HOST", "127.0.0.1"),
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        )

        self.speech = SpeechProcessor(
            stt_base_url=os.getenv("STT_BASE_URL", "https://api.openai.com/v1"),
            stt_api_key=os.getenv("STT_API_KEY", ""),
            stt_model=os.getenv("STT_MODEL", "whisper-1"),
            tts_base_url=os.getenv("TTS_BASE_URL", "https://api.openai.com/v1"),
            tts_api_key=os.getenv("TTS_API_KEY", ""),
            tts_model=os.getenv("TTS_MODEL", "tts-1"),
            tts_voice=os.getenv("TTS_VOICE", "alloy"),
        )

        self.brain = AgentBrain(
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            companion_name=os.getenv("COMPANION_NAME", "Tamagci"),
            rpc_call_fn=self._call_pi_tool_rpc,
        )

        self.voip = SpixiVoIPSessionManager(
            send_spixi_fn=self.quixi.send_spixi_message,
            supported_codecs=("pcm", "opus"),
            on_audio_data=self._on_voip_audio_data,
        )
        self.quixi.attach_voip_manager(self.voip)

        self._rx_audio_buf = bytearray()
        self._pending_rpcs: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        self._turn_lock = asyncio.Lock()

    def _is_authorized(self, sender: str) -> bool:
        if not self.pi_address or self.pi_address.startswith("1Replace"):
            return True
        return sender == self.pi_address

    async def start(self) -> None:
        self.quixi.on_topic("AppProtocolData", self._on_app_protocol_data)
        self.quixi.on_topic("Chat", self._on_text_chat)
        await self.quixi.start()
        logger.info("Server Companion Service listening on QuIXI (MQTT + REST)...")

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.quixi.stop()

    async def _on_voip_audio_data(self, session_id: bytes, data: bytes, codec: str) -> None:
        """Buffer incoming Spixi VoIP audio frames sent from Pi Bluetooth Mic."""
        self._rx_audio_buf.extend(data)

    async def _on_app_protocol_data(self, topic: str, payload: Dict[str, Any]) -> None:
        sender = str(payload.get("sender", ""))
        if not self._is_authorized(sender):
            return

        try:
            raw = _extract_b64_bytes(payload)
            msg = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            logger.warning("Invalid AppProtocolData JSON: %s", exc)
            return

        kind = msg.get("kind")

        if kind == "tool_res":
            req_id = str(msg.get("id", ""))
            fut = self._pending_rpcs.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(msg.get("result", {}))

        elif kind == "end_of_turn":
            audio_snapshot = bytes(self._rx_audio_buf)
            self._rx_audio_buf.clear()
            target_addr = sender or self.pi_address
            asyncio.create_task(self._handle_voice_turn(target_addr, audio_snapshot))

        elif kind == "hw_event":
            # Proactive events from ESP32-S3 (e.g. shake gesture or ping)
            evt = msg.get("event", {})
            target_addr = sender or self.pi_address
            if evt.get("src") == "dbg" and evt.get("act") == "ping":
                await self.quixi.send_app_data(
                    target_addr,
                    json.dumps({"kind": "pong", "ts": evt.get("ts", 0)}),
                    protocol_id=AGENT_PROTOCOL_ID,
                )
            elif evt.get("src") == "imu" and evt.get("gest") == "shake":
                asyncio.create_task(
                    self._handle_text_turn(
                        target_addr,
                        "[System event: the user just shook your body playfully! React briefly.]",
                    )
                )

    async def _on_text_chat(self, topic: str, payload: Dict[str, Any]) -> None:
        """Also support direct text chats from a Spixi app contact or Pi."""
        sender = str(payload.get("sender", ""))
        if not self._is_authorized(sender):
            return
        try:
            raw = _extract_b64_bytes(payload)
            user_text = raw.decode("utf-8", errors="ignore").strip()
        except Exception:
            return
        if user_text:
            await self._handle_text_turn(sender, user_text)

    async def _call_pi_tool_rpc(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        target = self.voip.current_contact or self.pi_address
        if not target:
            return {"ok": False, "error": "No connected Pi Ixian address"}

        req_id = uuid.uuid4().hex[:10]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending_rpcs[req_id] = fut

        rpc_payload = json.dumps(
            {"kind": "tool_call", "id": req_id, "tool": tool_name, "args": args}
        )
        await self.quixi.send_app_data(target, rpc_payload, protocol_id=AGENT_PROTOCOL_ID)

        try:
            return await asyncio.wait_for(fut, timeout=8.0)
        except asyncio.TimeoutError:
            self._pending_rpcs.pop(req_id, None)
            return {"ok": False, "error": "Hardware RPC timed out"}

    async def _handle_voice_turn(self, target_addr: str, pcm_16k: bytes) -> None:
        async with self._turn_lock:
            try:
                transcript = await self.speech.transcribe_pcm(pcm_16k, sample_rate=16000)
                if not transcript:
                    await self.quixi.send_app_data(
                        target_addr,
                        json.dumps({"kind": "face_state", "emo": "IDLE", "sub": "Didn't catch that."}),
                        protocol_id=AGENT_PROTOCOL_ID,
                    )
                    return

                logger.info("User transcript: %s", transcript)
                await self._run_agent_and_reply(target_addr, transcript)
            except Exception as exc:
                logger.exception("Voice turn failed: %s", exc)
                await self.quixi.send_app_data(
                    target_addr,
                    json.dumps({"kind": "face_state", "emo": "SAD", "sub": "Server error."}),
                    protocol_id=AGENT_PROTOCOL_ID,
                )

    async def _handle_text_turn(self, target_addr: str, user_text: str) -> None:
        async with self._turn_lock:
            try:
                await self._run_agent_and_reply(target_addr, user_text)
            except Exception as exc:
                logger.exception("Text turn failed: %s", exc)

    async def _run_agent_and_reply(self, target_addr: str, user_text: str) -> None:
        reply_text, emotion = await self.brain.process_user_turn(user_text)
        logger.info("Agent reply (%s): %s", emotion, reply_text)

        # 1. Stream live text subtitle over QuIXI ChatStream (SpixiMessageCode.CHAT_STREAM = 53)
        stream_res = await self.quixi.send_chat_stream_message(
            target_addr,
            reply_text,
            message_id_b64=None,
            sequence=0,
            is_stream=False,
        )

        # 2. Synthesize TTS PCM + 20Hz viseme envelope
        pcm_16k, envelope = await self.speech.synthesize_pcm_16k(reply_text)
        if not pcm_16k:
            await self.quixi.send_app_data(
                target_addr,
                json.dumps({"kind": "face_state", "emo": emotion, "sub": reply_text}),
                protocol_id=AGENT_PROTOCOL_ID,
            )
            return

        # 3. Send synced subtitle + viseme envelope metadata via AppProtocolData
        turn_id = uuid.uuid4().hex[:8]
        meta_payload = json.dumps(
            {
                "kind": "tts_meta",
                "turn_id": turn_id,
                "emo": emotion,
                "text": reply_text,
                "sample_rate": 16000,
                "envelope": envelope,
                "total_bytes": len(pcm_16k),
                "chat_stream_ok": bool(stream_res),
            }
        )
        await self.quixi.send_app_data(target_addr, meta_payload, protocol_id=AGENT_PROTOCOL_ID)

        # 4. Stream TTS audio frames over native Spixi VoIP appData (type 21)
        frames: List[bytes] = split_audio_frames(pcm_16k, sample_rate=16000, frame_ms=100)
        for frame in frames:
            if self.voip.is_in_call:
                await self.voip.send_audio_frame(frame)
            else:
                # Fallback if session was not initiated yet: send via AppProtocolData chunk
                b64 = base64.b64encode(frame).decode("ascii")
                await self.quixi.send_app_data(
                    target_addr,
                    json.dumps({"kind": "tts_chunk", "turn_id": turn_id, "pcm_b64": b64}),
                    protocol_id=AGENT_PROTOCOL_ID,
                )
            await asyncio.sleep(0.02)

        await self.quixi.send_app_data(
            target_addr,
            json.dumps({"kind": "tts_end", "turn_id": turn_id}),
            protocol_id=AGENT_PROTOCOL_ID,
        )


if __name__ == "__main__":
    service = ServerCompanionService()
    asyncio.run(service.start())

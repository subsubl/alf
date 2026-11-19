"""Offline 4-tier unit, boundary, integration, and scenario tests for server_agent."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import struct
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._stubs import (
    FakeAsyncOpenAI,
    FakeQuIXIBus,
    install_dependency_stubs,
    make_pcm16_sine,
    make_tool_call,
)

install_dependency_stubs()

from common_ixian.spixi_voip import SPIXI_VOIP_APP_ID, AppDataMessage
from server_agent.agent_router import AGENT_TOOLS, AgentBrain
from server_agent.main_server import AGENT_PROTOCOL_ID, ServerCompanionService
from server_agent.speech_processor import (
    SpeechProcessor,
    compute_viseme_envelope,
    pcm16_to_wav_bytes,
    resample_pcm16,
    split_audio_frames,
)


def _make_service_with_mocks(
    pi_addr: str = "1PiAuthorizedAddr",
    allow_dev: str = "",
) -> tuple[ServerCompanionService, FakeAsyncOpenAI, FakeQuIXIBus]:
    env_patch = {
        "PI_IXIAN_ADDRESS": pi_addr,
        "ALLOW_UNAUTHENTICATED_DEV": allow_dev,
        "LLM_API_KEY": "sk-test-llm",
        "STT_API_KEY": "sk-test-stt",
        "TTS_API_KEY": "sk-test-tts",
    }
    fake_ai = FakeAsyncOpenAI()
    with (
        patch.dict(os.environ, env_patch, clear=False),
        patch("server_agent.main_server.load_dotenv", return_value=False),
        patch("server_agent.agent_router.AsyncOpenAI", return_value=fake_ai),
        patch("server_agent.speech_processor.AsyncOpenAI", return_value=fake_ai),
    ):
        svc = ServerCompanionService()

    bus = FakeQuIXIBus()
    srv_http, srv_mqtt = bus.register("1SrvAddr", svc.quixi)
    svc.quixi._http = srv_http
    svc.quixi._mqtt = srv_mqtt
    srv_mqtt.on_message = svc.quixi._on_mqtt_message
    return svc, fake_ai, bus


class TestSpeechProcessor(unittest.IsolatedAsyncioTestCase):
    """Tier 1 & Tier 2 DSP and STT/TTS tests for SpeechProcessor."""

    def test_wav_resample_viseme_and_frame_splitting(self) -> None:
        pcm_16k = make_pcm16_sine(440.0, duration_s=0.25, sample_rate=16000, amp=10000)
        wav_bytes = pcm16_to_wav_bytes(pcm_16k, sample_rate=16000, channels=1)
        self.assertEqual(wav_bytes[:4], b"RIFF")
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.readframes(wf.getnframes()), pcm_16k)

        # Resample 24k -> 16k exact length ratio (2/3)
        pcm_24k = make_pcm16_sine(440.0, duration_s=0.3, sample_rate=24000, amp=12000)
        res_16k = resample_pcm16(pcm_24k, 24000, 16000)
        self.assertEqual(len(res_16k), int(round(len(pcm_24k) * (16000 / 24000))))
        self.assertEqual(resample_pcm16(pcm_16k, 16000, 16000), pcm_16k)
        self.assertEqual(resample_pcm16(b"", 24000, 16000), b"")

        # 20Hz viseme envelope (ceil(4000 / 800) == 5)
        env = compute_viseme_envelope(pcm_16k, sample_rate=16000, fps=20)
        self.assertEqual(len(env), 5)
        self.assertTrue(all(0 <= v <= 100 for v in env))
        self.assertGreater(max(env), 50)

        # Silence floor & empty input (16000Hz * 2B / 20fps = 1600 bytes per frame -> 3200B = 2 frames)
        self.assertEqual(compute_viseme_envelope(b"", 16000, 20), [])
        self.assertEqual(compute_viseme_envelope(b"\x00" * 3200, 16000, 20), [0, 0])

        # 100ms frame splitting (3200B + partial tail)
        frames = split_audio_frames(b"A" * 7000, sample_rate=16000, frame_ms=100)
        self.assertEqual([len(f) for f in frames], [3200, 3200, 600])

    async def test_stt_and_tts_api_flows_and_boundaries(self) -> None:
        fake_ai = FakeAsyncOpenAI()
        with patch("server_agent.speech_processor.AsyncOpenAI", return_value=fake_ai):
            sp = SpeechProcessor("http://x", "k", "whisper-1", "http://x", "k", "tts-1", "alloy")

        # < 50ms (<1600 bytes) skips STT API
        self.assertEqual(await sp.transcribe_pcm(b"\x00" * 1598), "")
        self.assertEqual(len(fake_ai.stt_calls), 0)

        # Valid audio uploads speech.wav and strips whitespace
        fake_ai.stt_script.append("  Turn on pin 16  ")
        text = await sp.transcribe_pcm(make_pcm16_sine(300, 0.1, 16000))
        self.assertEqual(text, "Turn on pin 16")
        self.assertEqual(fake_ai.stt_calls[0]["file"].name, "speech.wav")

        # Blank TTS input skipped
        self.assertEqual(await sp.synthesize_pcm_16k("   "), (b"", []))
        self.assertEqual(len(fake_ai.tts_calls), 0)

        # Raw 24kHz PCM TTS -> 16kHz + viseme envelope
        fake_ai.tts_script.append(make_pcm16_sine(440, 0.25, 24000))
        pcm_16k, env = await sp.synthesize_pcm_16k("Hello")
        self.assertEqual(len(pcm_16k), 8000)  # 0.25s * 16000 * 2B
        self.assertEqual(len(env), 5)

        # Custom TTS server returning RIFF WAV (22050Hz) -> 16kHz
        wav_22k = pcm16_to_wav_bytes(make_pcm16_sine(440, 0.2, 22050), sample_rate=22050)
        fake_ai.tts_script.append(wav_22k)
        pcm_wav_16k, env_wav = await sp.synthesize_pcm_16k("RIFF test")
        self.assertEqual(len(pcm_wav_16k), 6400)  # 0.2s * 16000 * 2B
        self.assertEqual(len(env_wav), 4)

    async def test_tier5_stereo_wav_downmix_bad_sampwidth_and_pure_python_fallback(self) -> None:
        from server_agent import speech_processor as sp_mod

        fake_ai = FakeAsyncOpenAI()
        with patch("server_agent.speech_processor.AsyncOpenAI", return_value=fake_ai):
            sp = SpeechProcessor("http://x", "k", "whisper-1", "http://x", "k", "tts-1", "alloy")

        # 1. Stereo 24kHz WAV downmix (L=3000, R=1000 -> avg=2000; L=-3000, R=-1000 -> avg=-2000)
        stereo_frames = struct.pack("<4h", 3000, 1000, -3000, -1000) * 120
        stereo_buf = io.BytesIO()
        with wave.open(stereo_buf, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(stereo_frames)
        fake_ai.tts_script.append(stereo_buf.getvalue())
        pcm_mono, env_mono = await sp.synthesize_pcm_16k("Stereo test")
        self.assertGreater(len(pcm_mono), 0)
        self.assertGreater(len(env_mono), 0)

        # 2. Unsupported sample width (sampwidth != 2) & corrupt WAV -> (b"", [])
        bad_sw_buf = io.BytesIO()
        with wave.open(bad_sw_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(1)
            wf.setframerate(16000)
            wf.writeframes(b"\x80" * 160)
        fake_ai.tts_script.append(bad_sw_buf.getvalue())
        self.assertEqual(await sp.synthesize_pcm_16k("8bit wav"), (b"", []))

        fake_ai.tts_script.append(b"RIFF\x00\x00\x00\x00WAVEcorrupt")
        self.assertEqual(await sp.synthesize_pcm_16k("corrupt wav"), (b"", []))

        # 3. Pure-Python np=None fallback path
        with patch.object(sp_mod, "np", None):
            mono_py = sp_mod._downmix_pcm16_to_mono(struct.pack("<4h", 3000, 1000, -3000, -1000), 2)
            self.assertEqual(struct.unpack("<2h", mono_py), (2000, -2000))
            pcm_24k = make_pcm16_sine(440.0, duration_s=0.1, sample_rate=24000, amp=10000)
            resamp_py = resample_pcm16(pcm_24k, 24000, 16000)
            self.assertEqual(len(resamp_py), 3200)
            env_py = compute_viseme_envelope(resamp_py, sample_rate=16000, fps=20)
            self.assertEqual(len(env_py), 2)


class TestAgentBrain(unittest.IsolatedAsyncioTestCase):
    """Tier 1 & Tier 2 LLM tool-loop, error recovery, and history-trim tests."""

    def test_agent_tools_schema_and_gpio_allowlist(self) -> None:
        names = [t["function"]["name"] for t in AGENT_TOOLS]
        self.assertEqual(
            names,
            [
                "set_companion_emotion",
                "gpio_configure_and_write",
                "gpio_read_pin",
                "scan_ble_environment",
                "read_imu_motion",
                "update_companion_settings",
            ],
        )
        allowlist = [15, 16, 17, 18, 21, 33, 34, 35, 36, 37]
        by_name = {t["function"]["name"]: t["function"] for t in AGENT_TOOLS}
        self.assertEqual(by_name["gpio_configure_and_write"]["parameters"]["properties"]["pin"]["enum"], allowlist)
        self.assertEqual(by_name["gpio_read_pin"]["parameters"]["properties"]["pin"]["enum"], allowlist)

    async def test_tool_loop_bad_json_rpc_exception_and_max_rounds(self) -> None:
        fake_ai = FakeAsyncOpenAI()
        rpc_calls: list[tuple[str, dict]] = []

        async def rpc_fn(name: str, args: dict) -> dict:
            rpc_calls.append((name, args))
            if name == "read_imu_motion":
                raise RuntimeError("I2C busy")
            return {"ok": True}

        with patch("server_agent.agent_router.AsyncOpenAI", return_value=fake_ai):
            brain = AgentBrain("http://x", "k", "gpt-4o-mini", "Tamagci", rpc_fn)

        # Round 1: set_companion_emotion + invalid JSON args (not sent to rpc) + raising valid RPC
        fake_ai.chat_script.append(
            types.SimpleNamespace(
                content="",
                tool_calls=[
                    make_tool_call("t1", "set_companion_emotion", {"emotion": "surprised"}),
                    make_tool_call("t2", "read_imu_motion", "{bad json"),
                    make_tool_call("t3", "read_imu_motion", {}),
                ],
            )
        )
        fake_ai.chat_script.append(types.SimpleNamespace(content="Done!", tool_calls=None))

        reply, emo = await brain.process_user_turn("Check IMU")
        self.assertEqual(reply, "Done!")
        self.assertEqual(emo, "SURPRISED")
        self.assertEqual(rpc_calls, [("set_companion_emotion", {"emotion": "surprised"}), ("read_imu_motion", {})])
        tool_msgs = [m for m in fake_ai.chat_calls[-1]["messages"] if m.get("role") == "tool"]
        self.assertIn("Invalid JSON", tool_msgs[1]["content"])
        self.assertIn("I2C busy", tool_msgs[2]["content"])

        # Verify max 6 rounds fallback
        for r in range(6):
            fake_ai.chat_script.append(
                types.SimpleNamespace(
                    content="",
                    tool_calls=[make_tool_call(f"loop_{r}", "read_imu_motion", {})],
                )
            )
        fallback_reply, fallback_emo = await brain.process_user_turn("Loop forever")
        self.assertIn("too many hardware steps", fallback_reply)
        self.assertEqual(fallback_emo, "SAD")

    async def test_trim_history_preserves_tool_call_pairs(self) -> None:
        # M2 contract (PROJECT.md:59,104): _trim_history must NEVER orphan role="tool"
        fake_ai = FakeAsyncOpenAI()

        async def rpc_fn(name: str, args: dict) -> dict:
            return {"ok": True}

        with patch("server_agent.agent_router.AsyncOpenAI", return_value=fake_ai):
            brain = AgentBrain("http://x", "k", "gpt-4o-mini", "Tamagci", rpc_fn, max_history_messages=3)

        brain.history = [
            {"role": "user", "content": "turn 1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_imu_motion", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "turn 2"},
        ]
        brain._trim_history()

        self.assertGreater(len(brain.history), 0)
        self.assertNotEqual(brain.history[0]["role"], "tool")
        assistant_call_ids = {
            tc["id"]
            for m in brain.history
            if m.get("role") == "assistant"
            for tc in m.get("tool_calls", [])
        }
        for m in brain.history:
            if m.get("role") == "tool":
                self.assertIn(m["tool_call_id"], assistant_call_ids)


class TestServerCompanionService(unittest.IsolatedAsyncioTestCase):
    """Tier 1–4 tests for ServerCompanionService auth, RX buffer scoping, ChatStream, and turn pipeline."""

    def test_fail_closed_authorization_contract(self) -> None:
        # M2 contract (PROJECT.md:18,105): empty or 1Replace... MUST fail closed unless ALLOW_UNAUTHENTICATED_DEV=true
        svc_empty, _, _ = _make_service_with_mocks(pi_addr="", allow_dev="")
        self.assertFalse(svc_empty._is_authorized("1AnySender"))

        svc_placeholder, _, _ = _make_service_with_mocks(pi_addr="1ReplaceWithYourPiQuIXIWalletAddress", allow_dev="false")
        self.assertFalse(svc_placeholder._is_authorized("1AnySender"))

        svc_dev, _, _ = _make_service_with_mocks(pi_addr="", allow_dev="true")
        self.assertTrue(svc_dev._is_authorized("1AnySender"))

        svc_strict, _, _ = _make_service_with_mocks(pi_addr="1RealPiWallet", allow_dev="")
        self.assertTrue(svc_strict._is_authorized("1RealPiWallet"))
        self.assertFalse(svc_strict._is_authorized("1Attacker"))

    async def test_voip_auto_answer_and_unauthorized_drop(self) -> None:
        svc, fake_ai, bus = _make_service_with_mocks(pi_addr="1RealPi")
        svc.quixi._loop = asyncio.get_running_loop()

        # Unauthorized AppProtocolData & Chat dropped before calling LLM
        bad_proto = base64.b64encode(json.dumps({"kind": "end_of_turn"}).encode()).decode("ascii")
        await svc._on_app_protocol_data("AppProtocolData/1Attacker", {"sender": "1Attacker", "data": {"data": bad_proto}})
        await svc._on_text_chat("Chat/1Attacker", {"sender": "1Attacker", "data": {"data": base64.b64encode(b"hi").decode("ascii")}})
        await asyncio.sleep(0.02)
        self.assertEqual(len(fake_ai.chat_calls), 0)

        # Authorized spixi.voip AppRequest auto-accepts
        accepted = await svc.voip.on_received_call(
            "1RealPi",
            AppDataMessage(session_id=b"sid_1234", data=b"pcm|opus", app_id=SPIXI_VOIP_APP_ID),
            auto_accept=True,
        )
        self.assertTrue(accepted)
        self.assertTrue(svc.voip.is_in_call)
        self.assertEqual(svc.voip.current_codec, "pcm")

    async def test_session_scoped_rx_buffer_and_chat_stream_resilience(self) -> None:
        # M2 contracts: session-scoped RX audio buffer + ChatStream(is_stream=True) + error resilience
        svc, fake_ai, bus = _make_service_with_mocks(pi_addr="1RealPi")
        svc.quixi._loop = asyncio.get_running_loop()

        # Interleave two VoIP sessions' audio; verify session A does not absorb session B frames
        sid_a = b"session_A_000001"
        sid_b = b"session_B_000002"
        pcm_a = make_pcm16_sine(300, 0.1, 16000)
        pcm_b = make_pcm16_sine(600, 0.1, 16000)

        svc.voip.current_session_id = sid_a
        svc.voip.current_contact = "1RealPi"
        svc.voip.call_accepted = True
        svc.voip.callee_accepted = True
        svc.voip.current_codec = "pcm"

        await svc._on_voip_audio_data(sid_a, pcm_a, "pcm")
        await svc._on_voip_audio_data(sid_b, pcm_b, "pcm")

        # Verify session B's audio is NOT mixed into session A's turn buffer
        fake_ai.stt_script.append("Turn A")
        fake_ai.chat_script.append(types.SimpleNamespace(content="Reply A", tool_calls=None))

        # Force sendChatStreamMessage to fail with HTTP 500; TTS + tts_meta + VoIP must STILL succeed!
        bus.force_http_error("1SrvAddr", "sendChatStreamMessage", status=500)

        end_b64 = base64.b64encode(json.dumps({"kind": "end_of_turn", "session_id": sid_a.hex()}).encode()).decode("ascii")
        await svc._on_app_protocol_data("AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": end_b64}})
        await asyncio.sleep(0.15)

        self.assertEqual(len(fake_ai.stt_calls), 1)
        # STT input should contain pcm_a length (not pcm_a + pcm_b combined)
        uploaded_wav = fake_ai.stt_calls[0]["file"].getvalue()
        with wave.open(io.BytesIO(uploaded_wav), "rb") as wf:
            self.assertEqual(wf.readframes(wf.getnframes()), pcm_a)

        # Verify ChatStream used isStream="true" and TTS still sent tts_meta + APP_DATA + tts_end
        chat_calls = [p for _, m, p in bus.rest_log if m == "sendChatStreamMessage"]
        self.assertGreaterEqual(len(chat_calls), 1)
        self.assertTrue(any(c.get("isStream") == "true" for c in chat_calls))

        app_data_kinds = [
            json.loads(p["data"])["kind"]
            for _, m, p in bus.rest_log
            if m == "sendAppData"
        ]
        self.assertIn("tts_meta", app_data_kinds)
        self.assertIn("tts_end", app_data_kinds)

    async def test_fallbacks_rpc_errors_and_turn_serialization(self) -> None:
        svc, fake_ai, bus = _make_service_with_mocks(pi_addr="")
        svc.quixi._loop = asyncio.get_running_loop()

        # _call_pi_tool_rpc with no connected Pi address
        err_no_pi = await svc._call_pi_tool_rpc("read_imu_motion", {})
        self.assertFalse(err_no_pi["ok"])

        svc.pi_address = "1RealPi"
        # RPC timeout cleans _pending_rpcs
        with patch("server_agent.main_server.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            err_to = await svc._call_pi_tool_rpc("read_imu_motion", {})
        self.assertFalse(err_to["ok"])
        self.assertEqual(len(svc._pending_rpcs), 0)

        # Empty STT sends face_state(IDLE, "Didn't catch that.")
        await svc._handle_voice_turn("1RealPi", b"\x00" * 100)
        last_app = json.loads([p["data"] for _, m, p in bus.rest_log if m == "sendAppData"][-1])
        self.assertEqual(last_app, {"kind": "face_state", "emo": "IDLE", "sub": "Didn't catch that."})

        # Empty TTS sends face_state(emo, reply) without tts_meta
        bus.rest_log.clear()
        fake_ai.tts_script.append(b"")
        fake_ai.chat_script.append(types.SimpleNamespace(content="Silent text reply", tool_calls=None))
        await svc._handle_text_turn("1RealPi", "Say something silently")
        app_msgs = [json.loads(p["data"]) for _, m, p in bus.rest_log if m == "sendAppData"]
        self.assertEqual(app_msgs[-1]["kind"], "face_state")
        self.assertEqual(app_msgs[-1]["sub"], "Silent text reply")

        # When voip.is_in_call is False and TTS has audio, falls back to tts_chunk + tts_end
        bus.rest_log.clear()
        fake_ai.tts_script.append(make_pcm16_sine(440, 0.15, 24000))
        fake_ai.chat_script.append(types.SimpleNamespace(content="Fallback audio", tool_calls=None))
        await svc._handle_text_turn("1RealPi", "Speak over fallback")
        kinds = [json.loads(p["data"])["kind"] for _, m, p in bus.rest_log if m == "sendAppData"]
        self.assertIn("tts_meta", kinds)
        self.assertIn("tts_chunk", kinds)
        self.assertEqual(kinds[-1], "tts_end")

        # hw_event ping -> pong & upstream exception releases _turn_lock
        bus.rest_log.clear()
        ping_b64 = base64.b64encode(json.dumps({"kind": "hw_event", "event": {"src": "dbg", "act": "ping", "ts": 99}}).encode()).decode("ascii")
        await svc._on_app_protocol_data("AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": ping_b64}})
        self.assertEqual(json.loads(bus.rest_log[-1][2]["data"]), {"kind": "pong", "ts": 99})

        fake_ai.stt_script.append(RuntimeError("503 upstream"))
        await svc._handle_voice_turn("1RealPi", make_pcm16_sine(440, 0.1, 16000))
        self.assertFalse(svc._turn_lock.locked())
        self.assertEqual(json.loads(bus.rest_log[-1][2]["data"])["emo"], "IDLE")

        # Unexpected exception inside transcribe_pcm triggers SAD "Server error." and releases _turn_lock
        with patch.object(svc.speech, "transcribe_pcm", side_effect=RuntimeError("unexpected crash")):
            await svc._handle_voice_turn("1RealPi", make_pcm16_sine(440, 0.1, 16000))
        self.assertFalse(svc._turn_lock.locked())
        self.assertEqual(json.loads(bus.rest_log[-1][2]["data"])["emo"], "SAD")

    async def test_tier5_barge_in_during_stt_aborts_turn_without_tts(self) -> None:
        svc, fake_ai, bus = _make_service_with_mocks(pi_addr="1RealPi")
        svc.quixi._loop = asyncio.get_running_loop()

        stt_started = asyncio.Event()
        release_stt = asyncio.Event()

        async def slow_transcribe(pcm_bytes: bytes, sample_rate: int = 16000, cancel_event: asyncio.Event | None = None) -> str:
            stt_started.set()
            await release_stt.wait()
            if cancel_event is not None and cancel_event.is_set():
                return ""
            return "Should be cancelled"

        with patch.object(svc.speech, "transcribe_pcm", side_effect=slow_transcribe):
            turn_task = asyncio.create_task(
                svc._handle_voice_turn("1RealPi", make_pcm16_sine(440, 0.1, 16000))
            )
            await asyncio.wait_for(stt_started.wait(), timeout=1.0)

            barge_b64 = base64.b64encode(json.dumps({"kind": "barge_in", "act": "stop_speech"}).encode()).decode("ascii")
            await svc._on_app_protocol_data(
                "AppProtocolData/1RealPi",
                {"sender": "1RealPi", "data": {"data": barge_b64}},
            )
            release_stt.set()
            await asyncio.wait_for(turn_task, timeout=1.0)

        self.assertEqual(len(fake_ai.chat_calls), 0)
        self.assertEqual(len(fake_ai.tts_calls), 0)
        app_msgs = [json.loads(p["data"]) for _, m, p in bus.rest_log if m == "sendAppData"]
        self.assertEqual(app_msgs, [])
        self.assertFalse(svc._turn_lock.locked())

    async def test_tier5_back_to_back_eot_then_barge_in_and_queued_turn_cancellation(self) -> None:
        svc, fake_ai, bus = _make_service_with_mocks(pi_addr="1RealPi")
        svc.quixi._loop = asyncio.get_running_loop()

        sid = b"sid_race_01"
        svc._session_senders[sid] = "1RealPi"
        svc._sender_sessions["1RealPi"] = sid

        eot_b64 = base64.b64encode(
            json.dumps({"kind": "end_of_turn", "session_id": sid.hex()}).encode("utf-8")
        ).decode("ascii")
        barge_b64 = base64.b64encode(
            json.dumps({"kind": "barge_in", "act": "stop_speech"}).encode("utf-8")
        ).decode("ascii")

        # 1. Back-to-back end_of_turn + barge_in before task steps -> cancelled before STT/LLM/TTS
        await svc._on_voip_audio_data(sid, make_pcm16_sine(440, 0.1, 16000), "pcm")
        await svc._on_app_protocol_data(
            "AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": eot_b64}}
        )
        await svc._on_app_protocol_data(
            "AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": barge_b64}}
        )
        await asyncio.sleep(0.05)

        self.assertEqual(len(fake_ai.stt_calls), 0)
        self.assertEqual(len(fake_ai.chat_calls), 0)
        self.assertEqual(len(fake_ai.tts_calls), 0)
        self.assertEqual(svc._tts_cancel_events, {})
        self.assertFalse(svc._turn_lock.locked())

        # 2. Queued end_of_turn behind _turn_lock + barge_in -> both active and queued turns cancelled
        stt_started = asyncio.Event()
        release_stt = asyncio.Event()

        async def slow_stt(pcm_bytes: bytes, sample_rate: int = 16000, cancel_event: asyncio.Event | None = None) -> str:
            stt_started.set()
            await release_stt.wait()
            if cancel_event is not None and cancel_event.is_set():
                return ""
            return "Should not run"

        with patch.object(svc.speech, "transcribe_pcm", side_effect=slow_stt):
            await svc._on_voip_audio_data(sid, make_pcm16_sine(440, 0.1, 16000), "pcm")
            await svc._on_app_protocol_data(
                "AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": eot_b64}}
            )
            await asyncio.wait_for(stt_started.wait(), timeout=1.0)

            # Queue second turn while Turn 1 holds _turn_lock, then barge_in
            await svc._on_voip_audio_data(sid, make_pcm16_sine(550, 0.1, 16000), "pcm")
            await svc._on_app_protocol_data(
                "AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": eot_b64}}
            )
            await asyncio.sleep(0)  # Let queued task block on _turn_lock
            await svc._on_app_protocol_data(
                "AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": barge_b64}}
            )
            release_stt.set()
            await asyncio.sleep(0.06)

        self.assertEqual(len(fake_ai.chat_calls), 0)
        self.assertEqual(len(fake_ai.tts_calls), 0)
        self.assertEqual(svc._tts_cancel_events, {})

        # 3. Subsequent fresh end_of_turn is NOT pre-cancelled and runs normally
        fake_ai.stt_script.append("Fresh turn")
        fake_ai.chat_script.append(types.SimpleNamespace(content="Fresh reply", tool_calls=None))
        await svc._on_voip_audio_data(sid, make_pcm16_sine(440, 0.1, 16000), "pcm")
        await svc._on_app_protocol_data(
            "AppProtocolData/1RealPi", {"sender": "1RealPi", "data": {"data": eot_b64}}
        )
        await asyncio.sleep(0.12)

        self.assertEqual(len(fake_ai.stt_calls), 1)
        self.assertEqual(len(fake_ai.chat_calls), 1)
        self.assertEqual(len(fake_ai.tts_calls), 1)
        self.assertFalse(svc._turn_lock.locked())


if __name__ == "__main__":
    unittest.main()
